"""Manager transactions/recovery and transport lifecycle; analysis is never simulated.

Overview exports use the real Deep Agents tool flow without generated-code execution.
Transport doubles below test process orchestration only, not Docker acceptance.
"""

import copy
import json
from pathlib import Path
import queue
import threading
import time

import pytest

from labweaver.agent import create_session
from labweaver.offline import OfflineIntakeModel
from labweaver.web import manager as manager_module
from labweaver.web.manager import ApplicationLock, TaskManager
from labweaver.web.security import AppError, digest
from labweaver.web.settings import WebSettings
from labweaver.web.store import encode, now, uid


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    settings = WebSettings(data_dir=tmp_path / "application", project_dir=Path.cwd(),
                           mode="offline", token="manager-local-test-token", cancel_grace=0.01)
    item = TaskManager(settings)
    yield item
    item.close()


def bound_conversation(manager):
    conversation = manager.store.new_conversation()
    file = manager.upload(conversation["id"], "实际输入.csv", "编号,数量\n001,2\n002,3\n".encode())
    return conversation, file


def insert_running(manager, conversation, *, pending=None, status="running", core=None):
    run_id, timestamp = uid(), now()
    with manager.store.connect() as db:
        db.execute("INSERT INTO runs(id,conversation_id,request_id,task,status,created_at,updated_at,pending_json,core_session_id,core_task_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (run_id, conversation["id"], uid(), "概览字段与缺失情况", status, timestamp, timestamp,
                    encode(pending) if pending else None, (core or {}).get("session_id"), (core or {}).get("task_id")))
    return run_id


def real_overview_export(manager, file):
    registered = manager.store.require("files", file["id"])
    session = create_session(manager.store.root / registered["relative_path"], OfflineIntakeModel())
    report = session.invoke("概览字段与缺失情况")
    assert report["status"] == "completed"
    assert report["profile_result"]["row_count"] == 2
    assert report["code_executions"] == []  # This is overview acceptance, not Docker analysis.
    saved = session.save(manager.store.root / "exports")
    return saved


def test_appdata_alias_uses_one_physical_root_for_worker_and_exports(tmp_path, monkeypatch):
    nominal, physical = tmp_path / "nominal-AppData", tmp_path / "physical-LocalCache"
    nominal.mkdir()
    physical.mkdir()
    original_resolve = Path.resolve

    def redirected(path, *args, **kwargs):
        return physical if path == nominal else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    settings = WebSettings(data_dir=nominal, project_dir=Path.cwd(), mode="offline",
                           token="canonical-root-test-token")
    manager = TaskManager(settings)
    try:
        assert manager.store.root == manager.settings.data_dir == physical
        assert manager.store.path.parent == physical
        conversation, file = bound_conversation(manager)
        saved = real_overview_export(manager, file)
        assert Path(saved["record_path"]).is_relative_to(physical)
        run_id = insert_running(manager, conversation)
        manager.active = run_id
        manager._register_report(run_id, saved)
        assert manager.run_view(run_id)["status"] == "completed"
    finally:
        manager.close()


def counts(manager):
    with manager.store.connect() as db:
        return {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("files", "messages", "artifacts", "executions", "run_events")}


def test_actual_core_export_is_completed_only_with_atomic_metadata_and_event(manager):
    conversation, file = bound_conversation(manager)
    saved = real_overview_export(manager, file)
    run_id = insert_running(manager, conversation)
    manager.active = run_id
    manager._register_report(run_id, saved)
    result = manager.run_view(run_id)
    assert result["status"] == "completed" and manager.active is None
    assert result["core_session_id"] == saved["session_id"]
    assert result["core_task_id"] == saved["task_id"]
    assert result["final_answer"] == saved["final_answer"]
    assert saved['report_version']==3
    assert result['task_spec']==saved['task_spec']
    assert result['verification']==saved['verification']
    assert result['token_usage']==saved['token_usage']
    assert result["events"][-1]["type"] == "completed"
    assert result["events"][-1]["payload"]["status"] == "completed"
    assert {file["media_type"] for file in result["downloads"]} >= {"application/json", "text/markdown"}
    messages = manager.conversation_view(conversation["id"])["messages"]
    assert messages[-1]["state"] == "validated"


def test_terminal_event_failure_rolls_back_status_message_and_file_registration(manager, monkeypatch):
    conversation, file = bound_conversation(manager)
    saved = real_overview_export(manager, file)
    run_id = insert_running(manager, conversation)
    manager.active = run_id
    before = counts(manager)
    original = manager.store.append_event

    def failed_event(*args, **kwargs):
        assert kwargs.get("conn") is not None
        raise OSError("injected SQLite event-write failure")

    monkeypatch.setattr(manager.store, "append_event", failed_event)
    with pytest.raises(OSError):
        manager._register_report(run_id, saved)
    row = manager.store.require("runs", run_id)
    assert row["status"] == "running" and row["report_path"] is None and row["report_json"] is None
    assert counts(manager) == before
    assert manager.active == run_id
    assert Path(saved["record_path"]).is_file()  # Valid disk export is retained, not claimed complete.
    monkeypatch.setattr(manager.store, "append_event", original)
    manager._register_report(run_id, saved)
    assert manager.run_view(run_id)["status"] == "completed"


def test_error_event_failure_rolls_back_error_state(manager, monkeypatch):
    conversation, _ = bound_conversation(manager)
    run_id = insert_running(manager, conversation)

    def failed_event(*args, **kwargs):
        assert kwargs.get("conn") is not None
        raise OSError("injected event-write failure")

    monkeypatch.setattr(manager.store, "append_event", failed_event)
    with pytest.raises(OSError):
        manager.fail(run_id, "test_error", "未完成")
    row = manager.store.require("runs", run_id)
    assert row["status"] == "running" and row["error_json"] is None
    assert manager.store.events(run_id) == []


def test_checkpoint_export_registers_history_without_publishing_completion(manager):
    conversation, file = bound_conversation(manager)
    saved = real_overview_export(manager, file)
    run_id = insert_running(manager, conversation)
    manager.active = run_id
    manager._register_report(run_id, saved, final=False)
    result = manager.run_view(run_id)
    assert result["status"] == "running" and manager.active == run_id
    assert result["downloads"]
    assert [event["type"] for event in result["events"]] == ["artifacts_saved"]
    assert manager.conversation_view(conversation["id"])["messages"] == []
    manager.active = None  # Model a crash fixture without a real active process.


def test_export_missing_or_provenance_mismatch_cannot_complete(manager):
    conversation, file = bound_conversation(manager)
    saved = real_overview_export(manager, file)
    run_id = insert_running(manager, conversation)
    bad = copy.deepcopy(saved)
    bad["source"]["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        manager._register_report(run_id, bad)
    assert manager.store.require("runs", run_id)["status"] == "running"
    assert manager.run_view(run_id)["downloads"] == []
    # Even matching IPC and disk metadata cannot authorize a different CSV source.
    Path(saved["record_path"]).write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        manager._register_report(run_id, bad)
    assert manager.store.require("runs", run_id)["status"] == "running"
    Path(saved["record_path"]).unlink()
    with pytest.raises(OSError):
        manager._register_report(run_id, saved)
    assert manager.store.require("runs", run_id)["status"] == "running"


def reopen(manager, monkeypatch):
    settings = manager.settings
    manager.active = None
    manager.close()
    monkeypatch.setattr(TaskManager, "cleanup_owned", lambda self, run_id, runtime=None: True)

    def no_auto_runtime(*args, **kwargs):
        raise AssertionError("Startup must not instantiate or resume an Agent runtime")

    monkeypatch.setattr(manager_module, "Runtime", no_auto_runtime)
    return TaskManager(settings)


def test_startup_recovers_valid_orphan_export_but_marks_run_interrupted(manager, monkeypatch):
    conversation, file = bound_conversation(manager)
    saved = real_overview_export(manager, file)
    run_id = insert_running(manager, conversation, core=saved)
    # A metric/other JSON beside a report is not itself a report.
    (Path(saved["record_path"]).parent / "orphan-metric.json").write_text("[1,2,3]", encoding="utf-8")
    restarted = reopen(manager, monkeypatch)
    try:
        result = restarted.run_view(run_id)
        assert result["status"] == "interrupted"
        assert result["error"]["code"] == "interrupted"
        assert result["downloads"]
        assert result["core_session_id"] == saved["session_id"]
        assert result["events"][-1]["type"] == "interrupted"
        assert not any(event["type"] == "completed" for event in result["events"])
        assert restarted.runtime is None and restarted.active is None
    finally:
        restarted.close()


def test_old_clarification_becomes_interrupted_and_cannot_resume(manager, monkeypatch):
    conversation, _ = bound_conversation(manager)
    pending = {"id": uid(), "kind": "question", "question": "采用哪个范围？"}
    run_id = insert_running(manager, conversation, status="awaiting_input", pending=pending)
    restarted = reopen(manager, monkeypatch)
    try:
        assert restarted.run_view(run_id)["status"] == "interrupted"
        assert restarted.run_view(run_id)["pending_input"] is None
        with pytest.raises(AppError) as error:
            restarted.reply(run_id, pending["id"], "全部记录", "old-reply")
        assert error.value.code == "question_expired"
        assert restarted.runtime is None
    finally:
        restarted.close()


def test_invalid_orphan_provenance_is_not_exposed_as_registered_files(manager, monkeypatch):
    conversation, file = bound_conversation(manager)
    saved = real_overview_export(manager, file)
    run_id = insert_running(manager, conversation, core=saved)
    saved["source"]["sha256"] = "0" * 64
    Path(saved["record_path"]).write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    restarted = reopen(manager, monkeypatch)
    try:
        result = restarted.run_view(run_id)
        assert result["status"] == "interrupted"
        assert result["downloads"] == [] and result["artifacts"] == []
    finally:
        restarted.close()


def test_tampered_orphan_code_bytes_are_not_registered_as_verified_execution(manager, monkeypatch):
    conversation, file = bound_conversation(manager)
    saved = real_overview_export(manager, file)
    run_id = insert_running(manager, conversation, core=saved)
    folder = Path(saved["record_path"]).parent
    code_path = folder / "tampered-attempt.py"
    code_path.write_text("print('changed')\n", encoding="utf-8")
    original_code = "print('original')\n"
    # This is deliberately forged crash metadata, never execution acceptance.
    saved["code_executions"] = [{"id": uid(), "status": "completed", "code": original_code,
                                 "code_sha256": digest(original_code.encode()), "code_path": str(code_path)}]
    saved["artifact_paths"].append(str(code_path))
    Path(saved["record_path"]).write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    restarted = reopen(manager, monkeypatch)
    try:
        result = restarted.run_view(run_id)
        assert result["status"] == "interrupted"
        assert result["downloads"] == []
        with restarted.store.connect() as db:
            assert db.execute("SELECT count(*) FROM executions WHERE run_id=?", (run_id,)).fetchone()[0] == 0
    finally:
        restarted.close()


def test_application_directory_lock_rejects_second_service_and_releases(manager):
    with pytest.raises(AppError) as error:
        ApplicationLock(manager.settings.data_dir)
    assert error.value.code == "already_running"
    manager.close()
    acquired = ApplicationLock(manager.settings.data_dir)
    acquired.close()


class TransportProcess:
    """A process transport fixture; it runs no Agent or generated analysis code."""
    def __init__(self):
        self.alive = True
        self.join_calls = []

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)
        self.alive = False


class TransportRuntime:
    def __init__(self, settings, app_id, conversation_id, csv_path, materials, context, target):
        self.conversation_id, self.run_id = conversation_id, None
        self.commands, self.events, self.cancel_signal = queue.Queue(), queue.Queue(), threading.Event()
        self.process = TransportProcess()
        self.known_containers = {}
        self.closed = False
        self.stops = []

    def stop(self, *, force=False):
        self.stops.append(force)
        self.closed = True
        self.process.alive = False


@pytest.mark.parametrize("event_kind", ["checkpoint_report", "worker_error"])
def test_save_failure_keeps_execution_diagnostics_and_redacts_secrets(manager, monkeypatch, event_kind):
    """Diagnostic transport fixture, not evidence of a successful analysis."""
    conversation, _ = bound_conversation(manager)
    run_id = insert_running(manager, conversation)
    runtime = TransportRuntime(manager.settings, manager.store.app_id, conversation["id"],
                               None, [], "", None)
    runtime.run_id = run_id
    manager.runtime, manager.active = runtime, run_id
    sentinel = "secret-sentinel-diagnostic-12345"
    manager.secrets += (sentinel,)
    execution = {"id": uid(), "status": "error", "exit_code": 2,
                 "error": {"code": "python_failed"},
                 "stderr": {"text": "missing input script " + sentinel, "truncated": False},
                 "container_state": {"OOMKilled": False, "ExitCode": 0},
                 "container_removed": True}
    if event_kind == "checkpoint_report":
        def register_failed(*args, **kwargs):
            raise ValueError("injected canonical export registration failure")

        monkeypatch.setattr(manager, "_register_report", register_failed)
        payload = {"report": {"code_executions": [execution]}}
        expected_stage = "report_registration"
    else:
        payload = {"code": "save_failed", "message": "执行记录无法保存。",
                   "exception_type": "PermissionError",
                   "save_error": {"stage": "partial_export", "exception_type": "PermissionError", "errno": 13},
                   "execution_error": execution}
        expected_stage = "partial_export"
    runtime.events.put({"run_id": run_id, "kind": event_kind, "payload": payload})
    monkeypatch.setattr(manager, "cleanup_owned", lambda run_id, runtime=None: True)
    monkeypatch.setattr(manager, "_recover_exports", lambda run: None)
    manager._listen(runtime)
    result = manager.run_view(run_id)
    assert result["status"] == "error" and result["error"]["code"] == "save_failed"
    assert result["error"]["save_error"]["stage"] == expected_stage
    assert result["error"]["save_error"]["exception_type"] == ("ValueError" if event_kind == "checkpoint_report" else "PermissionError")
    actual = result["error"]["execution_error"]
    assert actual["exit_code"] == 2 and actual["error"]["code"] == "python_failed"
    assert actual["container_removed"] and actual["container_state"]["OOMKilled"] is False
    assert "[REDACTED]" in actual["stderr"]["text"]
    assert sentinel not in encode(result)
    assert manager.active is None and manager.runtime is None and runtime.closed


def test_waiting_for_reply_occupies_global_slot_and_cancel_releases_it(manager, monkeypatch):
    monkeypatch.setattr(manager_module, "Runtime", TransportRuntime)
    monkeypatch.setattr(manager, "_listen", lambda runtime: None)
    monkeypatch.setattr(manager, "cleanup_owned", lambda run_id, runtime=None: True)
    conversation, _ = bound_conversation(manager)
    other, _ = bound_conversation(manager)
    first = manager.start(conversation["id"], "概览字段", "first-task")
    runtime = manager.runtime
    pending = {"id": uid(), "kind": "question", "question": "采用哪个范围？"}
    with manager.store.connect() as db:
        db.execute("UPDATE runs SET status='awaiting_input',pending_json=? WHERE id=?", (encode(pending), first["id"]))
    with pytest.raises(AppError) as error:
        manager.start(other["id"], "另一项任务", "second-task")
    assert error.value.code == "task_active"
    stopped = manager.cancel(first["id"])
    assert stopped["status"] == "cancelling" and runtime.cancel_signal.is_set()
    deadline = time.monotonic() + 3
    while manager.store.require("runs", first["id"])["status"] == "cancelling" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.run_view(first["id"])["status"] == "cancelled"
    assert manager.active is None and manager.runtime is None
    assert runtime.closed and runtime.stops and runtime.process.join_calls
    assert manager.cancel(first["id"])["status"] == "cancelled"


def test_duplicate_task_request_does_not_send_second_worker_command(manager, monkeypatch):
    monkeypatch.setattr(manager_module, "Runtime", TransportRuntime)
    monkeypatch.setattr(manager, "_listen", lambda runtime: None)
    monkeypatch.setattr(manager, "cleanup_owned", lambda run_id, runtime=None: True)
    conversation, _ = bound_conversation(manager)
    first = manager.start(conversation["id"], "概览字段", "same-task-request")
    runtime = manager.runtime
    repeated = manager.start(conversation["id"], "概览字段", "same-task-request")
    assert repeated["id"] == first["id"] and runtime.commands.qsize() == 1
    with pytest.raises(AppError) as error:
        manager.start(conversation["id"], "不同任务", "same-task-request")
    assert error.value.code == "request_reused"


def test_reply_idempotency_does_not_resume_twice(manager, monkeypatch):
    monkeypatch.setattr(manager_module, "Runtime", TransportRuntime)
    monkeypatch.setattr(manager, "_listen", lambda runtime: None)
    monkeypatch.setattr(manager, "cleanup_owned", lambda run_id, runtime=None: True)
    conversation, _ = bound_conversation(manager)
    started = manager.start(conversation["id"], "概览字段", "task")
    pending = {"id": uid(), "kind": "question", "question": "采用哪个范围？"}
    with manager.store.connect() as db:
        db.execute("UPDATE runs SET status='awaiting_input',pending_json=? WHERE id=?", (encode(pending), started["id"]))
    runtime = manager.runtime
    manager.reply(started["id"], pending["id"], "全部记录", "same-reply-request")
    repeated = manager.reply(started["id"], pending["id"], "全部记录", "same-reply-request")
    assert repeated["status"] == "running"
    assert runtime.commands.qsize() == 2  # One invoke, one resume.
    with pytest.raises(AppError) as error:
        manager.reply(started["id"], pending["id"], "另一口径", "same-reply-request")
    assert error.value.code == "request_reused"
    with pytest.raises(AppError) as error:
        manager.reply(started["id"], pending["id"], "全部记录", "new-reply-request")
    assert error.value.code == "question_expired"


def test_unconfirmed_container_cleanup_is_error_not_cancelled(manager, monkeypatch):
    monkeypatch.setattr(manager_module, "Runtime", TransportRuntime)
    monkeypatch.setattr(manager, "_listen", lambda runtime: None)
    monkeypatch.setattr(manager, "cleanup_owned", lambda run_id, runtime=None: False)
    conversation, _ = bound_conversation(manager)
    started = manager.start(conversation["id"], "概览字段", "task")
    manager.cancel(started["id"])
    deadline = time.monotonic() + 3
    while manager.store.require("runs", started["id"])["status"] == "cancelling" and time.monotonic() < deadline:
        time.sleep(0.01)
    result = manager.run_view(started["id"])
    assert result["status"] == "error"
    assert result["error"]["code"] == "cancel_cleanup_failed"
    assert result["error"]["cleanup_confirmed"] is False
    assert not any(event["type"] == "cancelled" for event in result["events"])
