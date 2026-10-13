"""Chat metadata and actual removal of owned files; no simulated code analysis.

The overview export uses the real Agent/tool graph. Other fixtures exercise only
storage, file ownership, runtime supervision and failure recovery.
"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from labweaver.agent import create_session
from labweaver.offline import OfflineIntakeModel
from labweaver.web.conversations import ConversationService
from labweaver.web.manager import TaskManager
from labweaver.web.security import AppError, digest
from labweaver.web.settings import WebSettings
from labweaver.web.store import Store, encode, now, uid


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    item = TaskManager(WebSettings(data_dir=tmp_path / "application", project_dir=Path.cwd(),
                                  mode="offline", token="chat-management-test-token", cancel_grace=.01))
    yield item
    item.active = None  # Storage-only active fixtures never represent real processes.
    item.close()


def bound(manager, title="新对话"):
    conversation = manager.store.new_conversation(title)
    file = manager.upload(conversation["id"], "实际数据.csv", "编号,数量\n001,2\n002,3\n".encode())
    return conversation, file


def insert_run(manager, conversation, *, status="completed", core=None):
    run_id, timestamp = uid(), now()
    with manager.store.connect() as db:
        db.execute("INSERT INTO runs(id,conversation_id,request_id,task,status,created_at,updated_at,core_session_id,core_task_id) VALUES (?,?,?,?,?,?,?,?,?)",
                   (run_id, conversation["id"], uid(), "概览数据", status, timestamp, timestamp,
                    (core or {}).get("session_id"), (core or {}).get("task_id")))
    return run_id


def generated_file(manager, conversation, run_id, name="attempt.py", raw=b"# storage fixture only\n"):
    run = manager.store.require("runs", run_id)
    relative = f"exports/{run['core_session_id']}/{run['core_task_id']}/{name}"
    path = manager.store.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    file = dict(id=uid(), conversation_id=conversation["id"], run_id=run_id, name=name,
                relative_path=relative, media_type="text/x-python", size=len(raw), sha256=digest(raw),
                preview_json=None, parse_error_json=None, category="generated")
    with manager.store.connect() as db:
        db.execute("INSERT INTO files VALUES (:id,:conversation_id,:run_id,:name,:relative_path,:media_type,:size,:sha256,:preview_json,:parse_error_json,:category)", file)
    return file


def overview(manager, conversation, file):
    registered = manager.store.require("files", file["id"])
    session = create_session(manager.store.root / registered["relative_path"], OfflineIntakeModel())
    report = session.invoke("概览字段与缺失情况")
    assert report["status"] == "completed" and report["profile_result"]["row_count"] == 2
    assert report["code_executions"] == []  # Genuine overview, not Docker analysis acceptance.
    saved = session.save(manager.store.root / "exports")
    run_id = insert_run(manager, conversation, status="running")
    manager.active = run_id
    manager._register_report(run_id, saved)
    return run_id, saved


def fake_idle_runtime(conversation_id, *, survives=False):
    runtime = SimpleNamespace(conversation_id=conversation_id, cancel_signal=threading.Event(), stopped=False)
    runtime.process = SimpleNamespace(is_alive=lambda: not runtime.stopped or survives)

    def stop(*, force=False):
        assert force is True
        runtime.stopped = True

    runtime.stop = stop
    return runtime


def assert_error(code, operation, status=None):
    with pytest.raises(AppError) as caught:
        operation()
    assert caught.value.code == code
    if status is not None:
        assert caught.value.status == status


def test_rename_pin_order_and_reopen_persist_without_changing_inputs(manager):
    service = ConversationService(manager)
    first, file = bound(manager, "先创建")
    second = manager.store.new_conversation("后创建")
    before = manager.store.require("files", file["id"])
    changed = service.patch(first["id"], {"title": "  实验汇总  ", "pinned": True})
    assert changed["title"] == "实验汇总" and changed["pinned"] is True
    assert changed["active_run_id"] is None and changed["deletion_pending"] is False
    assert manager.store.conversations()[0]["id"] == first["id"]
    assert manager.store.require("files", file["id"]) == before
    reopened = Store(manager.store.root)
    assert reopened.require("conversations", first["id"])["pinned"] is True
    service.patch(first["id"], {"pinned": False})
    with manager.store.connect() as db:
        db.execute("UPDATE conversations SET updated_at='2099-01-01' WHERE id=?", (second["id"],))
    assert manager.store.conversations()[0]["id"] == second["id"]


@pytest.mark.parametrize("payload,code", [
    ({}, "invalid_conversation"), ({"unrecognized": True}, "invalid_conversation"),
    ({"title": " "}, "invalid_title"), ({"title": "x" * 121}, "invalid_title"),
    ({"title": "换\n行"}, "invalid_title"), ({"title": "nul\0"}, "invalid_title"),
    ({"title": "delete\x7f"}, "invalid_title"), ({"title": 3}, "invalid_title"),
    ({"pinned": 1}, "invalid_pinned"), ({"pinned": "true"}, "invalid_pinned"),
    ({"title": "chat-management-test-token"}, "secret_in_input"),
])
def test_invalid_patch_never_persists(manager, payload, code):
    conversation = manager.store.new_conversation("保留原标题")
    assert_error(code, lambda: ConversationService(manager).patch(conversation["id"], payload))
    assert manager.store.require("conversations", conversation["id"])["title"] == "保留原标题"


def test_v2_migration_preserves_existing_messages_models_file_and_app_id(tmp_path):
    root = tmp_path / "old-application"
    root.mkdir()
    conversation_id, message_id, app_id = uid(), uid(), uid()
    migrations = Path(__file__).parents[1] / "src/labweaver/web/migrations"
    with sqlite3.connect(root / "history.sqlite3") as db:
        db.executescript((migrations / "001.sql").read_text(encoding="utf-8"))
        db.executescript((migrations / "002.sql").read_text(encoding="utf-8"))
        db.execute("INSERT INTO conversations VALUES (?,?,?,?,?)", (conversation_id, "已有历史", now(), now(), 1))
        db.execute("INSERT INTO messages(id,conversation_id,role,content,state,created_at) VALUES (?,?,?,?,?,?)",
                   (message_id, conversation_id, "user", "原任务", "sent", now()))
        db.execute("INSERT INTO conversation_models VALUES (?,?)", (conversation_id, '{"provider_id":"deepseek"}'))
        db.execute("INSERT INTO app_meta VALUES ('app_id',?)", (app_id,))
    unrelated = root / "preexisting.txt"
    unrelated.write_bytes(b"unchanged")
    store = Store(root)
    assert store.app_id == app_id and store.require("conversations", conversation_id)["pinned"] is False
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT content FROM messages WHERE id=?", (message_id,)).fetchone()[0] == "原任务"
        assert db.execute("SELECT selection_json FROM conversation_models").fetchone()[0] == '{"provider_id":"deepseek"}'
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert unrelated.read_bytes() == b"unchanged"


def test_real_overview_and_orphan_exports_are_removed_original_and_other_chat_kept(manager, tmp_path):
    service = ConversationService(manager)
    original = tmp_path / "用户原始文件.csv"
    original.write_bytes("编号,数量\n001,2\n002,3\n".encode())
    original_hash = digest(original.read_bytes())
    conversation = manager.store.new_conversation()
    file = manager.upload(conversation["id"], original.name, original.read_bytes())
    run_id, saved = overview(manager, conversation, file)
    owned = Path(saved["record_path"]).parent
    (owned / "unregistered-attempt.py").write_text("# private interrupted attempt\n", encoding="utf-8")
    (owned / "orphan.json").write_text('{"private": true}', encoding="utf-8")
    runtime = fake_idle_runtime(conversation["id"])
    manager.runtime = runtime
    second, other_file = bound(manager, "保留另一对话")
    other_record = manager.store.require("files", other_file["id"])
    with manager.store.connect() as db:
        downloads = [r[0] for r in db.execute("SELECT id FROM files WHERE run_id=?", (run_id,))]
        db.execute("INSERT INTO conversation_models VALUES (?,?)", (conversation["id"], "{}"))
        db.execute("INSERT INTO run_models VALUES (?,?)", (run_id, "{}"))
    result = service.delete(conversation["id"], confirm=True)
    assert result == {"deleted": True, "conversation_id": conversation["id"]}
    assert runtime.stopped and runtime.cancel_signal.is_set() and manager.runtime is None
    assert not owned.exists() and not (manager.store.root / "attachments" / file["id"]).exists()
    assert digest(original.read_bytes()) == original_hash
    assert (manager.store.root / other_record["relative_path"]).read_bytes() == original.read_bytes()
    assert manager.store.require("conversations", second["id"])["title"] == "保留另一对话"
    for table in ("conversations", "runs", "files"):
        assert_error("not_found", lambda table=table: manager.store.require(table, {"conversations": conversation["id"], "runs": run_id, "files": file["id"]}[table]), 404)
    for download_id in downloads:
        assert_error("not_found", lambda: manager.store.require("files", download_id), 404)
    with manager.store.connect() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        for table in ("messages", "runs", "executions", "artifacts", "artifact_files", "run_events", "conversation_models", "run_models", "conversation_deletions"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_registered_code_artifact_rows_and_unregistered_attempt_are_removed(manager):
    conversation, _ = bound(manager)
    run_id = insert_run(manager, conversation, core={"session_id": uid(), "task_id": uid()})
    file = generated_file(manager, conversation, run_id)
    path = manager.store.root / file["relative_path"]
    (path.parent / "unregistered.log").write_text("private log", encoding="utf-8")
    with manager.store.connect() as db:
        artifact_id = uid()
        db.execute("INSERT INTO executions VALUES (?,?,?,?,?)", (uid(), run_id, 1, file["id"], "{}"))
        db.execute("INSERT INTO artifacts VALUES (?,?,?,?)", (artifact_id, run_id, "test-transport-artifact", "{}"))
        db.execute("INSERT INTO artifact_files VALUES (?,?,?)", (artifact_id, file["id"], "code"))
    assert ConversationService(manager).delete(conversation["id"], confirm=True)["deleted"]
    assert not path.parent.exists()
    with manager.store.connect() as db:
        for table in ("files", "executions", "artifacts", "artifact_files"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("status", ["running", "awaiting_input", "cancelling"])
def test_active_chat_deletion_denied_and_list_identifies_run(manager, status):
    conversation, file = bound(manager)
    run_id = insert_run(manager, conversation, status=status)
    assert manager.store.conversations()[0]["active_run_id"] == run_id
    path = manager.store.root / manager.store.require("files", file["id"])["relative_path"]
    assert_error("task_active", lambda: ConversationService(manager).delete(conversation["id"], confirm=True), 409)
    assert path.is_file()


def test_other_chat_active_runtime_is_never_stopped(manager):
    target, file = bound(manager, "可删除")
    other, _ = bound(manager, "正在运行")
    manager.active = insert_run(manager, other, status="awaiting_input")
    runtime = fake_idle_runtime(other["id"])
    manager.runtime = runtime
    assert ConversationService(manager).delete(target["id"], confirm=True)["deleted"]
    assert manager.active and manager.runtime is runtime and not runtime.stopped
    assert manager.store.require("conversations", other["id"])
    assert_error("not_found", lambda: manager.store.require("files", file["id"]))
    manager.runtime = None  # The double is never a real active process.


def test_idle_runtime_that_did_not_stop_blocks_all_file_removal(manager):
    conversation, file = bound(manager)
    runtime = fake_idle_runtime(conversation["id"], survives=True)
    manager.runtime = runtime
    assert_error("conversation_runtime_stop_failed", lambda: ConversationService(manager).delete(conversation["id"], confirm=True), 503)
    assert (manager.store.root / manager.store.require("files", file["id"])["relative_path"]).is_file()
    manager.runtime = None


def test_shared_core_task_folder_is_rejected_but_different_task_sibling_is_kept(manager):
    service = ConversationService(manager)
    first, _ = bound(manager)
    second, _ = bound(manager)
    session, task = uid(), uid()
    first_run = insert_run(manager, first, core={"session_id": session, "task_id": task})
    second_run = insert_run(manager, second, core={"session_id": session, "task_id": task})
    first_file = generated_file(manager, first, first_run)
    first_path = manager.store.root / first_file["relative_path"]
    assert_error("conversation_file_conflict", lambda: service.delete(first["id"], confirm=True), 409)
    assert first_path.is_file()
    with manager.store.connect() as db:
        db.execute("UPDATE runs SET core_task_id=? WHERE id=?", (uid(), second_run))
    second_file = generated_file(manager, second, second_run)
    second_path = manager.store.root / second_file["relative_path"]
    assert service.delete(first["id"], confirm=True)["deleted"]
    assert not first_path.parent.exists() and second_path.is_file()


@pytest.mark.parametrize("relationship", ["attachment", "artifact", "code", "file_run", "message"])
def test_cross_chat_relationship_denied_before_removing_any_file(manager, relationship):
    target, target_file = bound(manager)
    other, other_file = bound(manager)
    target_run = insert_run(manager, target, core={"session_id": uid(), "task_id": uid()})
    other_run = insert_run(manager, other, core={"session_id": uid(), "task_id": uid()})
    with manager.store.connect() as db:
        if relationship == "attachment":
            db.execute("DELETE FROM conversation_files WHERE file_id=?", (target_file["id"],))
            db.execute("INSERT INTO conversation_files VALUES (?,?,?,?)", (other["id"], target_file["id"], "material", 1))
        elif relationship == "artifact":
            artifact_id = uid()
            db.execute("INSERT INTO artifacts VALUES (?,?,?,?)", (artifact_id, other_run, "foreign", "{}"))
            db.execute("INSERT INTO artifact_files VALUES (?,?,?)", (artifact_id, target_file["id"], "data"))
        elif relationship == "code":
            db.execute("INSERT INTO executions VALUES (?,?,?,?,?)", (uid(), other_run, 1, target_file["id"], "{}"))
        elif relationship == "file_run":
            db.execute("UPDATE files SET run_id=? WHERE id=?", (target_run, other_file["id"]))
        else:
            db.execute("INSERT INTO messages(id,conversation_id,run_id,role,content,state,created_at) VALUES (?,?,?,?,?,?,?)",
                       (uid(), other["id"], target_run, "assistant", "foreign reference", "validated", now()))
    path = manager.store.root / manager.store.require("files", target_file["id"])["relative_path"]
    assert_error("conversation_file_conflict", lambda: ConversationService(manager).delete(target["id"], confirm=True), 409)
    assert path.is_file() and manager.store.require("conversations", target["id"])


@pytest.mark.parametrize("component", ["../foreign", "/absolute", "x\\y", "", "."])
def test_illegal_core_folder_is_denied_before_file_removal(manager, component):
    conversation, file = bound(manager)
    insert_run(manager, conversation, core={"session_id": component, "task_id": uid()})
    assert_error("conversation_file_conflict", lambda: ConversationService(manager).delete(conversation["id"], confirm=True), 409)
    assert (manager.store.root / manager.store.require("files", file["id"])["relative_path"]).is_file()


def test_nested_windows_reparse_point_is_rejected_before_staging(manager, monkeypatch):
    conversation, file = bound(manager)
    path = manager.store.root / manager.store.require("files", file["id"])["relative_path"]
    unsafe = path.parent / "unexpected-link"
    unsafe.mkdir()
    original = Path.lstat

    def marked(candidate, *args, **kwargs):
        info = original(candidate, *args, **kwargs)
        if candidate != unsafe:
            return info
        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)

    monkeypatch.setattr(Path, "lstat", marked)
    assert_error("unsafe_path", lambda: ConversationService(manager).delete(conversation["id"], confirm=True), 403)
    assert path.is_file()
    with manager.store.connect() as db:
        assert db.execute("SELECT count(*) FROM conversation_deletions").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["filesystem", "database"])
def test_failed_delete_retains_pending_journal_and_truthful_retry(manager, monkeypatch, stage):
    service = ConversationService(manager)
    conversation, file = bound(manager)
    path = manager.store.root / manager.store.require("files", file["id"])["relative_path"]
    method = "_purge_tree" if stage == "filesystem" else "_delete_rows"
    original = getattr(service, method)

    def fail(*args, **kwargs):
        raise PermissionError("injected locked file") if stage == "filesystem" else sqlite3.OperationalError("injected commit failure")

    monkeypatch.setattr(service, method, fail)
    assert_error("conversation_delete_failed", lambda: service.delete(conversation["id"], confirm=True), 503)
    listed = manager.store.conversations()[0]
    assert listed["deletion_pending"] is True
    assert_error("conversation_deletion_pending", lambda: manager.start(conversation["id"], "不能重新运行", uid()), 409)
    assert_error("conversation_deletion_pending", lambda: service.patch(conversation["id"], {"title": "不能假装仍可使用"}), 409)
    assert not path.exists()  # Quarantined first, even when actual cleanup fails.
    with manager.store.connect() as db:
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 1
        job = dict(db.execute("SELECT * FROM conversation_deletions").fetchone())
    quarantine = manager.store.root / json.loads(job["manifest_json"])[0]["quarantine"]
    assert quarantine.exists() is (stage == "filesystem")
    monkeypatch.setattr(service, method, original)
    assert service.delete(conversation["id"], confirm=True)["deleted"]
    assert not quarantine.exists() and not manager.store.conversations()


def test_unrelated_chat_remains_responsive_while_deletion_purges(manager, monkeypatch):
    service = ConversationService(manager)
    target, _ = bound(manager)
    other = manager.store.new_conversation("不阻塞")
    entered, release = threading.Event(), threading.Event()
    original = service._purge_tree

    def paused(relative):
        entered.set()
        assert release.wait(5)
        original(relative)

    monkeypatch.setattr(service, "_purge_tree", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        deletion = pool.submit(service.delete, target["id"], confirm=True)
        try:
            assert entered.wait(5)
            assert_error("conversation_delete_in_progress", lambda: service.delete(target["id"], confirm=True), 409)
            rename = pool.submit(service.patch, other["id"], {"title": "正在运行另一个对话"})
            assert rename.result(timeout=2)["title"] == "正在运行另一个对话"
        finally:
            release.set()
        assert deletion.result(timeout=5)["deleted"]


def test_missing_owned_file_allows_metadata_cleanup(manager):
    conversation, file = bound(manager)
    (manager.store.root / manager.store.require("files", file["id"])["relative_path"]).unlink()
    assert ConversationService(manager).delete(conversation["id"], confirm=True)["deleted"]


def test_inconsistent_report_path_is_not_ignored_during_permanent_deletion(manager):
    conversation, file = bound(manager)
    run_id = insert_run(manager, conversation, core={"session_id": uid(), "task_id": uid()})
    with manager.store.connect() as db:
        db.execute("UPDATE runs SET report_path='exports/foreign/task/private.json' WHERE id=?", (run_id,))
    assert_error("conversation_file_conflict", lambda: ConversationService(manager).delete(conversation["id"], confirm=True), 409)
    assert (manager.store.root / manager.store.require("files", file["id"])["relative_path"]).is_file()


def test_failed_initial_journal_write_leaves_source_and_metadata_untouched(manager, monkeypatch):
    service = ConversationService(manager)
    conversation, file = bound(manager)
    path = manager.store.root / manager.store.require("files", file["id"])["relative_path"]
    original = manager.store.connect
    count = 0

    # The first connection authorizes ownership; the second must commit a durable
    # journal before any directory may move.
    def fail_registration():
        nonlocal count
        count += 1
        if count == 2:
            raise sqlite3.OperationalError("injected unavailable journal database")
        return original()

    monkeypatch.setattr(manager.store, "connect", fail_registration)
    assert_error("conversation_delete_failed", lambda: service.delete(conversation["id"], confirm=True), 503)
    monkeypatch.setattr(manager.store, "connect", original)
    assert path.is_file() and not manager.store.conversations()[0]["deletion_pending"]


@pytest.mark.parametrize("confirm", [False, None, 1, "true"])
def test_permanent_deletion_requires_explicit_boolean_confirmation(manager, confirm):
    conversation, file = bound(manager)
    assert_error("delete_confirmation_required", lambda: ConversationService(manager).delete(conversation["id"], confirm=confirm))
    assert manager.store.require("files", file["id"])


def test_chat_api_contract_auth_validation_and_registered_download_404(manager):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from labweaver.web.api import create_app

    with TestClient(create_app(manager.settings, manager=manager), base_url="http://127.0.0.1",
                    headers={"Authorization": "Bearer " + manager.settings.token}) as client:
        conversation, file = bound(manager)
        endpoint = f"/api/v1/conversations/{conversation['id']}"
        assert client.patch(endpoint, json={"title": "改名", "pinned": True}).json()["pinned"] is True
        assert client.get("/api/v1/conversations").json()["conversations"][0]["title"] == "改名"
        assert client.patch(endpoint, json={"pinned": 1}).status_code == 422
        assert client.patch(endpoint, json={"title": "x" * 121}).status_code == 422
        assert client.request("DELETE", endpoint, json={"confirm": False}).status_code == 400
        assert client.request("DELETE", endpoint, json={"confirm": "true"}).status_code == 422
        assert client.request("DELETE", endpoint, json={"confirm": True}, headers={"Authorization": ""}).status_code == 401
        assert client.request("DELETE", endpoint, json={"confirm": True}, headers={"Origin": "https://evil.invalid"}).status_code == 403
        assert client.request("DELETE", endpoint, json={"confirm": True}).json() == {"deleted": True, "conversation_id": conversation["id"]}
        assert client.get(endpoint).status_code == 404
        assert client.get(f"/api/v1/files/{file['id']}/content").status_code == 404


def test_chat_api_active_run_deletion_409_and_list_state(manager):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from labweaver.web.api import create_app

    with TestClient(create_app(manager.settings, manager=manager), base_url="http://127.0.0.1",
                    headers={"Authorization": "Bearer " + manager.settings.token}) as client:
        conversation, _ = bound(manager)
        run_id = insert_run(manager, conversation, status="awaiting_input")
        assert client.get("/api/v1/conversations").json()["conversations"][0]["active_run_id"] == run_id
        response = client.request("DELETE", f"/api/v1/conversations/{conversation['id']}", json={"confirm": True})
        assert response.status_code == 409 and response.json()["error"]["code"] == "task_active"
