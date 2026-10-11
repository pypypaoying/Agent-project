"""Opaque managed paths, launch authentication and defensive secret filtering."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat


class AppError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def identifier(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{32}", value) is None:
        raise AppError("not_found", "记录不存在。", 404)
    return value


def managed_path(root: Path, relative: str) -> Path:
    """Reject symlinks and Windows reparse points at every managed component."""
    root = root.absolute()
    candidate = root / relative
    if Path(relative).is_absolute() or any(part in {"..", "."} for part in Path(relative).parts):
        raise AppError("unsafe_path", "文件路径无效。", 403)
    current = root
    for part in ("", *Path(relative).parts):
        if part:
            current /= part
        if current.exists() or current.is_symlink():
            try:
                info = current.lstat()
            except FileNotFoundError:
                # SQLite WAL/SHM files can disappear between stat checks.
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise AppError("unsafe_path", "应用文件不能使用符号链接或重解析点。", 403)
    # All existing components were checked without following links. Lexical
    # containment avoids Windows GetFinalPathName races on transient WAL files.
    if not candidate.absolute().is_relative_to(root.absolute()):
        raise AppError("unsafe_path", "文件不在应用目录中。", 403)
    return candidate


def secret_values(settings):
    values = [settings.token, os.environ.get("LLM_API_KEY", "")]
    if settings.mode == "live":
        from labweaver.config import load_config, ConfigurationError
        try:
            values.append(load_config(settings.env_file, use_default_env=False).api_key)
        except ConfigurationError:
            pass
    return tuple(v for v in set(values) if v)


def scrub(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {scrub(str(k), secrets): scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, secrets) for v in value]
    return value


def contains_secret(value, secrets):
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, default=str).encode()
    return any(secret.encode() in raw for secret in secrets)
