"""Public provider metadata and backend-only model configuration resolution."""

from copy import deepcopy
import json
import re
import sqlite3
import threading
from urllib.parse import urlsplit
import uuid

from labweaver.config import ConfigurationError, ModelConfig, load_config
from .credentials import CredentialStore, provider_identifier, validate_key
from .security import AppError

_META_KEY = "providers_v1"
_FIELDS = {"name", "base_url", "models", "default_model_id", "api_key", "clear_key"}
_PUBLIC = ("id", "name", "kind", "base_url", "models", "default_model_id", "revision")


def _contains_secret(value, secrets):
    """Compare original strings before JSON escaping can hide a key."""
    if isinstance(value, str):
        return any(secret and secret in value for secret in secrets)
    if isinstance(value, bytes):
        return any(secret and secret.encode() in value for secret in secrets)
    if isinstance(value, dict):
        return any(_contains_secret(key, secrets) or _contains_secret(item, secrets) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item, secrets) for item in value)
    return False


def validate_base_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise AppError("invalid_base_url", "Base URL 必须是有效的 HTTP(S) 地址。")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or "?" in value or "#" in value or "%" in parsed.netloc):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", parsed.hostname):
            raise ValueError
    except ValueError:
        raise AppError("invalid_base_url", "Base URL 不能包含登录信息、查询参数或片段。") from None
    return value.rstrip("/")


def _text(value, maximum, message):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AppError("invalid_provider", message)
    return value.strip()


def _models(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise AppError("invalid_models", "请填写 1–50 个模型 ID。")
    result = []
    for item in value:
        model = _text(item, 128, "模型 ID 无效。")
        if any(c.isspace() for c in model):
            raise AppError("invalid_models", "模型 ID 不能包含空白字符。")
        if model not in result:
            result.append(model)
    return result


class ProviderRegistry:
    """Metadata uses existing app_meta; credentials never enter its transaction."""
    def __init__(self, store, settings, *, credentials=None):
        self.store, self.settings = store, settings
        self.credentials = credentials or CredentialStore(store.root)
        self._lock = threading.RLock()
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM app_meta WHERE key=?", (_META_KEY,)).fetchone()
            if row is None:
                config = {"version": 1, "default_provider_id": "deepseek", "default_model_id": "deepseek-flash",
                          "providers": [{"id": "deepseek", "name": "DeepSeek", "kind": "preset",
                                         "base_url": "https://api.deepseek.com", "models": ["deepseek-flash", "deepseek-v4-pro"],
                                         "default_model_id": "deepseek-flash", "revision": 1, "env_reference": False}]}
                existing = self._env_config()
                if existing is not None:
                    base_url = validate_base_url(existing.base_url)
                    _models([existing.model_id])
                    parsed = urlsplit(base_url)
                    if (parsed.scheme == "https" and parsed.hostname == "api.deepseek.com"
                            and parsed.port in {None, 443} and parsed.path in {"", "/v1"}):
                        item = config["providers"][0]
                        item.update(base_url=base_url, env_reference=True)
                        if existing.model_id not in item["models"]:
                            item["models"].insert(0, existing.model_id)
                        item["default_model_id"] = config["default_model_id"] = existing.model_id
                    else:
                        config["providers"].append({"id": "legacy-env", "name": "现有本地配置", "kind": "env",
                                                   "base_url": base_url, "models": [existing.model_id],
                                                   "default_model_id": existing.model_id, "revision": 1, "env_reference": True})
                    if _contains_secret(config, (existing.api_key,)):
                        raise AppError("secret_in_provider", "公开服务商字段不能包含 API Key。")
                self._write(db, config)
            else:
                self._read(db)

    def _env_config(self):
        try:
            return load_config(self.settings.env_file, use_default_env=False)
        except ConfigurationError:
            return None

    def _read(self, db):
        try:
            row = db.execute("SELECT value FROM app_meta WHERE key=?", (_META_KEY,)).fetchone()
            data = json.loads(row[0])
            if data["version"] != 1 or not isinstance(data["providers"], list) or not 1 <= len(data["providers"]) <= 30:
                raise ValueError
            ids = set()
            for item in data["providers"]:
                provider_identifier(item["id"])
                if item["id"] in ids or item["kind"] not in {"preset", "custom", "env"}:
                    raise ValueError
                ids.add(item["id"])
                _text(item["name"], 80, "服务商名称无效。")
                validate_base_url(item["base_url"])
                models = _models(item["models"])
                if item["default_model_id"] not in models or type(item["revision"]) is not int or item["revision"] < 1 or type(item["env_reference"]) is not bool:
                    raise ValueError
            selected = self._find(data, data["default_provider_id"])
            if data["default_model_id"] not in selected["models"]:
                raise ValueError
            return data
        except (KeyError, TypeError, ValueError, AppError):
            raise AppError("provider_config_invalid", "服务商配置数据无效。", 503) from None

    def _write(self, db, data):
        db.execute("INSERT INTO app_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (_META_KEY, json.dumps(data, ensure_ascii=False, allow_nan=False)))

    def _find(self, data, provider_id):
        provider_identifier(provider_id)
        item = next((p for p in data["providers"] if p["id"] == provider_id), None)
        if item is None:
            raise AppError("provider_not_found", "服务商不存在。", 404)
        return item

    def _key(self, item):
        value = self.credentials.get(item["id"])
        if value:
            return value
        if item["env_reference"]:
            config = self._env_config()
            if config and validate_base_url(config.base_url) == item["base_url"]:
                return config.api_key
        return None

    def _public(self, item):
        if self.credentials.has(item["id"]):
            source = "dpapi" if self.credentials.persistent else "session"
            has_key = True
        else:
            has_key = bool(self._key(item))
            source = "env" if has_key else "none"
        return {key: deepcopy(item[key]) for key in _PUBLIC} | {"has_key": has_key, "key_source": source}

    def list(self):
        with self._lock, self.store.connect() as db:
            data = self._read(db)
            result = {"providers": [self._public(p) for p in data["providers"]],
                      "default_provider_id": data["default_provider_id"], "default_model_id": data["default_model_id"],
                      "credential_persistence": self.credentials.mode}
            if _contains_secret(result, self.secret_values()):
                raise AppError("secret_in_provider", "公开服务商字段不能包含 API Key。")
            return result

    def secret_values(self):
        with self._lock, self.store.connect() as db:
            data = self._read(db)
            values = []
            for item in data["providers"]:
                try:
                    values.append(self._key(item))
                except AppError as error:
                    if error.code != "credential_unavailable":
                        raise
                    # Damaged protected bytes must remain clearable. They are
                    # never exposed, executed, or treated as a valid key.
            existing = self._env_config()
            if existing:
                values.append(existing.api_key)
            return tuple({value for value in values if value})

    def upsert(self, provider_id, payload):
        if not isinstance(payload, dict) or set(payload) - _FIELDS:
            raise AppError("invalid_provider", "包含不支持的服务商字段。")
        if "clear_key" in payload and type(payload["clear_key"]) is not bool:
            raise AppError("invalid_provider", "clear_key 必须是布尔值。")
        key = payload.get("api_key")
        if key is not None and not isinstance(key, str):
            raise AppError("invalid_api_key", "API Key 必须是文本。")
        key = validate_key(key) if key and key.strip() else None
        if key and payload.get("clear_key"):
            raise AppError("invalid_provider", "不能同时保存和清除 API Key。")
        creating = provider_id is None
        provider_id = provider_identifier(provider_id) if not creating else "custom-" + uuid.uuid4().hex
        with self._lock:
            secrets = self.secret_values() + ((key,) if key else ())
            public_input = {k: v for k, v in payload.items() if k != "api_key"}
            if _contains_secret(public_input, secrets):
                raise AppError("secret_in_provider", "公开服务商字段不能包含 API Key。")
            changing_key = bool(key or payload.get("clear_key"))
            previous = self.credentials.snapshot(provider_id) if changing_key else None
            try:
                with self.store.connect() as db:
                    data = self._read(db)
                    item = next((p for p in data["providers"] if p["id"] == provider_id), None)
                    if item is None:
                        if not creating or len(data["providers"]) >= 30:
                            raise AppError("provider_not_found", "服务商不存在或数量达到上限。", 404)
                        item = {"id": provider_id, "kind": "custom", "revision": 0, "env_reference": False}
                        data["providers"].append(item)
                    if item["kind"] == "env" and set(payload) - {"api_key", "clear_key"}:
                        raise AppError("env_provider_read_only", "现有本地配置通过环境引用，请新增自定义服务商。")
                    old_url = item.get("base_url")
                    item["name"] = _text(payload.get("name", item.get("name")), 80, "服务商名称不能为空。")
                    item["base_url"] = validate_base_url(payload.get("base_url", old_url))
                    item["models"] = _models(payload.get("models", item.get("models")))
                    item["default_model_id"] = _text(payload.get("default_model_id", item.get("default_model_id", item["models"][0])), 128, "默认模型 ID 无效。")
                    if item["default_model_id"] not in item["models"]:
                        raise AppError("invalid_models", "默认模型必须包含在模型列表中。")
                    if item["base_url"] != old_url or key or payload.get("clear_key"):
                        item["env_reference"] = False
                    item["revision"] += 1
                    if _contains_secret(data, secrets):
                        raise AppError("secret_in_provider", "公开服务商字段不能包含 API Key。")
                    if payload.get("clear_key"):
                        self.credentials.clear(provider_id)
                    elif key:
                        self.credentials.set(provider_id, key)
                    if data["default_provider_id"] == provider_id:
                        data["default_model_id"] = item["default_model_id"]
                    self._write(db, data)
                    result = self._public(item)
                if previous is not None:
                    self.credentials.discard_snapshot(previous)
                return result
            except BaseException as error:
                if previous is not None:
                    self.credentials.restore(provider_id, previous)
                if isinstance(error, (OSError, sqlite3.Error)):
                    raise AppError("provider_save_failed", "服务商配置未保存，请重试。", 503) from None
                raise

    def set_default(self, provider_id, model_id):
        with self._lock, self.store.connect() as db:
            data = self._read(db)
            item = self._find(data, provider_id)
            if model_id not in item["models"]:
                raise AppError("invalid_models", "模型不在该服务商的列表中。")
            data["default_provider_id"], data["default_model_id"] = provider_id, model_id
            self._write(db, data)
        return self.list()

    def resolve(self, provider_id=None, model_id=None):
        with self._lock, self.store.connect() as db:
            data = self._read(db)
            provider_id = data["default_provider_id"] if provider_id is None else provider_id
            item = self._find(data, provider_id)
            if model_id is None:
                model_id = data["default_model_id"] if provider_id == data["default_provider_id"] else item["default_model_id"]
            if model_id not in item["models"]:
                raise AppError("invalid_models", "模型不在该服务商的列表中。")
            key = self._key(item)
            if not key:
                raise AppError("provider_key_missing", "请在服务商设置中填写 API Key。", 409)
            previous = self._env_config() if item["env_reference"] else None
            return ModelConfig(model_id=model_id, base_url=item["base_url"], api_key=key,
                               timeout=previous.timeout if previous else 30.0)
