"""Provider probes: explicit HTTP requests, bounded responses and safe failures."""

import json

import pytest
httpx = pytest.importorskip('httpx')

from labweaver.config import ModelConfig
from labweaver.web import provider_http
from labweaver.web.security import AppError

KEY = 'provider-http-canary-key'
CONFIG = ModelConfig('model-a', 'https://model.example.invalid/v1', KEY)
CLIENT_TYPE = httpx.Client


def transport(monkeypatch, handler):
    monkeypatch.setattr(provider_http.httpx, 'Client', lambda **kwargs:
                        CLIENT_TYPE(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.parametrize('status,code', [(302, 'provider_redirect'), (401, 'provider_auth_failed'),
                                       (403, 'provider_auth_failed'), (404, 'provider_endpoint_missing'),
                                       (429, 'provider_rate_limited'), (500, 'provider_request_failed')])
def test_errors_never_echo_response_or_follow_redirects(monkeypatch, status, code):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={'Location':'https://other.example.invalid'}, text=KEY)
    transport(monkeypatch, handler)
    with pytest.raises(AppError) as failure:
        provider_http.fetch_models(CONFIG)
    assert failure.value.code == code
    assert KEY not in failure.value.message and len(requests) == 1
    assert requests[0].headers['authorization'] == 'Bearer ' + KEY
    assert str(requests[0].url) == CONFIG.base_url + '/models'


def test_bounded_response_and_invalid_json_do_not_echo_body(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, content=b'x' * (1024 * 1024 + 1)))
    with pytest.raises(AppError, match='provider_response_limit'):
        provider_http.fetch_models(CONFIG)
    transport(monkeypatch, lambda request: httpx.Response(200, text=KEY))
    with pytest.raises(AppError) as failure:
        provider_http.fetch_models(CONFIG)
    assert failure.value.code == 'provider_connection_failed' and KEY not in failure.value.message


def test_timeout_never_echoes_transport_credentials(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout(KEY, request=request)
    transport(monkeypatch, handler)
    with pytest.raises(AppError) as failure:
        provider_http.fetch_models(CONFIG)
    assert failure.value.code == 'provider_timeout' and KEY not in failure.value.message


def test_model_refresh_deduplicates_and_does_not_truncate_large_catalog(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json={'data':[{'id':'a'}, {'id':'a'}, {'id':'b'}]}))
    assert provider_http.fetch_models(CONFIG) == ['a','b']
    transport(monkeypatch, lambda request: httpx.Response(200, json={'data':[{'id':f'model-{i}'} for i in range(51)]}))
    with pytest.raises(AppError, match='provider_models_invalid'):
        provider_http.fetch_models(CONFIG)


@pytest.mark.parametrize('calls,supported', [(None, False),
    ([{'id':'call-test','type':'function','function':{'name':'connection_check','arguments':'{"ok":true}'}}], True),
    ([{'function':{'name':'connection_check','arguments':'not-json'}}], False),
    ([{'id':'call-test','type':'function','function':{'name':'connection_check','arguments':'{"ok":1}'}}], False)])
def test_connection_check_contains_no_user_data_and_reports_actual_tool_capability(monkeypatch, calls, supported):
    def handler(request):
        body = json.loads(request.content)
        assert body['model'] == CONFIG.model_id and body['stream'] is False
        assert body['tools'][0]['function']['name'] == 'connection_check'
        assert set(body) == {'model','messages','tools','max_tokens','stream'}
        return httpx.Response(200, json={'choices':[{'message':{'content':KEY,'tool_calls':calls}}]})
    transport(monkeypatch, handler)
    result = provider_http.test_connection(CONFIG)
    assert result == {'ok':True,'model_id':'model-a','tool_call_supported':supported}
    assert KEY not in json.dumps(result)


def test_slow_stream_has_elapsed_deadline(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json={'data':[{'id':'model-a'}]}))
    elapsed = iter([0, 16])
    monkeypatch.setattr(provider_http.time, 'monotonic', lambda: next(elapsed))
    with pytest.raises(AppError, match='provider_timeout'):
        provider_http.fetch_models(CONFIG)
