"""A saved model description must not become writable task parameters."""
import json

import pytest
pytest.importorskip('fastapi')
from fastapi.testclient import TestClient

from labweaver.web.api import create_app
from labweaver.web.store import uid
from test_web_models import TOKEN, add_provider, config, model_server
from test_web_api import wait_run


def test_restored_selection_keeps_strict_api_and_followup_reuses_bound_csv(tmp_path, monkeypatch):
    for key in ('LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL_ID'):
        monkeypatch.delenv(key, raising=False)
    with model_server() as (url, calls), TestClient(create_app(config(tmp_path)), base_url='http://127.0.0.1', headers={'Authorization': 'Bearer ' + TOKEN}) as client:
        provider = add_provider(client, url + '/v1')
        conversation = client.post('/api/v1/conversations', json={}).json()['id']
        uploaded = client.post(f'/api/v1/conversations/{conversation}/files', files={'file': ('same.csv', b'id,value\n001,2\n002,3\n')}).json()
        task = '只概览字段与缺失情况，不需要计算或绘图。'
        first = client.post(f'/api/v1/conversations/{conversation}/runs', json={'task': task, 'request_id': uid(), 'provider_id': provider['id'], 'model_id': 'model-a'})
        assert first.status_code == 202
        completed = wait_run(client, first.json()['id'], 'completed', timeout=80)
        history = client.get(f'/api/v1/conversations/{conversation}').json()
        selected = history['model_selection']
        assert set(selected) == {'provider_id', 'model_id', 'provider_name', 'base_url'}
        calls.clear()
        # Reproduce the old browser spread: the API rejects read-only metadata
        # before scheduling a worker or writing a second user message.
        invalid = client.post(f'/api/v1/conversations/{conversation}/runs', json={'task': task, 'request_id': uid(), **selected})
        assert invalid.status_code == 422 and invalid.json()['error']['code'] == 'invalid_request'
        assert not calls
        assert len(client.get(f'/api/v1/conversations/{conversation}').json()['runs']) == 1
        identifiers = {key: selected[key] for key in ('provider_id', 'model_id')}
        request = {'task': task, 'request_id': uid(), **identifiers}
        followup = client.post(f'/api/v1/conversations/{conversation}/runs', json=request)
        assert followup.status_code == 202, followup.text
        second = wait_run(client, followup.json()['id'], 'completed', timeout=80)
        assert second['core_session_id'] == completed['core_session_id']
        assert second['core_task_id'] != completed['core_task_id']
        history = client.get(f'/api/v1/conversations/{conversation}').json()
        assert len(history['runs']) == 2 and history['files'][0]['id'] == uploaded['id']
        assert second['model_selection'] == selected
        assert calls and all(c['request']['model'] == 'model-a' for c in calls)
        calls.clear()
        duplicate = client.post(f'/api/v1/conversations/{conversation}/runs', json=request)
        assert duplicate.json()['id'] == second['id'] and not calls
        assert 'models-private-key-canary' not in json.dumps(history)
