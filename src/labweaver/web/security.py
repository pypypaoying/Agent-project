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


def managed_tree(root, relative):
    """Inspect an owned tree without following symlinks or reparse points."""
    start = managed_path(root, relative)
    if not start.exists():
        return []
    entries, pending = [], [start]
    while pending:
        path = pending.pop()
        safe = managed_path(root, path.relative_to(root).as_posix())
        info = safe.lstat()
        if stat.S_ISDIR(info.st_mode):
            entries.append((safe, True))
            pending.extend(safe.iterdir())
        elif stat.S_ISREG(info.st_mode):
            entries.append((safe, False))
        else:
            raise AppError("unsafe_path", "应用文件目录包含不允许的文件类型。", 403)
    return entries


def purge_managed_tree(root, relative):
    target = managed_path(root, relative)
    if target.exists() and not target.resolve(strict=True).is_relative_to(root):
        raise AppError("unsafe_path", "删除目录不在应用数据范围内。", 403)
    for path, directory in reversed(managed_tree(root, relative)):
        checked = managed_path(root, path.relative_to(root).as_posix())
        checked.rmdir() if directory else checked.unlink()


def secret_values(settings):
    values = [settings.token, os.environ.get("LLM_API_KEY", "")]
    if getattr(settings, "model_config", None) is not None:
        values.append(settings.model_config.api_key)
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
    """Inspect original values; JSON escaping must not hide a credential."""
    if isinstance(value, str):
        return any(secret in value for secret in secrets)
    if isinstance(value, dict):
        return any(contains_secret(k, secrets) or contains_secret(v, secrets) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_secret(item, secrets) for item in value)
    if isinstance(value, bytes):
        # Registered artifacts can already contain serialized JSON strings.
        return any(secret.encode() in value or
                   json.dumps(secret, ensure_ascii=False)[1:-1].encode() in value or
                   json.dumps(secret, ensure_ascii=True)[1:-1].encode() in value
                   for secret in secrets)
    return contains_secret(str(value), secrets)
