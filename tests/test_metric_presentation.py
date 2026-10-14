"""Display formatting must preserve recorded facts and expose preview limits."""
import copy
import json

from labweaver.runtime.presentation import MAX_BYTES, metric_display_fields, metric_presentation, presentation_markdown
from labweaver.runtime.records import _markdown_brief


def test_nested_statistics_are_real_tables_and_values_remain_exact():
    value = {"count": 0, "ok": False, "missing": None, "identifier": "001", "large": 2**64 + 1,
             "learning_rate": .1, "categories": {"A": 12, "B": 0},
             "scores": [{"group": "A", "value": .6206896551724138}]}
    original = copy.deepcopy(value)
    presentation = metric_presentation(value)
    text = presentation_markdown(presentation)
    assert value == original
    assert "| 数量 | 0 |" in text and "| ok | 否 |" in text
    assert "| missing | 未提供 |" in text and "| identifier | 001 |" in text
    assert str(2**64 + 1) in text and "| 学习率 | 0.1 |" in text
    assert "0.62069" in text and "%" not in text
    assert "6 位有效数字" in presentation["note"]
    assert all(isinstance(cell, str) for block in presentation["blocks"] if block["type"] == "table" for row in block["rows"] for cell in row)


def test_long_arrays_matrices_and_columns_are_explicit_previews():
    value = {"indices": list(range(951)), "matrix": [list(range(30)) for _ in range(25)],
             "records": [{f"c{i}": i for i in range(15)}] * 25}
    result = metric_presentation(value)
    tables = [b for b in result["blocks"] if b["type"] == "table"]
    assert all(len(t["rows"]) <= 20 and len(t["columns"]) <= 12 for t in tables)
    assert "951" in presentation_markdown(result)
    assert "前 20 行" in result["note"] and "前 12 列" in result["note"]
    assert any(t["truncated"] for t in tables)


def test_overall_utf8_budget_and_depth_are_bounded():
    wide = {f"项目{i}": {f"长字段{j}": "数据" * 1000 for j in range(20)} for i in range(100)}
    result = metric_presentation(wide)
    assert len(json.dumps(result, ensure_ascii=False).encode()) < MAX_BYTES
    assert "展示预算" in result["note"] and "截断" in result["note"]
    nested = {"x": 0}
    for _ in range(20):
        nested = {"deep": nested}
    assert len(presentation_markdown(metric_presentation(nested))) < 4096


def test_matrix_cells_escape_newlines_and_pipes():
    text = presentation_markdown(metric_presentation([{"label": "A|B\nC", "count": 0}]))
    assert "A\\|B C" in text


def test_large_metric_api_omits_raw_value_without_omitting_downloadable_data():
    large = {"weights": list(range(30000))}
    view = metric_display_fields(large)
    assert view["value_preview_only"] is True and "value" not in view
    assert "完整" in view["presentation"]["note"]
    assert large["weights"][-1] == 29999
    assert len(json.dumps(view, ensure_ascii=False).encode()) < MAX_BYTES
    small = {"count": 0, "identifier": "001"}
    assert metric_display_fields(small)["value"] is small


def test_unsafe_browser_integers_use_exact_display_and_download_only():
    value = {"nested": [True, {"identifier": 2**64 + 1, "negative": -(2**64 + 1)}]}
    view = metric_display_fields(value)
    assert view["value_preview_only"] is True and "value" not in view
    assert value["nested"][1]["identifier"] == 2**64 + 1
    # A nested-record cell is an object summary; a normal scalar metric is exact.
    exact = metric_display_fields({"identifier": 2**64 + 1})
    assert str(2**64 + 1) in presentation_markdown(exact["presentation"])
    assert metric_display_fields({"safe": 2**53 - 1})["value"]["safe"] == 2**53 - 1


def test_export_is_professional_and_failure_is_not_restyled_as_success():
    data = {"status": "error", "task": "请统计数据", "final_answer": "## 结论\n\n**尚未完成**统计。",
            "task_plan": [{"id": "missing", "description": "完整结果表"}], "fulfilled_deliverable_ids": [],
            "artifacts": [{"kind": "metric", "label": "处理概况", "value": {"rows_before": 4, "rows_after": 3}, "paths": {}, "execution_id": "execution-private", "deliverable_id": "secret-plan"}]}
    text = _markdown_brief(data)
    assert "# AnaliyMaster 数据分析报告" in text
    assert "未完成" in text and "完整结果表（未完成）" in text
    assert "| 处理前记录数 | 4 |" in text and "```json" not in text
    assert "execution-private" not in text and "secret-plan" not in text
