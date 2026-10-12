"""Explicit provider checks, entirely backend-side and without task/file contents."""

import json
import time
from urllib.parse import urlsplit

import httpx

from .security import AppError


def _request(config, method, suffix, body=None):
    url = config.base_url.rstrip('/') + suffix
    started = time.monotonic()
    try:
        # No redirects: a saved endpoint may not forward credentials to another host.
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            with client.stream(method, url, headers={"Authorization": "Bearer " + config.api_key}, json=body) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    raise AppError("provider_redirect", "接口返回重定向，请填写最终 API 根地址。", 502)
                if response.status_code in (401, 403):
                    raise AppError("provider_auth_failed", "服务商拒绝认证，请核对 API Key 和账户权限。", 502)
                if response.status_code == 404:
                    raise AppError("provider_endpoint_missing", "接口或模型不存在，请核对 API 根地址与模型 ID。", 502)
                if response.status_code == 429:
                    raise AppError("provider_rate_limited", "服务商限流或额度不足，请稍后重试。", 502)
                if response.status_code >= 400:
                    raise AppError("provider_request_failed", "服务商未接受请求，请核对接口协议、模型和账户配置。", 502)
                raw = bytearray()
                for part in response.iter_bytes():
                    if time.monotonic() - started > 15:
                        raise AppError("provider_timeout", "服务商请求超时，请核对地址或稍后重试。", 502)
                    raw.extend(part)
                    if len(raw) > 1024 * 1024:
                        raise AppError("provider_response_limit", "服务商返回内容超过 1 MiB。", 502)
                if time.monotonic() - started > 15:
                    raise AppError("provider_timeout", "服务商请求超时，请核对地址或稍后重试。", 502)
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError()
                return payload
    except AppError:
        raise
    except httpx.TimeoutException:
        raise AppError("provider_timeout", "服务商请求超时，请核对地址或稍后重试。", 502) from None
    except (httpx.HTTPError, ValueError, TypeError):
        # SDK/HTTP exceptions can contain URLs, credentials or response bodies.
        raise AppError("provider_connection_failed", "连接失败或响应格式不兼容，请核对 API 根地址和网络。", 502) from None


def fetch_models(config):
    payload = _request(config, 'GET', '/models')
    data = payload.get('data')
    if not isinstance(data, list):
        raise AppError("provider_models_invalid", "服务商没有返回 OpenAI 兼容的模型列表，可手动填写模型 ID。", 502)
    models = []
    for item in data:
        model = item.get('id') if isinstance(item, dict) else None
        if isinstance(model, str) and 0 < len(model) <= 128 and not any(c.isspace() or ord(c) < 32 for c in model):
            if model not in models:
                models.append(model)
    if not models or len(models) > 50:
        raise AppError("provider_models_invalid", "模型列表为空或超过 50 个，可手动维护所需模型。", 502)
    return models


def test_connection(config):
    """Ask for a tiny real tool call; connection success is distinct from Agent acceptance."""
    body = {
        "model": config.model_id,
        "messages": [{"role": "user", "content": "Call connection_check with ok=true. This is only a connection test."}],
        "tools": [{"type": "function", "function": {"name": "connection_check", "description": "Confirm the test connection.",
                   "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}}}],
        "max_tokens": 128, "stream": False,
    }
    if urlsplit(config.base_url).hostname == "api.deepseek.com":
        body["thinking"] = {"type": "disabled"}
    payload = _request(config, 'POST', '/chat/completions', body)
    choices = payload.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AppError("provider_response_invalid", "模型响应不符合 Chat Completions 格式。", 502)
    message = choices[0].get('message')
    if not isinstance(message, dict):
        raise AppError("provider_response_invalid", "模型响应缺少消息。", 502)
    calls = message.get('tool_calls')
    supported = False
    if isinstance(calls, list):
        for call in calls:
            if not (isinstance(call, dict) and call.get('type') == 'function' and isinstance(call.get('id'), str)
                    and call['id'] and isinstance(call.get('function'), dict)
                    and call['function'].get('name') == 'connection_check'):
                continue
            try:
                arguments = json.loads(call['function']['arguments'])
            except (KeyError, TypeError, ValueError):
                continue
            if isinstance(arguments, dict) and arguments == {'ok': True} and type(arguments['ok']) is bool:
                supported = True
    return {"ok": True, "model_id": config.model_id, "tool_call_supported": supported}
