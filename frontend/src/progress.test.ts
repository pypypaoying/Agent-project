import { describe, expect, it } from 'vitest';
import { summarizeProgress } from './progress';
import type { RunEvent } from './types';

function event(seq: number, type: string, payload: Record<string, unknown>): RunEvent {
  return { version: 1, conversation_id: 'c', run_id: 'r', seq, type, payload };
}

describe('genuine central execution progress', () => {
  it('pairs tool starts and results by real call ID without duplicate steps', () => {
    const steps = summarizeProgress([
      event(1, 'tool_start', { name: 'profile_csv', id: 'p' }),
      event(2, 'tool_start', { name: 'profile_csv', id: 'p' }),
      event(3, 'tool_end', { name: 'profile_csv', id: 'p', status: 'completed' }),
      event(4, 'tool_start', { name: 'delegate_analysis', id: 'a' }),
    ], true);
    expect(steps).toHaveLength(2);
    expect(steps[0]).toMatchObject({ title: '读取 CSV 数据', state: 'complete', status: '完成' });
    expect(steps[1]).toMatchObject({ state: 'running', status: '执行中' });
  });

  it('uses actual code execution status, exit code and elapsed time', () => {
    const steps = summarizeProgress([
      event(1, 'execution_start', { tool_call_id: 'python', attempt: 2, code: 'private long code' }),
      event(2, 'execution_end', { tool_call_id: 'python', attempt: 2, status: 'completed', record: { status: 'error', exit_code: 1, elapsed_seconds: 2.57 } }),
    ], true);
    expect(steps).toHaveLength(1);
    expect(steps[0]).toMatchObject({ title: '代码 Agent · 第 2 次 Python 修正执行', state: 'error', status: '失败', detail: '退出码 1 · 耗时 2.6 秒' });
    expect(JSON.stringify(steps)).not.toContain('private long code');
  });

  it('never treats model prose or an unknown tool return status as success', () => {
    expect(summarizeProgress([event(1, 'answer_delta', { text: '代码执行成功' })], false)).toEqual([]);
    expect(summarizeProgress([event(1, 'tool_end', { name: 'read_artifact', id: 'r', status: 'unknown' })], false)[0]).toMatchObject({ state: 'neutral', status: '已返回' });
  });

  it('shows only a bounded real stderr summary and keeps full code out of chat progress', () => {
    const steps = summarizeProgress([event(1, 'execution_end', { tool_call_id: 'p', attempt: 1, status: 'error', code: 'entire generated code', record: { status: 'error', stderr: `Traceback\nFile generated.py\nValueError: ${'x'.repeat(220)}` } })], false);
    expect(steps[0].detail).toHaveLength(160);
    expect(steps[0].detail).toMatch(/^ValueError:/);
    expect(steps[0].detail).not.toContain('File generated.py');
    expect(JSON.stringify(steps)).not.toContain('entire generated code');
  });

  it('handles the actual core stderr object and discloses a truncated log preview', () => {
    const steps = summarizeProgress([event(1, 'execution_end', { tool_call_id: 'p', attempt: 1, status: 'error', record: { status: 'error', stderr: { text: 'Traceback\n  assert actual == expected\nAssertionError: unexpected total', truncated: true }, error: { message: 'Python exited unsuccessfully' } } })], false);
    expect(steps[0].detail).toBe('AssertionError: unexpected total · 日志预览已截断');
    expect(steps[0].detail).not.toContain('assert actual == expected');
    expect(steps[0].detail).not.toContain('Python exited unsuccessfully');
    expect(summarizeProgress([event(1, 'interrupted', {})], false)[0].status).toBe('已中断');
    expect(summarizeProgress([event(1, 'awaiting_input', {})], true)[0].status).toBe('等待回复');
  });

  it('keeps pending tools paused and final task completion separate from core validation', () => {
    expect(summarizeProgress([event(1, 'tool_start', { name: 'profile_csv', id: 'p' })], true, true)[0]).toMatchObject({ state: 'neutral', status: '等待回复' });
    const steps = summarizeProgress([event(1, 'validated', { status: 'completed' }), event(2, 'saving', {}), event(3, 'completed', {})], false);
    expect(steps[0].detail).toBe('等待成果保存与登记');
    expect(steps[2].title).toBe('成果已核验、保存并登记');
  });
});
