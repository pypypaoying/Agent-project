"""Complex-task protocol regressions through the genuine child Agent graph.

Transport/executor fixtures are explicit: these tests verify orchestration and
evidence, not the numerical correctness of a model-generated analysis.
"""

import base64
import copy
import hashlib
import json
import socket
import struct
import zlib

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr

from labweaver.analysis import AnalysisRunner
from labweaver.offline import OfflineIntakeModel
from labweaver.runtime.artifacts import validate_payload
from labweaver.tools.csv_profile import load_csv_snapshot


PLAN = [{"id": "table", "kind": "table", "description": "Complete result table"}]
SECRET_SENTINEL = "private-complex-protocol-sentinel-never-in-events"


def code_call(code="print('fixture result')", *, finish_reason="tool_calls"):
    return AIMessage(
        content="",
        tool_calls=[{"name": "execute_python", "args": {"code": code}, "id": "code-complete", "type": "tool_call"}],
        response_metadata={"finish_reason": finish_reason},
    )


def malformed_call(*, truncated=True):
    return AIMessage(
        content="",
        invalid_tool_calls=[{
            "name": "execute_python", "id": "code-truncated",
            "args": '{"code":"' + SECRET_SENTINEL + '\\nfor epoch in range(100):',
            "error": "Invalid JSON", "type": "invalid_tool_call",
        }],
        response_metadata={"finish_reason": "length" if truncated else "tool_calls"},
    )


class ScriptedAnalysisModel(OfflineIntakeModel):
    responses: list[AIMessage] = Field(default_factory=list)
    received: list = Field(default_factory=list)
    _cursor: int = PrivateAttr(default=0)

    def _generate(self, messages, **kwargs):
        self.received.append(copy.deepcopy(messages))
        if self._cursor >= len(self.responses):
            raise AssertionError("Unexpected model request after finite protocol budget")
        result = copy.deepcopy(self.responses[self._cursor])
        self._cursor += 1
        return ChatResult(generations=[ChatGeneration(message=result)])


class RepairAfterMissingModel(OfflineIntakeModel):
    received: list = Field(default_factory=list)

    def _generate(self, messages, **kwargs):
        self.received.append(copy.deepcopy(messages))
        results = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == "execute_python"]
        if not results:
            message = code_call("print('no emit on first attempt')")
        elif results[-1].get("error", {}).get("code") == "missing_deliverables":
            message = code_call("print('corrected complete script')")
            message.tool_calls[0]["id"] = "code-corrected"
        else:
            message = AIMessage(content=json.dumps({
                "status": "completed", "artifact_ids": results[-1]["artifact_ids"], "summary": "Checked actual receipt",
            }))
        return ChatResult(generations=[ChatGeneration(message=message)])


class ReceiptAfterRepairModel(ScriptedAnalysisModel):
    """One malformed response, then a real dispatch, then actual host IDs."""

    def _generate(self, messages, **kwargs):
        if self._cursor < len(self.responses):
            return super()._generate(messages, **kwargs)
        self.received.append(copy.deepcopy(messages))
        actual = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == "execute_python"]
        if not actual:
            raise AssertionError("The valid script was never dispatched")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps({
            "status": "completed", "artifact_ids": actual[-1]["artifact_ids"], "summary": "Real receipt after format repair",
        })))])


class FailingTransportModel(OfflineIntakeModel):
    def _generate(self, messages, **kwargs):
        raise RuntimeError("Transport diagnostics contain " + SECRET_SENTINEL)


class KeepPartialAfterExecutionErrorModel(RepairAfterMissingModel):
    def _generate(self, messages, **kwargs):
        results = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == "execute_python"]
        if results and results[-1].get("error", {}).get("code") == "python_failed":
            self.received.append(copy.deepcopy(messages))
            # A claimed completion must still fail the missing-table check.
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps({
                "status": "completed", "artifact_ids": results[0]["artifact_ids"], "summary": "Partial old charts only",
            })))])
        return super()._generate(messages, **kwargs)


def table_payload():
    return {
        "metadata": {"kind": "table", "deliverable_id": "table", "label": "Result", "row_count": 2, "columns": ["group", "value"]},
        "files": {"table.csv": base64.b64encode(b"group,value\nA,2\nB,4\n").decode()},
    }


def figure_payload(deliverable_id):
    """A real, CRC-valid one-pixel PNG and passive SVG transport fixture."""

    def png_chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n"
           + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
           + png_chunk(b"IDAT", zlib.compress(b"\x00\x12\x34\x56\xff"))
           + png_chunk(b"IEND", b""))
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><rect width="1" height="1" fill="#123456"/></svg>'
    return {
        "metadata": {"kind": "figure", "deliverable_id": deliverable_id, "label": deliverable_id, "row_count": 1, "columns": ["x", "value"]},
        "files": {name: base64.b64encode(raw).decode() for name, raw in {
            "figure.png": png, "figure.svg": svg, "data.csv": b"x,value\n0,1\n",
        }.items()},
    }


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def deny(*args, **kwargs):
        raise AssertionError("Deterministic complex protocol test attempted networking")

    monkeypatch.setattr(socket.socket, "connect", deny)


@pytest.fixture
def runner_factory(tmp_path):
    csv = tmp_path / "sample.csv"
    csv.write_text("group,value\nA,2\nB,4\n", encoding="utf-8")
    snapshot = load_csv_snapshot(csv)

    def make(model, *, first_empty=False, attempt_payloads=None):
        assets, calls, progress = {}, [], []

        class Executor:
            def __init__(self):
                self.received_limits = []

            def reset(self):
                pass

            def execute(self, code, selected, deliverables, **kwargs):
                calls.append(code)
                self.received_limits.append(copy.deepcopy(kwargs))
                record = {
                    "id": f"execution-{len(calls)}", "status": "completed", "exit_code": 0,
                    "elapsed_seconds": .01, "artifact_ids": [], "code": code,
                    "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                    "source": selected.source, "image": "test-transport-only",
                    "image_id": "sha256:" + "a" * 64,
                }
                payload = [] if first_empty and len(calls) == 1 else [table_payload()]
                if attempt_payloads is not None:
                    payload = copy.deepcopy(attempt_payloads[len(calls) - 1])
                if payload == "python_failed":
                    return {**record, "status": "error", "exit_code": 1,
                            "error": {"code": "python_failed"},
                            "stderr": {"text": "Fixture Python error", "truncated": False}}, {}
                registered = validate_payload(payload, deliverables=deliverables, source=selected.source,
                                              execution_id=record["id"], image_id=record["image_id"],
                                              remaining_bytes=kwargs["remaining_bytes"], remaining_figures=kwargs["remaining_figures"])
                record["artifact_ids"] = list(registered)
                return record, registered

        def register(items, operation):
            if operation == "bytes":
                return 50 * 1024 * 1024 - sum(len(raw) for asset in assets.values() for raw in asset["files"].values())
            if operation == "figures":
                return 2 - sum(asset["metadata"]["kind"] == "figure" for asset in assets.values())
            if operation == "replace":
                assert set(items) == {"remove", "add"}
                assert set(items["remove"]).issubset(assets)
                replaced = {key: asset for key, asset in assets.items() if key not in items["remove"]}
                replaced.update(items["add"])
                assets.clear()
                assets.update(replaced)
                return
            assert operation == "add"
            assets.update(items)

        runner = AnalysisRunner(model, Executor(), get_snapshot=lambda: snapshot,
                                register=register, read_asset=assets.__getitem__, progress=progress.append)
        return runner, assets, calls, progress

    return make


def test_exit_zero_without_emit_receives_host_feedback_then_repairs(runner_factory):
    model = RepairAfterMissingModel()
    runner, assets, calls, _ = runner_factory(model, first_empty=True)
    output = runner.delegate("Compute the requested complete result", PLAN, "parent-repair")
    assert output["status"] == "completed", output
    assert len(calls) == 2 and calls[0] != calls[1]
    assert runner.model_calls == 3
    ledger = runner.runs[0]["execution_ledger"]
    missing = next(row["result"] for row in ledger if row["name"] == "execute_python")
    assert missing["status"] == "error"
    assert missing["error"]["code"] == "missing_deliverables"
    assert missing["missing_deliverables"] == ["table"]
    assert missing["exit_code"] == 0
    assert set(output["artifact_ids"]) == set(assets)
    assert all(a["metadata"]["execution_id"] == "execution-2" for a in assets.values())


@pytest.mark.parametrize("truncated,code", [(True, "model_output_truncated"), (False, "invalid_tool_arguments")])
def test_repeated_malformed_model_output_has_specific_error_and_no_execution(runner_factory, truncated, code):
    model = ScriptedAnalysisModel(responses=[malformed_call(truncated=truncated), malformed_call(truncated=truncated)])
    runner, assets, calls, events = runner_factory(model)
    output = runner.delegate("Compute complete result", PLAN, "parent-malformed")
    assert output["status"] == "error" and output["error"]["code"] == code, output
    assert runner.model_calls == 2
    assert not calls and not assets and not runner.attempts
    assert not runner.runs[0]["execution_ledger"]
    assert SECRET_SENTINEL not in json.dumps(events)
    assert not any(e["type"] in {"execution_start", "execution_end"} for e in events)


def test_length_finish_reason_blocks_even_a_parseable_tool_call(runner_factory):
    model = ScriptedAnalysisModel(responses=[code_call(finish_reason="length"), code_call(finish_reason="length")])
    runner, assets, calls, _ = runner_factory(model)
    output = runner.delegate("Compute result", PLAN, "parent-length")
    assert output["error"]["code"] == "model_output_truncated", output
    assert runner.model_calls == 2 and not calls and not assets


def test_one_malformed_response_repairs_without_executing_partial_code(runner_factory):
    model = ReceiptAfterRepairModel(responses=[malformed_call(), code_call()])
    runner, assets, calls, events = runner_factory(model)
    output = runner.delegate("Compute complete result", PLAN, "parent-format-repair")
    assert output["status"] == "completed", output
    assert runner.model_calls == 3 and len(calls) == 1
    assert set(output["artifact_ids"]) == set(assets)
    assert len(runner.runs[0]["execution_ledger"]) == 1
    assert SECRET_SENTINEL not in json.dumps(events)
    model_events = [e for e in events if e["type"] in {"model_start", "model_end", "model_retry"}]
    assert [e["type"] for e in model_events].count("model_start") == 3
    assert [e["type"] for e in model_events].count("model_end") == 3
    assert [e["type"] for e in model_events].count("model_retry") == 1
    assert all(e["role"] == "analysis" for e in model_events)
    assert all(not {"code", "content", "args", "messages", "api_key"}.intersection(e) for e in model_events)


def test_model_transport_error_emits_safe_end_and_no_raw_exception(runner_factory):
    runner, assets, calls, events = runner_factory(FailingTransportModel())
    output = runner.delegate("Compute complete result", PLAN, "parent-transport-failure")
    assert output["error"]["code"] == "analysis_model_failed"
    assert not calls and not assets
    assert SECRET_SENTINEL not in json.dumps({"events": events, "output": output})
    model_events = [event for event in events if event["type"] in {"model_start", "model_end"}]
    assert [event["type"] for event in model_events] == ["model_start", "model_end"]
    assert model_events[-1]["status"] == "error"


CHART_PLAN = [*PLAN,
              {"id": "figure_a", "kind": "figure", "description": "First requested chart"},
              {"id": "figure_b", "kind": "figure", "description": "Second requested chart"}]


def test_partial_two_figures_do_not_consume_repaired_complete_script_budget(runner_factory):
    charts = [figure_payload("figure_a"), figure_payload("figure_b")]
    runner, assets, calls, _ = runner_factory(RepairAfterMissingModel(), attempt_payloads=[charts, [table_payload(), *charts]])
    output = runner.delegate("Deliver both charts and the complete table", CHART_PLAN, "parent-partial-repair")
    assert output["status"] == "completed", output
    assert len(calls) == 2
    assert [limits["remaining_figures"] for limits in runner.executor.received_limits] == [2, 2]
    assert [limits["remaining_bytes"] for limits in runner.executor.received_limits] == [50 * 1024 * 1024] * 2
    assert len(assets) == 3 and set(output["artifact_ids"]) == set(assets)
    assert sum(asset["metadata"]["kind"] == "figure" for asset in assets.values()) == 2
    assert {asset["metadata"]["execution_id"] for asset in assets.values()} == {"execution-2"}
    old_ids = set(runner.attempts[0]["artifact_ids"])
    assert len(old_ids) == 2 and old_ids.isdisjoint(assets)
    assert len(runner.attempts) == 2 and all(record["status"] == "completed" for record in runner.attempts)
    assert runner.runs[0]["execution_ledger"][0]["result"]["missing_deliverables"] == ["table"]


def test_failed_complete_script_keeps_previously_validated_partial_figures(runner_factory):
    charts = [figure_payload("figure_a"), figure_payload("figure_b")]
    runner, assets, calls, _ = runner_factory(KeepPartialAfterExecutionErrorModel(), attempt_payloads=[charts, "python_failed"])
    output = runner.delegate("Deliver both charts and the complete table", CHART_PLAN, "parent-failed-repair")
    assert output["status"] == "error" and output["error"]["code"] == "missing_deliverables", output
    assert output["missing_deliverables"] == ["table"]
    assert len(calls) == 2 and len(assets) == 2
    assert set(output["artifact_ids"]) == set(assets) == set(runner.attempts[0]["artifact_ids"])
    assert {asset["metadata"]["execution_id"] for asset in assets.values()} == {"execution-1"}
    assert runner.attempts[1]["status"] == "error" and runner.attempts[1]["error"]["code"] == "python_failed"


def test_later_delegation_can_repair_partial_outputs_with_same_authorization(runner_factory):
    charts = [figure_payload("figure_a"), figure_payload("figure_b")]
    runner, assets, calls, _ = runner_factory(KeepPartialAfterExecutionErrorModel(),
                                             attempt_payloads=[charts, "python_failed", [table_payload(), *charts]])
    first = runner.delegate("Deliver complete outputs", CHART_PLAN, "parent-incomplete")
    assert first["status"] == "error" and first["missing_deliverables"] == ["table"]
    partial_ids = set(assets)
    runner.model = ReceiptAfterRepairModel(responses=[code_call("complete script from a later delegation")])
    repaired = runner.delegate("Repair all originally authorized outputs", CHART_PLAN, "parent-later-repair")
    assert repaired["status"] == "completed", repaired
    assert len(calls) == 3 and runner.model_calls == 5
    assert runner.executor.received_limits[-1]["remaining_figures"] == 2
    assert runner.executor.received_limits[-1]["remaining_bytes"] == 50 * 1024 * 1024
    assert len(assets) == 3 and set(repaired["artifact_ids"]) == set(assets)
    assert partial_ids.isdisjoint(assets)
    assert {asset["metadata"]["execution_id"] for asset in assets.values()} == {"execution-3"}
    assert [record["status"] for record in runner.attempts] == ["completed", "error", "completed"]
    assert len(runner.runs) == 2 and runner.runs[0]["status"] == "error"


def test_later_delegation_cannot_replace_partial_outputs_outside_its_authorization(runner_factory):
    charts = [figure_payload("figure_a"), figure_payload("figure_b")]
    runner, assets, _, _ = runner_factory(KeepPartialAfterExecutionErrorModel(),
                                         attempt_payloads=[charts, "python_failed", [table_payload()]])
    first = runner.delegate("Deliver all outputs", CHART_PLAN, "parent-partial-original")
    assert first["status"] == "error"
    old_ids = set(assets)
    old_bytes = sum(len(raw) for asset in assets.values() for raw in asset["files"].values())
    runner.model = ReceiptAfterRepairModel(responses=[code_call("compute the authorized table only")])
    result = runner.delegate("Compute only the authorized table", PLAN, "parent-different-authorization")
    assert result["status"] == "completed", result
    assert runner.executor.received_limits[-1]["remaining_figures"] == 0
    assert runner.executor.received_limits[-1]["remaining_bytes"] == 50 * 1024 * 1024 - old_bytes
    assert old_ids.issubset(assets)
    assert set(result["artifact_ids"]).isdisjoint(old_ids)
    assert len(assets) == 3


def test_model_deadline_uses_elapsed_wall_time_not_recent_tokens():
    from labweaver.web.deadlines import RunDeadline

    current = [0.0]
    deadline = RunDeadline(model_timeout=5, task_timeout=30, clock=lambda: current[0])
    deadline.observe("model_start", {"role": "analysis", "call": 1})
    for stamp in (1, 3, 4.9):
        current[0] = stamp
        deadline.observe("heartbeat", {"role": "analysis"})
        deadline.observe("answer_delta", {"id": "draft", "text": "Still streaming"})
        assert deadline.error() is None
    current[0] = 5.1
    assert deadline.error()["code"] == "model_request_timeout"


def test_unmatched_model_end_cannot_release_another_role_deadline():
    from labweaver.web.deadlines import RunDeadline

    current = [0.0]
    deadline = RunDeadline(model_timeout=5, task_timeout=30, clock=lambda: current[0])
    deadline.observe("model_start", {"role": "analysis", "call": 1})
    current[0] = 4.0
    deadline.observe("model_end", {"role": "main", "call": 1, "status": "completed"})
    current[0] = 5.1
    assert deadline.error()["code"] == "model_request_timeout"


def test_task_deadline_does_not_reset_between_model_calls():
    from labweaver.web.deadlines import RunDeadline

    current = [0.0]
    deadline = RunDeadline(model_timeout=5, task_timeout=10, clock=lambda: current[0])
    for call, start, end in ((1, 0, 2), (2, 3, 5), (3, 7, 9)):
        current[0] = start
        deadline.observe("model_start", {"role": "main", "call": call})
        current[0] = end
        deadline.observe("model_end", {"role": "main", "call": call, "status": "completed"})
        assert deadline.error() is None
    current[0] = 10.1
    assert deadline.error()["code"] == "task_timeout"


def test_awaiting_user_time_does_not_consume_running_task_budget():
    from labweaver.web.deadlines import RunDeadline

    current = [0.0]
    deadline = RunDeadline(model_timeout=5, task_timeout=10, clock=lambda: current[0])
    current[0] = 3.0
    deadline.pause()
    current[0] = 1003.0
    assert deadline.error() is None
    deadline.resume()
    current[0] = 1009.0
    assert deadline.error() is None
    current[0] = 1011.0
    assert deadline.error()["code"] == "task_timeout"
