"""Measured time remains reproducible across pauses, duplicate events and history."""

import json
from pathlib import Path
import threading
import pytest

from labweaver.agent import create_session
from labweaver.runtime.timing import TaskTiming
from labweaver.web.deadlines import RunDeadline
from test_agent import ReceiptModel, ScriptedModel, answer, call, coordinator_responses, inject_executor


def test_active_time_pauses_human_wait_and_model_events_are_deduplicated():
    current = [100.0]
    timer = TaskTiming(clock=lambda: current[0])
    timer.observe("model_start", {"role": "main", "call": 1, "started_monotonic": 100.0})
    current[0] = 103.0
    timer.observe("model_start", {"role": "main", "call": 1})
    assert timer.snapshot()["current_models"][0]["elapsed_seconds"] == 3
    timer.observe("model_end", {"role": "main", "call": 1, "elapsed_seconds": 2.8})
    timer.observe("model_end", {"role": "main", "call": 1, "elapsed_seconds": 999})
    timer.observe("model_start", {"role": "main", "call": 1})
    timer.pause()
    current[0] = 1003.0
    assert timer.snapshot()["active_seconds"] == 3
    assert timer.snapshot()["model_seconds"] == 2.8
    timer.resume()
    current[0] = 1005.0
    timer.observe("model_start", {"role": "analysis", "parent_call_id": "d1", "call": 1})
    current[0] = 1009.0
    timer.stop()
    timer.stop()
    frozen = timer.snapshot()
    current[0] = 9000.0
    timer.resume()  # Terminal time cannot restart accidentally.
    assert timer.snapshot() == frozen
    assert frozen["active_seconds"] == 9 and frozen["wall_seconds"] == 909
    assert frozen["model_seconds"] == 6.8 and frozen["model_calls"] == 2
    assert frozen["current_models"] == [] and frozen["active"] is False


def test_request_identity_keeps_separate_delegations_and_normalizes_bad_times():
    current = [50.0]
    timer = TaskTiming(clock=lambda: current[0])
    for parent in ("first", "second"):
        timer.observe("model_start", {"role": "analysis", "parent_call_id": parent, "call": 1, "started_monotonic": float("nan")})
        current[0] += 2
        timer.observe("model_end", {"role": "analysis", "parent_call_id": parent, "call": 1, "elapsed_seconds": float("inf")})
    timer.observe("model_start", {"role": "analysis", "call": True})
    timer.observe("answer_delta", {"text": "任务已经执行 999 秒"})
    result = timer.snapshot()
    assert result["model_calls"] == 2 and result["model_seconds"] == 4
    json.dumps(result, allow_nan=False)
    timer.reset()
    assert timer.snapshot()["active_seconds"] == timer.snapshot()["model_calls"] == 0


def test_deadline_observes_real_model_clock_without_duplicate_reset():
    current = [10.0]
    timer = RunDeadline(5, 30, clock=lambda: current[0])
    current[0] = 12
    timer.observe("model_start", {"role": "analysis", "parent_call_id": "d", "call": 1, "started_monotonic": 10.5})
    assert timer.snapshot()["current_models"][0]["elapsed_seconds"] == 1.5
    current[0] = 15.6
    timer.observe("model_start", {"role": "analysis", "parent_call_id": "d", "call": 1})
    assert timer.error()["code"] == "model_request_timeout"


def test_killed_model_wait_freezes_before_host_cleanup_while_task_time_continues():
    current = [0.0]
    timer = TaskTiming(clock=lambda: current[0])
    timer.observe("model_start", {"role": "analysis", "call": 1})
    current[0] = 5.0
    timer.finish_models()  # The owned process has exited; Docker cleanup continues.
    current[0] = 9.0
    assert timer.snapshot()["active_seconds"] == 9
    assert timer.snapshot()["model_seconds"] == 5
    assert timer.snapshot()["current_models"] == []
    timer.stop()
    current[0] = 99.0
    assert timer.snapshot()["active_seconds"] == 9 and timer.snapshot()["model_seconds"] == 5


def test_real_session_clarification_resume_export_and_new_task_keep_correct_timing(tmp_path):
    path = tmp_path / "sample.csv"
    path.write_text("group,value\nA,2\nB,3\n", encoding="utf-8")
    session = create_session(path, ScriptedModel(responses=[call(), call("ask_user", "q", {"question": "是否仅概览？"}), answer(), answer()]))
    current = [0.0]
    session.timing = TaskTiming(clock=lambda: current[0])
    original = session.graph.invoke
    def measured(*args, **kwargs):
        current[0] += 5
        return original(*args, **kwargs)
    session.graph.invoke = measured
    paused = session.invoke("请查看实际数据，再确认是否仅概览。")
    assert paused["status"] == "awaiting_input" and paused["timing"]["active_seconds"] == 5
    assert paused["timing"]["active"] is False
    current[0] += 100
    done = session.resume("是，只需要概览。")
    assert done["status"] == "completed" and done["timing"]["active_seconds"] == 10
    assert done["timing"]["wall_seconds"] == 110
    assert done["timing"]["model_calls"] == 3
    saved = session.save(tmp_path / "exports")
    assert saved["timing"] == done["timing"]
    assert json.loads(Path(saved["record_path"]).read_text(encoding="utf-8"))["timing"] == done["timing"]
    current[0] += 200
    next_task = session.invoke("继续概览已读取的数据")
    assert next_task["status"] == "completed" and next_task["timing"]["active_seconds"] == 5
    assert next_task["timing"]["model_calls"] == 1


@pytest.mark.parametrize("streamed", [False, True])
def test_independent_child_model_requests_are_measured_in_both_execution_modes(tmp_path, monkeypatch, streamed):
    path = tmp_path / "sample.csv"
    path.write_text("group,value\nA,2\nB,4\n", encoding="utf-8")
    session = create_session(path, ScriptedModel(responses=coordinator_responses()), analysis_model=ReceiptModel(fail_first=True))
    inject_executor(session, monkeypatch, failures=1)  # Transport clock test, not real Docker acceptance.
    events = []
    result = session.invoke("分组统计", on_event=events.append if streamed else None)
    assert result["status"] == "completed"
    assert result["analysis_model_calls"] == 3
    assert result["timing"]["model_calls"] == result["model_calls"] + result["analysis_model_calls"] == 7
    assert result["timing"]["model_seconds"] >= 0
    assert result["timing"]["active"] is False
    assert result["timing"]["current_models"] == []


def test_deadline_and_forced_stop_share_the_request_iteration_lock():
    requested, attempted, finished = threading.Event(), threading.Event(), threading.Event()
    timer = RunDeadline(100, 200, clock=lambda: 10)
    timer.observe("model_start", {"role": "main", "call": 1})
    timer.observe("model_start", {"role": "analysis", "parent_call_id": "d", "call": 1})

    class ConcurrentModels(dict):
        def items(self):
            iterator = iter(super().items())
            first = next(iterator, None)
            if first is None:
                return
            yield first
            requested.set()
            assert attempted.wait(2)
            # With the shared lock the stop operation cannot clear this mapping
            # until the deadline finishes inspecting the remaining request.
            finished.wait(.1)
            yield from iterator

    timer.models = ConcurrentModels(timer.models)
    def cancel_request():
        assert requested.wait(2)
        attempted.set()
        timer.finish_models()
        finished.set()

    worker = threading.Thread(target=cancel_request)
    worker.start()
    assert timer.error() is None
    worker.join(timeout=2)
    assert finished.is_set() and not worker.is_alive()
    assert timer.snapshot()["current_models"] == []
