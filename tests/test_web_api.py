"""HTTP/worker integration uses genuine Agent graphs; hanging/crashing workers test supervision only."""

import json
import os
from pathlib import Path
import time

import pytest
pytest.importorskip("fastapi", reason="Optional browser application requires the web extra")
from fastapi.testclient import TestClient
from labweaver.web.api import create_app
from labweaver.web.manager import TaskManager
from labweaver.web.settings import WebSettings
from labweaver.web.store import uid
from labweaver.web.worker import agent_worker

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "private-test-application-token-438195"


def clarification_worker(*args):
    from test_agent import ScriptedModel, answer, call
    import labweaver.offline
    labweaver.offline.OfflineIntakeModel = lambda: ScriptedModel(responses=[call(), call("ask_user", "question1", {"question": "请确认统计范围。"}), answer()])
    agent_worker(*args)


def blocked_worker(settings, app_id, csv, materials, context, commands, events, cancel):
    command = commands.get()
    events.put({"run_id": command["run_id"], "kind": "tool_start", "payload": {"name": "blocked_model", "id": "call1"}})
    time.sleep(120)  # Deliberately ignores cooperative stop, like a blocked native transport.


def crash_worker(settings, app_id, csv, materials, context, commands, events, cancel):
    commands.get()
    os._exit(7)


def settings(tmp_path):
    return WebSettings(data_dir=tmp_path / "app", token=TOKEN, project_dir=ROOT, mode="offline",
                       frontend_dir=ROOT / "frontend/dist", cancel_grace=0.15)


def client_for(tmp_path, target=agent_worker):
    config = settings(tmp_path)
    manager = TaskManager(config, worker_target=target)
    return TestClient(create_app(config, manager=manager), base_url="http://127.0.0.1", headers={"Authorization": "Bearer " + TOKEN}), manager


def prepared(client):
    conversation = client.post("/api/v1/conversations", json={}).json()["id"]
    uploaded = client.post(f"/api/v1/conversations/{conversation}/files", files={"file": ("中文数据.csv", b"id,score\n001,2\n002,4\n003,\n", "text/csv")})
    assert uploaded.status_code == 201, uploaded.text
    return conversation, uploaded.json()


def wait_run(client, run_id, expected=None, timeout=50):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] not in {"running", "cancelling"}:
            if expected:
                assert result["status"] == expected, result
            return result
        time.sleep(.08)
    raise AssertionError("Worker did not reach expected state")


def test_auth_host_origin_and_no_query_tokens(tmp_path):
    client, _ = client_for(tmp_path)
    with client:
        assert client.get("/api/v1/conversations", headers={"Authorization": ""}).status_code == 401
        assert client.get("/api/v1/conversations", headers={"Origin": "https://evil.invalid"}).status_code == 403
        assert client.get("/api/v1/conversations", headers={"Host": "evil.invalid"}).status_code == 403
        assert client.get("/api/v1/conversations", headers={"Host": "testserver"}).status_code == 403
        assert client.get("/api/v1/conversations?token=anything").status_code == 400
        assert client.get("/api/v1/conversations").status_code == 200
        invalid = client.post("/api/v1/conversations", json={"extra": TOKEN})
        assert invalid.status_code == 422 and TOKEN not in invalid.text


def test_real_agent_overview_replay_events_freeze_and_restart(tmp_path):
    client, manager = client_for(tmp_path)
    with client:
        conversation, uploaded = prepared(client)
        assert uploaded["preview"]["rows"][0][0] == "001"
        request = {"task": "概览字段与缺失情况", "request_id": uid()}
        run = client.post(f"/api/v1/conversations/{conversation}/runs", json=request).json()
        duplicate = client.post(f"/api/v1/conversations/{conversation}/runs", json=request).json()
        assert duplicate["id"] == run["id"]
        done = wait_run(client, run["id"], "completed")
        assert done["model_calls"] > 0 and done["downloads"]
        assert any(e["type"] == "tool_end" and e["payload"]["name"] == "profile_csv" for e in done["events"])
        evidence = client.get(f"/api/v1/files/{next(f['id'] for f in done['downloads'] if f['name'].endswith('.json'))}/content")
        assert evidence.json()["profile_result"]["row_count"] == 3
        stream = client.get(f"/api/v1/runs/{run['id']}/events?after=1").text
        assert f'"seq":1,' not in stream and '"type":"completed"' in stream.replace(" ", "")
        assert client.delete(f"/api/v1/conversations/{conversation}/files/{uploaded['id']}").status_code == 409
        # A follow-up in the same conversation uses the same in-memory session, with a new task budget.
        again = client.post(f"/api/v1/conversations/{conversation}/runs", json={"task": "概览字段", "request_id": uid()}).json()
        second = wait_run(client, again["id"], "completed")
        assert second["core_session_id"] == done["core_session_id"]
        assert second["core_task_id"] != done["core_task_id"]
    reopened, manager2 = client_for(tmp_path)
    with reopened:
        history = reopened.get(f"/api/v1/conversations/{conversation}").json()
        assert len(history["runs"]) == 2 and history["messages"]
        new = reopened.post(f"/api/v1/conversations/{conversation}/runs", json={"task": "概览字段", "request_id": uid()}).json()
        third = wait_run(reopened, new["id"], "completed")
        assert third["core_session_id"] != second["core_session_id"]
        assert third["history_run_ids"] == [second["id"], done["id"]]


def test_real_interrupt_idempotent_resume_and_global_active_slot(tmp_path):
    client, _ = client_for(tmp_path, clarification_worker)
    with client:
        conversation, _ = prepared(client)
        run = client.post(f"/api/v1/conversations/{conversation}/runs", json={"task": "概览数据", "request_id": uid()}).json()
        waiting = wait_run(client, run["id"], "awaiting_input")
        question = waiting["pending_input"]
        assert question["id"] and question["question"] == "请确认统计范围。"
        blocked = client.post(f"/api/v1/conversations/{conversation}/runs", json={"task": "其他任务", "request_id": uid()})
        assert blocked.status_code == 409
        reply = {"reply": "全部数据", "question_id": question["id"], "request_id": uid()}
        assert client.post(f"/api/v1/runs/{run['id']}/reply", json=reply).status_code == 202
        assert client.post(f"/api/v1/runs/{run['id']}/reply", json=reply).status_code == 202
        done = wait_run(client, run["id"], "completed")
        assert done["model_calls"] == 3
        assert client.post(f"/api/v1/runs/{run['id']}/reply", json=reply | {"request_id": uid()}).status_code == 409


def test_forced_stop_terminates_owned_worker(tmp_path):
    client, manager = client_for(tmp_path, blocked_worker)
    with client:
        conversation, _ = prepared(client)
        run = client.post(f"/api/v1/conversations/{conversation}/runs", json={"task": "概览", "request_id": uid()}).json()
        process = manager.runtime.process
        response = client.post(f"/api/v1/runs/{run['id']}/cancel")
        assert response.status_code == 202
        assert client.post(f"/api/v1/runs/{run['id']}/cancel").status_code == 202
        done = wait_run(client, run["id"], "cancelled")
        assert done["error"]["cleanup_confirmed"] and not process.is_alive()
        assert manager.active is None


def test_worker_crash_and_single_application_lock(tmp_path):
    client, manager = client_for(tmp_path, crash_worker)
    with client:
        from labweaver.web.security import AppError
        with pytest.raises(AppError, match="already_running"):
            TaskManager(settings(tmp_path))
        conversation, _ = prepared(client)
        run = client.post(f"/api/v1/conversations/{conversation}/runs", json={"task": "概览", "request_id": uid()}).json()
        done = wait_run(client, run["id"], "error")
        assert done["error"]["code"] == "worker_crashed" and manager.active is None


def test_save_failure_never_completed_and_secret_never_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "SECRET-CANARY-KEY-3948735639")
    client, manager = client_for(tmp_path)
    with client:
        conversation, _ = prepared(client)
        rejected = client.post(f"/api/v1/conversations/{conversation}/runs", json={"task": "SECRET-CANARY-KEY-3948735639", "request_id": uid()})
        assert rejected.status_code == 400
        monkeypatch.setattr(manager, "_register_report", lambda *a, **k: (_ for _ in ()).throw(OSError("disk failure")))
        run = client.post(f"/api/v1/conversations/{conversation}/runs", json={"task": "概览字段", "request_id": uid()}).json()
        failed = wait_run(client, run["id"], "error")
        assert failed["error"]["code"] == "save_failed"
        assert not any(e["type"] == "completed" for e in failed["events"])
        assert "SECRET-CANARY" not in json.dumps(client.get(f"/api/v1/conversations/{conversation}").json())
        assert b"SECRET-CANARY" not in manager.store.path.read_bytes()


def test_decoded_utf16_key_cannot_enter_preview_or_history(tmp_path, monkeypatch):
    canary = "ENCODED-SECRET-CANARY-4318534627"
    monkeypatch.setenv("LLM_API_KEY", canary)
    client, manager = client_for(tmp_path)
    with client:
        conversation = client.post("/api/v1/conversations", json={}).json()["id"]
        raw = ("label\n" + canary + "\n").encode("utf-16")
        assert canary.encode() not in raw
        response = client.post(f"/api/v1/conversations/{conversation}/files", files={"file": ("utf16.csv", raw)})
        assert response.status_code == 400 and canary not in response.text
        assert not client.get(f"/api/v1/conversations/{conversation}").json()["files"]
        assert not list((manager.store.root / "attachments").rglob("*.csv"))


def test_bound_materials_real_search_and_pure_text_preview(tmp_path):
    client, _ = client_for(tmp_path)
    with client:
        conversation, _ = prepared(client)
        text = "# score 变量说明\nscore 是问卷评分，范围 1 到 5。必须根据实际缺失情况说明可用数据。\n"
        uploaded = client.post(f"/api/v1/conversations/{conversation}/files", files={"file": ("变量说明.md", text.encode())})
        assert uploaded.status_code == 201 and "score" in uploaded.json()["preview"]["text"]
        run = client.post(f"/api/v1/conversations/{conversation}/runs", json={"task": "基于资料说明，概览字段与缺失情况", "request_id": uid()}).json()
        done = wait_run(client, run["id"], "completed")
        retrieval = [e for e in done["events"] if e["type"] == "retrieval_result"]
        assert len(retrieval) == 1 and retrieval[0]["payload"]["matches"]
        assert "[D1-C" in done["final_answer"]
