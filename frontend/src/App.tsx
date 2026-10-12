import { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { api, ApiError, downloadFile, streamEvents } from './api';
import { summarizeProgress, type ProgressStep } from './progress';
import type { Artifact, Conversation, ConversationDetail, ManagedFile, Message, Run, RunEvent, RunStatus, TablePreview } from './types';

const activeStatuses: RunStatus[] = ['running', 'awaiting_input', 'cancelling'];
const labels: Record<RunStatus, string> = { running: '正在分析', awaiting_input: '等待回复', cancelling: '正在停止', completed: '已完成', error: '未完成', cancelled: '已停止', interrupted: '运行已中断' };
const extension = (name: string) => name.split('.').pop()?.toLowerCase() ?? '';
const text = (value: unknown): string => typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value, null, 2);
const answerText = (value: unknown): string => {
  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    for (const key of ['answer', 'summary', 'final_answer', 'content']) if (typeof obj[key] === 'string') return obj[key];
  }
  return text(value);
};
const displayColumn = (value: unknown): string => {
  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    return text(obj.name ?? obj.original_name ?? obj.label ?? obj.id ?? value);
  }
  return text(value);
};

function Icon({ name }: { name: 'plus' | 'attach' | 'panel' | 'arrow' | 'stop' | 'file' | 'download' | 'close' | 'check' }) {
  const paths = {
    plus: <path d="M12 5v14M5 12h14" />,
    attach: <path d="m9 14 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9" />,
    panel: <><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M15 4v16" /></>,
    arrow: <path d="m6 11 6-6 6 6M12 5v14" />,
    stop: <rect x="6" y="6" width="12" height="12" rx="2" />,
    file: <><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" /><path d="M14 3v6h6M8 14h8M8 17h5" /></>,
    download: <><path d="M12 3v12m-5-5 5 5 5-5M5 17v4h14v-4" /></>,
    close: <path d="m6 6 12 12M18 6 6 18" />,
    check: <path d="m5 12 4 4 10-10" />,
  };
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

const isEmptyDraft = (candidate: ConversationDetail) => !candidate.conversation.frozen && !candidate.conversation.inputs_frozen && candidate.files.length === 0 && candidate.messages.length === 0 && candidate.runs.length === 0;
async function findEmptyDraft(conversations: Conversation[], current: ConversationDetail | null): Promise<ConversationDetail | null> {
  if (current && isEmptyDraft(current)) return current;
  for (const conversation of conversations) {
    if (conversation.id === current?.conversation.id || conversation.frozen || conversation.inputs_frozen) continue;
    try {
      const candidate = await api.conversation(conversation.id);
      if (isEmptyDraft(candidate)) return candidate;
    } catch (reason) {
      if (!(reason instanceof ApiError && reason.status === 404)) throw reason;
    }
  }
  return null;
}

function Markdown({ children }: { children: string }) {
  return <div className="prose"><ReactMarkdown skipHtml components={{
    a: ({ href, children: linkText }) => href && /^https?:\/\//.test(href) ? <a href={href} target="_blank" rel="noopener noreferrer">{linkText}</a> : <span>{linkText}</span>,
    // Model-generated image paths are never loaded. Only registered PNG files are rendered below.
    img: ({ alt }) => <span>{alt ?? ''}</span>,
  }}>{children}</ReactMarkdown></div>;
}

function DataTable({ preview }: { preview: TablePreview }) {
  return <div className="table-scroll"><table>
    <thead><tr>{preview.columns.map((column, index) => <th key={index}>{displayColumn(column)}</th>)}</tr></thead>
    <tbody>{preview.rows.map((row, index) => <tr key={index}>{row.map((cell, column) => <td key={column}>{text(cell)}</td>)}</tr>)}</tbody>
  </table>{preview.rows.length === 0 && <p className="muted empty-row">结果为空</p>}</div>;
}

function MetricOutput({ value }: { value: unknown }) {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return <dl className="metric-grid">{Object.entries(value as Record<string, unknown>).map(([label, metric]) => <div key={label}><dt>{label}</dt><dd>{text(metric)}</dd></div>)}</dl>;
  }
  return <div className="metric-value">{text(value)}</div>;
}

function ProtectedImage({ file, onError }: { file: ManagedFile; onError: (message: string) => void }) {
  const [url, setUrl] = useState('');
  useEffect(() => {
    let disposed = false; let objectUrl = '';
    void api.blob(file.id).then(blob => {
      if (disposed) return;
      objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
    }).catch(error => { if (!disposed) onError(error.message); });
    return () => { disposed = true; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [file.id, onError]);
  return url ? <img className="chart" src={url} alt={file.name} /> : <p className="muted">正在加载图表…</p>;
}

function ArtifactCard({ artifact, onError }: { artifact: Artifact; onError: (message: string) => void }) {
  const [preview, setPreview] = useState<TablePreview | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const isTable = artifact.kind === 'table';
  useEffect(() => {
    if (!isTable) return;
    let disposed = false; setLoading(true);
    void api.preview(artifact.id, offset).then(result => { if (!disposed) setPreview(result); }).catch(error => { if (!disposed) onError(error.message); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [artifact.id, offset, isTable, onError]);
  return <section className="artifact-card" aria-label={artifact.label ?? artifact.kind}>
    <div className="artifact-heading"><strong>{artifact.label ?? ({ table: '结果表', metric: '计算结果', figure: '图表' }[artifact.kind] ?? '成果')}</strong>
      <div className="download-list">{(artifact.files ?? []).map(file => <button className="link-button" key={file.id} onClick={() => void downloadFile(file).catch(error => onError(error.message))}><Icon name="download" />{extension(file.name).toUpperCase()}</button>)}</div>
    </div>
    {artifact.kind === 'metric' && <MetricOutput value={artifact.value} />}
    {(artifact.files ?? []).filter(file => extension(file.name) === 'png').map(file => <ProtectedImage key={file.id} file={file} onError={onError} />)}
    {isTable && <>{loading && !preview ? <p className="muted">正在读取结果…</p> : preview && <DataTable preview={preview} />}
      {preview && <div className="pagination"><span>共 {preview.row_count.toLocaleString()} 行 · 当前 {preview.row_count === 0 ? 0 : offset + 1}–{Math.min(offset + preview.rows.length, preview.row_count)} 行</span><button disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</button><button disabled={offset + preview.rows.length >= preview.row_count || loading} onClick={() => setOffset(offset + 20)}>下一页</button></div>}
    </>}
  </section>;
}

function ProgressRow({ step }: { step: ProgressStep }) {
  return <li className={`progress-row progress-${step.state}`}><span className="progress-mark">{step.state === 'complete' ? <Icon name="check" /> : step.state === 'running' ? <span className="pulse" /> : <span className="progress-ring" />}</span><div><span>{step.title}</span>{step.detail && <small>{step.detail}</small>}</div><span className="progress-status">{step.status}</span></li>;
}

function RunProgress({ run, events }: { run: Run; events: RunEvent[] }) {
  const active = activeStatuses.includes(run.status);
  const steps = summarizeProgress(events, active, run.status === 'awaiting_input');
  const plans = events.filter(event => event.type === 'plan').at(-1)?.payload.deliverables;
  if (!steps.length && !Array.isArray(plans)) return null;
  const visibleCount = active ? 6 : 5;
  const older = steps.slice(0, Math.max(0, steps.length - visibleCount));
  const recent = steps.slice(-visibleCount);
  return <section className="run-progress" aria-label="真实执行过程"><div className="progress-heading">执行过程<span>{steps.length} 项实际记录</span></div>
    {Array.isArray(plans) && plans.length > 0 && <details className="progress-plan" open={active}><summary>交付计划</summary><ul>{plans.map((raw, index) => { const plan = raw as Record<string, unknown>; return <li key={index}>{text(plan.description)}</li>; })}</ul></details>}
    {older.length > 0 && <details className="older-progress"><summary>查看较早的 {older.length} 项执行记录</summary><ol>{older.map(step => <ProgressRow key={step.id} step={step} />)}</ol></details>}
    <ol className="recent-progress" aria-live={active ? 'polite' : 'off'}>{recent.map(step => <ProgressRow key={step.id} step={step} />)}</ol>
  </section>;
}

function RunCard({ run, liveAnswer, messages, events, onError, onRerun }: { run: Run; liveAnswer?: string; messages: Message[]; events: RunEvent[]; onError: (message: string) => void; onRerun: (task: string) => void }) {
  const final = answerText(run.final_answer);
  const provisional = activeStatuses.includes(run.status);
  return <article className="run-card" data-testid={`run-${run.id}`}>
    {run.task && <div className="user-message"><span className="message-label">你</span><p>{run.task}</p></div>}
    {messages.filter(message => message.question_id && (message.role === 'user' || message.state === 'answered')).map(message => <div key={message.id} className={`confirmation-message ${message.role === 'user' ? 'confirmed-reply' : ''}`}><span className="message-label">{message.role === 'user' ? '你的确认回复' : 'Agent 澄清'}</span><p>{message.content}</p></div>)}
    <div className="assistant-message">
      <div className="assistant-heading"><span className="brand-dot small" /><strong>LabWeaver</strong><span className={`status status-${run.status}`}>{labels[run.status]}</span></div>
      <RunProgress run={run} events={events} />
      {(liveAnswer || final) && <><Markdown>{final || liveAnswer || ''}</Markdown>{provisional && <span className="muted verification">正文待核验，尚未确认交付完成</span>}</>}
      {run.status === 'running' && !liveAnswer && !final && <p className="muted working"><span className="pulse" />正在分析，回答与成果仍需核验。</p>}
      {Boolean(run.error) && <p className="run-error">{text(run.error)}</p>}
      {run.status === 'interrupted' && <div className="interrupted-note"><p>服务重启前的执行已中断。历史结果仍可查看；原中断节点不能恢复。</p><button onClick={() => onRerun(run.task ?? '')}>按原任务重新执行</button></div>}
      {(run.artifacts ?? []).map(artifact => <ArtifactCard key={artifact.id} artifact={artifact} onError={onError} />)}
      {!!run.downloads?.length && <details className="all-files"><summary>报告、代码与全部文件（{run.downloads.length}）</summary>{run.downloads.map(file => <button className="file-download" key={file.id} onClick={() => void downloadFile(file).catch(error => onError(error.message))}><Icon name="file" /><span>{file.name}</span><Icon name="download" /></button>)}</details>}
    </div>
  </article>;
}

function ExecutionPanel({ events, run, onClose, onError }: { events: RunEvent[]; run?: Run; onClose: () => void; onError: (message: string) => void }) {
  const names: Record<string, string> = { running: '任务运行', completed: '交付完成', error: '执行未完成', cancelled: '任务停止', interrupted: '运行中断', cancelling: '正在停止', artifacts_saved: '已保存部分成果', container_lifecycle: '沙箱生命周期', plan: '任务计划', tool_start: '工具开始', tool_end: '工具结束', execution_start: '执行 Python', execution_end: '执行结果', retrieval_result: '资料检索', material_search: '资料检索', completion_feedback: '结果核验', saving: '保存成果', validated: '执行证据核验', awaiting_input: '等待用户回复' };
  const detailEvents = events.filter(event => event.type !== 'answer_delta');
  return <aside className="execution-panel" aria-label="执行面板"><header><strong>执行与成果</strong><button className="icon-button" aria-label="关闭执行面板" onClick={onClose}><Icon name="close" /></button></header>
    {run && <div className={`panel-state status-${run.status}`}>{labels[run.status]}</div>}
    {!detailEvents.length && <p className="muted panel-empty">任务开始后，这里显示真实计划、工具执行、代码与来源。</p>}
    <div className="event-list">{detailEvents.map(event => <details key={event.seq} className="event-card" open={event.type === 'plan' || event.type === 'retrieval_result' || event.type === 'execution_end' && event.payload.status === 'error'}><summary><span className="event-index">{event.seq}</span><span>{names[event.type] ?? event.type}</span>{typeof event.payload.name === 'string' ? <small>{event.payload.name}</small> : Boolean(event.payload.role) && <small>{event.payload.role === 'analysis' ? '代码 Agent' : '主 Agent'}</small>}</summary>
      {typeof event.payload.code === 'string' && <pre className="code-block"><code>{event.payload.code}</code></pre>}
      {(event.type === 'retrieval_result' || event.type === 'material_search') && Array.isArray(event.payload.matches) && <div className="source-list"><p className="muted">检索：{text(event.payload.query)}</p>{event.payload.matches.length === 0 ? <p>当前查询没有匹配资料。</p> : event.payload.matches.map((raw, index) => { const match = raw as Record<string, unknown>; const location = match.location as Record<string, unknown> | undefined;
        return <blockquote key={index}><strong>[{text(match.id)}] {text(match.name)}</strong><small>{location?.page ? `第 ${text(location.page)} 页` : location?.line_start ? `第 ${text(location.line_start)}–${text(location.line_end)} 行` : ''}</small><p>{text(match.text)}</p></blockquote>;
      })}</div>}
      <pre className="event-payload">{text(Object.fromEntries(Object.entries(event.payload).filter(([key]) => key !== 'code')))}</pre>
    </details>)}</div>
    {!!run?.downloads?.length && <section className="panel-files"><h3>生成文件</h3>{run.downloads.map(file => <button className="file-download" key={file.id} onClick={() => void downloadFile(file).catch(error => onError(error.message))}><Icon name="file" /><span>{file.name}</span><Icon name="download" /></button>)}</section>}
  </aside>;
}

export function App({ authenticated }: { authenticated: boolean }) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [health, setHealth] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [task, setTask] = useState('');
  const [reply, setReply] = useState('');
  const [panel, setPanel] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [selectedContexts, setSelectedContexts] = useState<string[]>([]);
  const [contextsExplicit, setContextsExplicit] = useState(false);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [liveAnswer, setLiveAnswer] = useState('');
  const [connection, setConnection] = useState('');
  const uploadInput = useRef<HTMLInputElement>(null);
  const taskInput = useRef<HTMLTextAreaElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const composing = useRef(false);
  const pendingRequest = useRef<{ key: string; id: string } | null>(null);
  const newConversationPending = useRef(false);
  const displayedConversation = useRef<string | null>(null);
  const activeRun = detail?.runs.find(run => activeStatuses.includes(run.status));
  const latestRun = activeRun ?? detail?.runs.at(-1);
  const frozen = Boolean(detail?.conversation.frozen || detail?.conversation.inputs_frozen || detail?.runs.length);
  const hasCsv = detail?.files.some(file => file.role === 'csv' || extension(file.name) === 'csv') ?? false;
  const showError = useCallback((message: string) => setError(message), []);

  const refreshList = useCallback(async () => {
    const [result, status] = await Promise.all([api.conversations(), api.health()]);
    setConversations(result.conversations); setHealth(status);
  }, []);
  const refreshDetail = useCallback(async (id: string) => {
    const result = await api.conversation(id);
    if (displayedConversation.current === id) setDetail(result);
    return result;
  }, []);
  const openConversation = useCallback(async (id: string) => {
    setError(''); setBusy(true); displayedConversation.current = id; setTask(''); setReply(''); setLiveAnswer(''); setEvents([]);
    try {
      const result = await refreshDetail(id);
      if (displayedConversation.current !== id) return;
      setSelectedContexts(result.runs.filter(run => run.status === 'completed').slice(-2).map(run => run.id)); setContextsExplicit(false);
      const last = result.runs.at(-1); setEvents(last?.events ?? []);
    } catch (reason) { setError((reason as Error).message); } finally { setBusy(false); }
  }, [refreshDetail]);
  const newConversation = useCallback(async () => {
    if (newConversationPending.current) return displayedConversation.current;
    newConversationPending.current = true;
    setError(''); setBusy(true);
    try {
      const draft = await findEmptyDraft(conversations, detail);
      if (draft && draft.conversation.id === detail?.conversation.id) { taskInput.current?.focus(); return draft.conversation.id; }
      const next = draft ?? { conversation: await api.createConversation(), files: [], messages: [], runs: [] };
      if (!draft) await refreshList();
      displayedConversation.current = next.conversation.id;
      setDetail(next); setTask(''); setReply(''); setSelectedContexts([]); setContextsExplicit(false); setEvents([]); setLiveAnswer('');
      return next.conversation.id;
    } catch (reason) { setError((reason as Error).message); return null; } finally { setBusy(false); newConversationPending.current = false; }
  }, [refreshList, conversations, detail]);

  useEffect(() => {
    if (!authenticated) return;
    void refreshList().catch(reason => setError(reason instanceof ApiError && reason.status === 401 ? '访问令牌已失效，请使用启动器重新打开应用。' : reason.message));
  }, [authenticated, refreshList]);

  useEffect(() => {
    if (!activeRun || activeRun.status === 'awaiting_input') return;
    const abort = new AbortController();
    let disposed = false; let lastSeq = 0;
    const initial = activeRun.events ?? [];
    initial.forEach(event => { lastSeq = Math.max(lastSeq, event.seq); });
    let answerId = initial.filter(event => event.type === 'answer_delta').at(-1)?.payload.id;
    setEvents(initial); setLiveAnswer(initial.filter(event => event.type === 'answer_delta' && event.payload.id === answerId).map(event => text(event.payload.text)).join(''));
    const loop = async () => {
      while (!disposed) {
        let terminal = false;
        try {
          setConnection('');
          await streamEvents(activeRun.id, lastSeq, abort.signal, event => {
            if (disposed || event.seq <= lastSeq) return;
            lastSeq = event.seq; setEvents(previous => [...previous, event]);
            if (event.type === 'answer_delta') {
              if (event.payload.id !== answerId) { answerId = event.payload.id; setLiveAnswer(text(event.payload.text)); }
              else setLiveAnswer(previous => previous + text(event.payload.text));
            }
            if (['completed', 'error', 'cancelled', 'interrupted', 'awaiting_input'].includes(event.type)) terminal = true;
          });
          if (disposed) return;
          const snapshot = await api.run(activeRun.id);
          if (!activeStatuses.includes(snapshot.status) || snapshot.status === 'awaiting_input' || terminal) {
            if (snapshot.events) setEvents(snapshot.events);
            await refreshDetail(displayedConversation.current!); await refreshList(); return;
          }
        } catch (reason) {
          if (disposed || abort.signal.aborted) return;
          if (reason instanceof ApiError && (reason.status === 401 || reason.status === 404)) { setError(reason.message); return; }
          setConnection('连接已暂时断开，正在续接执行事件；任务不会重新运行。');
        }
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    };
    void loop();
    return () => { disposed = true; abort.abort(); };
  }, [activeRun?.id, activeRun?.status, refreshDetail, refreshList]);

  useEffect(() => { bottom.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' }); }, [liveAnswer, detail?.runs.length]);

  const uploadFiles = async (list: File[]) => {
    if (busy) { setError('正在准备对话或保存文件，请稍后再添加附件。'); return; }
    if (frozen || activeRun) { setError('当前对话的输入已冻结。更换 CSV 或资料请新建对话。'); return; }
    const valid = list.filter(file => ['csv', 'txt', 'md', 'pdf'].includes(extension(file.name)));
    if (valid.length !== list.length) setError('目前支持 CSV、TXT、Markdown 和文字型 PDF；图片、Excel、JSON、DOCX 暂不支持分析。');
    if (!valid.length) return;
    setBusy(true);
    let id = detail?.conversation.id;
    try {
      if (!id) {
        const draft = await findEmptyDraft(conversations, null);
        const next = draft ?? { conversation: await api.createConversation(), files: [], messages: [], runs: [] };
        id = next.conversation.id;
        displayedConversation.current = id; setDetail(next);
        setSelectedContexts([]); setContextsExplicit(false); setEvents([]); setLiveAnswer('');
      }
      for (const file of valid) await api.upload(id, file);
    } catch (reason) { setError((reason as Error).message); } finally {
      try { if (id) await refreshDetail(id); await refreshList(); } catch (reason) { setError((reason as Error).message); }
      setBusy(false);
    }
  };
  const submit = async () => {
    if (busy || activeRun || !task.trim() || !detail || !hasCsv) return;
    setError(''); setBusy(true);
    try {
      const key = JSON.stringify([detail.conversation.id, task.trim(), contextsExplicit ? selectedContexts : null]);
      if (pendingRequest.current?.key !== key) pendingRequest.current = { key, id: crypto.randomUUID() };
      await api.start(detail.conversation.id, task.trim(), contextsExplicit ? selectedContexts : undefined, pendingRequest.current.id);
      pendingRequest.current = null;
      setTask(''); setLiveAnswer(''); setEvents([]); await refreshDetail(detail.conversation.id); await refreshList();
    } catch (reason) { setError((reason as Error).message); } finally { setBusy(false); }
  };
  const submitReply = async () => {
    if (!activeRun?.pending_input || !reply.trim() || busy) return;
    setBusy(true); setError('');
    try {
      const key = JSON.stringify([activeRun.id, activeRun.pending_input.id, reply.trim()]);
      if (pendingRequest.current?.key !== key) pendingRequest.current = { key, id: crypto.randomUUID() };
      await api.reply(activeRun.id, activeRun.pending_input.id, reply.trim(), pendingRequest.current.id); pendingRequest.current = null; setReply('');
      await refreshDetail(detail!.conversation.id);
    } catch (reason) { setError((reason as Error).message); } finally { setBusy(false); }
  };
  const cancel = async () => {
    if (!activeRun || busy) return;
    setBusy(true); setError('');
    try { await api.cancel(activeRun.id); await refreshDetail(detail!.conversation.id); }
    catch (reason) { setError((reason as Error).message); } finally { setBusy(false); }
  };

  if (!authenticated) return <main className="access-screen"><span className="brand-dot" /><h1>LabWeaver</h1><p>请通过正式启动器打开应用，以获取本次运行的本地访问令牌。</p><p className="muted">已打开的页面失效时，请重新运行启动器。模型 API Key 无需输入网页。</p></main>;

  return <div className={`app-shell ${panel ? 'panel-open' : ''}`} onDragOver={event => { event.preventDefault(); if (event.dataTransfer.types.includes('Files')) setDragging(true); }} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }} onDrop={event => { event.preventDefault(); setDragging(false); void uploadFiles(Array.from(event.dataTransfer.files)); }}>
    <nav className="sidebar" aria-label="对话导航"><div className="logo"><span className="brand-dot" /><strong>LabWeaver</strong><span className="logo-caption">本地数据分析</span></div>
      <button className="new-chat" disabled={busy} onClick={() => void newConversation()}><Icon name="plus" />新建对话</button>
      <div className="nav-caption">对话历史</div><div className="conversation-list">{conversations.map(conversation => <button className={`conversation-item ${detail?.conversation.id === conversation.id ? 'selected' : ''}`} key={conversation.id} data-conversation-id={conversation.id} disabled={busy} onClick={() => void openConversation(conversation.id)} title={conversation.title}>{conversation.title || '未命名对话'}</button>)}{!conversations.length && <p className="muted nav-empty">还没有对话</p>}</div>
      <footer className="sidebar-footer"><span className="local-indicator" />本机运行 · 历史保存在本地<span className="muted">分析执行使用 Docker；任务与资料会发给配置的模型服务。</span></footer>
    </nav>
    <main className="chat-main"><header className="topbar"><div><strong>{detail?.conversation.title || '开始一次数据分析'}</strong><span className="muted">{health?.mode === 'offline' ? '确定性离线演示' : '通用 CSV 分析助手'}</span></div><button className={`icon-button ${panel ? 'selected' : ''}`} aria-label="查看执行面板" aria-pressed={panel} onClick={() => setPanel(!panel)}><Icon name="panel" /></button></header>
      {error && <div className="notice error-notice" role="alert"><span>{error}</span><button className="icon-button" aria-label="关闭错误提示" onClick={() => setError('')}><Icon name="close" /></button></div>}
      {connection && <div className="notice muted">{connection}</div>}
      {health && (() => { const model = health.model as { ready?: boolean; error?: unknown } | undefined; const docker = health.docker as { ready?: boolean; error?: unknown } | undefined;
        return model?.ready === false || docker?.ready === false ? <div className="notice environment-notice"><span>{model?.ready === false ? '模型配置未就绪，请检查后端配置文件。' : ''}{model?.ready === false && docker?.ready === false ? ' ' : ''}{docker?.ready === false ? 'Docker 或沙箱镜像未就绪，暂时不能执行 Python 分析；可浏览历史或查看 CSV 概览。' : ''}</span></div> : null;
      })()}
      {typeof health?.active_run_id === 'string' && health.active_run_id !== activeRun?.id && <div className="notice"><span>其他对话有活动任务，完成或停止后可开始新分析。</span><button className="link-button" onClick={() => void api.run(health.active_run_id as string).then(async run => { if (run.conversation_id) await openConversation(run.conversation_id); }).catch(reason => setError(reason.message))}>查看任务</button></div>}
      <div className="chat-scroll">
        {!detail?.runs.length && <section className="welcome"><span className="brand-dot welcome-dot" /><h1>从数据到可核对的成果</h1><p>添加一个 CSV，用自然语言说明你想分析什么。可选资料用于解释字段、规则与方法。</p><div className="welcome-steps"><span>1 添加数据</span><span>2 描述任务</span><span>3 查看真实结果</span></div><button className="outline-button" disabled={busy} onClick={() => uploadInput.current?.click()}><Icon name="attach" />选择 CSV 或资料</button><small>支持 CSV、TXT、MD、文字型 PDF · 可拖入文件</small></section>}
        <div className="timeline">{detail?.runs.map(run => <RunCard key={run.id} run={run} messages={detail.messages.filter(message => message.run_id === run.id)} events={run.id === activeRun?.id ? events : run.events ?? []} liveAnswer={run.id === activeRun?.id ? liveAnswer : undefined} onError={showError} onRerun={value => { setTask(value); taskInput.current?.focus(); }} />)}</div><div ref={bottom} />
      </div>
      <div className="composer-area">
        {detail?.files.length ? <div className="attachments" aria-label="当前附件">{detail.files.map(file => <div className="attachment" key={file.id}><Icon name="file" /><div><strong title={file.name}>{file.name}</strong><small>{file.parse_error ? '解析需确认' : file.role === 'csv' || extension(file.name) === 'csv' ? file.preview?.row_count == null ? 'CSV 数据' : `${file.preview.row_count} 行 · ${file.preview.columns?.length ?? '?'} 列` : '任务资料'}</small></div>{!frozen && <button className="icon-button" aria-label={`移除 ${file.name}`} disabled={busy} onClick={() => { setBusy(true); void api.removeFile(detail.conversation.id, file.id).then(() => refreshDetail(detail.conversation.id)).catch(reason => setError(reason.message)).finally(() => setBusy(false)); }}><Icon name="close" /></button>}<details className="attachment-preview"><summary>预览</summary>{file.preview?.rows && file.preview.columns ? <DataTable preview={{ rows: file.preview.rows, columns: file.preview.columns, row_count: file.preview.row_count ?? file.preview.rows.length }} /> : <pre>{file.parse_error ? text(file.parse_error) : file.preview?.text ?? '资料将在实际检索时解析；上传不等于解析成功。'}</pre>}</details></div>)}</div> : null}
        {activeRun?.status === 'awaiting_input' && activeRun.pending_input ? <section className="clarification" aria-label="Agent 澄清问题"><strong>需要你的回复</strong><p>{activeRun.pending_input.question}</p><textarea aria-label="澄清回复" value={reply} onChange={event => setReply(event.target.value)} placeholder="说明你的选择或统计口径…" onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); void submitReply(); } }} /><div className="composer-actions"><button className="text-button" disabled={busy} onClick={() => void cancel()}>停止任务</button><button className="primary-button" disabled={!reply.trim() || busy} onClick={() => void submitReply()}>回复并继续</button></div></section> : <>
          {detail && detail.runs.some(run => run.status === 'completed') && !activeRun && <details className="context-selection"><summary>历史任务作为背景（已选 {selectedContexts.length}）</summary>{detail.runs.filter(run => run.status === 'completed').map(run => <label key={run.id}><input type="checkbox" checked={selectedContexts.includes(run.id)} disabled={selectedContexts.length >= 2 && !selectedContexts.includes(run.id)} onChange={event => { setContextsExplicit(true); setSelectedContexts(previous => event.target.checked ? [...previous, run.id] : previous.filter(id => id !== run.id)); }} />{run.task || run.id}</label>)}<small>最多选择两次历史任务作为背景，新任务会从原始 CSV 重新计算。</small></details>}
          <div className="composer"><textarea ref={taskInput} aria-label="任务说明" placeholder={activeRun ? '当前任务正在运行…' : hasCsv ? '描述你想完成的分析，或继续追问…' : '先添加一个 CSV，再输入分析任务…'} value={task} disabled={Boolean(activeRun)} onChange={event => setTask(event.target.value)} onPaste={event => { const files = Array.from(event.clipboardData.files); if (files.length) { event.preventDefault(); void uploadFiles(files); } }} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); void submit(); } }} />
            <div className="composer-actions"><button className="icon-button" aria-label="添加文件" title={frozen ? '更换输入请新建对话' : '添加 CSV 或资料'} disabled={busy || frozen} onClick={() => uploadInput.current?.click()}><Icon name="attach" /></button><span className="composer-hint">{frozen ? '输入已冻结 · 换文件请新建对话' : '拖入或粘贴文件 · Shift+Enter 换行'}</span>{activeRun ? <button className="send-button stop-button" aria-label="停止任务" disabled={busy || activeRun.status === 'cancelling'} onClick={() => void cancel()}><Icon name="stop" /></button> : <button className="send-button" aria-label="发送任务" disabled={busy || !task.trim() || !hasCsv} onClick={() => void submit()}><Icon name="arrow" /></button>}</div>
          </div>
        </>}
        <input ref={uploadInput} className="visually-hidden" type="file" accept=".csv,.txt,.md,.pdf" multiple disabled={busy || frozen} aria-label="选择附件" onChange={event => { void uploadFiles(Array.from(event.target.files ?? [])); event.target.value = ''; }} />
        <p className="composer-footnote">生成内容可能有误，请核对计算口径与实际成果。仅在全部交付核验后显示完成。</p>
      </div>
    </main>
    {panel && <ExecutionPanel events={events.length ? events : latestRun?.events ?? []} run={latestRun} onClose={() => setPanel(false)} onError={showError} />}
    {dragging && <div className="drop-overlay"><Icon name="attach" /><strong>将文件放入当前对话</strong><span>一个 CSV，及可选 TXT / MD / PDF</span></div>}
  </div>;
}
