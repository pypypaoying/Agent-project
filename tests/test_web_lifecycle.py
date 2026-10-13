"""Actual local service lifecycle plus shutdown/authentication race contracts."""

from argparse import Namespace
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit
import uuid

import pytest
pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
import httpx
from labweaver.web.api import create_app
from labweaver.web.instances import InstanceRegistry, identity_proof, project_id
from labweaver.web.security import AppError
from labweaver.web.settings import WebSettings
from labweaver.web import server

TOKEN = 'lifecycle-owned-application-token'


def settings(tmp_path):
    project = tmp_path / 'project'
    frontend = project / 'frontend/dist'
    frontend.mkdir(parents=True, exist_ok=True)
    (frontend / 'index.html').write_text('<html><body>Lifecycle test interface</body></html>', encoding='utf-8')
    return WebSettings(data_dir=tmp_path / 'app', project_dir=project, mode='offline', token=TOKEN, frontend_dir=frontend)


def test_identity_requires_domain_bound_proof_and_never_returns_token(tmp_path):
    config = settings(tmp_path)
    identity = {'version':1, 'project':project_id(config.project_dir), 'port':8765,
                'instance_id':uuid.uuid4().hex, 'mode':'offline'}
    app = create_app(config, instance_id=identity['instance_id'], shutdown=lambda: None)
    nonce = uuid.uuid4().hex * 2
    proof = identity_proof(TOKEN, 'launcher', nonce, identity)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert client.get('/api/v1/application/identity', params={'nonce':nonce, 'proof':'0'*64}).status_code == 401
        valid = client.get('/api/v1/application/identity', params={'nonce':nonce, 'proof':proof})
        assert valid.status_code == 200 and TOKEN not in valid.text
        assert valid.json() == identity | {'proof':identity_proof(TOKEN, 'server', nonce, identity)}
        assert client.get('/api/v1/settings/providers', headers={'Authorization':'Bearer '+proof}).status_code == 401
        assert client.get('/api/v1/application/identity', params={'nonce':nonce, 'proof':proof}, headers={'Origin':'https://outside.example.invalid'}).status_code == 403
        client.headers['Authorization'] = 'Bearer ' + TOKEN
        assert client.post('/api/v1/application/shutdown').status_code == 202
        assert client.get('/api/v1/application/identity', params={'nonce':nonce, 'proof':proof}).status_code == 503


def test_shutdown_is_authenticated_idempotent_and_blocks_new_tasks(tmp_path):
    calls = []
    app = create_app(settings(tmp_path), shutdown=lambda: calls.append('shutdown'))
    manager = app.state.manager
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert client.post('/api/v1/application/shutdown').status_code == 401
        client.headers['Authorization'] = 'Bearer ' + TOKEN
        conversation = client.post('/api/v1/conversations', json={}).json()['id']
        for state in ('running', 'awaiting_input', 'cancelling'):
            manager.active = 'owned-task-' + state
            assert client.post('/api/v1/application/shutdown').status_code == 409
            assert not manager.stopping and not calls
        manager.active = None
        assert client.post('/api/v1/application/shutdown').json() == {'status':'stopping'}
        assert client.post('/api/v1/application/shutdown').status_code == 202 and calls == ['shutdown']
        assert client.post('/api/v1/conversations', json={}).status_code == 503
        assert client.post(f'/api/v1/conversations/{conversation}/runs', json={'task':'概览', 'request_id':uuid.uuid4().hex}).status_code == 503
        with pytest.raises(AppError, match='application_stopping'):
            manager.start(conversation, '概览', uuid.uuid4().hex)
    assert manager.closed


def test_browser_fragment_preserves_custom_token_special_characters(monkeypatch):
    opened = []
    monkeypatch.setattr(server.webbrowser, 'open', opened.append)
    token = 'application-token+with/&%symbols'
    server._open(8765, token)
    assert parse_qs(urlsplit(opened[0]).fragment)['token'] == [token]
    assert urlsplit(opened[0]).query == ''


def test_foreign_busy_port_never_opens_browser_or_initializes_history(tmp_path, monkeypatch):
    config = settings(tmp_path)
    opened = []
    monkeypatch.setattr(server.WebSettings, 'from_project', lambda **kwargs: config)
    monkeypatch.setattr(server.webbrowser, 'open', opened.append)
    def forbidden(*args, **kwargs):
        pytest.fail('A failed bind must not construct or recover an Agent manager')
    monkeypatch.setattr('labweaver.web.api.create_app', forbidden)
    times = iter([0, 4])
    monkeypatch.setattr(server.time, 'monotonic', lambda: next(times))
    with socket.socket() as foreign:
        foreign.bind(('127.0.0.1', 0)); foreign.listen()
        args = Namespace(data_dir=config.data_dir, mode='offline', config=None, port=foreign.getsockname()[1], open=True)
        with pytest.raises(AppError, match='port_in_use'):
            server.serve(args)
    assert not opened and not (config.data_dir / 'history.sqlite3').exists()


def test_reuse_uses_existing_token_and_rejects_mode_change(tmp_path, monkeypatch):
    config = settings(tmp_path)
    opened = []
    class Registry:
        mode = 'offline'
        def verified_instance(self):
            return {'mode':self.mode, 'token':'existing-service-token'}
    registry = Registry()
    args = Namespace(port=8765, open=True)
    monkeypatch.setattr(server, '_open', lambda port, token: opened.append((port, token)))
    assert server._reuse(registry, config, args)
    assert opened == [(8765, 'existing-service-token')]
    registry.mode = 'live'
    with pytest.raises(AppError, match='running_mode_mismatch'):
        server._reuse(registry, config, args)
    assert len(opened) == 1


def test_real_repeated_launch_exit_and_restart_preserve_history(tmp_path):
    config = settings(tmp_path)
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0)); port = reserve.getsockname()[1]
    command = [sys.executable, '-u', '-m', 'labweaver', 'serve', '--offline', '--data-dir', str(config.data_dir), '--port', str(port)]
    environment = dict(os.environ, LABWEAVER_ACCESS_TOKEN=TOKEN, PYTHONUTF8='1', LANGSMITH_TRACING='false', LANGCHAIN_TRACING_V2='false')
    for name in ('LLM_API_KEY','LLM_BASE_URL','LLM_MODEL_ID'): environment.pop(name, None)
    registry = InstanceRegistry(config, port, explicit_data_dir=True)
    processes = []
    def launch(token):
        env = environment | {'LABWEAVER_ACCESS_TOKEN':token}
        log = (tmp_path / (uuid.uuid4().hex + '.log')).open('wb')
        process = subprocess.Popen(command, cwd=config.project_dir, env=env, stdout=log, stderr=log)
        processes.append((process, log))
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            assert process.poll() is None, 'Owned test service exited before readiness'
            try:
                with httpx.Client(timeout=.5, trust_env=False) as client:
                    response = client.get(f'http://127.0.0.1:{port}/api/v1/settings/providers', headers={'Authorization':'Bearer '+token})
                if response.status_code == 200: return process
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        pytest.fail('Owned service did not become ready')
    try:
        first = launch(TOKEN)
        old_identity = registry.verified_instance()
        assert old_identity and old_identity['token'] == TOKEN
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', headers={'Authorization':'Bearer '+TOKEN}, timeout=5, trust_env=False) as client:
            conversation = client.post('/api/v1/conversations', json={}).json()['id']
            # A second launcher gets a new token, but must reuse the first service.
            duplicate = subprocess.run(command, cwd=config.project_dir, env=environment | {'LABWEAVER_ACCESS_TOKEN':'different-new-launcher-token'}, capture_output=True, timeout=20)
            assert duplicate.returncode == 0 and first.poll() is None
            assert b'10048' not in duplicate.stdout + duplicate.stderr
            assert TOKEN.encode() not in duplicate.stdout + duplicate.stderr
            assert registry.verified_instance()['instance_id'] == old_identity['instance_id']
            assert client.get(f'/api/v1/conversations/{conversation}').status_code == 200
            assert client.post('/api/v1/application/shutdown').status_code == 202
        first.wait(timeout=15)
        assert first.returncode == 0 and registry.read() is None
        second_token = 'new-restarted-application-token'
        second = launch(second_token)
        assert registry.verified_instance()['instance_id'] != old_identity['instance_id']
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=5, trust_env=False) as client:
            assert client.get('/api/v1/settings/providers', headers={'Authorization':'Bearer '+TOKEN}).status_code == 401
            assert client.get(f'/api/v1/conversations/{conversation}', headers={'Authorization':'Bearer '+second_token}).status_code == 200
            assert client.post('/api/v1/application/shutdown', headers={'Authorization':'Bearer '+second_token}).status_code == 202
        second.wait(timeout=15)
        assert second.returncode == 0
        with server._bind(port):
            pass  # The actual port is free, not merely a cancelled UI state.
    finally:
        for process, log in processes:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=10)
            log.close()
