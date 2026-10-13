"""Single-instance loopback launcher; open a browser only after successful startup."""

from dataclasses import replace
import os
import socket
import threading
import time
import uuid
import webbrowser
from urllib.parse import urlencode

from .instances import InstanceRegistry
from .security import AppError, managed_path
from .settings import WebSettings


def _open(port, token):
    # Never print the private bootstrap link or schedule it before binding.
    webbrowser.open(f'http://127.0.0.1:{port}/#' + urlencode({'token': token}))


def _reuse(registry, settings, args):
    existing = registry.verified_instance()
    if not existing:
        return False
    if existing['mode'] != settings.mode:
        raise AppError('running_mode_mismatch', '已有 LabWeaver 使用另一运行模式，请退出应用后再切换模式。', 409)
    print(f'LabWeaver 已在 http://127.0.0.1:{args.port} 运行，复用现有服务。', flush=True)
    if args.open:
        _open(args.port, existing['token'])
    return True


def _bind(port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == 'nt':
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(128)
        listener.set_inheritable(True)
        return listener
    except OSError:
        listener.close()
        raise


def serve(args):
    from labweaver.config import ConfigurationError
    try:
        import uvicorn
        from .api import create_app
    except ImportError:
        raise ConfigurationError('Install the optional app dependencies: uv sync --extra web') from None
    settings = WebSettings.from_project(data_dir=args.data_dir, mode=args.mode,
                                        token=os.environ.get('LABWEAVER_ACCESS_TOKEN'), config=args.config)
    if not settings.frontend_dir or not (settings.frontend_dir / 'index.html').is_file():
        raise ConfigurationError('Build the frontend once: cd frontend; npm ci; npm run build')
    registry = InstanceRegistry(settings, args.port, explicit_data_dir=args.data_dir is not None)
    settings = replace(settings, data_dir=registry.storage_root(settings.data_dir))
    managed_path(settings.data_dir, 'placeholder')
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings = replace(settings, data_dir=settings.data_dir.resolve(strict=True))
    if _reuse(registry, settings, args):
        return 0
    try:
        listener = _bind(args.port)
    except OSError:
        # A simultaneous first launch can be between bind and readiness.
        starting = registry.read()
        began = time.monotonic()
        extended = bool(starting and starting.get('state') == 'starting')
        deadline = began + (30 if extended else 3)
        while time.monotonic() < deadline:
            current = registry.read()
            if not extended and current and current.get('state') == 'starting':
                starting, extended, deadline = current, True, began + 30
            if _reuse(registry, settings, args):
                return 0
            try:
                listener = _bind(args.port)
                break
            except OSError:
                pass
            time.sleep(.15)
        else:
            if starting and starting.get('state') == 'starting':
                raise AppError('application_starting', '运行记录显示 LabWeaver 正在启动，尚未完成身份验证。请稍后重新打开；本次未打开新页面。', 409) from None
            raise AppError('port_in_use', f'端口 {args.port} 已被占用，且未能验证为同一 LabWeaver 服务。请先关闭原启动终端，或使用其他端口；本次未打开新页面。', 409) from None

    instance_id, app = uuid.uuid4().hex, None
    previous_cwd = os.getcwd()
    try:
        def shutdown():
            server.should_exit = True

        app = create_app(settings, instance_id=instance_id, port=args.port, shutdown=shutdown)
        registry.write(instance_id, settings.mode, settings.token, app.state.manager.settings.data_dir, state='starting')
        print('LabWeaver 正在启动并检查本机环境……', flush=True)
        health = app.state.manager.health()
        if not health['model']['ready']:
            print('模型配置未就绪。可浏览历史；分析前请在设置页面配置服务商与 API Key。', flush=True)
        if not health['docker']['ready']:
            print('Docker/分析镜像未就绪。代码分析没有宿主 Python 降级路径。', flush=True)

        class DesktopServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                if self.started and not self.should_exit:
                    registry.write(instance_id, settings.mode, settings.token, app.state.manager.settings.data_dir)
                    print(f'LabWeaver Desktop: http://127.0.0.1:{args.port} | mode={settings.mode}', flush=True)
                    print('关闭页面保留后台服务；退出应用可用左下角按钮或在启动终端按 Ctrl+C。', flush=True)
                    if args.open:
                        threading.Thread(target=_open, args=(args.port, settings.token), daemon=True).start()

        server = DesktopServer(uvicorn.Config(app, host='127.0.0.1', port=args.port,
                                              access_log=False, log_level='warning'))
        os.chdir(app.state.manager.settings.data_dir)
        server.run(sockets=[listener])
        return 0
    finally:
        listener.close()
        if app is not None:
            app.state.manager.close()
        registry.remove(instance_id)
        os.chdir(previous_cwd)
