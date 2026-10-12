"""Real HTTP model adapter and spawned Agent worker; no network outside loopback."""

from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import uuid

import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from labweaver.web.api import create_app
from labweaver.web.manager import TaskManager
from labweaver.web.settings import WebSettings
from labweaver.web.store import uid
from test_web_api import wait_run, blocked_worker

ROOT = Path(__file__).parents[1]
TOKEN = "models-private-application-token"
KEY_A = "models-private-key-canary-A"
KEY_B = "models-private-key-canary-B"


@contextmanager
def model_server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append({"path": self.path, "key": self.headers.get('Authorization')})
            body = json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append({"path": self.path, "key": self.headers.get('Authorization'), "request": request})
            tools = [t['function']['name'] for t in request.get('tools', [])]
            is_probe = 'connection_check' in tools
            is_profile = 'profile_csv' in tools and not any(m.get('role') == 'tool' for m in request['messages'])
            call = None
            if is_probe or is_profile:
                call = {"id": 'call-' + uuid.uuid4().hex, "type": "function", "function": {"name": 'connection_check' if is_probe else 'profile_csv', "arguments": '{"ok":true}' if is_probe else '{}'}}
            content = None if call else json.dumps({"task_kind": "summary", "retrieval_reason": "本任务只需 CSV", "answer": "已根据真实工具结果完成概览。"}, ensure_ascii=False)
            if not request.get('stream'):
                message = {"role": "assistant", "content": content}
                if call: message['tool_calls'] = [call]
                body = json.dumps({"id": 'chat-completion', "object": "chat.completion", "created": 0, "model": request['model'], "choices": [{"index": 0, "message": message, "finish_reason": 'tool_calls' if call else 'stop'}]}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)
                return
            delta = {"role": "assistant", "content": content}
            if call: delta['tool_calls'] = [{"index": 0, **call}]
            chunks = [{"id": 'chat-completion', "object": "chat.completion.chunk", "created": 0, "model": request['model'], "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"id": 'chat-completion', "object": "chat.completion.chunk", "created": 0, "model": request['model'], "choices": [{"index": 0, "delta": {}, "finish_reason": 'tool_calls' if call else 'stop'}]}]
            body = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers(); self.wfile.write(body.encode()); self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown(); server.server_close(); thread.join(3)


def config(tmp_path, *, mode='live'):
    return WebSettings(data_dir=tmp_path / 'models', token=TOKEN, project_dir=ROOT, mode=mode, frontend_dir=ROOT / 'frontend/dist', cancel_grace=.15)


def add_provider(client, url, name='本地测试接口', model='model-a', key=KEY_A):
    response = client.post('/api/v1/settings/providers', json={"name": name, "base_url": url, "models": ['model-a', 'model-b'], "default_model_id": model, "api_key": key})
    assert response.status_code == 201, response.text
    return response.json()


def test_settings_protected_and_secret_never_returned_or_persisted_plain(tmp_path):
    settings = config(tmp_path, mode='offline')
    with TestClient(create_app(settings), base_url='http://127.0.0.1') as client:
        assert client.get('/api/v1/settings/providers').status_code == 401
        client.headers['Authorization'] = 'Bearer ' + TOKEN
        provider = add_provider(client, 'http://127.0.0.1:9999/v1')
        id = provider['id']
        assert client.put('/api/v1/settings/providers/'+id, json={'name': TOKEN}).status_code == 400
        assert provider['has_key'] and KEY_A not in json.dumps(provider)
        kept = client.put('/api/v1/settings/providers/'+id, json={"api_key":""}).json()
        assert kept['has_key']
        assert client.put('/api/v1/settings/providers/'+id, json={"base_url":"https://user:password@example.com"}).status_code == 400
        invalid = client.put('/api/v1/settings/providers/'+id, json={"api_key":123, "base_url":KEY_A})
        assert invalid.status_code == 422 and KEY_A not in invalid.text
        assert client.post('/api/v1/settings/providers/default', json={"provider_id":id,"model_id":"model-b"}).status_code == 200
        public = client.get('/api/v1/settings/providers').json()
        assert public['default_provider_id'] == id and public['default_model_id'] == 'model-b'
        assert KEY_A not in json.dumps(public)
        cleared = client.put('/api/v1/settings/providers/'+id, json={"clear_key":True}).json()
        assert not cleared['has_key']
    for path in settings.data_dir.rglob('*'):
        if path.is_file(): assert KEY_A.encode() not in path.read_bytes(), path.name


def test_real_model_switch_rebinds_worker_keeps_history_and_records_actual_configuration(tmp_path, monkeypatch):
    for name in ('LLM_API_KEY','LLM_BASE_URL','LLM_MODEL_ID'): monkeypatch.delenv(name, raising=False)
    with model_server() as (url, calls), TestClient(create_app(config(tmp_path)), base_url='http://127.0.0.1', headers={'Authorization':'Bearer '+TOKEN}) as client:
        a = add_provider(client, url+'/a/v1')
        b = add_provider(client, url+'/b/v1', name='另一个接口', model='model-b', key=KEY_B)
        assert client.post(f"/api/v1/settings/providers/{a['id']}/models/refresh").json()['models'] == ['model-a','model-b']
        probe = client.post(f"/api/v1/settings/providers/{a['id']}/test", json={'model_id':'model-a'})
        assert probe.json() == {'ok':True,'model_id':'model-a','tool_call_supported':True}
        assert all('request' not in c or 'CSV' not in json.dumps(c['request']) for c in calls)
        calls.clear()
        conversation = client.post('/api/v1/conversations',json={}).json()['id']
        assert client.post(f'/api/v1/conversations/{conversation}/files', files={'file':('data.csv', b'id,value\n001,2\n002,3\n')}).status_code == 201
        previous = None
        for provider, model, key, base in [(a,'model-a',KEY_A,'/a/'),(a,'model-b',KEY_A,'/a/'),(b,'model-b',KEY_B,'/b/')]:
            request_id = uid()
            response = client.post(f'/api/v1/conversations/{conversation}/runs',json={'task':'只概览字段与缺失情况，不需要计算或绘图。','request_id':request_id,'provider_id':provider['id'],'model_id':model})
            assert response.status_code == 202, response.text
            done = wait_run(client, response.json()['id'], 'completed', timeout=80)
            assert done['model_selection']['provider_id'] == provider['id'] and done['model_selection']['model_id'] == model
            assert any(e['type']=='tool_end' and e['payload']['name']=='profile_csv' for e in done['events'])
            assert all(c['request']['model']==model and c['key']=='Bearer '+key and c['path'].startswith(base) for c in calls)
            assert len(calls) >= 2
            calls.clear()
            if previous: assert done['core_session_id'] != previous['core_session_id'] and done['history_run_ids']
            previous = done
            duplicate = client.post(f'/api/v1/conversations/{conversation}/runs',json={'task':'只概览字段与缺失情况，不需要计算或绘图。','request_id':request_id,'provider_id':provider['id'],'model_id':model})
            assert duplicate.json()['id'] == done['id'] and not calls
            mismatch = client.post(f'/api/v1/conversations/{conversation}/runs',json={'task':'只概览字段与缺失情况，不需要计算或绘图。','request_id':request_id,'provider_id':provider['id'],'model_id':'model-b' if model=='model-a' else 'model-a'})
            assert mismatch.status_code == 409
        public = client.get(f'/api/v1/conversations/{conversation}').text
        assert KEY_A not in public and KEY_B not in public
        assert client.get(f'/api/v1/conversations/{conversation}').json()['model_selection']['provider_id'] == b['id']
    for path in config(tmp_path).data_dir.rglob('*'):
        if path.is_file(): assert KEY_A.encode() not in path.read_bytes() and KEY_B.encode() not in path.read_bytes(), path.name


def test_active_and_waiting_tasks_lock_configuration_and_model_selection(tmp_path, monkeypatch):
    with model_server() as (url, _):
        manager = TaskManager(config(tmp_path), worker_target=blocked_worker)
        with TestClient(create_app(manager.settings,manager=manager), base_url='http://127.0.0.1',headers={'Authorization':'Bearer '+TOKEN}) as client:
            p = add_provider(client,url+'/v1')
            conversation = client.post('/api/v1/conversations',json={}).json()['id']
            client.post(f'/api/v1/conversations/{conversation}/files',files={'file':('data.csv', b'x\n1\n')})
            run = client.post(f'/api/v1/conversations/{conversation}/runs',json={'task':'概览','request_id':uid(),'provider_id':p['id'],'model_id':'model-a'}).json()
            assert client.put('/api/v1/settings/providers/'+p['id'],json={'name':'新的名称'}).status_code == 409
            assert client.post('/api/v1/settings/providers/default',json={'provider_id':p['id'],'model_id':'model-b'}).status_code == 409
            assert client.post('/api/v1/settings/providers/'+p['id']+'/test',json={'model_id':'model-a'}).status_code == 409
            assert client.post(f'/api/v1/conversations/{conversation}/runs',json={'task':'概览','request_id':uid(),'provider_id':p['id'],'model_id':'model-b'}).status_code == 409
            with manager.store.connect() as db: db.execute("UPDATE runs SET status='awaiting_input' WHERE id=?",(run['id'],))
            assert client.put('/api/v1/settings/providers/'+p['id'],json={'clear_key':True}).status_code == 409
            client.post('/api/v1/runs/'+run['id']+'/cancel')
            wait_run(client,run['id'],'cancelled')


@pytest.mark.parametrize('key', ['canary-key-"quoted"', r'canary-key-\backslash'])
def test_key_with_json_escapes_cannot_enter_messages_or_uploads(tmp_path, key):
    manager = TaskManager(config(tmp_path, mode='offline'))
    with TestClient(create_app(manager.settings, manager=manager), base_url='http://127.0.0.1', headers={'Authorization':'Bearer '+TOKEN}) as client:
        provider = add_provider(client, 'http://127.0.0.1:9999/v1', key=key)
        assert provider['has_key']
        assert client.post('/api/v1/conversations', json={'title': key}).status_code == 400
        conversation = client.post('/api/v1/conversations', json={}).json()['id']
        assert client.post(f'/api/v1/conversations/{conversation}/runs', json={'task':key,'request_id':uid()}).status_code == 400
        assert client.post(f'/api/v1/conversations/{conversation}/files', files={'file':('data.csv', f'x\n{key}\n'.encode())}).status_code == 400
        assert key not in client.get(f'/api/v1/conversations/{conversation}').text
        with manager.store.connect() as db:
            assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM files').fetchone()[0] == 0


def test_refresh_does_not_hold_global_lock_or_overwrite_concurrent_configuration(tmp_path, monkeypatch):
    manager = TaskManager(config(tmp_path, mode='offline'))
    with TestClient(create_app(manager.settings, manager=manager), base_url='http://127.0.0.1', headers={'Authorization':'Bearer '+TOKEN}) as client:
        provider = add_provider(client, 'http://127.0.0.1:9999/v1')
        def fetch(config):
            acquired = []
            def check_lock():
                available = manager.lock.acquire(timeout=1)
                acquired.append(available)
                if available: manager.lock.release()
            thread = threading.Thread(target=check_lock)
            thread.start(); thread.join(2)
            assert acquired == [True], 'Provider network request held the task lock'
            manager.configure_provider(provider['id'], {'models':['model-a'], 'default_model_id':'model-a'})
            return ['model-b']
        monkeypatch.setattr('labweaver.web.provider_http.fetch_models', fetch)
        refreshed = client.post(f"/api/v1/settings/providers/{provider['id']}/models/refresh")
        assert refreshed.status_code == 409 and refreshed.json()['error']['code'] == 'provider_changed'
        saved = next(p for p in client.get('/api/v1/settings/providers').json()['providers'] if p['id']==provider['id'])
        assert saved['models'] == ['model-a']


def test_legacy_environment_refresh_is_rejected_before_network(tmp_path, monkeypatch):
    for name in ('LLM_API_KEY','LLM_BASE_URL','LLM_MODEL_ID'): monkeypatch.delenv(name, raising=False)
    env = tmp_path / 'existing.env'
    env.write_text('LLM_API_KEY='+KEY_A+'\nLLM_BASE_URL=https://example.invalid/v1\nLLM_MODEL_ID=model-a\n', encoding='utf-8')
    settings = replace(config(tmp_path, mode='offline'), env_file=env)
    def forbidden(config):
        pytest.fail('Read-only environment metadata must not trigger refresh traffic')
    monkeypatch.setattr('labweaver.web.provider_http.fetch_models', forbidden)
    with TestClient(create_app(settings), base_url='http://127.0.0.1', headers={'Authorization':'Bearer '+TOKEN}) as client:
        assert client.post('/api/v1/settings/providers/legacy-env/models/refresh').status_code == 409
