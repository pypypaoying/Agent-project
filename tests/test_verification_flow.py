"""Real agent graph / fixture executor: verifies orchestration, not numeric correctness."""
from test_complex_analysis import runner_factory, no_network, ReceiptAfterRepairModel, code_call, PLAN, table_payload, figure_payload


def test_claimed_completion_cannot_bypass_failed_verification(runner_factory,monkeypatch):
    monkeypatch.setattr('labweaver.runtime.verification.verify_artifacts',lambda *args:{'passed':False,'results':[dict(check='test_numeric_fault',status='failed',required=True,reason='Injected oracle mismatch',evidence={})]})
    runner,assets,calls,events=runner_factory(ReceiptAfterRepairModel(responses=[code_call()]))
    result=runner.delegate('Complete outputs',PLAN,'parent')
    assert result['status']=='error' and result['error']['code']=='verification_failed'
    assert assets and len(calls)==1
    assert runner.delegate('Complete outputs',PLAN,'parent')==result
    assert len(calls)==1
    assert any(e['type']=='verification_end' and not e['verification']['passed'] for e in events)


def test_repair_replaces_bad_outputs_without_resetting_budget(runner_factory,monkeypatch):
    checked=[]
    def verify(*args):
        checked.append(True)
        return {'passed':len(checked)>1,'results':[dict(check='test_numeric_fault',status='failed' if len(checked)==1 else 'passed',required=True,reason='Fixture numeric outcome',evidence={})]}
    monkeypatch.setattr('labweaver.runtime.verification.verify_artifacts',verify)
    first=code_call(); second=code_call(); second.tool_calls[0]['id']='corrected'
    runner,assets,calls,_=runner_factory(ReceiptAfterRepairModel(responses=[first,second]))
    result=runner.delegate('Complete outputs',PLAN,'parent')
    assert result['status']=='completed' and len(calls)==len(checked)==2
    assert len(runner.attempts)==2 and not runner.attempts[0]['verification']['passed']
    assert set(assets)==set(result['artifact_ids'])
    assert runner.runs[0]['model_calls']==3
    assert result['verification'][0]['passed'] is True
    assert result['verification'][0]['results'][0]['status']=='passed'


def test_later_chart_verifies_against_completed_current_task_outputs(runner_factory,monkeypatch):
    seen=[]
    def verify(executor,snapshot,spec,assets):
        seen.append({a['metadata']['deliverable_id'] for a in assets.values()})
        return {'passed':True,'results':[]}
    monkeypatch.setattr('labweaver.runtime.verification.verify_artifacts',verify)
    model=ReceiptAfterRepairModel(responses=[code_call()])
    runner,assets,calls,_=runner_factory(model,attempt_payloads=[[table_payload()],[figure_payload('chart')]])
    assert runner.delegate('Table first',PLAN,'first')['status']=='completed'
    runner.model=ReceiptAfterRepairModel(responses=[code_call()])
    charts=[{'id':'chart','kind':'figure','description':'Plot computed values'}]
    assert runner.delegate('Plot',charts,'second')['status']=='completed'
    assert seen==[{'table'},{'table','chart'}]
    runner.new_task()
    assert runner._verification_inputs({})=={}
    assert runner._repair_context({'table'}) is None


def test_second_delegation_receives_failed_attempt_and_real_check(runner_factory,monkeypatch):
    monkeypatch.setattr('labweaver.runtime.verification.verify_artifacts',lambda *args:{'passed':False,'results':[]})
    runner,_,_,_=runner_factory(ReceiptAfterRepairModel(responses=[code_call('print(42)')]))
    assert runner.delegate('Compute',PLAN,'first')['status']=='error'
    context=runner._repair_context({'table'})
    assert context['code']=='print(42)' and context['verification']['passed'] is False
    assert runner._repair_context({'other_delivery'}) is None
