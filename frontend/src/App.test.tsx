import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { api, streamEvents } from './api';
import type { ConversationDetail } from './types';

vi.mock('./api', () => ({
  ApiError: class extends Error {},
  api: { health: vi.fn(), conversations: vi.fn(), createConversation: vi.fn(), conversation: vi.fn(), upload: vi.fn(), removeFile: vi.fn(), start: vi.fn(), run: vi.fn(), reply: vi.fn(), cancel: vi.fn(), preview: vi.fn(), blob: vi.fn() },
  streamEvents: vi.fn(), downloadFile: vi.fn(),
}));

const empty = (): ConversationDetail => ({ conversation: { id: 'c', title: '问卷分析', frozen: false }, files: [], runs: [], messages: [] });
const withCsv = (): ConversationDetail => ({ ...empty(), files: [{ id: 'f', name: '调查.csv', role: 'csv', preview: { columns: ['编号', '评分'], rows: [['001', '4']], row_count: 1 } }] });
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.health).mockResolvedValue({ mode: 'offline' });
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
    expect(document.querySelector('img')).toBeNull();
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
});
