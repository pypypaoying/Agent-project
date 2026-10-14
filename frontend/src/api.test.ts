import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api, initializeToken, request, streamEvents } from './api';

beforeEach(() => { sessionStorage.clear(); window.history.replaceState(null, '', '/'); initializeToken(); vi.unstubAllGlobals(); });

describe('protected local API', () => {
  it('consumes fragment token immediately and retains it only in session storage', async () => {
    window.history.replaceState(null, '', '/#token=ephemeral-app-token');
    expect(initializeToken()).toBe(true);
    expect(window.location.hash).toBe('');
    expect(sessionStorage.getItem('labweaver.application-token')).toBe('ephemeral-app-token');
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ mode: 'offline' })));
    vi.stubGlobal('fetch', fetch);
    await api.health();
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/health');
    expect(fetch.mock.calls[0][1].headers.get('Authorization')).toBe('Bearer ephemeral-app-token');
    expect(fetch.mock.calls[0][1].credentials).toBe('omit');
  });

  it('does not put the token in SSE or file URLs', async () => {
    window.history.replaceState(null, '', '/#token=application-token'); initializeToken();
    const envelope = { version: 1, conversation_id: 'c', run_id: 'r', seq: 3, type: 'plan', payload: { deliverables: [] } };
    const bytes = new TextEncoder().encode(`id: 3\r\ndata: ${JSON.stringify(envelope)}\r\n\r\n`);
    const body = new ReadableStream({ start(controller) { controller.enqueue(bytes); controller.close(); } });
    const fetch = vi.fn().mockResolvedValue(new Response(body)); vi.stubGlobal('fetch', fetch);
    const receive = vi.fn(); await streamEvents('r', 2, new AbortController().signal, receive);
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/runs/r/events?after=2');
    expect(receive).toHaveBeenCalledWith(envelope);
  });

  it('parses fragmented CRLF SSE frames, ignores comments and joins data lines', async () => {
    const fragments = [': heartbeat\r\n\r\ndata: {"version":1,\r', '\ndata: "seq":7,"type":"answer_delta","payload":{"text":"中"}}\r\n\r', '\n'];
    const body = new ReadableStream({ start(controller) { for (const chunk of fragments) controller.enqueue(new TextEncoder().encode(chunk)); controller.close(); } });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)));
    const receive = vi.fn(); await streamEvents('run', 0, new AbortController().signal, receive);
    expect(receive).toHaveBeenCalledTimes(1);
    expect(receive.mock.calls[0][0].payload.text).toBe('中');
  });

  it('shows only sanitized error details and never raw response text', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('raw secret SDK stack', { status: 500 })));
    await expect(request('/health')).rejects.toThrow('HTTP 500');
  });

  it('omits history override by default so a live session can continue', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 'r', status: 'running' }))); vi.stubGlobal('fetch', fetch);
    await api.start('c', '任务');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).not.toHaveProperty('context_run_ids');
  });

  it('sends model identifiers without model credentials in the task request', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 'r', status: 'running' }))); vi.stubGlobal('fetch', fetch);
    await api.start('c', '任务', undefined, 'request-unique', { provider_id: 'deepseek', model_id: 'deepseek-v4-pro' });
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ task: '任务', request_id: 'request-unique', provider_id: 'deepseek', model_id: 'deepseek-v4-pro' });
  });

  it('starts a follow-up from a hydrated history selection without sending display metadata', async () => {
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ id: 'r', status: 'running' }))); vi.stubGlobal('fetch', fetch);
    const selection = { provider_id: 'deepseek', model_id: 'deepseek-v4-pro', provider_name: 'DeepSeek', base_url: 'https://api.deepseek.com' };
    await api.start('c', '首次任务', undefined, 'first-request', selection);
    await api.start('c', '继续分析', ['first-run'], 'follow-up-request', selection);
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ task: '继续分析', request_id: 'follow-up-request', context_run_ids: ['first-run'], provider_id: 'deepseek', model_id: 'deepseek-v4-pro' });
    expect(selection).toHaveProperty('provider_name', 'DeepSeek');
  });

  it('projects model identifiers at request boundaries and cannot overwrite task, context or retry IDs', async () => {
    const fetch = vi.fn().mockImplementation(async () => new Response('{}')); vi.stubGlobal('fetch', fetch);
    const selection = { provider_id: 'deepseek', model_id: 'deepseek-v4-pro', provider_name: 'DeepSeek', base_url: 'https://api.deepseek.com', task: '不应发送', context_run_ids: ['wrong'], request_id: 'wrong', api_key: 'untrusted-extra-field' };
    await api.start('c', '实际任务', [], 'stable-request', selection);
    await api.start('c', '实际任务', [], 'stable-request', selection);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ task: '实际任务', request_id: 'stable-request', context_run_ids: [], provider_id: 'deepseek', model_id: 'deepseek-v4-pro' });
    expect(fetch.mock.calls[0][1].body).toBe(fetch.mock.calls[1][1].body);
    await api.defaultProvider(selection);
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual({ provider_id: 'deepseek', model_id: 'deepseek-v4-pro' });
  });

  it('shuts down through the protected endpoint and clears the expired application token after acceptance', async () => {
    window.history.replaceState(null, '', '/#token=application-token'); initializeToken();
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'stopping' }), { status: 202 })); vi.stubGlobal('fetch', fetch);
    await api.shutdown();
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/application/shutdown');
    expect(fetch.mock.calls[0][1].method).toBe('POST');
    expect(fetch.mock.calls[0][1].headers.get('Authorization')).toBe('Bearer application-token');
    expect(sessionStorage.getItem('labweaver.application-token')).toBeNull();
  });

  it('explains a stale launcher token without displaying server authentication details', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'raw secret token details' }), { status: 401 })));
    await expect(api.providers()).rejects.toThrow('访问令牌已失效，请用启动器重新打开应用');
  });

  it('updates conversation metadata and explicitly confirms deletion without arbitrary file paths', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ id: 'c', title: '新名称', pinned: true }))).mockResolvedValueOnce(new Response(JSON.stringify({ deleted: true, conversation_id: 'c' }))); vi.stubGlobal('fetch', fetch);
    await api.updateConversation('c', { title: '新名称', pinned: true });
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/conversations/c');
    expect(fetch.mock.calls[0][1].method).toBe('PATCH');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ title: '新名称', pinned: true });
    await api.deleteConversation('c');
    expect(fetch.mock.calls[1][1].method).toBe('DELETE');
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ confirm: true });
  });
});
