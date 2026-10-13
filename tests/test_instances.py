"""Launcher metadata and identity proofs; no model or generated code execution."""

from dataclasses import replace
import json
import os
from pathlib import Path
import threading

import pytest

from labweaver.web.instances import InstanceRegistry, identity_proof, project_id
from labweaver.web.security import AppError
from labweaver.web.settings import WebSettings

TOKEN = "synthetic-instance-access-token-1234567890"
INSTANCE = "a" * 32
NEXT_INSTANCE = "b" * 32


@pytest.fixture
def settings(tmp_path):
    data, project = tmp_path / "data", tmp_path / "project"
    data.mkdir()
    project.mkdir()
    return WebSettings(data_dir=data, project_dir=project, token=TOKEN, mode="offline")


@pytest.fixture
def registry(settings):
    return InstanceRegistry(settings, 18765, explicit_data_dir=True)


def write(registry, settings, *, instance=INSTANCE, mode="offline", state="ready"):
    registry.write(instance, mode, TOKEN, settings.data_dir, state=state)
    return registry.read()


def test_proof_separates_purpose_nonce_and_every_identity_field(settings):
    identity = {"version": 1, "project": project_id(settings.project_dir), "port": 18765,
                "instance_id": INSTANCE, "mode": "offline"}
    nonce = "c" * 64
    proof = identity_proof(TOKEN, "launcher", nonce, identity)
    assert len(proof) == 64 and proof != identity_proof(TOKEN, "server", nonce, identity)
    assert proof != identity_proof(TOKEN, "launcher", "d" * 64, identity)
    for field, value in (("version", 2), ("project", "e" * 32), ("port", 18766),
                         ("instance_id", NEXT_INSTANCE), ("mode", "live")):
        assert proof != identity_proof(TOKEN, "launcher", nonce, identity | {field: value})


def test_explicit_root_isolated_and_real_os_protection_round_trips(registry, settings):
    item = write(registry, settings)
    assert item["token"] == TOKEN and item["instance_id"] == INSTANCE and item["state"] == "ready"
    assert registry.root == (settings.data_dir / ".runtime").resolve()
    raw = (registry.root / registry.name).read_bytes()
    if os.name == "nt":
        assert TOKEN.encode() not in raw  # Real current-user DPAPI, not a transport fixture.
    else:
        assert registry.root.stat().st_mode & 0o777 == 0o700
        assert (registry.root / registry.name).stat().st_mode & 0o777 == 0o600
    other = InstanceRegistry(replace(settings, data_dir=settings.data_dir.parent / "other"), 18765,
                             explicit_data_dir=True)
    assert other.read() is None
    assert registry.read()["token"] == TOKEN


def test_default_rendezvous_and_token_free_storage_binding_survive_instance_exit(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "synthetic-home")
    item = InstanceRegistry(settings, 18765)
    physical = settings.data_dir.parent / "physical-LocalCache"
    physical.mkdir()
    item.write(INSTANCE, "offline", TOKEN, physical)
    item.remove(INSTANCE)
    assert item.read() is None
    binding = item._read(item.storage_name)
    assert binding == {"version": 1, "project": item.project, "data_dir": str(physical)}
    assert "token" not in binding and TOKEN not in json.dumps(binding)
    explorer = InstanceRegistry(replace(settings, data_dir=tmp_path / "different-nominal-root"), 18765)
    assert explorer.root == item.root and explorer.storage_root(explorer.root) == physical
    assert explorer.read() is None
    separate_project = tmp_path / "different-project"
    separate_project.mkdir()
    another = InstanceRegistry(replace(settings, project_dir=separate_project), 18765)
    assert another.storage_root(settings.data_dir) == settings.data_dir


def test_remove_only_matching_generation(registry, settings):
    write(registry, settings, instance=NEXT_INSTANCE)
    registry.remove(INSTANCE)
    assert registry.read()["instance_id"] == NEXT_INSTANCE
    registry.remove(NEXT_INSTANCE)
    assert registry.read() is None


def test_compare_delete_and_new_generation_publish_are_serialized(registry, settings, monkeypatch):
    """A check followed by an unlocked unlink would delete the new descriptor."""
    write(registry, settings)
    old_read, release, attempted = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original_read = registry.read

    def blocked_read():
        result = original_read()
        if threading.current_thread().name == "old-instance-remove":
            old_read.set()
            if not release.wait(3):
                raise AssertionError("Test synchronization timed out")
        return result

    monkeypatch.setattr(registry, "read", blocked_read)

    def remove_old():
        try:
            registry.remove(INSTANCE)
        except BaseException as error:
            errors.append(error)

    def publish_new():
        attempted.set()
        try:
            registry.write(NEXT_INSTANCE, "offline", TOKEN, settings.data_dir)
        except BaseException as error:
            errors.append(error)

    old = threading.Thread(target=remove_old, name="old-instance-remove")
    new = threading.Thread(target=publish_new, name="new-instance-publish")
    old.start()
    try:
        assert old_read.wait(2)
        new.start()
        assert attempted.wait(2)
        new.join(.2)  # With the lock, the writer cannot finish before the old delete.
    finally:
        release.set()
        old.join(3)
        if new.ident is not None:
            new.join(3)
    assert not old.is_alive() and not new.is_alive() and not errors
    assert original_read()["instance_id"] == NEXT_INSTANCE


def test_descriptor_disappearing_during_read_is_absent_not_an_exception(registry, settings, monkeypatch):
    write(registry, settings)
    target = registry.root / registry.name
    original = Path.read_bytes

    def vanished(path):
        if path == target:
            path.unlink()
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", vanished)
    assert registry.read() is None


@pytest.mark.parametrize("field,value", [("version", True), ("project", "x" * 32), ("port", 18766),
                                        ("mode", "unknown"), ("instance_id", "not-an-instance"),
                                        ("token", "short"), ("state", "stopped")])
def test_invalid_descriptor_cannot_be_reused(registry, settings, field, value):
    item = write(registry, settings)
    registry._write(registry.name, item | {field: value})
    assert registry.read() is None


def test_oversized_and_corrupt_descriptor_fail_safely(registry, settings):
    write(registry, settings)
    target = registry.root / registry.name
    target.write_bytes(b"x" * (32 * 1024 + 1))
    with pytest.raises(AppError) as error:
        registry.read()
    assert error.value.code == "instance_record_invalid" and TOKEN not in str(error.value)
    target.write_bytes(b"invalid-not-a-descriptor")
    assert registry.read() is None


def test_managed_descriptor_symlink_is_rejected(registry, settings, tmp_path):
    target = registry.root / registry.name
    outside = tmp_path / "outside-record"
    outside.write_text("do not read outside")
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("OS does not permit this test symlink")
    with pytest.raises(AppError) as error:
        registry.read()
    assert error.value.code == "unsafe_path"
    assert outside.read_text() == "do not read outside"


def mock_probe(monkeypatch, registry, responder):
    httpx = pytest.importorskip("httpx")
    requests = []

    class Response:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_bytes(self):
            yield self.body

    class Client:
        def __init__(self, **options):
            assert options["trust_env"] is False and options["follow_redirects"] is False
            assert options["timeout"] <= 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def stream(self, method, url, **options):
            assert method == "GET" and url == f"http://127.0.0.1:{registry.port}/api/v1/application/identity"
            assert not any(key.lower() == "authorization" for key in options.get("headers", {}))
            assert TOKEN not in json.dumps(options)
            params = options["params"]
            identity = {key: registry.read()[key] for key in ("version", "project", "port", "instance_id", "mode")}
            assert len(params["nonce"]) == 64 and params["proof"] == identity_proof(TOKEN, "launcher", params["nonce"], identity)
            requests.append(params)
            body = responder(identity, params)
            return Response(body if isinstance(body, bytes) else json.dumps(body).encode())

    monkeypatch.setattr(httpx, "Client", Client)
    return requests


def test_verified_instance_never_sends_bearer_and_uses_fresh_nonce(registry, settings, monkeypatch):
    item = write(registry, settings)
    def answer(identity, params):
        return identity | {"proof": identity_proof(TOKEN, "server", params["nonce"], identity)}
    requests = mock_probe(monkeypatch, registry, answer)
    assert registry.verified_instance() == item
    assert registry.verified_instance() == item
    assert requests[0]["nonce"] != requests[1]["nonce"]


@pytest.mark.parametrize("reply_kind", ["reflection", "wrong-instance", "wrong-mode", "wrong-proof", "large", "malformed", "deep-json"])
def test_foreign_or_stale_port_cannot_obtain_reusable_bearer(registry, settings, monkeypatch, reply_kind):
    write(registry, settings)
    def stranger(identity, params):
        if reply_kind == "reflection":
            return identity | {"proof": params["proof"]}
        if reply_kind == "large":
            return b"x" * 8193
        if reply_kind == "malformed":
            return b"not-json"
        if reply_kind == "deep-json":
            return b"[" * 2000 + b"0" + b"]" * 2000
        changed = identity | ({"instance_id": NEXT_INSTANCE} if reply_kind == "wrong-instance" else
                              {"mode": "live"} if reply_kind == "wrong-mode" else {})
        proof = "0" * 64 if reply_kind == "wrong-proof" else identity_proof(TOKEN, "server", params["nonce"], changed)
        return changed | {"proof": proof}
    requests = mock_probe(monkeypatch, registry, stranger)
    assert registry.verified_instance() is None
    assert requests and TOKEN not in json.dumps(requests)
