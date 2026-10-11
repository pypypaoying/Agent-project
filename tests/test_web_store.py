"""Persistent application metadata and immutable imports; no model or fake execution."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3

import pytest

from labweaver.web.context import history_context
from labweaver.web.files import import_file, read_registered
from labweaver.web.security import AppError, digest
from labweaver.web.store import Store, encode, now, uid


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "application")


def insert_run(store, conversation, *, status="running", request="request-one",
               task="统计当前数据", report=None, created=None):
    """Create storage fixtures only; a completed fixture is not execution acceptance."""
    run_id = uid()
    with store.connect() as db:
        db.execute(
            "INSERT INTO runs (id,conversation_id,request_id,task,status,created_at,updated_at,report_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, conversation["id"], request, task, status, created or now(), now(),
             encode(report) if report is not None else None),
        )
    return run_id


def history_report(file, *, answer="总量为 5", code="result = 2 + 3\n"):
    return {
        "source": {"name": file["name"], "sha256": file["sha256"], "size_bytes": file["size"]},
        "final_answer": answer,
        "replies": ["按全部记录计算"],
        "artifacts": [{"id": "old-artifact", "kind": "metric", "label": "总量", "value": 5,
                       "deliverable_id": "old-deliverable"}],
        "code_executions": [{"status": "completed", "code": code,
                             "code_sha256": digest(code.encode())}],
    }


def test_initial_migration_is_persistent_and_uses_wal_foreign_keys(store):
    conversation = store.new_conversation("  实际分析  ")
    reopened = Store(store.root)
    assert reopened.app_id == store.app_id
    assert reopened.require("conversations", conversation["id"])["title"] == "实际分析"
    with reopened.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unknown_schema_is_rejected_without_resetting_user_history(store):
    conversation = store.new_conversation("保留历史")
    with store.connect() as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(AppError) as error:
        Store(store.root)
    assert error.value.code == "schema_version"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT title FROM conversations WHERE id=?", (conversation["id"],)).fetchone()[0] == "保留历史"


def test_run_request_uniqueness_is_scoped_to_conversation(store):
    first, second = store.new_conversation(), store.new_conversation()
    run_id = insert_run(store, first)
    with pytest.raises(sqlite3.IntegrityError):
        insert_run(store, first)
    other_id = insert_run(store, second)
    assert run_id != other_id
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 2


def test_orphan_run_is_rejected_and_transaction_rolls_back(store):
    with pytest.raises(sqlite3.IntegrityError):
        insert_run(store, {"id": uid()})
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_event_reconnect_is_ordered_persistent_and_does_not_append(store):
    conversation = store.new_conversation()
    run_id = insert_run(store, conversation)
    for kind in ("plan", "execution_start", "execution_end", "terminal"):
        store.append_event(run_id, kind, {"实际事件": kind})
    events = Store(store.root).events(run_id, after=2)
    assert [event["seq"] for event in events] == [3, 4]
    assert [event["type"] for event in events] == ["execution_end", "terminal"]
    assert all(event["conversation_id"] == conversation["id"] for event in events)
    assert all(event["run_id"] == run_id and event["version"] == 1 for event in events)
    assert store.events(run_id, after=4) == []
    assert len(store.events(run_id)) == 4


def test_concurrent_event_append_has_unique_monotonic_sequences(store):
    run_id = insert_run(store, store.new_conversation())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda number: store.append_event(run_id, "progress", {"number": number}), range(24)))
    events = store.events(run_id)
    assert [event["seq"] for event in events] == list(range(1, 25))
    assert {event["payload"]["number"] for event in events} == set(range(24))


def test_nonfinite_event_payload_does_not_create_event_or_sequence_gap(store):
    run_id = insert_run(store, store.new_conversation())
    with pytest.raises(ValueError):
        store.append_event(run_id, "invalid", {"value": float("nan")})
    event = store.append_event(run_id, "valid", {"value": 1})
    assert event["seq"] == 1
    assert store.events(run_id) == [event]


def test_real_chinese_csv_import_preserves_raw_values_and_original_file(store, tmp_path):
    conversation = store.new_conversation()
    raw = "编号;数量;备注\n001;0;NA\n002;5;NULL\n".encode("utf-8-sig")
    source = tmp_path / "用户原文件.csv"
    source.write_bytes(raw)
    source_hash = digest(source.read_bytes())
    file = import_file(store, conversation["id"], source.name, source.read_bytes())
    assert file["parse_error"] is None
    assert file["preview"]["columns"] == ["编号", "数量", "备注"]
    assert file["preview"]["rows"] == [["001", "0", "NA"], ["002", "5", "NULL"]]
    assert file["preview"]["row_count"] == 2
    assert file["preview"]["parsing"]["delimiter"] == ";"
    _, imported = read_registered(store, file["id"])
    assert imported == raw and file["sha256"] == source_hash
    assert digest(source.read_bytes()) == source_hash


def test_empty_csv_is_registered_with_error_not_fake_preview(store):
    file = import_file(store, store.new_conversation()["id"], "empty.csv", b"")
    assert file["preview"] is None
    assert file["parse_error"]["code"] == "empty_input"
    assert read_registered(store, file["id"])[1] == b""


def test_one_csv_and_frozen_inputs_are_enforced(store):
    conversation = store.new_conversation()
    import_file(store, conversation["id"], "one.csv", b"id,value\n001,2\n")
    with pytest.raises(AppError) as error:
        import_file(store, conversation["id"], "two.csv", b"id,value\n002,3\n")
    assert error.value.code == "single_csv"
    with store.connect() as db:
        db.execute("UPDATE conversations SET frozen=1 WHERE id=?", (conversation["id"],))
    with pytest.raises(AppError) as error:
        import_file(store, conversation["id"], "rules.md", "说明文字".encode())
    assert error.value.code == "inputs_frozen"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM conversation_files").fetchone()[0] == 1


def test_material_count_limit_keeps_previous_bindings(store):
    conversation = store.new_conversation()
    for number in range(10):
        file = import_file(store, conversation["id"], f"rules-{number}.txt", f"变量解释 {number}".encode())
        assert file["parse_error"] is None
    with pytest.raises(AppError) as error:
        import_file(store, conversation["id"], "eleventh.md", b"rules")
    assert error.value.code == "material_limit"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM conversation_files").fetchone()[0] == 10


@pytest.mark.parametrize("filename,cap", [("large.csv", 10), ("large.pdf", 5)])
def test_file_size_limit_creates_no_registered_files(store, filename, cap):
    with pytest.raises(AppError) as error:
        import_file(store, store.new_conversation()["id"], filename, b"x" * (cap * 1024 * 1024 + 1))
    assert error.value.code == "file_limit"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 0


def test_registered_hash_change_and_missing_file_are_rejected(store):
    file = import_file(store, store.new_conversation()["id"], "data.csv", b"id,value\n001,2\n")
    record = store.require("files", file["id"])
    path = store.root / record["relative_path"]
    path.write_bytes(b"id,value\n001,9\n")
    with pytest.raises(AppError) as error:
        read_registered(store, file["id"])
    assert error.value.code == "file_changed"
    path.unlink()
    with pytest.raises(AppError) as error:
        read_registered(store, file["id"])
    assert error.value.code == "file_missing"


def test_history_rebuild_keeps_two_completed_tasks_as_facts_not_artifact_authority(store):
    conversation = store.new_conversation()
    file = import_file(store, conversation["id"], "data.csv", b"id,value\n001,2\n002,3\n")
    run_ids = []
    for number in range(3):
        run_ids.append(insert_run(store, conversation, status="completed", request=f"request-{number}",
                                  task=f"历史任务 {number}", report=history_report(file),
                                  created=f"2026-10-10T00:00:0{number}+00:00"))
    insert_run(store, conversation, status="error", request="failed", task="失败内容不可当背景")
    text, selected = history_context(Store(store.root), conversation["id"])
    data = json.loads(text)
    assert selected == list(reversed(run_ids[-2:]))
    assert [fact["task"] for fact in data["tasks"]] == ["历史任务 1", "历史任务 2"]
    assert data["tasks"][0]["confirmed_replies"] == ["按全部记录计算"]
    assert data["tasks"][0]["successful_code"] == ["result = 2 + 3\n"]
    assert "不是新任务的执行证据" in data["note"]
    assert "old-artifact" not in text and "old-deliverable" not in text
    assert "失败内容不可当背景" not in text


def test_history_selection_is_scoped_and_rejects_duplicates(store):
    first, second = store.new_conversation(), store.new_conversation()
    file = import_file(store, first["id"], "data.csv", b"id,value\n001,2\n")
    run_id = insert_run(store, first, status="completed", report=history_report(file))
    with pytest.raises(AppError) as error:
        history_context(store, second["id"], [run_id])
    assert error.value.code == "context_not_authorized"
    with pytest.raises(AppError) as error:
        history_context(store, first["id"], [run_id, run_id])
    assert error.value.code == "context_limit"


def test_history_changed_source_or_code_is_rejected(store):
    conversation = store.new_conversation()
    file = import_file(store, conversation["id"], "data.csv", b"id,value\n001,2\n")
    report = history_report(file)
    run_id = insert_run(store, conversation, status="completed", report=report)
    report["code_executions"][0]["code"] = "result = 9000\n"
    with store.connect() as db:
        db.execute("UPDATE runs SET report_json=? WHERE id=?", (encode(report), run_id))
    with pytest.raises(AppError) as error:
        history_context(store, conversation["id"])
    assert error.value.code == "history_changed"
    report = history_report(file)
    report["source"]["sha256"] = "0" * 64
    with store.connect() as db:
        db.execute("UPDATE runs SET report_json=? WHERE id=?", (encode(report), run_id))
    with pytest.raises(AppError) as error:
        history_context(store, conversation["id"])
    assert error.value.code == "history_changed"


def test_history_budget_omits_whole_code_instead_of_truncating_python(store):
    conversation = store.new_conversation()
    file = import_file(store, conversation["id"], "data.csv", b"id,value\n001,2\n")
    code = "# 完整脚本\n" + "print('中文')\n" * 4000
    insert_run(store, conversation, status="completed", report=history_report(file, code=code))
    text, _ = history_context(store, conversation["id"])
    data = json.loads(text)
    assert len(text.encode()) <= 32 * 1024
    assert data["tasks"][0]["successful_code"] == []
    assert "省略" in data["omitted"]


def test_mutated_bound_input_prevents_history_rebuild_even_without_prior_runs(store):
    conversation = store.new_conversation()
    file = import_file(store, conversation["id"], "data.csv", b"id,value\n001,2\n")
    registered = store.require("files", file["id"])
    (store.root / registered["relative_path"]).write_bytes(b"id,value\n001,8\n")
    with pytest.raises(AppError) as error:
        history_context(store, conversation["id"])
    assert error.value.code == "file_changed"
