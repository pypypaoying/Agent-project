"""Actual HTTP -> owned worker -> both Agents -> Docker -> registered outputs."""

import hashlib
from pathlib import Path
import pytest
from labweaver.runtime.execution import DockerExecutor, ExecutionError
from test_web_api import client_for, wait_run
from labweaver.web.store import uid

pytestmark = pytest.mark.docker
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("filename,task,expected", [
    ("composite.csv", "拆分组合编码，按类别和区域交叉统计数量，绘制分组柱状图", [["A", "6", "3"], ["B", "5", "1"]]),
    ("dates.csv", "解析日期，按日汇总数量，计算峰值、谷值和差值，绘制趋势图", [["2026-12-01", "5"], ["2026-12-02", "4"], ["2026-12-03", "3"]]),
    ("cleaning.csv", "先去除完全重复记录，再将数量空白填为0，派生双倍数量，导出数据并报告前后变化", None),
])
def test_api_true_python_outputs(tmp_path, filename, task, expected):
    import os
    try:
        DockerExecutor().preflight()
    except ExecutionError as exc:
        if os.environ.get("LABWEAVER_REQUIRE_DOCKER") == "1":
            pytest.fail(str(exc))
        pytest.skip(exc.code)
    source = FIXTURES / filename
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    client, manager = client_for(tmp_path)
    with client:
        conversation = client.post("/api/v1/conversations", json={}).json()["id"]
        assert client.post(f"/api/v1/conversations/{conversation}/files", files={"file": (filename, source.read_bytes())}).status_code == 201
        run = client.post(f"/api/v1/conversations/{conversation}/runs", json={"task": task, "request_id": uid()}).json()
        done = wait_run(client, run["id"], "completed")
        table = next(a for a in done["artifacts"] if a["kind"] == "table")
        preview = client.get(f"/api/v1/artifacts/{table['id']}/preview").json()
        if expected is not None:
            assert preview["rows"] == expected
        else:
            assert preview["row_count"] == 3
        execution = next(e["payload"]["record"] for e in done["events"] if e["type"] == "execution_end")
        assert execution["exit_code"] == 0 and execution["container_removed"]
        assert execution["owner_labels"]["labweaver.run"] == run["id"]
        if "绘制" in task:
            chart = next(a for a in done["artifacts"] if a["kind"] == "figure")
            png = next(f for f in chart["files"] if f["name"] == "figure.png")
            raw = client.get(f"/api/v1/files/{png['id']}/content").content
            assert raw.startswith(b"\x89PNG") and len(raw) > 1000
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
