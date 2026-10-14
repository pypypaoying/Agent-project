import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { api, ApiError, streamEvents } from './api';
import type { ConversationDetail } from './types';

vi.mock('./api', () => ({
  ApiError: class extends Error { constructor(public status: number, message: string) { super(message); } },
  api: { health: vi.fn(), shutdown: vi.fn(), providers: vi.fn(), saveProvider: vi.fn(), defaultProvider: vi.fn(), refreshProviderModels: vi.fn(), testProvider: vi.fn(), conversations: vi.fn(), createConversation: vi.fn(), updateConversation: vi.fn(), deleteConversation: vi.fn(), conversation: vi.fn(), upload: vi.fn(), removeFile: vi.fn(), start: vi.fn(), run: vi.fn(), reply: vi.fn(), cancel: vi.fn(), preview: vi.fn(), blob: vi.fn() },
  streamEvents: vi.fn(), downloadFile: vi.fn(),
}));

const empty = (): ConversationDetail => ({ conversation: { id: 'c', title: '问卷分析', frozen: false }, files: [], runs: [], messages: [] });
const withCsv = (): ConversationDetail => ({ ...empty(), files: [{ id: 'f', name: '调查.csv', role: 'csv', preview: { columns: ['编号', '评分'], rows: [['001', '4']], row_count: 1 } }] });
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.health).mockResolvedValue({ mode: 'offline' });
  vi.mocked(api.providers).mockResolvedValue({ providers: [{ id: 'deepseek', name: 'DeepSeek', kind: 'preset', base_url: 'https://api.deepseek.com', models: ['deepseek-flash', 'deepseek-v4-pro'], default_model_id: 'deepseek-flash', has_key: true, revision: 1 }], default_provider_id: 'deepseek', default_model_id: 'deepseek-flash' });
  vi.mocked(api.conversations).mockResolvedValue({ conversations: [{ id: 'c', title: '问卷分析' }] });
  vi.mocked(api.createConversation).mockResolvedValue({ id: 'c', title: '新对话' });
  vi.mocked(api.conversation).mockResolvedValue(empty());
  vi.mocked(streamEvents).mockImplementation((_id, _after, signal) => new Promise(resolve => { signal.addEventListener('abort', () => resolve(), { once: true }); }));
});

async function open(detail = withCsv()) {
  vi.mocked(api.conversation).mockResolvedValue(detail);
  render(<App authenticated />);
  await userEvent.click(await screen.findByRole('button', { name: '问卷分析' }));
  await screen.findByText('调查.csv');
}

describe('real-data chat interface', () => {
  it('requires launcher authorization and never asks for a model key', () => {
    render(<App authenticated={false} />);
    expect(screen.getByText(/请通过正式启动器/)).toBeInTheDocument();
    expect(api.health).not.toHaveBeenCalled();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('opens saved conversation and renders source-preserving CSV preview', async () => {
    await open();
    await userEvent.click(screen.getByText('预览'));
    expect(screen.getByText('001')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '移除 调查.csv' })).toBeEnabled();
  });

  it('submits natural-language task but Shift+Enter and IME confirmation do not send', async () => {
    await open();
    const input = screen.getByRole('textbox', { name: '任务说明' });
    fireEvent.change(input, { target: { value: '概览数据' } });
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
    fireEvent.compositionStart(input); fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 }); fireEvent.compositionEnd(input);
    expect(api.start).not.toHaveBeenCalled();
    vi.mocked(api.start).mockResolvedValue({ id: 'r', status: 'running' });
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: false });
    await waitFor(() => expect(api.start).toHaveBeenCalledWith('c', '概览数据', undefined, expect.any(String)));
  });

  it('does not allow changed attachments after first task', async () => {
    await open({ ...withCsv(), conversation: { id: 'c', title: '问卷分析', frozen: true }, runs: [{ id: 'r', task: '概览', status: 'completed', final_answer: '已概览' }] });
    expect(screen.getByRole('button', { name: '添加文件' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '移除 调查.csv' })).not.toBeInTheDocument();
    const file = new File(['a\n1'], '新数据.csv', { type: 'text/csv' });
    fireEvent.paste(screen.getByRole('textbox', { name: '任务说明' }), { clipboardData: { files: [file] } });
    expect(await screen.findByRole('alert')).toHaveTextContent('输入已冻结');
    expect(api.upload).not.toHaveBeenCalled();
  });

  it('uses the actual question ID when resuming clarification', async () => {
    const run = { id: 'r', task: '概览', status: 'awaiting_input' as const, pending_input: { id: 'q-real', kind: 'csv_format', question: '请选择分隔符' } };
    await open({ ...withCsv(), runs: [run] });
    vi.mocked(api.reply).mockResolvedValue({ ...run, status: 'running' });
    await userEvent.type(screen.getByRole('textbox', { name: '澄清回复' }), '1');
    await userEvent.click(screen.getByRole('button', { name: '回复并继续' }));
    await waitFor(() => expect(api.reply).toHaveBeenCalledWith('r', 'q-real', '1', expect.any(String)));
  });

  it('shows a draft as unvalidated and stops the actual running task', async () => {
    const run = { id: 'r', task: '概览', status: 'running' as const };
    vi.mocked(streamEvents).mockImplementation(async (_id, _after, signal, receive) => {
      receive({ version: 1, conversation_id: 'c', run_id: 'r', seq: 1, type: 'answer_delta', payload: { text: '待核验正文' } });
      return new Promise(resolve => { signal.addEventListener('abort', () => resolve(), { once: true }); });
    });
    await open({ ...withCsv(), runs: [run] });
    expect(await screen.findByText('待核验正文')).toBeInTheDocument();
    expect(screen.getByText(/正文待核验/)).toBeInTheDocument();
    vi.mocked(api.cancel).mockResolvedValue({ ...run, status: 'cancelling' });
    await userEvent.click(screen.getByRole('button', { name: '停止任务' }));
    await waitFor(() => expect(api.cancel).toHaveBeenCalledWith('r'));
  });

  it('restarts an interrupted task instead of trying to resume an expired graph', async () => {
    await open({ ...withCsv(), runs: [{ id: 'old', task: '原任务', status: 'interrupted' }] });
    await userEvent.click(screen.getByRole('button', { name: '按原任务重新执行' }));
    expect(screen.getByRole('textbox', { name: '任务说明' })).toHaveValue('原任务');
    expect(api.reply).not.toHaveBeenCalled();
  });

  it('does not execute HTML or load model-provided images', async () => {
    await open({ ...withCsv(), runs: [{ id: 'r', status: 'completed', final_answer: '<script>alert(1)</script>\n![remote](https://external.invalid/tracker.png)' }] });
    expect(document.querySelector('script')).toBeNull();
    expect(document.querySelector('.prose img')).toBeNull();
    expect(document.querySelector('img[src^="https://external.invalid"]')).toBeNull();
  });

  it('renders registered report Markdown as readable sections while preserving ordinary zero metrics', async () => {
    await open({ ...withCsv(), runs: [{ id: 'r', status: 'completed', artifacts: [{ id: 'actual-report', kind: 'metric', label: '分析报告', files: [], value: { report_markdown: '# 模型评估\n\n## 方法\n\n按时间划分数据，并核对预测误差。\n\n## 结论\n\n**下一步**：补充更多独立样本。', count: 0 } }] }] });
    const report = screen.getByRole('region', { name: '分析报告' });
    expect(within(report).getByRole('heading', { name: '模型评估', level: 1 })).toBeVisible();
    expect(within(report).getByRole('heading', { name: '方法', level: 2 })).toBeVisible();
    expect(within(report).getByRole('heading', { name: '结论', level: 2 })).toBeVisible();
    expect(within(report).getByText('按时间划分数据，并核对预测误差。')).toBeVisible();
    expect(within(report).getByText('count')).toBeVisible();
    expect(within(report).getByText('0', { exact: true })).toBeVisible();
    expect(within(report).queryByText('report_markdown')).not.toBeInTheDocument();
  });

  it('does not execute narrative HTML or load images from an untrusted report', async () => {
    const narrative = '# 验收报告\n\n<script>window.reportInjected = true</script>\n\n<img src="https://external.invalid/html-tracker.png" onerror="window.reportInjected = true">\n\n![外链图片](https://external.invalid/markdown-tracker.png)\n\n[危险链接](javascript:alert(1))\n\n普通报告内容。';
    await open({ ...withCsv(), runs: [{ id: 'r', status: 'completed', artifacts: [{ id: 'actual-report', kind: 'metric', label: '安全报告', files: [], value: { report_markdown: narrative } }] }] });
    const report = screen.getByRole('region', { name: '安全报告' });
    expect(within(report).getByRole('heading', { name: '验收报告' })).toBeVisible();
    expect(within(report).getByText('普通报告内容。')).toBeVisible();
    expect(report.querySelector('script,img,iframe,[onerror],a[href^="javascript:"]')).toBeNull();
    expect(document.querySelector('img[src^="https://external.invalid"]')).toBeNull();
    expect(api.blob).not.toHaveBeenCalled();
  });

  it('renders verified answer tables and host metric presentation without default JSON blocks', async () => {
    await open({ ...withCsv(), runs: [{ id: 'r', status: 'completed', final_answer: '# 数据分析报告\n\n## 核心结果\n\n**样本完整**。\n\n| 指标 | 值 |\n| --- | --- |\n| 样本数 | 25 |', artifacts: [{ id: 'metric', kind: 'metric', label: '数据概况', files: [], value: { raw_count: 25 }, presentation: { blocks: [{ type: 'table', columns: ['指标', '值'], rows: [['原始记录数', '25']] }] } }] }] });
    const report = screen.getByRole('region', { name: '分析报告正文' });
    expect(within(report).getByRole('heading', { name: '数据分析报告', level: 1 })).toBeVisible();
    expect(within(report).getByRole('table')).toHaveTextContent('样本数25');
    const metric = screen.getByRole('region', { name: '数据概况' });
    expect(within(metric).getByRole('table')).toHaveTextContent('原始记录数25');
    expect(metric.querySelector('pre')).toBeNull();
    expect(within(metric).getByText('查看原始 JSON').closest('details')).not.toHaveAttribute('open');
  });

  it('shows a recorded terminal duration without inventing elapsed time for old histories', async () => {
    await open({ ...withCsv(), runs: [{ id: 'old', status: 'completed', final_answer: '旧结果' }, { id: 'new', status: 'completed', final_answer: '新结果', timing: { source: 'host_monotonic', active: false, active_seconds: 62.7, wall_seconds: 120, model_seconds: 38, model_calls: 4, current_models: [] } }] });
    expect(screen.getByLabelText('任务执行用时')).toHaveTextContent('执行用时 1分 02秒');
    expect(screen.getAllByLabelText('任务执行用时')).toHaveLength(1);
  });

  it('shows active model waiting duration but hides timing heartbeats from the execution panel', async () => {
    const timing = { source: 'host_monotonic', active: true, active_seconds: 12, wall_seconds: 12, model_seconds: 8, model_calls: 2, current_models: [{ role: 'analysis', call: 2, parent_call_id: 'd', elapsed_seconds: 5 }] };
    const events = [{ version: 1, conversation_id: 'c', run_id: 'r', seq: 1, type: 'timing', payload: { timing } }];
    await open({ ...withCsv(), runs: [{ id: 'r', status: 'running', timing, events }] });
    expect(screen.getByText(/代码 Agent · 第 2 次模型请求 · 已等待/)).toHaveTextContent('5秒');
    await userEvent.click(screen.getByRole('button', { name: '查看执行面板' }));
    const panel = screen.getByRole('complementary', { name: '执行面板' });
    expect(within(panel).getByText('模型请求累计')).toBeVisible();
    expect(panel.querySelector('.event-card')).toBeNull();
  });

  it('reads paged table preview from registered artifact ID', async () => {
    vi.mocked(api.preview).mockResolvedValue({ columns: ['编号'], rows: Array.from({ length: 20 }, (_, index) => [`${index}`]), row_count: 280 });
    await open({ ...withCsv(), runs: [{ id: 'r', status: 'completed', artifacts: [{ id: 'actual-artifact', kind: 'table', label: '完整分组', files: [] }] }] });
    expect(await screen.findByText(/共 280 行/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '下一页' }));
    await waitFor(() => expect(api.preview).toHaveBeenCalledWith('actual-artifact', 20));
  });

  it('keeps a submission request ID when a response is lost and the user retries', async () => {
    await open();
    vi.mocked(api.start).mockRejectedValueOnce(new Error('连接已断开')).mockResolvedValueOnce({ id: 'r', status: 'running' });
    await userEvent.type(screen.getByRole('textbox', { name: '任务说明' }), '概览');
    await userEvent.click(screen.getByRole('button', { name: '发送任务' }));
    await screen.findByRole('alert');
    await waitFor(() => expect(screen.getByRole('button', { name: '发送任务' })).toBeEnabled());
    await userEvent.click(screen.getByRole('button', { name: '发送任务' }));
    await waitFor(() => expect(api.start).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.start).mock.calls[0][3]).toBe(vi.mocked(api.start).mock.calls[1][3]);
  });

  it('uploads only actual browser clipboard files, not pasted path text', async () => {
    await open();
    const input = screen.getByRole('textbox', { name: '任务说明' });
    fireEvent.paste(input, { clipboardData: { files: [], getData: () => 'C:\\private\\sample.pdf' } });
    expect(api.upload).not.toHaveBeenCalled();
    const file = new File(['说明'], '变量说明.md', { type: 'text/markdown' });
    vi.mocked(api.upload).mockResolvedValue({ id: 'material', name: file.name, role: 'material' });
    fireEvent.paste(input, { clipboardData: { files: [file] } });
    await waitFor(() => expect(api.upload).toHaveBeenCalledWith('c', file));
  });

  it('reconnects from the last sequence and does not submit another task', async () => {
    const pending = { id: 'r', task: '概览', status: 'running' as const };
    const final = { ...pending, status: 'completed' as const, final_answer: '最终核验正文' };
    vi.mocked(api.conversation).mockResolvedValueOnce({ ...withCsv(), runs: [pending] }).mockResolvedValue({ ...withCsv(), runs: [final] });
    vi.mocked(api.run).mockResolvedValueOnce(pending).mockResolvedValue(final);
    vi.mocked(streamEvents).mockImplementationOnce(async (_id, _after, _signal, receive) => {
      receive({ version: 1, conversation_id: 'c', run_id: 'r', seq: 1, type: 'answer_delta', payload: { id: 'answer', text: '正文' } });
    }).mockImplementationOnce(async (_id, _after, _signal, receive) => {
      receive({ version: 1, conversation_id: 'c', run_id: 'r', seq: 1, type: 'answer_delta', payload: { id: 'answer', text: '正文' } });
      receive({ version: 1, conversation_id: 'c', run_id: 'r', seq: 2, type: 'completed', payload: {} });
    });
    render(<App authenticated />);
    await userEvent.click(await screen.findByRole('button', { name: '问卷分析' }));
    expect(await screen.findByText('最终核验正文', {}, { timeout: 3000 })).toBeInTheDocument();
    expect(vi.mocked(streamEvents).mock.calls[1][1]).toBe(1);
    expect(api.start).not.toHaveBeenCalled();
  });

  it('reuses the current empty draft on repeated New Conversation clicks', async () => {
    render(<App authenticated />);
    await userEvent.click(await screen.findByRole('button', { name: '问卷分析' }));
    const input = screen.getByRole('textbox', { name: '任务说明' });
    await userEvent.type(input, '尚未发送的草稿');
    await userEvent.click(screen.getByRole('button', { name: '新建对话' }));
    await userEvent.click(screen.getByRole('button', { name: '新建对话' }));
    expect(api.createConversation).not.toHaveBeenCalled();
    expect(input).toHaveValue('尚未发送的草稿');
    expect(screen.getAllByRole('button', { name: '问卷分析' })).toHaveLength(1);
  });

  it('creates exactly one draft from an empty history and reuses that single draft', async () => {
    vi.mocked(api.conversations).mockResolvedValueOnce({ conversations: [] }).mockResolvedValue({ conversations: [{ id: 'new', title: '新对话' }] });
    vi.mocked(api.createConversation).mockResolvedValue({ id: 'new', title: '新对话', frozen: false });
    render(<App authenticated />);
    await screen.findByText('还没有对话');
    await userEvent.click(screen.getByRole('button', { name: '新建对话' }));
    await waitFor(() => expect(document.querySelector('.conversation-item.selected')).toHaveAttribute('data-conversation-id', 'new'));
    await userEvent.click(screen.getByRole('button', { name: '新建对话' }));
    await userEvent.click(screen.getByRole('button', { name: '新建对话' }));
    expect(api.createConversation).toHaveBeenCalledTimes(1);
    expect(document.querySelectorAll('.conversation-item')).toHaveLength(1);
  });

  it('reuses another verified empty draft but never discards an attached CSV', async () => {
    vi.mocked(api.conversations).mockResolvedValue({ conversations: [{ id: 'c', title: '问卷分析' }, { id: 'empty', title: '空草稿' }] });
    vi.mocked(api.conversation).mockImplementation(async id => id === 'c' ? withCsv() : { ...empty(), conversation: { id: 'empty', title: '空草稿' } });
    render(<App authenticated />);
    await userEvent.click(await screen.findByRole('button', { name: '问卷分析' }));
    await screen.findByText('调查.csv');
    await userEvent.click(screen.getByRole('button', { name: '新建对话' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: '任务说明' })).toHaveAttribute('placeholder', '先添加一个 CSV，再输入分析任务…'));
    expect(api.createConversation).not.toHaveBeenCalled();
    expect(api.removeFile).not.toHaveBeenCalled();
    expect(document.querySelector('.conversation-item.selected')).toHaveAttribute('data-conversation-id', 'empty');
  });

  it('creates a new conversation when all existing drafts have attachments or messages', async () => {
    await open();
    await userEvent.click(screen.getByRole('button', { name: '新建对话' }));
    await waitFor(() => expect(api.createConversation).toHaveBeenCalledTimes(1));
  });

  it('shows actual tool and Python execution status in chat with the detail panel closed', async () => {
    const events = [
      { version: 1, conversation_id: 'c', run_id: 'r', seq: 1, type: 'tool_start', payload: { name: 'profile_csv', id: 'p' } },
      { version: 1, conversation_id: 'c', run_id: 'r', seq: 2, type: 'tool_end', payload: { name: 'profile_csv', id: 'p', status: 'completed' } },
      { version: 1, conversation_id: 'c', run_id: 'r', seq: 3, type: 'execution_start', payload: { role: 'analysis', tool_call_id: 'python', attempt: 1, code: 'never print entire code here' } },
    ];
    await open({ ...withCsv(), runs: [{ id: 'r', task: '分析', status: 'running', events }] });
    const progress = await screen.findByRole('region', { name: '真实执行过程' });
    expect(within(progress).getByText('读取 CSV 数据')).toBeVisible();
    expect(within(progress).getByText('完成')).toBeVisible();
    expect(within(progress).getByText('代码 Agent · 第 1 次 Python 执行')).toBeVisible();
    expect(within(progress).getByText('执行中')).toBeVisible();
    expect(screen.queryByRole('complementary', { name: '执行面板' })).not.toBeInTheDocument();
    expect(progress).not.toHaveTextContent('never print entire code here');
  });

  it('folds completed execution details so the report stays prominent while keeping real evidence accessible', async () => {
    const events = [{ version: 1, conversation_id: 'c', run_id: 'r', seq: 1, type: 'tool_end', payload: { name: 'profile_csv', id: 'p', status: 'completed' } }];
    await open({ ...withCsv(), runs: [{ id: 'r', status: 'completed', final_answer: '## 主要发现\n\n数据概览已完成。', events }] });
    const progress = screen.getByRole('region', { name: '真实执行过程' });
    expect(progress.querySelector('.completed-progress')).not.toHaveAttribute('open');
    expect(screen.getByRole('heading', { name: '主要发现' })).toBeVisible();
    await userEvent.click(within(progress).getByText('执行过程与核验记录'));
    expect(within(progress).getByText('读取 CSV 数据')).toBeVisible();
  });

  it('sends the selected live model on a new task without changing the frozen CSV', async () => {
    vi.mocked(api.health).mockResolvedValue({ mode: 'live' });
    await open({ ...withCsv(), conversation: { id: 'c', title: '问卷分析', frozen: true }, runs: [{ id: 'old', task: '上次任务', status: 'completed' }] });
    await userEvent.click(screen.getByRole('button', { name: '选择模型' }));
    await userEvent.click(screen.getByRole('menuitemradio', { name: 'deepseek-v4-pro' }));
    await userEvent.type(screen.getByRole('textbox', { name: '任务说明' }), '继续概览');
    vi.mocked(api.start).mockResolvedValue({ id: 'new', status: 'running' });
    await userEvent.click(screen.getByRole('button', { name: '发送任务' }));
    await waitFor(() => expect(api.start).toHaveBeenCalledWith('c', '继续概览', undefined, expect.any(String), { provider_id: 'deepseek', model_id: 'deepseek-v4-pro' }));
    expect(api.createConversation).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '添加文件' })).toBeDisabled();
  });

  it('continues a reopened live conversation with the saved model and retains the retry ID', async () => {
    vi.mocked(api.health).mockResolvedValue({ mode: 'live' });
    const savedSelection = { provider_id: 'deepseek', model_id: 'deepseek-v4-pro', provider_name: 'DeepSeek', base_url: 'https://api.deepseek.com' };
    const detail = { ...withCsv(), conversation: { id: 'c', title: '问卷分析', frozen: true }, model_selection: savedSelection, runs: [{ id: 'first', task: '首次概览', status: 'completed' as const, model_selection: savedSelection }] };
    await open(detail);
    expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('deepseek-v4-pro');
    vi.mocked(api.start).mockRejectedValueOnce(new Error('连接已断开')).mockResolvedValueOnce({ id: 'second', status: 'running' });
    await userEvent.type(screen.getByRole('textbox', { name: '任务说明' }), '继续分析同一文件');
    await userEvent.click(screen.getByRole('button', { name: '发送任务' }));
    await screen.findByRole('alert');
    await waitFor(() => expect(screen.getByRole('button', { name: '发送任务' })).toBeEnabled());
    await userEvent.click(screen.getByRole('button', { name: '发送任务' }));
    await waitFor(() => expect(api.start).toHaveBeenCalledTimes(2));
    const calls = vi.mocked(api.start).mock.calls;
    expect(calls[0]).toEqual(['c', '继续分析同一文件', undefined, expect.any(String), { provider_id: 'deepseek', model_id: 'deepseek-v4-pro' }]);
    expect(calls[1]).toEqual(calls[0]);
    expect(api.createConversation).not.toHaveBeenCalled();
    expect(api.upload).not.toHaveBeenCalled();
    expect(savedSelection.provider_name).toBe('DeepSeek');
  });

  it('keeps API keys only in the settings form and clears them after returning to chat', async () => {
    await open();
    await userEvent.click(screen.getByRole('button', { name: '设置' }));
    expect(screen.getByRole('textbox', { name: 'Base URL' })).toHaveValue('https://api.deepseek.com');
    await userEvent.type(screen.getByLabelText('API Key', { exact: true }), 'temporary-private-key');
    await userEvent.click(screen.getByRole('button', { name: '返回聊天' }));
    await userEvent.click(screen.getByRole('button', { name: '设置' }));
    expect(screen.getByLabelText('API Key', { exact: true })).toHaveValue('');
    expect(JSON.stringify(sessionStorage)).not.toContain('temporary-private-key');
    expect(JSON.stringify(localStorage)).not.toContain('temporary-private-key');
  });

  it('shows expired authorization clearly when loading providers, including on the settings page', async () => {
    vi.mocked(api.providers).mockRejectedValue(new ApiError(401, 'old token'));
    render(<App authenticated />);
    expect(await screen.findByRole('alert')).toHaveTextContent('访问令牌已失效，请用启动器重新打开应用');
    await userEvent.click(screen.getByRole('button', { name: '设置' }));
    expect(screen.getByRole('alert')).toHaveTextContent('访问令牌已失效');
    expect(screen.queryByText(/正在加载供应商配置/)).not.toBeInTheDocument();
  });

  it('keeps nonauthorization provider loading errors distinct', async () => {
    vi.mocked(api.providers).mockRejectedValue(new ApiError(500, 'failure'));
    render(<App authenticated />);
    expect(await screen.findByRole('alert')).toHaveTextContent('模型供应商列表未能加载');
    expect(screen.getByRole('alert')).not.toHaveTextContent('令牌已失效');
  });

  it('only exits after confirmation, hides chat after a genuine successful shutdown response', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    vi.mocked(api.shutdown).mockResolvedValue();
    await open();
    await userEvent.click(screen.getByRole('button', { name: '退出应用' }));
    expect(confirm).toHaveBeenCalledTimes(1); expect(api.shutdown).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: '退出应用' }));
    expect(await screen.findByRole('heading', { name: '应用已退出' })).toBeVisible();
    expect(api.shutdown).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/重新双击 AnaliyMaster 启动器/)).toBeVisible();
    expect(screen.queryByRole('textbox', { name: '任务说明' })).not.toBeInTheDocument();
  });

  it.each(['running', 'awaiting_input', 'cancelling'] as const)('disables application exit during an actual %s task', async status => {
    await open({ ...withCsv(), runs: [{ id: 'r', task: '概览', status }] });
    expect(screen.getByRole('button', { name: '退出应用' })).toBeDisabled();
    expect(screen.getByText('先停止或完成任务再退出')).toBeVisible();
    expect(api.shutdown).not.toHaveBeenCalled();
  });

  it('disables exit when another conversation has the global active task', async () => {
    vi.mocked(api.health).mockResolvedValue({ mode: 'offline', active_run_id: 'other-run' });
    await open();
    expect(screen.getByRole('button', { name: '退出应用' })).toBeDisabled();
  });

  it('keeps chat usable when a new activity races the shutdown request', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.shutdown).mockRejectedValue(new ApiError(409, 'active run'));
    await open();
    await userEvent.click(screen.getByRole('button', { name: '退出应用' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('还有活动任务');
    expect(screen.queryByRole('heading', { name: '应用已退出' })).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '任务说明' })).toBeVisible();
  });

  it('renames a saved conversation through the API and updates the current header', async () => {
    await open();
    vi.mocked(api.updateConversation).mockResolvedValue({ ...withCsv().conversation, title: '部门满意度' });
    vi.mocked(api.conversations).mockResolvedValue({ conversations: [{ id: 'c', title: '部门满意度' }] });
    await userEvent.click(screen.getByRole('button', { name: '对话操作：问卷分析' }));
    await userEvent.click(screen.getByRole('menuitem', { name: '重命名' }));
    const dialog = screen.getByRole('dialog', { name: '重命名对话' });
    fireEvent.change(within(dialog).getByRole('textbox', { name: '对话名称' }), { target: { value: '  部门满意度  ' } });
    await userEvent.click(within(dialog).getByRole('button', { name: '保存名称' }));
    await waitFor(() => expect(api.updateConversation).toHaveBeenCalledWith('c', { title: '部门满意度' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: '部门满意度' })).toBeVisible();
    expect(document.querySelector('.topbar strong')).toHaveTextContent('部门满意度');
    expect(screen.getByText('调查.csv')).toBeVisible();
  });

  it('persists pinning and unpinning without changing or resubmitting the active chat', async () => {
    await open();
    vi.mocked(api.updateConversation).mockResolvedValueOnce({ ...empty().conversation, pinned: true }).mockResolvedValueOnce({ ...empty().conversation, pinned: false });
    vi.mocked(api.conversations).mockResolvedValueOnce({ conversations: [{ id: 'c', title: '问卷分析', pinned: true }] }).mockResolvedValue({ conversations: [{ id: 'c', title: '问卷分析', pinned: false }] });
    await userEvent.click(screen.getByRole('button', { name: '对话操作：问卷分析' }));
    await userEvent.click(screen.getByRole('menuitem', { name: '置顶' }));
    await waitFor(() => expect(api.updateConversation).toHaveBeenCalledWith('c', { pinned: true }));
    await waitFor(() => expect(screen.getByRole('button', { name: '对话操作：问卷分析' })).toBeEnabled());
    await userEvent.click(screen.getByRole('button', { name: '对话操作：问卷分析' }));
    await userEvent.click(screen.getByRole('menuitem', { name: '取消置顶' }));
    await waitFor(() => expect(api.updateConversation).toHaveBeenLastCalledWith('c', { pinned: false }));
    expect(api.start).not.toHaveBeenCalled(); expect(api.createConversation).not.toHaveBeenCalled();
  });

  it('requires an explicit irreversible deletion confirmation and retains the conversation when cancelled', async () => {
    await open();
    await userEvent.click(screen.getByRole('button', { name: '对话操作：问卷分析' }));
    await userEvent.click(screen.getByRole('menuitem', { name: '永久删除' }));
    const dialog = screen.getByRole('dialog', { name: '永久删除对话' });
    expect(dialog).toHaveTextContent('消息、应用内附件副本和全部分析成果'); expect(dialog).toHaveTextContent('原本的本地文件会保留');
    await userEvent.click(within(dialog).getByRole('button', { name: '取消' }));
    expect(api.deleteConversation).not.toHaveBeenCalled(); expect(screen.getByText('调查.csv')).toBeVisible();
  });

  it('deletes the selected conversation and reuses an existing empty draft', async () => {
    const records = [{ id: 'c', title: '问卷分析' }, { id: 'draft', title: '空草稿' }];
    vi.mocked(api.conversations).mockResolvedValue({ conversations: records });
    vi.mocked(api.conversation).mockImplementation(async id => id === 'c' ? withCsv() : { ...empty(), conversation: records[1] });
    vi.mocked(api.deleteConversation).mockImplementation(async id => { vi.mocked(api.conversations).mockResolvedValue({ conversations: [records[1]] }); return { deleted: true, conversation_id: id }; });
    render(<App authenticated />);
    await userEvent.click(await screen.findByRole('button', { name: '问卷分析' }));
    await screen.findByText('调查.csv');
    await userEvent.click(screen.getByRole('button', { name: '对话操作：问卷分析' }));
    await userEvent.click(screen.getByRole('menuitem', { name: '永久删除' }));
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '永久删除' }));
    await waitFor(() => expect(api.deleteConversation).toHaveBeenCalledWith('c'));
    await waitFor(() => expect(document.querySelector('.conversation-item.selected')).toHaveAttribute('data-conversation-id', 'draft'));
    expect(screen.queryByText('调查.csv')).not.toBeInTheDocument();
    expect(api.createConversation).not.toHaveBeenCalled();
  });

  it('protects active conversations in the menu while allowing rename and pin operations', async () => {
    await open({ ...withCsv(), runs: [{ id: 'r', status: 'awaiting_input' }] });
    await userEvent.click(screen.getByRole('button', { name: '对话操作：问卷分析' }));
    expect(screen.getByRole('menuitem', { name: '永久删除' })).toBeDisabled();
    expect(screen.getByRole('menuitem', { name: '重命名' })).toBeEnabled();
    expect(screen.getByRole('menuitem', { name: '置顶' })).toBeEnabled();
    expect(api.deleteConversation).not.toHaveBeenCalled();
  });

  it('preserves the conversation when server activity races a deletion', async () => {
    await open(); vi.mocked(api.deleteConversation).mockRejectedValue(new ApiError(409, 'active task'));
    await userEvent.click(screen.getByRole('button', { name: '对话操作：问卷分析' }));
    await userEvent.click(screen.getByRole('menuitem', { name: '永久删除' }));
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '永久删除' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('有活动任务');
    expect(screen.getByText('调查.csv')).toBeVisible();
    expect(screen.getByRole('dialog')).toBeVisible();
  });

  it('returns to a new-chat view after deleting the last conversation without creating surplus drafts', async () => {
    await open();
    vi.mocked(api.deleteConversation).mockImplementation(async id => { vi.mocked(api.conversations).mockResolvedValue({ conversations: [] }); return { deleted: true, conversation_id: id }; });
    await userEvent.click(screen.getByRole('button', { name: '对话操作：问卷分析' }));
    await userEvent.click(screen.getByRole('menuitem', { name: '永久删除' }));
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '永久删除' }));
    expect(await screen.findByText('还没有对话')).toBeVisible();
    expect(screen.queryByText('调查.csv')).not.toBeInTheDocument();
    expect(api.createConversation).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '发送任务' })).toBeDisabled();
  });

  it('never opens pending deletion entries while reusing a safe empty draft', async () => {
    const records = [{ id: 'c', title: '问卷分析' }, { id: 'pending', title: '未完成清理', deletion_pending: true }, { id: 'draft', title: '空草稿' }];
    vi.mocked(api.conversations).mockResolvedValue({ conversations: records });
    vi.mocked(api.conversation).mockImplementation(async id => id === 'c' ? withCsv() : { ...empty(), conversation: records.find(item => item.id === id)! });
    render(<App authenticated />);
    await userEvent.click(await screen.findByRole('button', { name: '问卷分析' }));
    await screen.findByText('调查.csv');
    await userEvent.click(screen.getByRole('button', { name: '新建对话' }));
    await waitFor(() => expect(document.querySelector('.conversation-item.selected')).toHaveAttribute('data-conversation-id', 'draft'));
    expect(api.conversation).not.toHaveBeenCalledWith('pending');
  });
});
