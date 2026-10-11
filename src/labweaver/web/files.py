"""Input imports and authorized downloads. Never accepts client filesystem paths."""

import csv
import io
import json
from pathlib import Path
import re

from .security import AppError, contains_secret, digest, managed_path
from .store import encode, uid

MEDIA = {".csv": "text/csv", ".txt": "text/plain", ".md": "text/markdown", ".pdf": "application/pdf",
         ".json": "application/json", ".py": "text/x-python", ".png": "image/png", ".svg": "image/svg+xml"}


def safe_name(name):
    if not name or len(name) > 180 or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.rstrip(" .") != name:
        raise AppError("filename_invalid", "文件名无效，请使用不含路径和特殊控制字符的文件名。")
    if name in {".", ".."} or re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name):
        raise AppError("filename_invalid", "文件名为保留名称。")
    return name


def input_preview(path, role, *, forbidden=()):
    try:
        if role == "csv":
            from labweaver.tools.csv_profile import profile_csv
            report = profile_csv(path, encoding="auto", delimiter="auto")
            if contains_secret(report, forbidden):
                raise AppError("secret_in_input", "解析后的输入包含敏感配置，拒绝保存。")
            if report.get("status") != "completed":
                return None, report.get("error", {"code": "csv_parse_failed"})
            if forbidden:
                from labweaver.tools.csv_profile import load_csv_snapshot
                snapshot = load_csv_snapshot(path)
                if contains_secret({"headers": snapshot.headers, "rows": snapshot.rows}, forbidden):
                    raise AppError("secret_in_input", "解析后的输入包含敏感配置，拒绝保存。")
            return {"columns": [c["name"] for c in report["columns"]],
                    "rows": report["sample_rows"], "row_count": report["row_count"],
                    "column_count": report["column_count"], "parsing": report.get("parsing")}, None
        from labweaver.materials import build_material_index
        index = build_material_index([path])
        chunks = index._chunks
        if contains_secret(chunks, forbidden):
            raise AppError("secret_in_input", "资料原文包含敏感配置，拒绝保存。")
        return {"text": "\n\n".join(c["text"] for c in chunks)[:4000], "truncated": sum(len(c["text"]) for c in chunks) > 4000,
                "sources": index.sources}, None
    except AppError:
        raise
    except Exception as exc:
        # Library exceptions may contain input text/paths. Only disclose a typed error.
        return None, {"code": getattr(exc, "code", "material_parse_failed"), "message": "资料无法解析；仅支持 UTF-8 TXT/MD 和文字型 PDF。"}


def file_view(item):
    return {key: item[key] for key in ("id", "name", "media_type", "size", "sha256", "category")} | {
        "preview": json.loads(item["preview_json"]) if item.get("preview_json") else None,
        "parse_error": json.loads(item["parse_error_json"]) if item.get("parse_error_json") else None,
        "role": item.get("role", "generated")}


def import_file(store, conversation_id, name, raw, *, forbidden=()):
    safe_name(name)
    extension = Path(name).suffix.lower()
    if extension not in {".csv", ".txt", ".md", ".pdf"}:
        raise AppError("unsupported_format", "目前只支持 CSV、TXT、Markdown 和文字型 PDF；图片与其他格式暂不支持分析。", 415)
    role = "csv" if extension == ".csv" else "material"
    cap = (10 if role == "csv" else 5) * 1024 * 1024
    if len(raw) > cap:
        raise AppError("file_limit", "CSV 上限 10 MiB，单份资料上限 5 MiB。", 413)
    with store.connect() as db:
        conversation = store.require("conversations", conversation_id, db)
        if conversation["frozen"]:
            raise AppError("inputs_frozen", "首次任务已冻结附件；更换输入请新建对话。", 409)
        bound = db.execute("SELECT cf.role,f.size FROM conversation_files cf JOIN files f ON f.id=cf.file_id WHERE cf.conversation_id=?", (conversation_id,)).fetchall()
        if role == "csv" and any(f["role"] == "csv" for f in bound):
            raise AppError("single_csv", "一个对话只能绑定一份 CSV。", 409)
        materials = [f for f in bound if f["role"] == "material"]
        if role == "material" and (len(materials) >= 10 or sum(f["size"] for f in materials) + len(raw) > 20 * 1024 * 1024):
            raise AppError("material_limit", "资料最多 10 份，总计不超过 20 MiB。", 413)
        id = uid()
        relative = f"attachments/{id}/{name}"
        path = managed_path(store.root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as target:
                target.write(raw)
            preview, error = input_preview(path, role, forbidden=forbidden)
            item = dict(id=id, conversation_id=conversation_id, run_id=None, name=name, relative_path=relative,
                        media_type=MEDIA[extension], size=len(raw), sha256=digest(raw), preview_json=encode(preview) if preview else None,
                        parse_error_json=encode(error) if error else None, category="input")
            db.execute("INSERT INTO files VALUES (:id,:conversation_id,:run_id,:name,:relative_path,:media_type,:size,:sha256,:preview_json,:parse_error_json,:category)", item)
            db.execute("INSERT INTO conversation_files VALUES (?,?,?,?)", (conversation_id, id, role, len(bound)))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return file_view(item | {"role": role})


def read_registered(store, file_id):
    file = store.require("files", file_id)
    path = managed_path(store.root, file["relative_path"])
    try:
        raw = path.read_bytes()
    except OSError:
        raise AppError("file_missing", "文件已丢失。", 410) from None
    if len(raw) != file["size"] or digest(raw) != file["sha256"]:
        raise AppError("file_changed", "文件内容已变化，拒绝读取未核验数据。", 409)
    return file, raw


def preview_artifact(store, artifact_id, offset=0, limit=20):
    artifact = store.require("artifacts", artifact_id)
    meta = json.loads(artifact["metadata_json"])
    if meta["kind"] == "metric":
        return {"value": meta["value"]}
    with store.connect() as db:
        entry = db.execute("SELECT file_id FROM artifact_files WHERE artifact_id=? AND name IN ('table.csv','data.csv')", (artifact_id,)).fetchone()
    if not entry:
        raise AppError("not_table", "此成果没有可预览表格。")
    _, raw = read_registered(store, entry["file_id"])
    reader = csv.reader(io.StringIO(raw.decode("utf-8-sig")), strict=True)
    columns = next(reader)
    rows, total = [], 0
    for row in reader:
        if offset <= total < offset + limit:
            rows.append(row)
        total += 1
    return {"columns": columns, "rows": rows, "row_count": total, "offset": offset, "limit": limit,
            "truncated": offset + len(rows) < total}
