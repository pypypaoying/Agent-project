import type { Conversation, ConversationDetail, ManagedFile, ModelSelection, Provider, ProviderCatalog, ProviderKeyUpdate, ProviderUpdate, Run, RunEvent, TablePreview } from './types';

const TOKEN_KEY = 'labweaver.application-token';
let accessToken = '';

export function initializeToken(): boolean {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const incoming = fragment.get('token');
  if (incoming) {
    accessToken = incoming;
    sessionStorage.setItem(TOKEN_KEY, incoming);
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
  } else {
    accessToken = sessionStorage.getItem(TOKEN_KEY) ?? '';
  }
  return Boolean(accessToken);
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); this.name = 'ApiError'; }
}

async function protectedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set('Authorization', `Bearer ${accessToken}`);
  const response = await fetch(`/api/v1${path}`, { ...init, headers, credentials: 'omit' });
  if (!response.ok) {
    if (response.status === 401) throw new ApiError(401, '访问令牌已失效，请用启动器重新打开应用。');
    let message = `请求未完成（HTTP ${response.status}）`;
    try {
      const body = await response.json();
      const detail = body.detail ?? body.error;
      if (typeof detail === 'string') message = detail;
      else if (detail && typeof detail.message === 'string') message = detail.message;
    } catch { /* Never display request headers or raw server exceptions. */ }
    throw new ApiError(response.status, message);
  }
  return response;
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await protectedFetch(path, init);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
const json = (body: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
export const api = {
  health: () => request<Record<string, unknown>>('/health'),
  shutdown: async () => {
    await protectedFetch('/application/shutdown', json({}));
    accessToken = ''; sessionStorage.removeItem(TOKEN_KEY);
  },
  providers: () => request<ProviderCatalog>('/settings/providers'),
  saveProvider: (id: string | null, values: ProviderUpdate | ProviderKeyUpdate) => request<Provider>(id ? `/settings/providers/${encodeURIComponent(id)}` : '/settings/providers', { ...json(values), method: id ? 'PUT' : 'POST' }),
  defaultProvider: (selection: ModelSelection) => request<ProviderCatalog>('/settings/providers/default', json(selection)),
  refreshProviderModels: (id: string) => request<{ models: string[] }>(`/settings/providers/${encodeURIComponent(id)}/models/refresh`, json({})),
  testProvider: (id: string, model: string) => request<{ ok: boolean; model_id: string; tool_call_supported?: boolean }>(`/settings/providers/${encodeURIComponent(id)}/test`, json({ model_id: model })),
  conversations: () => request<{ conversations: Conversation[] }>('/conversations'),
  createConversation: () => request<Conversation>('/conversations', json({})),
  updateConversation: (id: string, values: { title?: string; pinned?: boolean }) => request<Conversation>(`/conversations/${encodeURIComponent(id)}`, { ...json(values), method: 'PATCH' }),
  deleteConversation: (id: string) => request<{ deleted: boolean; conversation_id: string }>(`/conversations/${encodeURIComponent(id)}`, { ...json({ confirm: true }), method: 'DELETE' }),
  conversation: (id: string) => request<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`),
  upload: (id: string, file: File) => {
    const form = new FormData(); form.append('file', file);
    return request<ManagedFile>(`/conversations/${encodeURIComponent(id)}/files`, { method: 'POST', body: form });
  },
  removeFile: (conversation: string, file: string) => request<void>(`/conversations/${encodeURIComponent(conversation)}/files/${encodeURIComponent(file)}`, { method: 'DELETE' }),
  start: (id: string, task: string, context?: string[], requestId: string = crypto.randomUUID(), selection?: ModelSelection) => request<Run>(`/conversations/${encodeURIComponent(id)}/runs`, json({ task, request_id: requestId, ...(context === undefined ? {} : { context_run_ids: context }), ...(selection ?? {}) })),
  run: (id: string) => request<Run>(`/runs/${encodeURIComponent(id)}`),
  reply: (id: string, pending: string, reply: string, requestId: string = crypto.randomUUID()) => request<Run>(`/runs/${encodeURIComponent(id)}/reply`, json({ reply, question_id: pending, request_id: requestId })),
  cancel: (id: string) => request<Run>(`/runs/${encodeURIComponent(id)}/cancel`, json({})),
  preview: (id: string, offset = 0) => request<TablePreview>(`/artifacts/${encodeURIComponent(id)}/preview?offset=${offset}&limit=20`),
  blob: async (id: string) => (await protectedFetch(`/files/${encodeURIComponent(id)}/content`)).blob(),
};

/** Read SSE over fetch so the application token never appears in a URL. */
export async function streamEvents(runId: string, after: number, signal: AbortSignal, receive: (event: RunEvent) => void): Promise<void> {
  const response = await protectedFetch(`/runs/${encodeURIComponent(runId)}/events?after=${after}`, { signal, headers: { Accept: 'text/event-stream' } });
  if (!response.body) throw new Error('浏览器无法读取执行事件流。');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      // CRLF can straddle a transport chunk; normalize only complete frames.
      let match: RegExpExecArray | null;
      while ((match = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        if (data) receive(JSON.parse(data) as RunEvent);
      }
      if (done) break;
    }
  } finally { reader.releaseLock(); }
}

export async function downloadFile(file: ManagedFile): Promise<void> {
  const blob = await api.blob(file.id);
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url; link.download = file.name; link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
