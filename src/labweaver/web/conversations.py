"""Conversation metadata and resumable removal of exclusively owned application files."""

import json
import os
from pathlib import Path
import re
import sqlite3

from .security import AppError, contains_secret, identifier, managed_path, managed_tree, purge_managed_tree
from .store import encode, now, uid

ACTIVE = {"running", "awaiting_input", "cancelling"}


class ConversationService:
    def __init__(self, manager):
        self.manager, self.store = manager, manager.store
        self._deleting = set()

    def _available(self):
        if self.manager.closed or getattr(self.manager, "stopping", False):
            raise AppError("application_stopping", "应用正在退出，请重新启动后操作。", 503)

    def patch(self, conversation_id, payload):
        identifier(conversation_id)
        if not isinstance(payload, dict) or not payload or set(payload) - {"title", "pinned"}:
            raise AppError("invalid_conversation", "请提供对话标题或置顶状态。")
        if "title" in payload:
            title = payload["title"]
            if (not isinstance(title, str) or not title.strip() or len(title) > 120
                    or any(ord(c) < 32 or ord(c) == 127 for c in title)):
                raise AppError("invalid_title", "标题不能为空、包含控制字符或超过 120 字符。")
            payload = payload | {"title": title.strip()}
        if "pinned" in payload and type(payload["pinned"]) is not bool:
            raise AppError("invalid_pinned", "置顶状态必须是布尔值。")
        with self.manager.lock:
            self._available()
            if contains_secret(payload, self.manager.secrets):
                raise AppError("secret_in_input", "对话标题不能包含敏感配置。")
            with self.store.connect() as db:
                self.store.require("conversations", conversation_id, db)
                fields = list(payload)
                db.execute("UPDATE conversations SET " + ",".join(field + "=?" for field in fields) + ",updated_at=? WHERE id=?",
                           [payload[field] for field in fields] + [now(), conversation_id])
            return next(row for row in self.store.conversations() if row["id"] == conversation_id)

    def _raw_conversation(self, db, conversation_id):
        row = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if row is None:
            raise AppError("not_found", "对话不存在。", 404)
        return dict(row)

    def _component(self, value):
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) is None:
            raise AppError("conversation_file_conflict", "对话文件归属信息无效，未执行删除。", 409)
        return value

    def _ownership(self, db, conversation_id):
        """Authorize whole input/task folders, including unregistered partial exports."""
        runs = [dict(row) for row in db.execute("SELECT * FROM runs WHERE conversation_id=?", (conversation_id,))]
        if any(row["status"] in ACTIVE for row in runs):
            raise AppError("task_active", "请先停止或完成该对话的任务，再永久删除。", 409)
        if self.manager.active:
            active = self.store.require("runs", self.manager.active, db)
            if active["conversation_id"] == conversation_id:
                raise AppError("task_active", "请先停止或完成该对话的任务，再永久删除。", 409)
        by_id = {row["id"]: row for row in runs}
        directories = {f"runtime/{identifier(conversation_id)}"}
        for run in runs:
            session, task = run["core_session_id"], run["core_task_id"]
            if session is None and task is None:
                if run["report_path"] is not None:
                    raise AppError("conversation_file_conflict", "对话报告缺少归属信息，未执行删除。", 409)
                continue
            directory = f"exports/{self._component(session)}/{self._component(task)}"
            if run["report_path"] is not None:
                report = run["report_path"]
                if not isinstance(report, str) or "\\" in report or len(Path(report).parts) != 4 or Path(report).parent != Path(directory):
                    raise AppError("conversation_file_conflict", "对话报告路径与归属信息不一致，未执行删除。", 409)
                managed_path(self.store.root, report)
            directories.add(directory)
        files = [dict(row) for row in db.execute("SELECT * FROM files WHERE conversation_id=?", (conversation_id,))]
        for file in files:
            relative = file["relative_path"]
            if not isinstance(relative, str) or "\\" in relative:
                raise AppError("conversation_file_conflict", "对话文件归属信息无效，未执行删除。", 409)
            path = managed_path(self.store.root, relative)
            parts = Path(relative).parts
            if file["category"] == "input" and file["run_id"] is None:
                identifier(file["id"])
                expected = f"attachments/{file['id']}"
                valid = len(parts) == 3 and Path(relative).parent == Path(expected)
            elif file["category"] == "generated" and file["run_id"] in by_id:
                run = by_id[file["run_id"]]
                expected = f"exports/{self._component(run['core_session_id'])}/{self._component(run['core_task_id'])}"
                valid = len(parts) == 4 and Path(relative).parent == Path(expected)
            else:
                valid, expected = False, ""
            if not valid or path.name != file["name"]:
                raise AppError("conversation_file_conflict", "对话文件归属信息无效，未执行删除。", 409)
            directories.add(expected)
        # A shared task folder or a foreign file/relationship is never removed.
        foreign_paths = [row[0] for row in db.execute("SELECT relative_path FROM files WHERE conversation_id<>?", (conversation_id,))]
        foreign_paths.extend(f"exports/{row[0]}/{row[1]}" for row in db.execute(
            "SELECT core_session_id,core_task_id FROM runs WHERE conversation_id<>? AND core_session_id IS NOT NULL AND core_task_id IS NOT NULL", (conversation_id,)))
        foreign_paths.extend(row[0] for row in db.execute(
            "SELECT report_path FROM runs WHERE conversation_id<>? AND report_path IS NOT NULL", (conversation_id,)))
        if any(Path(foreign).is_relative_to(Path(directory)) or Path(directory).is_relative_to(Path(foreign))
               for directory in directories for foreign in foreign_paths):
            raise AppError("conversation_file_conflict", "文件目录还被其他对话引用，未执行删除。", 409)
        checks = [
            ("SELECT 1 FROM conversation_files cf JOIN files f ON f.id=cf.file_id WHERE (cf.conversation_id=? AND f.conversation_id<>?) OR (cf.conversation_id<>? AND f.conversation_id=?) LIMIT 1", (conversation_id,) * 4),
            ("SELECT 1 FROM artifact_files af JOIN artifacts a ON a.id=af.artifact_id JOIN runs r ON r.id=a.run_id JOIN files f ON f.id=af.file_id WHERE (r.conversation_id=? AND f.conversation_id<>?) OR (r.conversation_id<>? AND f.conversation_id=?) LIMIT 1", (conversation_id,) * 4),
            ("SELECT 1 FROM executions e JOIN runs r ON r.id=e.run_id JOIN files f ON f.id=e.code_file_id WHERE (r.conversation_id=? AND f.conversation_id<>?) OR (r.conversation_id<>? AND f.conversation_id=?) LIMIT 1", (conversation_id,) * 4),
            ("SELECT 1 FROM messages m JOIN runs r ON r.id=m.run_id WHERE (m.conversation_id=? AND r.conversation_id<>?) OR (m.conversation_id<>? AND r.conversation_id=?) LIMIT 1", (conversation_id,) * 4),
            ("SELECT 1 FROM files f JOIN runs r ON r.id=f.run_id WHERE (f.conversation_id=? AND r.conversation_id<>?) OR (f.conversation_id<>? AND r.conversation_id=?) LIMIT 1", (conversation_id,) * 4),
        ]
        if any(db.execute(query, values).fetchone() for query, values in checks):
            raise AppError("conversation_file_conflict", "对话数据存在跨对话关联，未执行删除。", 409)
        return sorted(directories)

    def _tree(self, relative):
        """Inspect without following symlinks/reparse points before file operations."""
        return managed_tree(self.store.root, relative)

    def _stop_idle_runtime(self, conversation_id):
        runtime = self.manager.runtime
        if runtime is None or runtime.conversation_id != conversation_id:
            return
        if self.manager.active:
            raise AppError("conversation_runtime_conflict", "运行时归属不一致，未执行删除。", 409)
        runtime.cancel_signal.set()
        runtime.stop(force=True)
        if runtime.process.is_alive():
            raise AppError("conversation_runtime_stop_failed", "该对话运行时尚未停止，未执行文件删除。", 503)
        self.manager.runtime = None

    def _phase(self, conversation_id, phase, error=None):
        with self.store.connect() as db:
            db.execute("UPDATE conversation_deletions SET phase=?,updated_at=?,last_error_code=? WHERE conversation_id=?",
                       (phase, now(), error, conversation_id))

    def _purge_tree(self, relative):
        purge_managed_tree(self.store.root, relative)

    def _delete_rows(self, db, conversation_id):
        runs = "SELECT id FROM runs WHERE conversation_id=?"
        db.execute(f"DELETE FROM artifact_files WHERE artifact_id IN (SELECT id FROM artifacts WHERE run_id IN ({runs}))", (conversation_id,))
        for table in ("artifacts", "executions", "run_events", "run_models"):
            db.execute(f"DELETE FROM {table} WHERE run_id IN ({runs})", (conversation_id,))
        db.execute("DELETE FROM conversation_files WHERE conversation_id=?", (conversation_id,))
        for table in ("messages", "files", "conversation_models", "runs", "conversation_deletions", "conversations"):
            db.execute(f"DELETE FROM {table} WHERE conversation_id=?" if table not in {"conversations"} else "DELETE FROM conversations WHERE id=?", (conversation_id,))

    def delete(self, conversation_id, *, confirm=False):
        identifier(conversation_id)
        if confirm is not True:
            raise AppError("delete_confirmation_required", "请明确确认永久删除该对话及其应用文件。")
        with self.manager.lock:
            self._available()
            if conversation_id in self._deleting:
                raise AppError("conversation_delete_in_progress", "该对话正在删除，请等待本次操作结束。", 409)
            self._deleting.add(conversation_id)
        try:
            return self._delete(conversation_id)
        finally:
            with self.manager.lock:
                self._deleting.discard(conversation_id)

    def _delete(self, conversation_id):
        entries, job_id = [], None
        with self.manager.lock:
            self._available()
            with self.store.connect() as db:
                self._raw_conversation(db, conversation_id)
                directories = self._ownership(db, conversation_id)
                job = db.execute("SELECT * FROM conversation_deletions WHERE conversation_id=?", (conversation_id,)).fetchone()
            self._stop_idle_runtime(conversation_id)
            if job:
                job_id = identifier(job["job_id"])
                try:
                    entries = json.loads(job["manifest_json"])
                    valid = isinstance(entries, list) and len(entries) in {len(directories), len(directories) - 1}
                    valid = valid and all(isinstance(entry, dict) and set(entry) == {"original", "quarantine"}
                                          and entry["quarantine"] == f".conversation-trash/{job_id}/{index}"
                                          for index, entry in enumerate(entries))
                    originals = {entry["original"] for entry in entries}
                    runtime_folder = f"runtime/{conversation_id}"
                    # Continue an earlier prepared journal if it predates owned
                    # runtime folders and has no such directory to delete.
                    legacy = originals == set(directories) - {runtime_folder} and not managed_path(self.store.root, runtime_folder).exists()
                    valid = valid and (originals == set(directories) or legacy)
                except (TypeError, ValueError, KeyError, RecursionError):
                    valid = False
                if not valid:
                    raise AppError("conversation_file_conflict", "删除记录的文件归属已变化，未继续删除。", 409)
            else:
                for directory in directories:
                    self._tree(directory)
                job_id = uid()
                entries = [{"original": directory, "quarantine": f".conversation-trash/{job_id}/{index}"}
                           for index, directory in enumerate(directories)]
                try:
                    with self.store.connect() as db:
                        timestamp = now()
                        db.execute("INSERT INTO conversation_deletions VALUES (?,?,?,?,?,?,?)",
                                   (conversation_id, job_id, encode(entries), "prepared", timestamp, timestamp, None))
                except (OSError, sqlite3.Error):
                    raise AppError("conversation_delete_failed", "无法登记删除进度，未执行文件删除。", 503) from None
            try:
                for entry in entries:
                    expected = f".conversation-trash/{job_id}/"
                    if not entry["quarantine"].startswith(expected) or len(Path(entry["quarantine"]).parts) != 3:
                        raise AppError("unsafe_path", "删除进度路径无效。", 403)
                    original = managed_path(self.store.root, entry["original"])
                    quarantine = managed_path(self.store.root, entry["quarantine"])
                    self._tree(entry["original"])
                    self._tree(entry["quarantine"])
                    if original.exists() and quarantine.exists():
                        raise AppError("conversation_file_conflict", "待删除目录出现冲突，未覆盖文件。", 409)
                    if original.exists():
                        quarantine.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(original, quarantine)
                self._phase(conversation_id, "staged")
            except (OSError, sqlite3.Error, AppError) as error:
                try:
                    self._phase(conversation_id, "stage_failed", type(error).__name__)
                except (OSError, sqlite3.Error):
                    pass  # The durable prepared record still blocks reuse.
                raise AppError("conversation_delete_failed", "文件隔离未完成；已保留删除进度，请再次确认永久删除。", 503) from None
        # Keep unrelated Agent execution responsive during filesystem cleanup.
        try:
            for entry in entries:
                self._purge_tree(entry["quarantine"])
            parent = managed_path(self.store.root, f".conversation-trash/{job_id}")
            if parent.exists():
                parent.rmdir()
            with self.manager.lock, self.store.connect() as db:
                self._ownership(db, conversation_id)
                if any(managed_path(self.store.root, entry["original"]).exists() for entry in entries):
                    raise AppError("conversation_file_conflict", "原文件目录出现新的内容，请再次确认删除。", 409)
                self._delete_rows(db, conversation_id)
            return {"deleted": True, "conversation_id": conversation_id}
        except (OSError, sqlite3.Error, AppError) as error:
            try:
                self._phase(conversation_id, "cleanup_failed", type(error).__name__)
            except (OSError, sqlite3.Error):
                pass  # The durable prepared/staged record still blocks reuse.
            raise AppError("conversation_delete_failed", "永久删除尚未完成；已清理的文件不会恢复，请再次确认删除剩余文件与记录。", 503) from None
