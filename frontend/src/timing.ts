import { useEffect, useRef, useState } from 'react';
import type { RunEvent, RunStatus } from './types';

interface TimedRun { id: string; status: RunStatus; timing?: unknown; events?: RunEvent[]; }
interface CurrentModel { role: string; call: number; parentCallId: string | null; elapsedSeconds: number; }
interface Snapshot {
  activeSeconds: number; wallSeconds: number; modelSeconds: number; modelCalls: number;
  currentModels: CurrentModel[]; active: boolean; source: string; incomplete: boolean;
}
export interface RunTiming extends Omit<Snapshot, 'active'> {
  available: boolean; live: boolean; waiting: boolean;
  /** Live values are locally extrapolated between authoritative host events. */
  provisional: boolean;
}
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;
function parse(value: unknown): Snapshot | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  if (!finite(item.active_seconds) || !finite(item.wall_seconds) || !finite(item.model_seconds) || !finite(item.model_calls)) return null;
  const currentModels = Array.isArray(item.current_models) ? item.current_models.flatMap(raw => {
    if (!raw || typeof raw !== 'object') return [];
    const model = raw as Record<string, unknown>;
    return (model.role === 'main' || model.role === 'analysis') && finite(model.call) && model.call > 0 && finite(model.elapsed_seconds)
      ? [{ role: model.role, call: model.call, parentCallId: typeof model.parent_call_id === 'string' ? model.parent_call_id : null, elapsedSeconds: model.elapsed_seconds }] : [];
  }) : [];
  return { activeSeconds: item.active_seconds, wallSeconds: item.wall_seconds, modelSeconds: item.model_seconds,
    modelCalls: item.model_calls, currentModels, active: item.active === true,
    source: typeof item.source === 'string' ? item.source : 'host_monotonic', incomplete: item.interrupted_unknown_tail === true };
}

/** Select by genuine sequence rather than array order or receipt wall-clock time. */
export function timingSnapshot(run: TimedRun, events: RunEvent[]): Snapshot | null {
  let seq = -1;
  let latest: Snapshot | null = null;
  for (const event of events) {
    if (event.run_id !== run.id || event.seq <= seq) continue;
    const candidate = parse(event.payload.timing);
    if (candidate && (candidate.source === 'host_monotonic' || latest?.source !== 'host_monotonic')) { seq = event.seq; latest = candidate; }
  }
  const api = parse(run.timing);
  if (api?.source === 'host_monotonic' && latest?.source !== 'host_monotonic') return api;
  // GET snapshots can be more recent than the last buffered event. The final
  // terminal event remains authoritative even if a stale GET says "running".
  if (api && (!latest || api.activeSeconds > latest.activeSeconds && latest.active)) return api;
  return latest ?? api;
}

export function projectTiming(snapshot: Snapshot | null, status: RunStatus, secondsSinceReceipt = 0): RunTiming {
  const live = Boolean(snapshot?.active && (status === 'running' || status === 'cancelling'));
  const increment = live && finite(secondsSinceReceipt) ? secondsSinceReceipt : 0;
  return {
    available: snapshot !== null, source: snapshot?.source ?? 'unavailable', incomplete: snapshot?.incomplete ?? false,
    activeSeconds: (snapshot?.activeSeconds ?? 0) + increment,
    wallSeconds: (snapshot?.wallSeconds ?? 0) + increment,
    modelSeconds: (snapshot?.modelSeconds ?? 0) + (snapshot?.currentModels.length ?? 0) * increment,
    modelCalls: snapshot?.modelCalls ?? 0,
    currentModels: live ? (snapshot?.currentModels ?? []).map(model => ({ ...model, elapsedSeconds: model.elapsedSeconds + increment })) : [],
    live, waiting: status === 'awaiting_input', provisional: increment > 0,
  };
}

export function formatDuration(seconds: number): string {
  const value = finite(seconds) ? Math.floor(seconds) : 0;
  const hours = Math.floor(value / 3600), minutes = Math.floor(value % 3600 / 60), rest = value % 60;
  if (hours) return `${hours}小时 ${minutes.toString().padStart(2, '0')}分 ${rest.toString().padStart(2, '0')}秒`;
  if (minutes) return `${minutes}分 ${rest.toString().padStart(2, '0')}秒`;
  return `${rest}秒`;
}

/** Tick presentation locally; never perform a model call or mutate durable time. */
export function useRunTiming(run: TimedRun, events: RunEvent[]): RunTiming {
  const snapshot = timingSnapshot(run, events);
  const signature = `${run.id}:${JSON.stringify(snapshot)}`;
  const anchor = useRef({ signature, at: performance.now() });
  if (anchor.current.signature !== signature) anchor.current = { signature, at: performance.now() };
  const [, tick] = useState(0);
  const live = snapshot?.active && (run.status === 'running' || run.status === 'cancelling');
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => tick(previous => previous + 1), 1000);
    return () => window.clearInterval(timer);
  }, [live, run.id]);
  return projectTiming(snapshot, run.status, Math.max(0, performance.now() - anchor.current.at) / 1000);
}
