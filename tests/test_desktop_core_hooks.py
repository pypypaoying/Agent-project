"""Application hooks retain real graph execution, budgets and input boundaries."""

import os
import threading
from types import SimpleNamespace

import pytest

from labweaver.agent import create_session
from labweaver.offline import OfflineIntakeModel
from labweaver.runtime.execution import DockerExecutor, ExecutionError
from labweaver.runtime.harness import ExecutionHarness, HarnessRejected
from test_agent import (
    ReceiptModel,
    ScriptedModel,
    answer,
    call,
    coordinator_responses,
    inject_executor,
)


@pytest.fixture
def csv_path(tmp_path):
    path = tmp_path / "数据.csv"
    path.write_text("group,score\nA,2\nB,4\nA,\n", encoding="utf-8")
    return path


def test_history_is_bounded_and_injected_once_without_registering_old_assets(csv_path):
    model = ScriptedModel(responses=[call(), answer(), answer()])
    history = '{"confirmed_rule":"保留原始分组", "artifact_ids":["old-table"]}'
    session = create_session(csv_path, model, history_context=history)
    first = session.invoke("仅概览字段")
    assert first["status"] == "completed", first.get("error")
    assert history in first["messages"][0]["content"]
    assert not session.artifact_assets
    second = session.invoke("再次概览字段")
    assert second["status"] == "completed", second.get("error")
    assert all(history not in message["content"] for message in second["messages"])
    assert second["model_calls"] == 1
    with pytest.raises(ValueError, match="32 KiB"):
        create_session(csv_path, model, history_context="中" * 11000)
    with pytest.raises(ValueError, match="32 KiB"):
        create_session(csv_path, model, history_context={"not": "text"})


def test_shared_cancel_is_not_cleared_or_charged_on_invoke(csv_path):
    event = threading.Event()
    event.set()
    session = create_session(csv_path, OfflineIntakeModel(), cancellation_event=event)
    report = session.invoke("概览")
    assert event.is_set()
    assert report["status"] == "cancelled"
    assert report["model_calls"] == report["tool_attempts"] == 0
    assert report["execution_ledger"] == []


def test_cancelled_harness_never_calls_handler_or_shared_model_counter():
    event = threading.Event()
    event.set()
    harness = ExecutionHarness(
        {"execute_python": 3},
        cancellation_event=event,
        model_counter=lambda: pytest.fail("Budget charged after cancellation"),
    )
    with pytest.raises(HarnessRejected, match="cancelled"):
        harness.wrap_model_call(None, lambda request: pytest.fail("Model called"))
    with pytest.raises(HarnessRejected, match="cancelled"):
        harness.wrap_tool_call(
            SimpleNamespace(tool_call={"id": "x", "name": "execute_python"}),
            lambda request: pytest.fail("Tool executed"),
        )
    assert harness.model_calls == harness.tool_attempts == 0


def test_structured_csv_question_has_stable_interrupt_id_and_real_candidates(tmp_path):
    path = tmp_path / "ambiguous.csv"
    path.write_text("a,b;c\n1,2;3\n4,5;6\n", encoding="utf-8")
    session = create_session(path, OfflineIntakeModel())
    events = []
    report = session.invoke("概览", on_event=events.append)
    question = report["pending_input"]
    assert report["status"] == "awaiting_input"
    assert question["id"] and question["kind"] == "csv_format"
    assert question["question"] == report["question"]
    assert question["candidates"] == report["profile_result"]["error"]["candidates"]
    emitted = next(event for event in events if event["type"] == "pending_input")
    assert emitted == {"type": "pending_input", **question}
    assert session.resume("1")["pending_input"] is None


def test_execution_events_include_actual_code_error_and_host_receipts(csv_path, monkeypatch):
    session = create_session(
        csv_path,
        ScriptedModel(responses=coordinator_responses()),
        analysis_model=ReceiptModel(fail_first=True),
    )
    scripts = inject_executor(session, monkeypatch, failures=1)
    events = []
    report = session.invoke("分组统计", on_event=events.append)
    assert report["status"] == "completed", report.get("error")
    starts = [event for event in events if event["type"] == "execution_start"]
    ends = [event for event in events if event["type"] == "execution_end"]
    assert [event["code"] for event in starts] == scripts
    assert all(event["parent_call_id"] == "delegate" for event in starts)
    assert all(event["tool_call_id"] for event in starts)
    assert [event["record"] for event in ends] == report["code_executions"]
    assert ends[0]["record"]["stderr"]["text"] == "NameError: fixture"
    assert ends[1]["artifacts"][0]["id"] in report["code_executions"][1]["artifact_ids"]


def test_retrieval_event_contains_only_actual_tool_result_and_sources(csv_path, tmp_path):
    material = tmp_path / "说明.md"
    material.write_text("score 表示评分，空白值表示未回答。\n", encoding="utf-8")
    session = create_session(
        csv_path,
        ScriptedModel(responses=[
            call(),
            call("search_materials", "search", {"query": "score 评分"}),
            answer(text="score 是评分 [D1-C1]。"),
        ]),
        material_paths=[material],
    )
    events = []
    report = session.invoke("基于资料概述字段", on_event=events.append)
    assert report["status"] == "completed", report.get("error")
    retrieved = next(event for event in events if event["type"] == "retrieval_result")
    evidence = next(item for item in report["execution_ledger"] if item["name"] == "search_materials")
    assert retrieved["tool_call_id"] == evidence["tool_call_id"] == "search"
    assert retrieved["query"] == evidence["result"]["query"]
    assert retrieved["matches"] == evidence["result"]["matches"]
    assert retrieved["sources"] == report["materials"]


def test_owned_container_labels_and_lifecycle_are_trusted_and_bounded(monkeypatch):
    import labweaver.runtime.execution as module

    executor = DockerExecutor()
    events = []
    executor.set_owner("app-123", "run-456", events.append)
    executor._endpoint = "unix:///verified.sock"
    command = executor.command("docker", "sha256:" + "a" * 64, "selected", "/stage")
    assert "labweaver.app=app-123" in command
    assert "labweaver.run=run-456" in command
    monkeypatch.setattr(module, "docker_executable", lambda: "docker")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    assert executor._remove("selected")
    assert events == [{
        "type": "container_lifecycle", "phase": "removed", "name": "selected",
        "endpoint": "unix:///verified.sock",
        "labels": {"labweaver.app": "app-123", "labweaver.run": "run-456"},
        "removed": True,
    }]
    with pytest.raises(ValueError, match="owner"):
        executor.set_owner("app=untrusted", "run")
    executor._container = "selected"
    with pytest.raises(ValueError, match="active"):
        executor.set_owner("other-app", "other-run")


def test_shared_cancel_prevents_docker_preflight_and_survives_reset(monkeypatch):
    event = threading.Event()
    event.set()
    executor = DockerExecutor(cancellation_event=event)
    executor.reset()
    monkeypatch.setattr(executor, "preflight", lambda: pytest.fail("Docker called"))
    record, assets = executor.execute(
        "print('not run')", SimpleNamespace(source={"sha256": "a" * 64}), []
    )
    assert record["error"]["code"] == "cancelled" and not assets
    assert event.is_set()


@pytest.mark.docker
def test_real_container_emits_owned_lifecycle_and_actual_receipts(csv_path):
    from labweaver.tools.csv_profile import load_csv_snapshot

    executor = DockerExecutor()
    try:
        executor.preflight()
    except ExecutionError as exc:
        if os.environ.get("LABWEAVER_REQUIRE_DOCKER") == "1":
            pytest.fail(f"Real Docker required: {exc}")
        pytest.skip(f"Real Docker unavailable: {exc.code}")
    events = []
    executor.set_owner("core-hooks-acceptance", "real-run", events.append)
    record, assets = executor.execute(
        "from helper import load_dataset, emit_metric\n"
        "df = load_dataset()\n"
        "assert len(df) == 3\n"
        "emit_metric(len(df), 'rows', '真实行数')\n",
        load_csv_snapshot(csv_path),
        [{"id": "rows", "kind": "metric", "description": "行数"}],
    )
    assert record["status"] == "completed", record.get("error")
    assert record["exit_code"] == 0 and record["container_removed"]
    assert record["artifact_ids"] == list(assets)
    assert events[0]["phase"] == "starting"
    assert events[-1]["phase"] == "removed" and events[-1]["removed"]
    assert all(event["name"] == record["container_name"] for event in events)
    assert all(event["labels"] == record["owner_labels"] for event in events)
