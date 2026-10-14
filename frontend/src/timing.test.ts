import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { formatDuration, projectTiming, timingSnapshot, useRunTiming } from './timing';
import type { RunEvent } from './types';

const raw = { version: 1, source: 'host_monotonic', active: true, active_seconds: 12, wall_seconds: 42,
  model_seconds: 8, model_calls: 3, current_models: [{ role: 'analysis', call: 2, parent_call_id: 'd', elapsed_seconds: 5 }] };
function event(seq: number, timing: unknown, run_id = 'r'): RunEvent {
  return { version: 1, conversation_id: 'c', run_id, seq, type: 'timing', payload: { timing } };
}
describe('real task timing presentation', () => {
  it('deduplicates and orders SSE timing by sequence, without resetting on repeated starts', () => {
    const snapshot = timingSnapshot({ id: 'r', status: 'running' }, [event(2, raw), event(1, { ...raw, active_seconds: 1 }), event(2, raw), event(99, { ...raw, active_seconds: 99 }, 'other')]);
    expect(projectTiming(snapshot, 'running', 2)).toMatchObject({ activeSeconds: 14, wallSeconds: 44, modelSeconds: 10, modelCalls: 3, live: true, provisional: true });
    expect(projectTiming(snapshot, 'running', 2).currentModels[0].elapsedSeconds).toBe(7);
  });
  it('pauses human reply time, resumes cumulatively, and freezes every terminal status', () => {
    const paused = timingSnapshot({ id: 'r', status: 'awaiting_input', timing: { ...raw, active: false, current_models: [] } }, []);
    expect(projectTiming(paused, 'awaiting_input', 3600)).toMatchObject({ activeSeconds: 12, wallSeconds: 42, waiting: true, live: false, provisional: false });
    const resumed = timingSnapshot({ id: 'r', status: 'running' }, [event(3, { ...raw, active_seconds: 12, wall_seconds: 3642 })]);
    expect(projectTiming(resumed, 'running', 4).activeSeconds).toBe(16);
    for (const status of ['completed', 'error', 'cancelled', 'interrupted'] as const) {
      expect(projectTiming(resumed, status, 3600)).toMatchObject({ activeSeconds: 12, currentModels: [], live: false });
    }
  });
  it('prefers newer authoritative GET but preserves a terminal event over stale snapshots', () => {
    expect(timingSnapshot({ id: 'r', status: 'running', timing: { ...raw, active_seconds: 15 } }, [event(1, raw)])?.activeSeconds).toBe(15);
    const end = { ...raw, active: false, active_seconds: 16, current_models: [] };
    expect(timingSnapshot({ id: 'r', status: 'completed', timing: { ...raw, active_seconds: 17 } }, [event(2, end)])?.activeSeconds).toBe(16);
  });
  it('does not infer durations from model prose, task creation, malformed or absent history', () => {
    expect(projectTiming(timingSnapshot({ id: 'r', status: 'completed' }, [event(1, { ...raw, active_seconds: NaN })]), 'completed').available).toBe(false);
    expect(timingSnapshot({ id: 'r', status: 'running' }, [{ ...event(1, null), payload: { text: '已经耗时 100 秒' } }])).toBeNull();
    expect(projectTiming(timingSnapshot({ id: 'r', status: 'interrupted', timing: { ...raw, active: false, interrupted_unknown_tail: true } }, []), 'interrupted').incomplete).toBe(true);
  });
  it('formats bounded human-readable duration without negative or nonfinite clocks', () => {
    expect(formatDuration(0)).toBe('0秒'); expect(formatDuration(62.8)).toBe('1分 02秒');
    expect(formatDuration(3661)).toBe('1小时 01分 01秒'); expect(formatDuration(-5)).toBe('0秒'); expect(formatDuration(Infinity)).toBe('0秒');
  });
  it('ticks once per second locally, retains duplicate receipt anchors and clears stopped clocks', () => {
    vi.useFakeTimers();
    try {
      const { result, rerender, unmount } = renderHook(({ status, events }) => useRunTiming({ id: 'r', status }, events), {
        initialProps: { status: 'running' as import('./types').RunStatus, events: [event(1, raw)] },
      });
      act(() => { vi.advanceTimersByTime(3000); });
      expect(result.current.activeSeconds).toBeCloseTo(15, 1);
      rerender({ status: 'running', events: [event(1, raw), event(1, raw)] });
      expect(result.current.activeSeconds).toBeCloseTo(15, 1);
      rerender({ status: 'awaiting_input', events: [event(2, { ...raw, active: false, active_seconds: 15, current_models: [] })] });
      act(() => { vi.advanceTimersByTime(60000); });
      expect(result.current.activeSeconds).toBe(15);
      expect(result.current.waiting).toBe(true);
      unmount();
      expect(vi.getTimerCount()).toBe(0);
    } finally { vi.useRealTimers(); }
  });
});
