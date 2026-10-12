"""Provider metadata, environment references and actual Windows DPAPI isolation."""

import json
import os
from pathlib import Path

import pytest

from labweaver.web import credentials as credential_module
from labweaver.web.credentials import CredentialStore, validate_key
from labweaver.web.providers import ProviderRegistry, validate_base_url
from labweaver.web.security import AppError
from labweaver.web.settings import WebSettings
from labweaver.web.store import Store

KEY = "provider-sentinel-key-123456789"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID", "LLM_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(credential_module, "_windows", lambda: False)
    store = Store(tmp_path / "app")
    settings = WebSettings(data_dir=store.root, project_dir=Path.cwd(), token="application-provider-test-token", mode="offline")
    return store, settings


@pytest.fixture
def registry(setup):
    return ProviderRegistry(*setup)


def custom(registry, **extra):
    return registry.upsert(None, {"name": "自定义服务", "base_url": "https://models.example/v1",
                                  "models": ["model-a", "model-b"], "default_model_id": "model-a", **extra})


def test_default_preset_and_public_shape_are_explicit(registry):
    result = registry.list()
    assert result["default_provider_id"] == "deepseek"
    assert result["default_model_id"] == "deepseek-flash"
    assert result["credential_persistence"] == "session_only"
    provider = result["providers"][0]
    assert provider == {"id": "deepseek", "name": "DeepSeek", "kind": "preset",
                        "base_url": "https://api.deepseek.com", "models": ["deepseek-flash", "deepseek-v4-pro"],
                        "default_model_id": "deepseek-flash", "has_key": False, "key_source": "none", "revision": 1}
    with pytest.raises(AppError) as error:
        registry.resolve()
    assert error.value.code == "provider_key_missing"


def test_session_key_never_enters_sqlite_or_disk_and_does_not_survive_reopen(registry, setup):
    provider = custom(registry, api_key=KEY)
    assert provider["has_key"] and provider["key_source"] == "session"
    assert "api_key" not in provider and KEY not in json.dumps(registry.list())
    config = registry.resolve(provider["id"], "model-b")
    assert config.api_key == KEY and config.model_id == "model-b"
    assert KEY not in repr(config)
    assert registry.secret_values() == (KEY,)
    for file in setup[0].root.rglob("*"):
        if file.is_file():
            assert KEY.encode() not in file.read_bytes()
    reopened = ProviderRegistry(*setup)
    retained = next(p for p in reopened.list()["providers"] if p["id"] == provider["id"])
    assert retained["models"] == ["model-a", "model-b"] and retained["has_key"] is False
    assert not (setup[0].root / "credentials").exists()


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_api_key_preserves_existing_key(registry, blank):
    provider = custom(registry, api_key=KEY)
    saved = registry.upsert(provider["id"], {"name": "更新的名称", "api_key": blank})
    assert saved["has_key"] and saved["revision"] == provider["revision"] + 1
    assert registry.resolve(provider["id"]).api_key == KEY


def test_clear_key_is_explicit_and_persistent_metadata_stays_public(registry):
    provider = custom(registry, api_key=KEY)
    changed = registry.upsert(provider["id"], {"clear_key": True})
    assert changed["has_key"] is False and changed["key_source"] == "none"
    assert registry.secret_values() == ()
    with pytest.raises(AppError):
        registry.resolve(provider["id"])


def test_default_selection_reopens_with_metadata_and_selected_model(setup):
    registry = ProviderRegistry(*setup)
    item = custom(registry)
    result = registry.set_default(item["id"], "model-b")
    assert result["default_provider_id"] == item["id"] and result["default_model_id"] == "model-b"
    assert ProviderRegistry(*setup).list() == result


@pytest.mark.parametrize("url", ["https://user:pass@models.example/v1", "https://models.example/v1?key=secret",
                                 "https://models.example/#x", "https://models.example/?", "https://models.example/#",
                                 "file:///private", "ftp://host", "https:///v1", "https://host:99999",
                                 "https://host/\nprivate", "https://host\\private", "https://%68ost/v1"])
def test_unsafe_base_urls_fail_without_echoing_url(url):
    with pytest.raises(AppError) as error:
        validate_base_url(url)
    assert error.value.code == "invalid_base_url" and url not in str(error.value)


@pytest.mark.parametrize("url", ["https://api.deepseek.com/", "https://models.example:8443/v1",
                                 "http://127.0.0.1:8080/v1", "http://[::1]:8080/v1"])
def test_http_compatible_urls_are_accepted_without_network_io(url):
    assert validate_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize("key", ["short", "x" * 8193, "valid-key\nsecret", "valid-key\x00secret", 1])
def test_invalid_key_uses_safe_error_and_never_creates_file(key, registry):
    with pytest.raises(AppError) as error:
        custom(registry, api_key=key)
    assert error.value.code == "invalid_api_key"
    assert repr(key) not in str(error.value)
    assert len(registry.list()["providers"]) == 1


@pytest.mark.parametrize("field,value", [("name", KEY), ("models", [KEY]),
                                        ("base_url", "https://models.example/" + KEY)])
def test_new_key_cannot_be_embedded_in_public_metadata(registry, field, value):
    with pytest.raises(AppError) as error:
        custom(registry, api_key=KEY, **{field: value})
    assert error.value.code == "secret_in_provider"
    assert KEY not in str(error.value) and KEY not in json.dumps(registry.list())


def test_existing_key_cannot_be_embedded_in_another_provider(registry):
    custom(registry, api_key=KEY)
    with pytest.raises(AppError) as error:
        custom(registry, name=KEY)
    assert error.value.code == "secret_in_provider"


@pytest.mark.parametrize("key", ['sentinel-quote-"-key-123456', "sentinel-backslash-\\-key-123456"])
def test_json_escaping_cannot_hide_keys_from_public_metadata(registry, key):
    with pytest.raises(AppError) as error:
        custom(registry, api_key=key, name=key)
    assert error.value.code == "secret_in_provider"
    item = custom(registry, api_key=key)
    assert registry.resolve(item["id"]).api_key == key
    with pytest.raises(AppError):
        custom(registry, name=key)
    for file in registry.store.root.rglob("*"):
        if file.is_file():
            assert key.encode() not in file.read_bytes()


def test_invalid_payload_and_unknown_ids_never_mutate_registry(registry):
    before = registry.list()
    for provider_id, payload in [("custom-unknown", {}), ("../escape", {}),
                                  (None, {"has_key": True}), (None, {"clear_key": "yes"}),
                                  (None, {"models": []}), (None, {"models": ["has space"]}),
                                  (None, {"api_key": KEY, "clear_key": True})]:
        with pytest.raises(AppError):
            registry.upsert(provider_id, payload)
        assert registry.list() == before
    with pytest.raises(AppError):
        registry.set_default("deepseek", "invented-model")
    with pytest.raises(AppError):
        registry.resolve("")
    with pytest.raises(AppError):
        registry.resolve("deepseek", "")


def test_failure_rolls_back_metadata_and_replaced_key(registry, monkeypatch):
    item = custom(registry, api_key=KEY)
    before = registry.list()
    def failed_write(*args):
        raise OSError("injected database persistence failure")
    monkeypatch.setattr(registry, "_write", failed_write)
    with pytest.raises(AppError) as error:
        registry.upsert(item["id"], {"name": "不应保存", "api_key": "replacement-key-987654321"})
    assert error.value.code == "provider_save_failed"
    assert registry.list() == before and registry.resolve(item["id"]).api_key == KEY


def test_public_receipt_failure_before_commit_rolls_back_metadata_and_key(registry, monkeypatch):
    item = custom(registry, api_key=KEY)
    before = registry.list()
    original = registry._public
    def failed_receipt(*args):
        raise AppError("credential_unavailable", "无法读取系统保护的凭据。", 503)
    monkeypatch.setattr(registry, "_public", failed_receipt)
    with pytest.raises(AppError):
        registry.upsert(item["id"], {"name": "不应提交", "api_key": "replacement-key-987654321"})
    monkeypatch.setattr(registry, "_public", original)
    assert registry.list() == before and registry.resolve(item["id"]).api_key == KEY


def set_env(monkeypatch, url, model="existing-model"):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setenv("LLM_BASE_URL", url)
    monkeypatch.setenv("LLM_MODEL_ID", model)
    monkeypatch.setenv("LLM_TIMEOUT", "42")


def test_deepseek_environment_is_referenced_without_copying_key(setup, monkeypatch):
    set_env(monkeypatch, "https://api.deepseek.com/v1", "existing-model")
    registry = ProviderRegistry(*setup)
    item = registry.list()["providers"][0]
    assert item["id"] == "deepseek" and item["has_key"] and item["key_source"] == "env"
    assert item["default_model_id"] == "existing-model" and "existing-model" in item["models"]
    config = registry.resolve()
    assert config.api_key == KEY and config.timeout == 42
    assert not (setup[0].root / "credentials").exists()
    assert KEY.encode() not in setup[0].path.read_bytes()


def test_foreign_env_stays_separate_and_is_not_sent_to_deepseek(setup, monkeypatch):
    set_env(monkeypatch, "https://existing.example/v1")
    registry = ProviderRegistry(*setup)
    result = registry.list()
    assert result["default_provider_id"] == "deepseek" and result["providers"][0]["has_key"] is False
    existing = next(p for p in result["providers"] if p["id"] == "legacy-env")
    assert existing["has_key"] and existing["key_source"] == "env"
    assert registry.resolve("legacy-env").api_key == KEY
    with pytest.raises(AppError):
        registry.resolve("deepseek")
    with pytest.raises(AppError) as error:
        registry.upsert("legacy-env", {"base_url": "https://other.example"})
    assert error.value.code == "env_provider_read_only"


def test_changed_url_drops_env_reference_and_blank_key_does_not_redirect_credentials(setup, monkeypatch):
    set_env(monkeypatch, "https://api.deepseek.com", "existing-model")
    registry = ProviderRegistry(*setup)
    saved = registry.upsert("deepseek", {"base_url": "https://other.example/v1", "api_key": ""})
    assert not saved["has_key"] and saved["key_source"] == "none"
    with pytest.raises(AppError) as error:
        registry.resolve("deepseek")
    assert error.value.code == "provider_key_missing"


def test_clear_disables_environment_reference_across_reopen(setup, monkeypatch):
    set_env(monkeypatch, "https://api.deepseek.com")
    registry = ProviderRegistry(*setup)
    registry.upsert("deepseek", {"clear_key": True})
    assert ProviderRegistry(*setup).list()["providers"][0]["has_key"] is False


def test_environment_host_change_is_not_applied_to_saved_reference(setup, monkeypatch):
    set_env(monkeypatch, "https://api.deepseek.com")
    registry = ProviderRegistry(*setup)
    monkeypatch.setenv("LLM_BASE_URL", "https://unexpected.example")
    assert registry.list()["providers"][0]["has_key"] is False
    with pytest.raises(AppError):
        registry.resolve()


def test_key_matching_default_preset_metadata_is_refused(registry):
    with pytest.raises(AppError) as error:
        registry.upsert("deepseek", {"api_key": "deepseek-flash"})
    assert error.value.code == "secret_in_provider"


def test_environment_key_change_cannot_expose_key_in_existing_public_metadata(setup, monkeypatch):
    registry = ProviderRegistry(*setup)
    set_env(monkeypatch, "https://existing.example", "another-model")
    monkeypatch.setenv("LLM_API_KEY", "deepseek-flash")
    with pytest.raises(AppError) as error:
        registry.list()
    assert error.value.code == "secret_in_provider"


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows provider registry DPAPI persistence")
def test_windows_registry_reopens_without_plaintext_keys_and_clears_damaged_credential(tmp_path, monkeypatch):
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID", "LLM_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    store = Store(tmp_path / "app")
    settings = WebSettings(data_dir=store.root, project_dir=Path.cwd(), token="test-provider-app-token", mode="offline")
    registry = ProviderRegistry(store, settings)
    registry.upsert("deepseek", {"api_key": KEY})
    reopened = ProviderRegistry(store, settings)
    assert reopened.resolve().api_key == KEY
    assert reopened.list()["providers"][0]["key_source"] == "dpapi"
    for file in store.root.rglob("*"):
        if file.is_file():
            assert KEY.encode() not in file.read_bytes()
    (store.root / "credentials/deepseek.bin").write_bytes(b"damaged-protected-key")
    assert reopened.list()["providers"][0]["has_key"]
    with pytest.raises(AppError) as error:
        reopened.resolve()
    assert error.value.code == "credential_unavailable"
    cleared = reopened.upsert("deepseek", {"clear_key": True})
    assert cleared["has_key"] is False and not (store.root / "credentials/deepseek.bin").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows oversized protected-file repair")
@pytest.mark.parametrize("replace_key", [False, True])
def test_windows_oversized_credential_can_be_repaired_without_reading_unbounded_bytes(tmp_path, monkeypatch, replace_key):
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID", "LLM_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    store = Store(tmp_path / "app")
    settings = WebSettings(data_dir=store.root, project_dir=Path.cwd(), token="repair-test-app-token", mode="offline")
    registry = ProviderRegistry(store, settings)
    registry.upsert("deepseek", {"api_key": KEY})
    path = store.root / "credentials/deepseek.bin"
    path.write_bytes(b"x" * 65537)
    assert registry.list()["providers"][0]["has_key"]
    payload = {"api_key": "replacement-key-987654321"} if replace_key else {"clear_key": True}
    repaired = registry.upsert("deepseek", payload)
    assert repaired["has_key"] == replace_key
    assert not list((store.root / "credentials").glob("*.rollback"))
    if replace_key:
        assert registry.resolve().api_key == payload["api_key"] and path.stat().st_size < 65537
    else:
        assert not path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows oversized credential rollback")
def test_windows_failed_repair_restores_oversized_opaque_file(tmp_path, monkeypatch):
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID", "LLM_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    store = Store(tmp_path / "app")
    settings = WebSettings(data_dir=store.root, project_dir=Path.cwd(), token="rollback-test-app-token", mode="offline")
    registry = ProviderRegistry(store, settings)
    registry.upsert("deepseek", {"api_key": KEY})
    path = store.root / "credentials/deepseek.bin"
    invalid = b"opaque-invalid-file" * 4000
    path.write_bytes(invalid)
    def fail_write(*args):
        raise OSError("injected database write failure")
    monkeypatch.setattr(registry, "_write", fail_write)
    with pytest.raises(AppError):
        registry.upsert("deepseek", {"clear_key": True})
    assert path.read_bytes() == invalid
    assert not list((store.root / "credentials").glob("*.rollback"))


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows current-user DPAPI acceptance")
def test_windows_dpapi_is_ciphertext_user_bound_and_reopens(tmp_path):
    root = tmp_path / "protected"
    root.mkdir()
    vault = CredentialStore(root)
    assert vault.persistent and vault.mode == "dpapi"
    vault.set("deepseek", KEY)
    cipher = (root / "credentials/deepseek.bin").read_bytes()
    assert KEY.encode() not in cipher and cipher.startswith(b"LABWEAVER-DPAPI-1\n")
    assert CredentialStore(root).get("deepseek") == KEY
    # Copying ciphertext to another provider does not authorize its entropy.
    (root / "credentials/custom-other.bin").write_bytes(cipher)
    with pytest.raises(AppError) as error:
        vault.get("custom-other")
    assert error.value.code == "credential_unavailable" and KEY not in str(error.value)
    vault.clear("deepseek")
    assert not vault.has("deepseek") and vault.get("deepseek") is None


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows DPAPI failure/no plaintext fallback")
def test_windows_protection_failure_never_writes_plaintext(tmp_path, monkeypatch):
    root = tmp_path / "protected"
    root.mkdir()
    vault = CredentialStore(root)
    def denied(*args, **kwargs):
        raise AppError("credential_unavailable", "系统凭据保护失败。", 503)
    monkeypatch.setattr(credential_module, "_dpapi", denied)
    with pytest.raises(AppError):
        vault.set("deepseek", KEY)
    assert not (root / "credentials").exists()
