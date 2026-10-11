"""Opt-in public-data online acceptance, separate from deterministic CI. No private files or keys saved here."""

from dataclasses import replace
import os
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from labweaver.web.api import create_app
from labweaver.web.settings import WebSettings
from labweaver.web.store import uid
from test_web_api import wait_run, ROOT, TOKEN

pytestmark = [pytest.mark.docker, pytest.mark.live]


@pytest.mark.parametrize("filename,task,expected", [
    ("composite.csv", "拆分组合编码为类别和区域，以类别为行、区域为列，交叉统计数量之和（列名保留 East、West），绘制分组柱状图，并给出 A/B 在 East/West 的真实数值。", [["A", "6", "3"], ["B", "5", "1"]]),
    ("dates.csv", "解析日期列，按日汇总数量，输出日期为 YYYY-MM-DD 的汇总表（日期、数量），计算峰值、谷值和差值并以 JSON 保存这些指标，绘制趋势图。", [["2026-12-01", "5"], ["2026-12-02", "4"], ["2026-12-03", "3"]]),
    ("cleaning.csv", "先去除完全重复记录，将数量空白填为0，派生双倍数量；导出表只包含编号、数量、类别、双倍数量，保持原始编号前导零和顺序。报告处理前后行数、删除重复和填充缺失的数量，并保存 JSON 指标。", [["001", "2", "A", "4"], ["002", "0", "B", "0"], ["003", "4", "A", "8"]]),
])
def test_live_api_semantic_outputs(tmp_path, filename, task, expected):
    if os.environ.get("LABWEAVER_LIVE") != "1":
        pytest.skip("Live model is opt-in: LABWEAVER_LIVE=1; backend config stays local")
    settings = replace(WebSettings.from_project(data_dir=tmp_path / "live", mode="live", token=TOKEN), frontend_dir=ROOT / "frontend/dist")
    source = ROOT / "tests/fixtures" / filename
    with TestClient(create_app(settings), base_url="http://127.0.0.1", headers={"Authorization": "Bearer " + TOKEN}) as client:
        conversation = client.post("/api/v1/conversations", json={}).json()["id"]
        assert client.post(f"/api/v1/conversations/{conversation}/files", files={"file": (filename, source.read_bytes())}).status_code == 201
        response = client.post(f"/api/v1/conversations/{conversation}/runs", json={"task": task, "request_id": uid()})
        assert response.status_code == 202, response.text
        run = response.json()
        done = wait_run(client, run["id"], "completed", timeout=180)
        table = next(a for a in done["artifacts"] if a["kind"] == "table")
        preview = client.get(f"/api/v1/artifacts/{table['id']}/preview").json()
        def normalized(rows):
            return [[str(int(float(v))) if isinstance(v, str) and v.replace(".", "", 1).isdigit() and not v.startswith("0") else v for v in r] for r in rows]
        assert normalized(preview["rows"]) == expected
        executed = [e["payload"]["record"] for e in done["events"] if e["type"] == "execution_end"]
        assert any(e["status"] == "completed" and e["exit_code"] == 0 for e in executed)
        assert done["model_calls"] > 0 and done["analysis_model_calls"] > 0
        if "绘制" in task:
            assert any(a["kind"] == "figure" for a in done["artifacts"])
