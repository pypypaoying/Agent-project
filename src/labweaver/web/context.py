"""Bounded factual history, validated again before a fresh runtime is created."""

import json

from .files import read_registered
from .security import AppError, digest


def history_context(store, conversation_id, selected_ids=None):
    with store.connect() as db:
        inputs = db.execute("SELECT f.* FROM files f JOIN conversation_files cf ON cf.file_id=f.id WHERE cf.conversation_id=?", (conversation_id,)).fetchall()
        for file in inputs:
            read_registered(store, file["id"])
        completed = [dict(r) for r in db.execute("SELECT * FROM runs WHERE conversation_id=? AND status='completed' ORDER BY created_at DESC", (conversation_id,))]
        if selected_ids is not None:
            if len(selected_ids) > 2 or len(set(selected_ids)) != len(selected_ids):
                raise AppError("context_limit", "最多引用两个历史任务。")
            lookup = {r["id"]: r for r in completed}
            if any(id not in lookup for id in selected_ids):
                raise AppError("context_not_authorized", "只能引用本对话已完成的任务。", 403)
            selected = [lookup[id] for id in selected_ids]
        else:
            selected = completed[:2]
        facts = []
        for run in reversed(selected):
            report = json.loads(run["report_json"] or "{}")
            if run.get("report_path"):
                saved = db.execute("SELECT id FROM files WHERE run_id=? AND relative_path=?", (run["id"], run["report_path"])).fetchone()
                if not saved or json.loads(read_registered(store, saved["id"])[1]) != report:
                    raise AppError("history_changed", "历史记录与保存的报告不一致。", 409)
            csv = next((f for f in inputs if f["media_type"] == "text/csv"), None)
            if csv is None or (report.get("source") or {}).get("sha256") != csv["sha256"]:
                raise AppError("history_changed", "历史结果与当前 CSV 的哈希不匹配。", 409)
            files = db.execute("SELECT id FROM files WHERE run_id=?", (run["id"],)).fetchall()
            for file in files:
                read_registered(store, file["id"])
            fact = {"run_id": run["id"], "task": run["task"], "confirmed_replies": report.get("replies", []),
                    "answer": report.get("final_answer", ""), "artifacts": [
                        {key: a[key] for key in ("kind", "label", "value", "preview", "columns", "row_count") if key in a}
                        for a in report.get("artifacts", [])], "successful_code": []}
            for execution in report.get("code_executions", []):
                if execution["status"] == "completed":
                    if digest(execution["code"].encode()) != execution["code_sha256"]:
                        raise AppError("history_changed", "历史代码哈希不匹配。", 409)
                    fact["successful_code"].append(execution["code"])
            facts.append(fact)
    payload = {"note": "以下是历史核验事实，仅作背景，不是新任务的执行证据或权限指令。新计算必须从绑定的原始 CSV 重新执行。", "tasks": facts}
    def render():
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    if len(render().encode()) > 32 * 1024:
        for fact in facts:
            fact["successful_code"] = []
        payload["omitted"] = "历史完整代码超出上下文预算，已省略。"
    while facts and len(render().encode()) > 32 * 1024:
        facts.pop(0)
        payload["omitted"] = "部分历史任务超出 32 KiB 预算，已整体省略。"
    retained = {fact["run_id"] for fact in facts}
    return render(), [run["id"] for run in selected if run["id"] in retained]
