"""User-bound Windows credentials; other platforms retain keys only in memory."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import threading
import uuid

from .security import AppError, managed_path

_HEADER = b"LABWEAVER-DPAPI-1\n"
_MAX_CREDENTIAL_BYTES = 8192


def provider_identifier(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is None:
        raise AppError("provider_not_found", "服务商不存在。", 404)
    return value


def validate_key(value):
    if not isinstance(value, str):
        raise AppError("invalid_api_key", "API Key 必须是文本。")
    value = value.strip()
    if len(value) < 8 or len(value.encode("utf-8")) > _MAX_CREDENTIAL_BYTES or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AppError("invalid_api_key", "API Key 长度无效或包含控制字符。")
    return value


def _windows():
    return os.name == "nt"


def _dpapi(raw, provider_id, *, decrypt=False):
    """DPAPI current-user protection, without prompts or machine-wide access."""
    if not _windows():
        raise AppError("credential_unavailable", "当前系统不能解密 Windows 凭据。", 503)
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    def blob(data):
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        return Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    data, data_buffer = blob(raw)
    entropy, entropy_buffer = blob(("LabWeaver/provider/v1/" + provider_id).encode())
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p if decrypt else ctypes.c_wchar_p,
                         ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    description = None if decrypt else "LabWeaver API credential"
    if not function(ctypes.byref(data), description, ctypes.byref(entropy), None, None,
                    1, ctypes.byref(result)):  # CRYPTPROTECT_UI_FORBIDDEN; never LOCAL_MACHINE.
        raise AppError("credential_unavailable", "系统凭据保护失败，请重新设置 API Key。", 503)
    try:
        if result.cbData > 64 * 1024:
            raise AppError("credential_unavailable", "凭据数据无效。", 503)
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        if result.pbData:
            ctypes.memset(result.pbData, 0, result.cbData)
            kernel.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


@dataclass(frozen=True)
class _Snapshot:
    data: bytes | str | None = field(repr=False)
    backup: str | None = field(default=None, repr=False)


class CredentialStore:
    """No plaintext files, SQLite secrets, key hashes, or plaintext fallback."""
    def __init__(self, root):
        self.root = Path(root)
        self.persistent = _windows()
        self.mode = "dpapi" if self.persistent else "session_only"
        self._memory = {}
        self._lock = threading.RLock()

    def _path(self, provider_id):
        return managed_path(self.root, f"credentials/{provider_identifier(provider_id)}.bin")

    def _read_cipher(self, provider_id):
        path = self._path(provider_id)
        try:
            if not path.exists():
                return None
            if not path.is_file() or path.stat().st_size > 64 * 1024:
                raise AppError("credential_unavailable", "凭据数据无效。", 503)
            return path.read_bytes()
        except OSError:
            raise AppError("credential_unavailable", "无法读取系统保护的凭据。", 503) from None

    def has(self, provider_id):
        provider_identifier(provider_id)
        with self._lock:
            # Presence is not a claim of successful decryption. Invalid bytes
            # must stay visible as configured so users can clear or replace them.
            return self._path(provider_id).exists() if self.persistent else provider_id in self._memory

    def get(self, provider_id):
        provider_identifier(provider_id)
        with self._lock:
            if not self.persistent:
                return self._memory.get(provider_id)
            cipher = self._read_cipher(provider_id)
            if cipher is None:
                return None
            if not cipher.startswith(_HEADER):
                raise AppError("credential_unavailable", "凭据数据无效，请重新设置 API Key。", 503)
            try:
                return validate_key(_dpapi(cipher[len(_HEADER):], provider_id, decrypt=True).decode("utf-8"))
            except (UnicodeError, ValueError):
                raise AppError("credential_unavailable", "凭据数据无效，请重新设置 API Key。", 503) from None

    def _write_cipher(self, provider_id, cipher):
        folder = managed_path(self.root, "credentials")
        self._path(provider_id)
        temporary = managed_path(self.root, f"credentials/{uuid.uuid4().hex}.tmp")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as stream:
                stream.write(cipher)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path(provider_id))
        except OSError:
            raise AppError("credential_unavailable", "无法保存系统保护的凭据。", 503) from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Any leftover contains only DPAPI-protected bytes.

    def set(self, provider_id, value):
        provider_identifier(provider_id)
        value = validate_key(value)
        with self._lock:
            if self.persistent:
                self._write_cipher(provider_id, _HEADER + _dpapi(value.encode("utf-8"), provider_id))
            else:
                self._memory[provider_id] = value

    def clear(self, provider_id):
        provider_identifier(provider_id)
        with self._lock:
            if self.persistent:
                try:
                    self._path(provider_id).unlink(missing_ok=True)
                except OSError:
                    raise AppError("credential_unavailable", "无法删除系统保护的凭据。", 503) from None
            else:
                self._memory.pop(provider_id, None)

    def snapshot(self, provider_id):
        with self._lock:
            if not self.persistent:
                return _Snapshot(self._memory.get(provider_id))
            try:
                return _Snapshot(self._read_cipher(provider_id))
            except AppError as error:
                path = self._path(provider_id)
                # Repair oversized regular files without reading unbounded
                # bytes. Directories, links and reparse points remain rejected.
                if error.code != "credential_unavailable" or not path.is_file() or path.stat().st_size <= 64 * 1024:
                    raise
                relative = f"credentials/{uuid.uuid4().hex}.rollback"
                try:
                    os.replace(path, managed_path(self.root, relative))
                except OSError:
                    raise AppError("credential_unavailable", "无法替换损坏的系统凭据。", 503) from None
                return _Snapshot(None, relative)

    def discard_snapshot(self, snapshot):
        if snapshot.backup:
            try:
                managed_path(self.root, snapshot.backup).unlink(missing_ok=True)
            except OSError:
                pass  # Retains only prior opaque bytes, never a plaintext fallback.

    def restore(self, provider_id, snapshot):
        with self._lock:
            if snapshot.backup:
                try:
                    os.replace(managed_path(self.root, snapshot.backup), self._path(provider_id))
                except OSError:
                    raise AppError("credential_unavailable", "系统凭据回滚失败，请重新设置 API Key。", 503) from None
            elif snapshot.data is None:
                self.clear(provider_id)
            elif self.persistent:
                self._write_cipher(provider_id, snapshot.data)
            else:
                self._memory[provider_id] = snapshot.data
