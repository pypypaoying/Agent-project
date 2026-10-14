"""Narrative reports remain readable, bounded, and passive on export."""
from labweaver.runtime.records import _markdown_brief


def report(value):
    return {"status": "completed", "task": "public fixture", "artifacts": [{
        "id": "a", "kind": "metric", "label": "Report", "execution_id": "e",
        "deliverable_id": "report", "value": value, "paths": {},
    }]}


def test_narrative_metric_is_real_markdown_and_other_metrics_are_retained():
    text = _markdown_brief(report({"report_markdown": "### 实验报告\n\n结果源于真实执行。", "count": 0}))
    assert "### 实验报告\n\n结果源于真实执行。" in text
    assert '| 数量 | 0 |' in text
    assert '"report_markdown"' not in text


def test_report_disables_active_html_unsafe_links_and_remote_images():
    text = _markdown_brief(report({"report_markdown": '<script>alert(1)</script>\n![remote](https://invalid.test/image)\n[x](javascript:alert(1))\n[safe](https://example.test/docs)'}))
    assert '<script>' not in text and '&lt;script&gt;' in text
    assert '![remote]' not in text and 'javascript:' not in text
    assert '[safe](https://example.test/docs)' in text


def test_large_metric_has_bounded_brief_and_full_json_stays_an_artifact():
    text = _markdown_brief(report({"report_markdown": 'x' * 100000}))
    assert len(text.encode()) < 4096
    assert '完整指标 JSON' in text


def test_zero_metric_is_not_omitted():
    assert '| 结果 | 0 |' in _markdown_brief(report(0))


def test_reference_images_and_unsafe_reference_links_are_inert():
    text = _markdown_brief(report({"report_markdown": '![remote][picture]\n![picture]\n[x][bad]\n[picture]: https://example.test/img\n[bad]: javascript:alert(1)'}))
    assert '![' not in text and 'javascript:' not in text


def test_task_answer_and_table_text_cannot_embed_html_or_remote_images():
    data=report(0)
    data.update(task='<img src="https://example.test/private">',final_answer='![x](https://example.test/private)')
    text=_markdown_brief(data)
    assert '<img' not in text and '![' not in text
