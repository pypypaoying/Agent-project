"""Opt-in real provider selection; public data only, no keys in test artifacts."""

from dataclasses import replace
import os

import pytest
pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from labweaver.web.api import create_app
from labweaver.web.settings import WebSettings
from labweaver.web.store import uid
from test_web_api import wait_run, ROOT, TOKEN

pytestmark = pytest.mark.live


def test_live_deepseek_selection_switches_actual_agent_in_one_conversation(tmp_path):
    if os.environ.get('LABWEAVER_LIVE') != '1':
        pytest.skip('Real provider requests require LABWEAVER_LIVE=1; local credentials stay backend-only')
    settings = replace(WebSettings.from_project(data_dir=tmp_path / 'provider-live', mode='live', token=TOKEN), frontend_dir=ROOT / 'frontend/dist')
    with TestClient(create_app(settings), base_url='http://127.0.0.1', headers={'Authorization':'Bearer '+TOKEN}) as client:
        preset = next(p for p in client.get('/api/v1/settings/providers').json()['providers'] if p['id']=='deepseek')
        assert preset['has_key'], 'Set the official DeepSeek key in backend configuration before this acceptance'
        conversation = client.post('/api/v1/conversations', json={}).json()['id']
        source = ROOT / 'examples/data/survey.csv'
        assert client.post(f'/api/v1/conversations/{conversation}/files', files={'file':('survey.csv', source.read_bytes())}).status_code == 201
        previous = None
        for model in ('deepseek-flash', 'deepseek-v4-pro'):
            response = client.post(f'/api/v1/conversations/{conversation}/runs', json={'task':'仅概览这份 CSV 的行列数、字段与缺失情况，无需计算、绘图或检索。','request_id':uid(),'provider_id':'deepseek','model_id':model})
            assert response.status_code == 202, response.text
            done = wait_run(client, response.json()['id'], 'completed', timeout=120)
            assert done['model_selection']['model_id'] == model
            assert done['model_calls'] >= 2 and done['analysis_model_calls'] == 0
            assert any(e['type']=='tool_end' and e['payload']['name']=='profile_csv' for e in done['events'])
            assert done['final_answer'] and not done['error']
            if previous:
                assert previous['core_session_id'] != done['core_session_id'] and done['history_run_ids']
            previous = done
