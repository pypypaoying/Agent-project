"""Private launcher rendezvous and nonce proofs; never send a bearer to a stale port."""

import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .security import AppError, managed_path


def project_id(path):
    return hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode()).hexdigest()[:32]


def identity_proof(token, purpose, nonce, identity):
    message = json.dumps({'purpose': purpose, 'nonce': nonce, **identity}, sort_keys=True, separators=(',', ':')).encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


class InstanceRegistry:
    def __init__(self, settings, port, *, explicit_data_dir=False):
        # LocalAppData can differ between packaged and Explorer processes.
        # The home-directory rendezvous is stable; explicit roots stay separate.
        root = settings.data_dir / '.runtime' if explicit_data_dir else Path.home() / '.labweaver' / 'instances'
        managed_path(root, 'placeholder')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root.resolve(strict=True)
        self.project, self.port = project_id(settings.project_dir), port
        self.name = f'{self.project}-{port}.json'
        self.storage_name = self.project + '.storage.json'
        self.context = 'instance-' + self.project
        self.explicit_data_dir = explicit_data_dir

    def _read(self, name):
        path = managed_path(self.root, name)
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024:
            raise AppError('instance_record_invalid', '本地服务运行记录无效。', 503)
        if os.name != 'nt' and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise AppError('instance_record_invalid', '本地服务运行记录的权限不安全。', 503)
        try:
            raw = path.read_bytes()
            if os.name == 'nt':
                from .credentials import _dpapi
                raw = _dpapi(base64.b64decode(raw, validate=True), self.context, decrypt=True)
            return json.loads(raw)
        except (FileNotFoundError, ValueError, TypeError, RecursionError, AppError):
            return None

    def read(self):
        try:
            item = self._read(self.name)
            if (not isinstance(item, dict) or type(item['version']) is not int or item['version'] != 1
                    or item['project'] != self.project or type(item['port']) is not int or item['port'] != self.port
                    or item['mode'] not in {'live', 'offline'} or not re.fullmatch('[a-f0-9]{32}', item['instance_id'])
                    or item.get('state', 'ready') not in {'starting', 'ready'}
                    or not isinstance(item['token'], str) or not 8 <= len(item['token']) <= 8192):
                return None
            return item
        except (ValueError, KeyError, TypeError):
            return None

    def _write(self, name, item):
        raw = json.dumps(item, sort_keys=True).encode()
        if os.name == 'nt':
            from .credentials import _dpapi
            raw = base64.b64encode(_dpapi(raw, self.context))
        path = managed_path(self.root, name)
        temporary = managed_path(self.root, uuid.uuid4().hex + '.tmp')
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def storage_root(self, fallback):
        if self.explicit_data_dir or not managed_path(self.root, self.storage_name).exists():
            return fallback
        item = self._read(self.storage_name)
        if not isinstance(item, dict) or item.get('version') != 1 or item.get('project') != self.project:
            raise AppError('instance_storage_invalid', '默认数据目录绑定无效，请核对本机运行记录或显式指定原数据目录。', 503)
        value = item.get('data_dir')
        if not isinstance(value, str) or not value or len(value) > 8192 or any(ord(c) < 32 for c in value):
            raise AppError('instance_storage_invalid', '默认数据目录绑定无效。', 503)
        root = Path(value)
        if not root.is_absolute():
            raise AppError('instance_storage_invalid', '默认数据目录绑定无效。', 503)
        managed_path(root, 'placeholder')
        return root

    @contextmanager
    def _generation_lock(self):
        from .manager import ApplicationLock
        deadline = time.monotonic() + 2
        while True:
            try:
                lock = ApplicationLock(self.root, filename='.instance-registration.lock')
                break
            except AppError as error:
                if error.code != 'already_running' or time.monotonic() >= deadline:
                    raise
                time.sleep(.02)
        try:
            yield
        finally:
            lock.close()

    def write(self, instance_id, mode, token, data_dir, *, state='ready'):
        with self._generation_lock():
            if not self.explicit_data_dir:
                # Retain this token-free binding after exit, so a normal Explorer
                # launch uses the same history as a packaged-app launch.
                self._write(self.storage_name, {'version': 1, 'project': self.project, 'data_dir': str(data_dir)})
            self._write(self.name, {'version': 1, 'project': self.project, 'port': self.port,
                                   'instance_id': instance_id, 'mode': mode, 'token': token, 'state': state})

    def remove(self, instance_id):
        with self._generation_lock():
            item = self.read()
            if item and item['instance_id'] == instance_id:
                managed_path(self.root, self.name).unlink(missing_ok=True)

    def verified_instance(self):
        item = self.read()
        if not item:
            return None
        # A listening PID can be reused. Prove token possession before exposing
        # the actual bearer in the browser's private bootstrap fragment.
        import httpx
        nonce = uuid.uuid4().hex + uuid.uuid4().hex
        identity = {k: item[k] for k in ('version', 'project', 'port', 'instance_id', 'mode')}
        proof = identity_proof(item['token'], 'launcher', nonce, identity)
        started = time.monotonic()
        try:
            with httpx.Client(timeout=.5, trust_env=False, follow_redirects=False) as client:
                with client.stream('GET', f'http://127.0.0.1:{self.port}/api/v1/application/identity',
                                   params={'nonce': nonce, 'proof': proof}) as response:
                    if response.status_code != 200:
                        return None
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() - started > 1 or len(raw) + len(chunk) > 8192:
                            return None
                        raw.extend(chunk)
                    reply = json.loads(raw)
            if (any(reply.get(k) != v for k, v in identity.items()) or
                    not isinstance(reply.get('proof'), str) or
                    not hmac.compare_digest(reply['proof'], identity_proof(item['token'], 'server', nonce, identity))):
                return None
            return item
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, RecursionError):
            return None
