"""Supervision tests using real SQLite and explicit process transport doubles."""

import json
from pathlib import Path
import threading

import pytest

from labweaver.web.deadlines import RunDeadline
from labweaver.web.manager import TaskManager
from labweaver.web.settings import WebSettings
from labweaver.web.store import encode, now, uid


@pytest.fixture
def manager(tmp_path, monkeypatch):
    for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID"):
        monkeypatch.delenv(key, raising=False)
    item = TaskManager(WebSettings(data_dir=tmp_path / "app", project_dir=Path.cwd(),
                                   mode="offline", token="deadline-private-app-token"))
    yield item
    item.close()


class OwnedRuntime:
    """No child or Docker execution: this fixture tests the supervisor boundary."""

    def __init__(self, run_id, deadline):
        self.run_id, self.deadline = run_id, deadline
        self.cancel_signal = threading.Event()
        self.closed, self.stop_calls = False, []
        self.known_containers = {"owned-container": {"labels": {"labweaver.run": run_id}}}

    def stop(self, *, force=False):
        self.stop_calls.append(force)
        self.closed = True


def running_record(manager, *, status="running", pending=None):
    conversation = manager.store.new_conversation("Supervisor fixture")
    manager.upload(conversation["id"], "sample.csv", b"group,value\nA,2\nB,4\n")
    run_id, timestamp, execution_id = uid(), now(), uid()
    metadata = {"status": "error", "task_plan": [{"id": "result", "kind": "table", "description": "Requested result"}],
                "artifacts": [], "code_executions": [], "final_answer": ""}
    with manager.store.connect() as db:
        db.execute("INSERT INTO runs(id,conversation_id,request_id,task,status,created_at,updated_at,report_json,pending_json) VALUES (?,?,?,?,?,?,?,?,?)",
                   (run_id, conversation["id"], uid(), "Compute the requested result", status, timestamp, timestamp,
                    encode(metadata), encode(pending) if pending else None))
        db.execute("INSERT INTO executions(id,run_id,attempt,record_json) VALUES (?,?,?,?)",
                   (execution_id, run_id, 1, encode({"status": "error", "error": {"code": "python_failed"}})))
        db.execute("INSERT INTO messages(id,conversation_id,run_id,role,content,state,created_at) VALUES (?,?,?,?,?,?,?)",
                   (uid(), conversation["id"], run_id, "user", "Compute the requested result", "sent", timestamp))
    manager.store.append_event(run_id, "execution_end", {"status": "error", "execution_id": execution_id})
    return run_id, metadata, execution_id


@pytest.mark.parametrize("phase,code", [("model", "model_request_timeout"), ("task", "task_timeout")])
def test_expired_running_task_stops_owned_runtime_and_preserves_metadata(manager, monkeypatch, phase, code):
    run_id, metadata, execution_id = running_record(manager)
    current = [0.0]
    deadline = RunDeadline(model_timeout=5, task_timeout=10, clock=lambda: current[0])
    if phase == "model":
        deadline.observe("model_start", {"role": "analysis", "call": 2, "parent_call_id": "delegate-owned"})
        current[0] = 5.1
    else:
        current[0] = 10.1
    runtime = OwnedRuntime(run_id, deadline)
    manager.active, manager.runtime = run_id, runtime
    cleanups = []
    unrelated = {"id": "unrelated-container", "labels": {"labweaver.app": "another-app", "labweaver.run": "another-run"}}

    def cleanup_only_owned(selected_run_id, selected_runtime=None):
        assert selected_run_id == run_id and selected_runtime is runtime
        assert selected_runtime.known_containers == {"owned-container": {"labels": {"labweaver.run": run_id}}}
        cleanups.append(selected_run_id)
        return True

    monkeypatch.setattr(manager, "cleanup_owned", cleanup_only_owned)
    assert manager._deadline_exceeded(runtime) is True
    assert runtime.cancel_signal.is_set() and runtime.closed
    assert runtime.stop_calls and all(runtime.stop_calls)
    assert cleanups == [run_id]
    assert unrelated["labels"]["labweaver.run"] == "another-run"
    assert manager.active is None and manager.runtime is None
    row = manager.store.require("runs", run_id)
    assert row["status"] == "error" and json.loads(row["report_json"]) == metadata
    error = json.loads(row["error_json"])
    assert error["code"] == code and error["cleanup_confirmed"] is True
    assert error["deadline"]["limit_seconds"] == (5 if phase == "model" else 10)
    with manager.store.connect() as db:
        assert db.execute("SELECT id FROM executions WHERE run_id=?", (run_id,)).fetchone()[0] == execution_id
        assert db.execute("SELECT content FROM messages WHERE run_id=?", (run_id,)).fetchone()[0] == "Compute the requested result"
    events = manager.store.events(run_id)
    assert events[0]["type"] == "execution_end" and events[-1]["type"] == "error"
    assert not any(event["type"] == "completed" for event in events)
    assert manager._deadline_exceeded(runtime) is False  # A terminal task is not stopped again.


def test_awaiting_user_reply_is_not_terminated_by_running_deadlines(manager, monkeypatch):
    pending = {"id": "question-current", "question": "Which processing rule?"}
    run_id, metadata, _ = running_record(manager, status="awaiting_input", pending=pending)
    current = [0.0]
    deadline = RunDeadline(model_timeout=5, task_timeout=10, clock=lambda: current[0])
    current[0] = 3.0
    deadline.pause()
    runtime = OwnedRuntime(run_id, deadline)
    manager.active, manager.runtime = run_id, runtime
    current[0] = 1003.0

    def no_cleanup_while_user_is_thinking(*args, **kwargs):
        raise AssertionError("Human waiting time attempted runtime cleanup")

    monkeypatch.setattr(manager, "cleanup_owned", no_cleanup_while_user_is_thinking)
    assert manager._deadline_exceeded(runtime) is False
    assert not runtime.cancel_signal.is_set() and not runtime.stop_calls
    row = manager.store.require("runs", run_id)
    assert row["status"] == "awaiting_input" and json.loads(row["pending_json"]) == pending
    assert json.loads(row["report_json"]) == metadata
    assert manager.active == run_id and manager.runtime is runtime
    # Avoid treating fixture teardown as a real application's active shutdown.
    manager.active = manager.runtime = None
