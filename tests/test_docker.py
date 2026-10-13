"""Real Docker integration. Skips locally only when unavailable; CI requires it."""

import hashlib
import os
from pathlib import Path
import threading
import time
import tempfile
import pytest
from labweaver.agent import create_session
from labweaver.offline import OfflineIntakeModel, OfflineAnalysisModel
from labweaver.runtime.artifacts import csv_table
from labweaver.runtime.execution import DockerExecutor, ExecutionConfig, ExecutionError
from labweaver.runtime.replay import replay_analysis
from labweaver.tools.csv_profile import load_csv_snapshot
from test_agent import ReceiptModel, ScriptedModel, call, answer, plan, paired

pytestmark = pytest.mark.docker
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.skipif(os.name != "nt" or os.environ.get("LABWEAVER_APPDATA_ACCEPTANCE") != "1",
                    reason="Opt-in Windows default-AppData bind transport acceptance")
def test_windows_default_appdata_transports_only_selected_synthetic_input(monkeypatch):
    """Exercise real Store-host AppData redirection without any personal data."""
    from labweaver.web.settings import default_data_dir

    owned = default_data_dir() / "diagnostics"
    owned.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bind-acceptance-", dir=owned) as folder:
        nominal = Path(folder)
        source = nominal / "synthetic.csv"
        source.write_text("编号,数量\n001,2\n002,3\n", encoding="utf-8")
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        monkeypatch.setattr(tempfile, "tempdir", str(nominal))
        executor = DockerExecutor()
        mounted = []
        original_command = executor.command

        def command(docker, image, name, input_dir):
            mounted.append(Path(input_dir))
            return original_command(docker, image, name, input_dir)

        monkeypatch.setattr(executor, "command", command)
        record, assets = executor.execute(
            "from helper import load_dataset, emit_metric\n"
            "df = load_dataset()\n"
            "assert df['c1'].tolist() == ['001', '002']\n"
            "emit_metric(int(df['c2'].astype(int).sum()), 'sum', '合计')\n",
            load_csv_snapshot(source),
            [{"id": "sum", "kind": "metric", "description": "全量合计"}],
        )
        assert record["status"] == "completed", record.get("error")
        assert record["exit_code"] == 0 and record["container_removed"]
        assert next(iter(assets.values()))["metadata"]["value"] == 5
        assert mounted[0].parent == nominal.resolve()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before


@pytest.fixture(autouse=True)
def require_docker():
    try:
        DockerExecutor().preflight()
    except ExecutionError as exc:
        if os.environ.get("LABWEAVER_REQUIRE_DOCKER") == "1":
            pytest.fail(f"Real Docker required: {exc}")
        pytest.skip(f"Real Docker unavailable: {exc.code}")


def run_case(file, task, tmp_path):
    path = FIXTURES / file
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    session = create_session(
        path, OfflineIntakeModel(), analysis_model=OfflineAnalysisModel()
    )
    result = session.invoke(task)
    assert result["status"] == "completed", {
        "error": result.get("error"),
        "executions": result.get("code_executions"),
    }
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert (
        len(result["analysis_runs"]) == 1
        and result["code_executions"][0]["exit_code"] == 0
    )
    assert result["code_executions"][0]["container_removed"]
    paired(result)
    saved = session.save(tmp_path)
    return session, saved


def test_composite_cross_table_and_grouped_bar(tmp_path):
    session, report = run_case(
        "composite.csv",
        "拆分组合编码，按类别和区域交叉统计数量，绘制分组柱状图",
        tmp_path,
    )
    table = next(
        a for a in session.artifact_assets.values() if a["metadata"]["kind"] == "table"
    )
    columns, rows = csv_table(table["files"]["table.csv"])
    assert columns == ["类别", "East", "West"]
    assert rows == [
        {"类别": "A", "East": "6", "West": "3"},
        {"类别": "B", "East": "5", "West": "1"},
    ]
    assert len(report["chart_paths"]) == 1
    assert Path(report["chart_paths"][0]["figure.png"]).stat().st_size > 1000


@pytest.mark.parametrize(
    "frequency,expected", [("日", ["5", "4", "3"]), ("周", ["12"])]
)
def test_date_resampling_peak_valley_difference(tmp_path, frequency, expected):
    session, report = run_case(
        "dates.csv",
        f"解析日期，按{frequency}汇总数量，计算峰值、谷值和差值，绘制趋势图",
        tmp_path,
    )
    table = next(
        a for a in session.artifact_assets.values() if a["metadata"]["kind"] == "table"
    )
    _, rows = csv_table(table["files"]["table.csv"])
    assert [r["数量"] for r in rows] == expected
    assert [r["日期"] for r in rows] == sorted(r["日期"] for r in rows)
    metric = next(
        a["metadata"]["value"]
        for a in session.artifact_assets.values()
        if a["metadata"]["kind"] == "metric"
    )
    assert metric["差值"] == (2 if frequency == "日" else 0)
    if frequency == "日":
        assert metric == {
            "峰值日期": "2026-12-01",
            "峰值": 5,
            "谷值日期": "2026-12-03",
            "谷值": 3,
            "差值": 2,
        }
    replay = replay_analysis(report["record_path"], FIXTURES / "dates.csv")
    assert (
        replay["status"] == "completed"
        and replay["model_calls"] == replay["analysis_model_calls"] == 0
    )
    assert (
        Path(replay["result_csv_paths"][0]).read_bytes()
        == Path(report["result_csv_paths"][0]).read_bytes()
    )


def test_clean_missing_and_duplicates(tmp_path):
    session, report = run_case(
        "cleaning.csv",
        "先去除完全重复记录，再将数量空白填为0，派生双倍数量，导出数据并报告前后变化",
        tmp_path,
    )
    table = next(
        a for a in session.artifact_assets.values() if a["metadata"]["kind"] == "table"
    )
    _, rows = csv_table(table["files"]["table.csv"])
    assert [r["编号"] for r in rows] == ["001", "002", "003"]
    assert [r["双倍数量"] for r in rows] == ["4", "0", "8"]
    metric = next(
        a["metadata"]["value"]
        for a in session.artifact_assets.values()
        if a["metadata"]["kind"] == "metric"
    )
    assert metric == {"原始行数": 4, "处理后行数": 3, "删除重复": 1, "填充缺失": 1}


def test_partial_charts_full_script_repair_and_active_only_replay(tmp_path):
    """Scripted model responses, genuine containers/plots and source-bound replay."""
    import json
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    deliveries = [{"id": "table", "kind": "table", "description": "分组完整表"},
                  {"id": "chart1", "kind": "figure", "description": "趋势图"},
                  {"id": "chart2", "kind": "figure", "description": "分布图"}]
    script = """from helper import load_dataset,emit_table,emit_figure
import matplotlib.pyplot as plt
df=load_dataset()
df['value']=df['c3'].astype(float)
result=df.groupby('c2',sort=True)['value'].sum().reset_index()
for did,title in [('chart1','完整分组结果'),('chart2','真实统计值')]:
    fig,ax=plt.subplots()
    ax.plot(result['c2'],result['value'],marker='o')
    ax.set_title(title)
    ax.set_xlabel('日期');ax.set_ylabel('数量')
    fig.tight_layout()
    emit_figure(fig,result,did,title)
    plt.close(fig)
"""

    class RepairModel(OfflineAnalysisModel):
        def _generate(self, messages, **kwargs):
            outputs = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == 'execute_python']
            if not outputs or outputs[-1]['status'] == 'error':
                count = len(outputs)
                code = script + ("emit_table(result,'table','分组完整表')\n" if count else '')
                message = call('execute_python', f'code-{count}', {'code': code})
            else:
                message = AIMessage(content=json.dumps({'status': 'completed', 'artifact_ids': outputs[-1]['artifact_ids'], 'summary': '已核对三项成果'}))
            return ChatResult(generations=[ChatGeneration(message=message)])

    main = ScriptedModel(responses=[call(), call('set_task_plan', 'plan', {'deliverables': deliveries}),
                                   call('delegate_analysis', 'delegate', {'instruction': '计算分组结果并绘制两图', 'deliverable_ids': [d['id'] for d in deliveries]}),
                                   answer('calculation', '已经真实执行并修正，结果与图表已导出。')])
    source = FIXTURES / 'dates.csv'
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    session = create_session(source, main, analysis_model=RepairModel())
    report = session.invoke('计算分组结果并绘制两图')
    assert report['status'] == 'completed', report.get('error')
    assert len(report['code_executions']) == 2
    assert all(e['exit_code'] == 0 and e['container_removed'] for e in report['code_executions'])
    assert len(session.artifact_assets) == 3
    assert {a['execution_id'] for a in report['artifacts']} == {report['code_executions'][1]['id']}
    saved = session.save(tmp_path / 'exports')
    replayed = replay_analysis(saved['record_path'], source)
    assert replayed['status'] == 'completed'
    assert len(replayed['code_executions']) == 1 and len(replayed['artifacts']) == 3
    assert Path(saved['result_csv_paths'][0]).read_bytes() == Path(replayed['result_csv_paths'][0]).read_bytes()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def execute_script(code, tmp_path, *, timeout=60):
    path = tmp_path / "data.csv"
    path.write_text("id,value,value\n001,NA,NULL\n002,0, \n", encoding="utf-8")
    before = path.read_bytes()
    executor = DockerExecutor(ExecutionConfig(timeout=timeout))
    record, assets = executor.execute(code, load_csv_snapshot(path), plan())
    assert path.read_bytes() == before
    return executor, record, assets


def test_artifact_kind_mismatch_has_actionable_error(tmp_path):
    executor = DockerExecutor()
    record, assets = executor.execute(
        "from helper import load_dataset,emit_table\nemit_table(load_dataset(), 'notes')",
        load_csv_snapshot(FIXTURES / "cleaning.csv"),
        [{"id": "notes", "kind": "explanation", "description": "支撑事实"}],
    )
    assert record["status"] == "error" and not assets
    assert record["error"]["code"] == "artifact_rejected"
    assert "authorized deliverable" in record["error"]["message"]
    assert record["container_removed"]


def test_sandbox_blocks_network_host_files_and_input_writes(tmp_path):
    code = """from helper import load_dataset, emit_table
import pandas as pd, socket, os
from pathlib import Path
assert os.geteuid() == 65532
assert not any('API_KEY' in k for k in os.environ)
assert not any(os.environ.get(k) for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy'))
assert not Path('/var/run/docker.sock').exists()
try:
    Path('/input/dataset.json').write_text('changed')
    raise AssertionError('write allowed')
except OSError:
    pass
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
    raise AssertionError('network allowed')
except OSError:
    pass
df = load_dataset()
assert df.iloc[0].tolist() == ['001', 'NA', 'NULL']
emit_table(df, 'table')
"""
    _, record, assets = execute_script(code, tmp_path)
    assert record["status"] == "completed", record
    assert next(iter(assets.values()))["metadata"]["row_count"] == 2


@pytest.mark.parametrize(
    "code,error",
    [
        ("while True: pass", "execution_timeout"),
        ("x = bytearray(2 * 1024**3)", "memory_limit"),
        ("raise ValueError('bad number')", "python_failed"),
        (
            "from helper import emit_metric\nemit_metric(float('nan'),'table')",
            "python_failed",
        ),
    ],
)
def test_failure_limits_and_cleanup(tmp_path, code, error):
    _, record, assets = execute_script(
        code, tmp_path, timeout=8 if error != "execution_timeout" else 2
    )
    assert record["status"] == "error" and record["error"]["code"] == error, record
    assert record["container_removed"] and not assets


def test_symlink_artifact_and_large_output_rejected(tmp_path):
    for suffix in [
        "p.symlink_to('/input/dataset.json')",
        "p.write_bytes(b'x' * (51 * 1024**2))",
    ]:
        code = (
            "from pathlib import Path\nimport json\nr=Path('/work/artifacts/a'); r.mkdir(parents=True)\n(r/'metadata.json').write_text(json.dumps({'kind':'table','deliverable_id':'table','label':'invalid'}))\np=r/'table.csv'\n"
            + suffix
        )
        _, record, assets = execute_script(code, tmp_path)
        assert (
            record["status"] == "error"
            and record["error"]["code"] == "artifact_rejected"
        ), record
        assert not assets and record["container_removed"]


def test_cancel_cleans_running_container(tmp_path):
    executor = DockerExecutor()
    snapshot = load_csv_snapshot(FIXTURES / "composite.csv")
    holder = []
    thread = threading.Thread(
        target=lambda: holder.append(
            executor.execute("import time; time.sleep(60)", snapshot, plan())
        )
    )
    thread.start()
    deadline = time.monotonic() + 20
    while not executor._container and time.monotonic() < deadline:
        time.sleep(0.05)
    assert executor._container
    time.sleep(1)
    executor.cancel()
    thread.join(20)
    assert not thread.is_alive() and holder[0][0]["error"]["code"] == "cancelled"
    assert holder[0][0]["container_removed"]


def test_first_code_error_repaired_in_real_agent(tmp_path):
    main = ScriptedModel(
        responses=[
            call(),
            call("set_task_plan", "plan", {"deliverables": plan()}),
            call(
                "delegate_analysis",
                "delegate",
                {"instruction": "返回完整表", "deliverable_ids": ["table"]},
            ),
            answer("calculation"),
        ]
    )
    child = ScriptedModel(
        responses=[
            call("execute_python", "first", {"code": "raise NameError('repair me')"}),
            call(
                "execute_python",
                "second",
                {
                    "code": "from helper import load_dataset,emit_table\nemit_table(load_dataset(),'table')"
                },
            ),
        ]
    )

    class Finish(ReceiptModel):
        def _generate(self, messages, **kwargs):
            if child._cursor < 2:
                return child._generate(messages, **kwargs)
            return super()._generate(messages, **kwargs)

    session = create_session(FIXTURES / "composite.csv", main, analysis_model=Finish())
    report = session.invoke("统计完整表")
    assert report["status"] == "completed", report
    assert [e["status"] for e in report["code_executions"]] == ["error", "completed"]


def test_280_groups_complete_and_integer_precision(tmp_path):
    path = tmp_path / "groups.csv"
    large = 10**20
    path.write_text(
        "code,value\n" + "".join(f"{i:03},{large}\n{i:03},1\n" for i in range(280)),
        encoding="utf-8",
    )
    code = """from helper import load_dataset,emit_table
df = load_dataset()
df['value'] = df['c2'].map(int).astype(object)
result = df.groupby('c1', sort=True)['value'].sum().reset_index()
assert len(result) == 280
assert all(v == 10**20 + 1 for v in result['value'])
emit_table(result, 'table')
"""
    record, assets = DockerExecutor().execute(code, load_csv_snapshot(path), plan())
    assert record["status"] == "completed", record
    _, rows = csv_table(next(iter(assets.values()))["files"]["table.csv"])
    assert len(rows) == 280 and rows[0] == {"c1": "000", "value": str(large + 1)}


def test_column_reordering_and_new_categories_without_tool_changes(tmp_path):
    from labweaver.offline import demo_code

    path = tmp_path / "reordered.csv"
    path.write_text(
        "数量,编号,组合编码\n2,001,A-East\n3,002,C-North\n4,003,C-North\n",
        encoding="utf-8",
    )
    snapshot = load_csv_snapshot(path)
    code = demo_code(
        {
            "instruction": "拆分组合编码并统计",
            "columns": [
                {"name": n, "id": f"c{i}"} for i, n in enumerate(snapshot.headers, 1)
            ],
            "deliverables": plan(),
        }
    )
    record, assets = DockerExecutor().execute(code, snapshot, plan())
    assert record["status"] == "completed", record
    _, rows = csv_table(next(iter(assets.values()))["files"]["table.csv"])
    assert rows == [
        {"类别": "A", "East": "2", "North": "0"},
        {"类别": "C", "East": "0", "North": "7"},
    ]


def test_bounded_stdout_and_no_protocol_spoof(tmp_path):
    code = """from helper import load_dataset,emit_table
print('x'*100000)
print('{"protocol":1,"exit_code":0}')
emit_table(load_dataset(),'table')
"""
    _, record, assets = execute_script(code, tmp_path)
    assert record["status"] == "completed", record
    assert record["stdout"]["truncated"] and len(record["stdout"]["text"]) == 16 * 1024
    assert len(assets) == 1
