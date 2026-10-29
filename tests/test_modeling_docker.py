"""Real training AND a separate trusted verification container. No model credentials."""
import hashlib
import json
from pathlib import Path
import pytest
from labweaver.contracts import TaskSpec
from labweaver.runtime.execution import DockerExecutor,ExecutionConfig
from labweaver.runtime.verification import verify_artifacts
from labweaver.tools.csv_profile import load_csv_snapshot

pytestmark=pytest.mark.docker
ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("target,kind",[("c5","classification"),("c6","classification"),("c7","regression")])
def test_real_tabular_modeling_and_independent_checks(tmp_path,target,kind):
    source=ROOT/"evaluation/fixtures/learning.csv"
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    snapshot=load_csv_snapshot(source)
    spec=TaskSpec(goal="Public synthetic modeling",modeling=dict(task_type=kind,target=target,features=['c2','c3','c4'],
        split="stratified" if kind=="classification" else "random",prediction_delivery="predictions",metadata_delivery="metadata",metrics_delivery="metrics")).model_dump()
    deliveries=[dict(id=id,kind=k,description=id) for id,k in [('predictions','table'),('metadata','metric'),('metrics','metric'),('chart','figure')]]
    code=(ROOT/'tests/fixtures/modeling_script.py').read_text(encoding='utf-8').replace('__SPEC__',json.dumps(spec['modeling']))
    executor=DockerExecutor(ExecutionConfig(timeout=120))
    record,assets=executor.execute(code,snapshot,deliveries)
    assert record['status']=='completed',record.get('stderr') or record.get('error')
    verification=verify_artifacts(executor,snapshot,spec,assets)
    assert verification['passed'],verification
    assert verification['execution']['container_name'] != record['container_name']
    assert record['container_removed'] and verification['execution']['container_removed']
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    for asset in assets.values():
        if asset['metadata']['kind']=='figure':
            for name,raw in asset['files'].items(): (tmp_path/name).write_bytes(raw)
    checks={r['check']:r for r in verification['results']}
    assert checks['prediction_coverage']['evidence']['prediction_count']==30
    assert checks['training_leakage']['status']=='not_checked'
    # A malicious/wrong numeric claim does not become independently verified.
    metrics=next(a for a in assets.values() if a['metadata']['deliverable_id']=='metrics')
    key='accuracy' if kind=='classification' else 'mae'
    metrics['metadata']['value']['model'][key]=12345
    rejected=verify_artifacts(executor,snapshot,spec,assets)
    assert not rejected['passed']
    assert any(r['check']=='prediction_metrics' and r['status']=='failed' for r in rejected['results'])


def test_real_second_delegation_and_replay_keep_modeling_evidence(tmp_path):
    from labweaver.analysis import AnalysisRunner
    from test_complex_analysis import ReceiptAfterRepairModel, code_call
    from labweaver.runtime.records import save_run
    from labweaver.runtime.replay import replay_analysis
    source=ROOT/'evaluation/fixtures/learning.csv'
    snapshot=load_csv_snapshot(source)
    spec=TaskSpec(goal='Two-stage regression and chart',modeling=dict(task_type='regression',target='c7',features=['c2','c3','c4'],split='random',prediction_delivery='predictions',metadata_delivery='metadata',metrics_delivery='metrics')).model_dump()
    deliveries=[dict(id=i,kind=k,description=i) for i,k in [('predictions','table'),('metadata','metric'),('metrics','metric'),('chart','figure')]]
    code=(ROOT/'tests/fixtures/modeling_script.py').read_text(encoding='utf-8').replace('__SPEC__',json.dumps(spec['modeling'])).split('fig,ax=plt.subplots()')[0]
    assets={}
    def register(items,operation):
        if operation=='bytes':return 50*1024*1024-sum(len(b) for a in assets.values() for b in a['files'].values())
        if operation=='figures':return 2-sum(a['metadata']['kind']=='figure' for a in assets.values())
        assert operation=='add'
        for a in items.values():a['metadata'].update(session_id='two-stage',task_id='modeling')
        assets.update(items)
    runner=AnalysisRunner(ReceiptAfterRepairModel(responses=[code_call(code)]),DockerExecutor(ExecutionConfig()),get_snapshot=lambda:snapshot,get_task_spec=lambda:spec,register=register,read_asset=assets.__getitem__)
    first=runner.delegate('Fit and export all modeling receipts',deliveries[:3],'modeling')
    assert first['status']=='completed',first
    chart="from helper import load_dataset,emit_figure\nimport matplotlib.pyplot as plt\ndf=load_dataset();plot=df[['c7']].astype(float);fig,ax=plt.subplots();ax.hist(plot['c7']);ax.set_title('原始目标分布');emit_figure(fig,plot,'chart','原始目标分布')"
    runner.model=ReceiptAfterRepairModel(responses=[code_call(chart)])
    second=runner.delegate('Plot original target distribution',deliveries[3:],'chart')
    assert second['status']=='completed',second
    assert any(c['check']=='prediction_metrics' and c['status']=='passed' for v in second['verification'] for c in v['results'])
    report=dict(report_version=3,session_id='two-stage',task_id='modeling',task='Two-stage synthetic regression',status='completed',source=snapshot.source,parsing=snapshot.profile['parsing'],task_spec=spec,task_plan=deliveries,code_executions=runner.attempts,artifacts=[a['metadata'] for a in assets.values()],answer_text='Test fixture computed in real containers.')
    saved=save_run(report,tmp_path,artifact_assets=assets)
    replayed=replay_analysis(saved,source)
    assert replayed['status']=='completed' and replayed['model_calls']==0
    original=next(a['files']['table.csv'] for a in assets.values() if a['metadata']['deliverable_id']=='predictions')
    prediction=next(a for a in replayed['artifacts'] if a['deliverable_id']=='predictions')
    assert Path(prediction['paths']['table.csv']).read_bytes()==original
