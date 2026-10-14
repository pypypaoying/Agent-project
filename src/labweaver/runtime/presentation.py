"""Bounded, deterministic views of recorded facts; never infer analytical claims."""

import math
import re
import json

MAX_ROWS = 20
MAX_COLUMNS = 12
MAX_BLOCKS = 32
MAX_TEXT = 1000
MAX_BYTES = 64 * 1024

_LABELS = {
    "row_count": "记录数", "column_count": "字段数", "count": "数量",
    "original_rows": "原始记录数", "original_columns": "原始字段数",
    "rows_before": "处理前记录数", "rows_after": "处理后记录数",
    "missing_count": "缺失数量", "missing_values": "缺失情况",
    "missing_total": "缺失总数", "removed_duplicates": "删除重复记录数",
    "duplicate_count": "重复记录数", "duplicates_removed": "删除重复记录数",
    "class_counts": "类别数量", "label_map": "标签映射", "seed": "随机种子",
    "accuracy": "准确率", "precision": "精确率", "recall": "召回率",
    "f1": "F1", "macro_f1": "宏平均 F1", "support": "样本数",
    "test_accuracy": "测试集准确率", "test_macro_f1": "测试集宏平均 F1",
    "train_size": "训练集样本数", "val_size": "验证集样本数", "test_size": "测试集样本数",
    "best_epoch": "最佳轮次", "stopped_epoch": "停止轮次", "epochs_run": "实际训练轮数",
    "best_val_loss": "最佳验证损失", "learning_rate": "学习率", "batch_size": "批量大小",
    "patience": "早停耐心轮数", "min": "最小值", "max": "最大值", "mean": "均值",
}


def metric_label(value):
    key = str(value)
    return _LABELS.get(key, re.sub(r"[_\s]+", " ", key).strip() or "未命名指标")


def metric_presentation(value):
    """Render strict JSON into scalar tables, record tables and matrix previews.

    Original values are never changed. Long structures have explicit preview
    limits; scalar integers retain precision and floats have a display note.
    """
    blocks, notes, used_bytes = [], [], 0

    def note(text):
        if text not in notes:
            notes.append(text)

    def bounded(text):
        text = str(text)
        if len(text) > MAX_TEXT:
            note("长文本已截断；完整内容见指标 JSON。")
            return text[:MAX_TEXT] + "…"
        return text

    def scalar(item):
        if item is None:
            return "未提供"
        if isinstance(item, bool):
            return "是" if item else "否"
        if isinstance(item, int):
            return str(item)
        if isinstance(item, float):
            if not math.isfinite(item):
                return "非有限数值"
            note("浮点数展示最多 6 位有效数字；完整精度见指标 JSON。")
            return format(item, ".6g")
        if isinstance(item, str):
            return bounded(item) if item else "（空字符串）"
        if isinstance(item, list):
            return f"列表，共 {len(item)} 项；完整内容见指标 JSON"
        if isinstance(item, dict):
            return f"对象，共 {len(item)} 项；完整内容见指标 JSON"
        return bounded(item)

    def add(block):
        nonlocal used_bytes
        if len(blocks) >= MAX_BLOCKS:
            note("结果超过展示预算；其余内容见完整指标 JSON。")
            return False
        if block.get("title"):
            block["title"] = bounded(block["title"])
        size = len(json.dumps(block, ensure_ascii=False).encode("utf-8"))
        if used_bytes + size > MAX_BYTES - 2048:
            note("结果超过展示预算；其余内容见完整指标 JSON。")
            return False
        blocks.append(block)
        used_bytes += size
        return True

    def table(title, columns, rows, total=None):
        columns = [bounded(metric_label(c)) for c in columns[:MAX_COLUMNS]]
        preview = [[scalar(cell) for cell in row[:MAX_COLUMNS]] for row in rows[:MAX_ROWS]]
        truncated = total is not None and total > len(preview)
        if truncated:
            note(f"表格只展示前 {MAX_ROWS} 行；完整内容见指标 JSON。")
        add({"type": "table", "title": title, "columns": columns, "rows": preview, "truncated": truncated})

    def visit(item, title="", depth=0):
        if len(blocks) >= MAX_BLOCKS:
            note("结果超过展示预算；其余内容见完整指标 JSON。")
            return
        if depth > 6:
            add({"type": "paragraph", "text": f"{title}：{scalar(item)}"})
            return
        if isinstance(item, dict):
            simple, total = [], 0
            for key, val in item.items():
                if not isinstance(val, (list, dict)) and key != "report_markdown":
                    total += 1
                    if len(simple) < MAX_ROWS:
                        simple.append((key, val))
            if simple:
                table(title, ["指标", "值"], [[metric_label(k), v] for k, v in simple], total)
            narrative = item.get("report_markdown")
            if isinstance(narrative, str) and len(narrative.encode("utf-8")) <= 64 * 1024:
                add({"type": "markdown", "text": narrative})
            elif narrative is not None:
                note("报告正文超过预览预算；完整内容见完整指标 JSON。")
            for key, val in item.items():
                if isinstance(val, (list, dict)):
                    visit(val, " · ".join(filter(None, [title, metric_label(key)])), depth + 1)
                    if len(blocks) >= MAX_BLOCKS:
                        note("结果超过展示预算；其余内容见完整指标 JSON。")
                        break
            if not item:
                add({"type": "paragraph", "text": f"{title + '：' if title else ''}没有记录的指标。"})
        elif isinstance(item, list):
            if not item:
                add({"type": "paragraph", "text": f"{title + '：' if title else ''}列表为空。"})
            elif all(isinstance(row, dict) for row in item[:MAX_ROWS]):
                add({"type": "paragraph", "text": f"共 {len(item)} 项，以下为记录预览。"})
                columns = list(dict.fromkeys(k for row in item[:MAX_ROWS] for k in row))
                if len(columns) > MAX_COLUMNS:
                    note(f"表格只展示前 {MAX_COLUMNS} 列；完整内容见指标 JSON。")
                table(title, columns, [[row.get(c) for c in columns[:MAX_COLUMNS]] for row in item[:MAX_ROWS]], len(item))
            elif all(isinstance(row, list) for row in item[:MAX_ROWS]):
                add({"type": "paragraph", "text": f"共 {len(item)} 行，以下为数组预览。"})
                width = max(len(row) for row in item[:MAX_ROWS])
                if width >= MAX_COLUMNS:
                    note(f"矩阵仅展示前 {MAX_ROWS} 行、前 {MAX_COLUMNS - 1} 列；完整内容见指标 JSON。")
                columns = ["行 / 列", *range(1, min(width, MAX_COLUMNS - 1) + 1)]
                rows = [[i + 1, *(row[j] if j < len(row) else None for j in range(len(columns) - 1))] for i, row in enumerate(item[:MAX_ROWS])]
                table(title, columns, rows, len(item))
            else:
                add({"type": "paragraph", "text": f"共 {len(item)} 项，以下为列表预览。"})
                table(title, ["序号", "值"], [[i + 1, row] for i, row in enumerate(item[:MAX_ROWS])], len(item))
        else:
            table(title, ["指标", "值"], [["结果", item]], 1)

    visit(value)
    return {"blocks": blocks, "note": " ".join(notes)}


def metric_display_fields(value):
    """Keep the API bounded; full precision and large values remain downloadable."""
    size = 0
    for fragment in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
        size += len(fragment.encode("utf-8"))
        if size > MAX_BYTES:
            return {"presentation": metric_presentation(value), "value_preview_only": True}
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, int) and not isinstance(item, bool) and abs(item) > 2**53 - 1:
            # JavaScript JSON.parse cannot preserve this integer. Keep its exact
            # display string and registered download, rather than label a rounded
            # browser reserialization as the original JSON.
            return {"presentation": metric_presentation(value), "value_preview_only": True}
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return {"presentation": metric_presentation(value), "value": value, "value_preview_only": False}


def presentation_markdown(presentation):
    """Create passive Markdown structure; callers escape untrusted text/links."""
    def cell(value):
        return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    lines = []
    for block in presentation["blocks"]:
        if block.get("title"):
            lines.extend(["#### " + cell(block["title"]), ""])
        if block["type"] == "table":
            columns = block["columns"]
            lines.extend(["| " + " | ".join(cell(c) for c in columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"])
            lines.extend("| " + " | ".join(cell(c) for c in row) + " |" for row in block["rows"])
        else:
            lines.append(block["text"])
        lines.append("")
    if presentation.get("note"):
        lines.append(presentation["note"])
    return "\n".join(lines)
