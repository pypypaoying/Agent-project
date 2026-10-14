"""Authoritative web task clocks persist separately from core execution exports."""

import json
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from labweaver.web.deadlines import RunDeadline
from labweaver.web.manager import TaskManager
from labweaver.web.settings import WebSettings
from test_web_manager import bound_conversation, insert_running, real_overview_export


@pytest.fixture
def manager(tmp_path, monkeypatch):
    for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID"):
        monkeypatch.delenv(key, raising=False)
    item = TaskManager(WebSettings(data_dir=tmp_path / "app", project_dir=Path.cwd(), mode="offline", token="timing-test-app-token"))
    yield item
    item.close()


def owned_runtime(run_id, current):
    runtime = SimpleNamespace(run_id=run_id, deadline=RunDeadline(5, 30, clock=lambda: current[0]),
                              last_timing_at=None, closed=False, cancel_signal=threading.Event(),
                              process=SimpleNamespace(is_alive=lambda: False))
    runtime.stop = lambda **kwargs: setattr(runtime, "closed", True)
    return runtime


def test_quiet_io_heartbeat_is_throttled_and_reads_current_monotonic_clock(manager):
    conversation, _ = bound_conversation(manager)
    run_id = insert_running(manager, conversation)
    current = [100.0]
    runtime = owned_runtime(run_id, current)
    manager.runtime, manager.active = runtime, run_id
    runtime.deadline.observe("model_start", {"role": "analysis", "parent_call_id": "d", "call": 1})
    manager._emit_timing(runtime, force=True)
    current[0] = 100.5
    manager._emit_timing(runtime)
    assert len(manager.all_events(run_id)) == 1
    current[0] = 102.0
    manager._emit_timing(runtime)
    assert len(manager.all_events(run_id)) == 2
    current[0] = 102.2
    view = manager.run_view(run_id)
    assert view["timing"]["active_seconds"] == 2.2
    assert view["timing"]["current_models"][0]["elapsed_seconds"] == 2.2
    assert view["events"][-1]["payload"]["timing"]["active_seconds"] == 2
    assert view["events"][-1]["payload"]["role"] == "host"
    runtime.deadline.pause()
    with manager.store.connect() as db:
        db.execute("UPDATE runs SET status='awaiting_input' WHERE id=?", (run_id,))
    current[0] = 900.0
    manager._emit_timing(runtime)
    assert len(manager.all_events(run_id)) == 2
    assert manager.run_view(run_id)["timing"]["active_seconds"] == 2.2
    manager.runtime = manager.active = None


def test_error_terminal_clock_and_partial_request_freeze_in_persisted_history(manager, monkeypatch):
    conversation, _ = bound_conversation(manager)
    run_id = insert_running(manager, conversation)
    current = [0.0]
    runtime = owned_runtime(run_id, current)
    manager.runtime, manager.active = runtime, run_id
    runtime.deadline.observe("model_start", {"role": "main", "call": 1})
    current[0] = 7.25
    monkeypatch.setattr(manager, "cleanup_owned", lambda *args: True)
    manager.fail(run_id, "test_request_failed", "请求失败。")
    current[0] = 7000.0
    view = manager.run_view(run_id)
    assert view["status"] == "error"
    assert view["timing"]["active_seconds"] == view["timing"]["model_seconds"] == 7.25
    assert view["timing"]["active"] is False and view["timing"]["current_models"] == []
    assert view["events"][-1]["payload"]["timing"] == view["timing"]


def test_completed_api_task_includes_save_time_but_export_uses_core_agent_time(manager):
    conversation, file = bound_conversation(manager)
    saved = real_overview_export(manager, file)
    run_id = insert_running(manager, conversation, core=saved)
    current = [0.0]
    runtime = owned_runtime(run_id, current)
    manager.runtime, manager.active = runtime, run_id
    current[0] = 15.0
    manager._register_report(run_id, saved)
    current[0] = 9000.0
    view = manager.run_view(run_id)
    assert view["status"] == "completed" and view["timing"]["active_seconds"] == 15
    assert view["timing"]["source"] == "host_monotonic"
    assert saved["timing"]["source"] == "agent_monotonic"
    assert json.loads(Path(saved["record_path"]).read_text(encoding="utf-8"))["timing"] == saved["timing"]
    manager.runtime = None
    assert manager.run_view(run_id)["timing"]["active_seconds"] == 15


def test_crash_recovery_keeps_last_measured_snapshot_without_counting_downtime(manager, monkeypatch):
    conversation, _ = bound_conversation(manager)
    run_id = insert_running(manager, conversation)
    manager.store.append_event(run_id, "timing", {"timing": {"version": 1, "source": "host_monotonic", "active": True,
        "active_seconds": 12.5, "wall_seconds": 19, "model_seconds": 8.5, "model_calls": 1,
        "current_models": [{"role": "main", "call": 1, "parent_call_id": None, "elapsed_seconds": 8.5}]}})
    settings = manager.settings
    manager.close()
    monkeypatch.setattr(TaskManager, "cleanup_owned", lambda *args: True)
    recovered = TaskManager(settings)
    try:
        view = recovered.run_view(run_id)
        assert view["status"] == "interrupted"
        assert view["timing"]["active_seconds"] == 12.5
        assert view["timing"]["interrupted_unknown_tail"] is True
        assert view["timing"]["current_models"] == [] and view["timing"]["active"] is False
    finally:
        recovered.close()
