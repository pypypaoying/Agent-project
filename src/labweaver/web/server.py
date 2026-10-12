"""Loopback launcher. Production serves a built frontend, with a private fragment bootstrap."""

import os
import threading
import webbrowser

from .settings import WebSettings


def serve(args):
    from labweaver.config import ConfigurationError
    try:
        import uvicorn
        from .api import create_app
    except ImportError:
        raise ConfigurationError("Install the optional app dependencies: uv sync --extra web") from None
    settings = WebSettings.from_project(data_dir=args.data_dir, mode=args.mode,
                                        token=os.environ.get("LABWEAVER_ACCESS_TOKEN"), config=args.config)
    if not settings.frontend_dir or not (settings.frontend_dir / "index.html").is_file():
        raise ConfigurationError("Build the frontend once: cd frontend; npm ci; npm run build")
    app = create_app(settings)
    health = app.state.manager.health()
    print(f"LabWeaver Desktop: http://127.0.0.1:{args.port} | mode={settings.mode}", flush=True)
    if not health["model"]["ready"]:
        print("模型配置未就绪。可浏览历史；分析前请在设置页面配置服务商与 API Key。", flush=True)
    if not health["docker"]["ready"]:
        print("Docker/分析镜像未就绪。代码分析没有宿主 Python 降级路径。", flush=True)
    if args.open:
        url = f"http://127.0.0.1:{args.port}/#token={settings.token}"
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # Keep text-tokenizer caches and library temp paths outside the source checkout.
    os.chdir(settings.data_dir)
    # Disable HTTP access logs: bootstrap secrets never belong in URLs or request logs.
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
    return 0
