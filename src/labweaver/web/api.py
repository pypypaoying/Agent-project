"""Protected loopback HTTP application and replayable SSE. No API accepts host paths."""

import asyncio
from contextlib import asynccontextmanager
import hmac
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from .files import preview_artifact, read_registered
from .manager import ACTIVE, TaskManager
from .security import AppError, contains_secret


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewConversation(Payload):
    title: StrictStr = Field(default="新对话", max_length=120)


class NewRun(Payload):
    task: StrictStr = Field(min_length=1, max_length=32000)
    request_id: StrictStr = Field(min_length=8, max_length=80)
    context_run_ids: list[StrictStr] | None = Field(default=None, max_length=2)
    provider_id: StrictStr | None = Field(default=None, min_length=1, max_length=80)
    model_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)


class ProviderUpdate(Payload):
    name: StrictStr | None = Field(default=None, min_length=1, max_length=80)
    base_url: StrictStr | None = Field(default=None, min_length=1, max_length=2048)
    models: list[StrictStr] | None = Field(default=None, min_length=1, max_length=50)
    default_model_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    api_key: StrictStr | None = Field(default=None, max_length=8192)
    clear_key: StrictBool = False


class ModelSelection(Payload):
    provider_id: StrictStr = Field(min_length=1, max_length=80)
    model_id: StrictStr = Field(min_length=1, max_length=128)


class ProviderTest(Payload):
    model_id: StrictStr = Field(min_length=1, max_length=128)


class Reply(Payload):
    reply: StrictStr = Field(min_length=1, max_length=8000)
    question_id: StrictStr = Field(min_length=1, max_length=160)
    request_id: StrictStr = Field(min_length=8, max_length=80)


def create_app(settings, *, manager=None):
    service = manager or TaskManager(settings)

    @asynccontextmanager
    async def lifespan(app):
        # The API process owns workers and container cleanup through shutdown.
        yield
        await asyncio.to_thread(service.close)

    app = FastAPI(title="LabWeaver Desktop", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.manager = service

    @app.middleware("http")
    async def boundary(request, call_next):
        host = request.headers.get("host", "")
        try:
            parsed = urlsplit("http://" + host)
        except ValueError:
            return JSONResponse({"error": {"code": "host_forbidden", "message": "Host 格式无效。"}}, status_code=403)
        if parsed.hostname not in settings.allowed_hosts or parsed.username or parsed.password:
            return JSONResponse({"error": {"code": "host_forbidden", "message": "只接受本机应用访问。"}}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"http://{host}" and origin not in settings.allowed_origins:
            return JSONResponse({"error": {"code": "origin_forbidden", "message": "来源不被允许。"}}, status_code=403)
        if request.url.path.startswith("/api/"):
            auth = request.headers.get("authorization", "")
            if not hmac.compare_digest(auth.encode(), ("Bearer " + settings.token).encode()):
                return JSONResponse({"error": {"code": "unauthorized", "message": "请通过应用启动链接打开页面。"}}, status_code=401)
            if "token" in request.query_params:
                return JSONResponse({"error": {"code": "query_token_forbidden", "message": "令牌不能使用查询参数传递。"}}, status_code=400)
            if request.method in {"POST", "PUT", "PATCH"}:
                # Bound even chunked multipart requests before Starlette spools them to disk.
                parts, size = [], 0
                async for part in request.stream():
                    size += len(part)
                    if size > 11 * 1024 * 1024:
                        return JSONResponse({"error": {"code": "body_limit", "message": "上传请求超过大小限制。"}}, status_code=413)
                    parts.append(part)
                request._body = b"".join(parts)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(AppError)
    async def application_error(request, error):
        return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Pydantic's default error includes raw input values, which can contain secrets.
        return JSONResponse({"error": {"code": "invalid_request", "message": "请求字段缺失、类型错误或包含不支持的参数。"}}, status_code=422)

    def reject_secret(value):
        if contains_secret(value, service.secrets):
            raise AppError("secret_in_input", "输入包含敏感配置，已拒绝写入聊天或附件。")

    @app.get("/api/v1/health")
    def health(refresh: bool = False):
        return service.health(refresh=refresh)

    @app.get("/api/v1/settings/providers")
    def providers():
        return service.providers.list()

    @app.post("/api/v1/settings/providers", status_code=201)
    def new_provider(body: ProviderUpdate):
        return service.configure_provider(None, body.model_dump(exclude_none=True))

    @app.put("/api/v1/settings/providers/{provider_id}")
    def update_provider(provider_id: str, body: ProviderUpdate):
        return service.configure_provider(provider_id, body.model_dump(exclude_none=True))

    @app.post("/api/v1/settings/providers/default")
    def default_provider(body: ModelSelection):
        reject_secret(body.model_dump())
        return service.configure_default(body.provider_id, body.model_id)

    @app.post("/api/v1/settings/providers/{provider_id}/models/refresh")
    def provider_models(provider_id: str):
        from .provider_http import fetch_models
        with service.lock:
            if service.active:
                raise AppError("task_active", "请完成当前任务或停止后再刷新模型。", 409)
            old = next((p for p in service.providers.list()["providers"] if p["id"] == provider_id), None)
            if old is None:
                raise AppError("provider_not_found", "服务商不存在。", 404)
            if old["kind"] == "env":
                raise AppError("env_provider_read_only", "环境配置的模型列表为只读，请新增自定义服务商。", 409)
            config = service.providers.resolve(provider_id, None)
        models = fetch_models(config)
        with service.lock:
            if service.active:
                raise AppError("task_active", "任务已开始，未更新模型列表。请完成当前任务后重试。", 409)
            current = next(p for p in service.providers.list()["providers"] if p["id"] == provider_id)
            if current["revision"] != old["revision"]:
                raise AppError("provider_changed", "服务商配置已更改，未覆盖新的配置，请重试。", 409)
            reject_secret(models)
            selected = old["default_model_id"] if old["default_model_id"] in models else models[0]
            service.configure_provider(provider_id, {"models": models, "default_model_id": selected})
            return {"models": models}

    @app.post("/api/v1/settings/providers/{provider_id}/test")
    def provider_test(provider_id: str, body: ProviderTest):
        from .provider_http import test_connection
        reject_secret(body.model_dump())
        with service.lock:
            if service.active:
                raise AppError("task_active", "请完成当前任务或停止后再测试连接。", 409)
            config = service.providers.resolve(provider_id, body.model_id)
        return test_connection(config)

    @app.get("/api/v1/conversations")
    def conversations():
        return {"conversations": service.store.conversations()}

    @app.post("/api/v1/conversations", status_code=201)
    def new_conversation(body: NewConversation):
        reject_secret(body.title)
        return service.store.new_conversation(body.title)

    @app.get("/api/v1/conversations/{conversation_id}")
    def conversation(conversation_id: str):
        return service.conversation_view(conversation_id)

    @app.post("/api/v1/conversations/{conversation_id}/files", status_code=201)
    async def upload(conversation_id: str, file: UploadFile = File(...)):
        # Read once with an absolute cap. Multipart parser spools larger bodies, never feeds them to the Agent.
        try:
            raw = await file.read(10 * 1024 * 1024 + 1)
            if len(raw) > 10 * 1024 * 1024:
                raise AppError("file_limit", "上传文件超过 10 MiB。", 413)
            reject_secret(raw)
            reject_secret(file.filename or "")
            return await asyncio.to_thread(service.upload, conversation_id, file.filename or "", raw)
        finally:
            await file.close()

    @app.delete("/api/v1/conversations/{conversation_id}/files/{file_id}")
    def remove_file(conversation_id: str, file_id: str):
        service.remove_file(conversation_id, file_id)
        return {"removed": True}

    @app.post("/api/v1/conversations/{conversation_id}/runs", status_code=202)
    def start_run(conversation_id: str, body: NewRun):
        reject_secret(body.model_dump())
        if not body.task.strip():
            raise AppError("empty_task", "任务不能为空。")
        return service.start(conversation_id, body.task.strip(), body.request_id, body.context_run_ids, body.provider_id, body.model_id)

    @app.get("/api/v1/runs/{run_id}")
    def run(run_id: str):
        return service.run_view(run_id)

    @app.get("/api/v1/runs/{run_id}/events", response_class=EventSourceResponse)
    async def events(run_id: str, request: Request, after: int = Query(default=0, ge=0)):
        service.store.require("runs", run_id)
        last = request.headers.get("last-event-id")
        if last:
            try:
                after = max(after, int(last))
            except ValueError:
                raise AppError("invalid_cursor", "事件序号无效。") from None

        async def stream():
            cursor = after
            while True:
                page = await asyncio.to_thread(service.store.events, run_id, cursor)
                for event in page:
                    cursor = event["seq"]
                    yield ServerSentEvent(data=event, id=str(cursor))
                status = (await asyncio.to_thread(service.store.require, "runs", run_id))["status"]
                if status not in {"running", "cancelling"} and len(page) < 500:
                    # awaiting_input ends this connection. Reply opens a stream from the previous cursor.
                    break
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.15)
        async for event in stream():
            yield event

    @app.post("/api/v1/runs/{run_id}/reply", status_code=202)
    def reply(run_id: str, body: Reply):
        reject_secret(body.model_dump())
        if not body.reply.strip():
            raise AppError("empty_reply", "回复不能为空。")
        return service.reply(run_id, body.question_id, body.reply.strip(), body.request_id)

    @app.post("/api/v1/runs/{run_id}/cancel", status_code=202)
    def cancel(run_id: str):
        return service.cancel(run_id)

    @app.get("/api/v1/artifacts/{artifact_id}/preview")
    def artifact_preview(artifact_id: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=1000)):
        return preview_artifact(service.store, artifact_id, offset, limit)

    @app.get("/api/v1/files/{file_id}/content")
    def download(file_id: str, inline: bool = False):
        file, raw = read_registered(service.store, file_id)
        from urllib.parse import quote
        # Only genuine PNG may be embedded. SVG/text remain downloads, never executable documents.
        disposition = "inline" if inline and file["media_type"] == "image/png" else "attachment"
        return Response(raw, media_type=file["media_type"], headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(file['name'])}"})

    frontend = settings.frontend_dir

    @app.get("/{path:path}")
    def static(path: str):
        if path.startswith("api/") or path.startswith(".") or ".." in Path(path).parts:
            raise AppError("not_found", "页面不存在。", 404)
        if frontend is None or not (frontend / "index.html").is_file():
            return Response("Frontend unavailable. Run npm ci and npm run build in frontend before launch.", status_code=503, media_type="text/plain")
        target = (frontend / path).resolve() if path else frontend / "index.html"
        if not target.is_relative_to(frontend.resolve()):
            raise AppError("not_found", "页面不存在。", 404)
        if not target.is_file():
            target = frontend / "index.html"
        return FileResponse(target)

    return app
