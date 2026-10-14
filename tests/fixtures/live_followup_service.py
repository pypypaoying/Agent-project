"""Browser-test service: live HTTP adapter and real Agent, no cloud requests.

The deterministic model lives on loopback. This fixture verifies overview and
conversation transport; it never pretends to perform Docker data analysis.
"""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))


def main():
    import uvicorn
    from labweaver.web.api import create_app
    from labweaver.web.settings import WebSettings
    from test_web_models import KEY_A, model_server

    for name in ('LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL_ID'):
        os.environ.pop(name, None)
    os.environ['LANGSMITH_TRACING'] = 'false'
    parent = ROOT / '.cache'
    parent.mkdir(exist_ok=True)
    token = 'live-followup-browser-test-token'
    with tempfile.TemporaryDirectory(prefix='followup-browser-', dir=parent) as folder, model_server() as (url, calls):
        settings = WebSettings(data_dir=Path(folder), project_dir=ROOT, mode='live', token=token, frontend_dir=ROOT / 'frontend/dist', cancel_grace=.15)
        server_box = {}
        app = create_app(settings, shutdown=lambda: setattr(server_box['server'], 'should_exit', True))
        provider = app.state.manager.configure_provider(None, {'name': '本地回归模型', 'base_url': url + '/v1',
                                                               'models': ['model-a'], 'default_model_id': 'model-a', 'api_key': KEY_A})
        app.state.manager.configure_default(provider['id'], 'model-a')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
            server_box['server'] = server
            print(json.dumps({'url': f'http://127.0.0.1:{port}', 'token': token}), flush=True)
            server.run(sockets=[listener])
        assert all(c.get('key') == 'Bearer ' + KEY_A for c in calls)


if __name__ == '__main__':
    main()
