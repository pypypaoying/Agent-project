"""Application file boundaries and credential canaries, independent of generated code."""

import json
import os
from pathlib import Path
import subprocess

import pytest

from labweaver.web.files import import_file, safe_name
from labweaver.web.security import AppError, contains_secret, identifier, managed_path, scrub, secret_values
from labweaver.web.settings import WebSettings
from labweaver.web.store import Store


@pytest.mark.parametrize("value", [None, 5, "", "..", "A" * 32, "0" * 31,
                                   "0" * 33, "0" * 32 + "/file", "SELECT * FROM files"])
def test_ids_do_not_accept_paths_sql_or_malformed_identifiers(value):
    with pytest.raises(AppError) as error:
        identifier(value)
    assert error.value.status == 404


def test_server_generated_opaque_identifier_is_valid():
    assert identifier("01abcdef" * 4) == "01abcdef" * 4


@pytest.mark.parametrize("name", ["../private.csv", "folder/data.csv", "folder\\data.csv",
                                  "C:\\private.csv", "bad\x00.csv", "bad\n.csv", "file.csv ",
                                  "file.csv.", "CON.csv", "nul", "COM1.txt", "LPT9.md"])
def test_invalid_or_windows_reserved_filenames_are_rejected(name):
    with pytest.raises(AppError) as error:
        safe_name(name)
    assert error.value.code == "filename_invalid"


def test_unicode_display_name_has_no_path_authority():
    assert safe_name("附件3（中文）.CSV") == "附件3（中文）.CSV"


@pytest.mark.parametrize("name", ["book.xlsx", "data.json", "report.docx", "image.png", "code.py"])
def test_unsupported_inputs_are_not_registered(tmp_path, name):
    store = Store(tmp_path / "application")
    with pytest.raises(AppError) as error:
        import_file(store, store.new_conversation()["id"], name, b"placeholder")
    assert error.value.status == 415
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 0


@pytest.mark.parametrize("relative", ["../outside.csv", "nested/../../outside.csv"])
def test_managed_path_rejects_parent_traversal(tmp_path, relative):
    with pytest.raises(AppError) as error:
        managed_path(tmp_path / "application", relative)
    assert error.value.code == "unsafe_path"


def test_managed_path_rejects_absolute_client_path(tmp_path):
    with pytest.raises(AppError):
        managed_path(tmp_path / "application", str((tmp_path / "private.csv").absolute()))


def test_real_symlink_cannot_escape_managed_directory(tmp_path):
    root, outside = tmp_path / "application", tmp_path / "private"
    root.mkdir()
    outside.mkdir()
    (outside / "private.txt").write_text("private", encoding="utf-8")
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"OS does not permit this test symlink: {error.winerror if hasattr(error, 'winerror') else error.errno}")
    with pytest.raises(AppError) as error:
        managed_path(root, "escape/private.txt")
    assert error.value.code == "unsafe_path"
    assert (outside / "private.txt").read_text(encoding="utf-8") == "private"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows junction boundary")
def test_real_windows_junction_cannot_escape_managed_directory(tmp_path):
    root, outside = tmp_path / "application", tmp_path / "private"
    root.mkdir()
    outside.mkdir()
    link = root / "escape"
    creation = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                              capture_output=True, timeout=10)
    if creation.returncode:
        pytest.skip("Windows refused to create a junction in the test temporary directory")
    try:
        with pytest.raises(AppError) as error:
            managed_path(root, "escape/private.txt")
        assert error.value.code == "unsafe_path"
    finally:
        # rmdir removes the junction itself, never its target contents.
        link.rmdir()


def test_real_dangling_symlink_is_rejected_before_file_exists(tmp_path):
    root = tmp_path / "application"
    root.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(tmp_path / "absent")
    except OSError:
        pytest.skip("OS does not permit this test symlink")
    with pytest.raises(AppError):
        managed_path(root, "escape")


def test_secret_canaries_are_scrubbed_from_nested_public_json_and_keys():
    secrets = ("sk-model-canary-94261", "launch-token-canary-15302")
    original = {f"header:{secrets[0]}": [{"code": f"print('{secrets[1]}')", "stderr": secrets[0]}],
                "value": 3, "status": "error"}
    assert contains_secret(original, secrets)
    safe = scrub(original, secrets)
    assert not contains_secret(safe, secrets)
    assert safe["value"] == 3 and safe["status"] == "error"
    assert "[REDACTED]" in json.dumps(safe)
    assert contains_secret(original, secrets)  # Scrubbing must not mutate original evidence.


def test_artifact_bytes_containing_key_are_detected():
    secret = "sk-artifact-canary-84902"
    assert contains_secret(f"id,value\n001,{secret}\n".encode(), (secret,))
    assert not contains_secret(b"id,value\n001,2\n", (secret,))


def test_known_credentials_include_selected_env_file_without_environment_mutation(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    env = tmp_path / "private.env"
    env.write_text("LLM_API_KEY=sk-file-canary-64892\nLLM_MODEL_ID=explicit-model\n"
                   "LLM_BASE_URL=https://example.invalid/v1\n", encoding="utf-8")
    settings = WebSettings(data_dir=tmp_path / "app", project_dir=tmp_path,
                           token="application-token-canary-93271", env_file=env)
    values = secret_values(settings)
    assert "sk-file-canary-64892" in values
    assert settings.token in values
    assert "LLM_API_KEY" not in os.environ
