"""Actionable admission errors through the real main-agent graph, no networking."""
from test_agent import ScriptedModel, call, answer, csv_path, no_tracing, ReceiptModel, inject_executor, plan
from test_complex_analysis import no_network
from labweaver.agent import create_session


def test_invalid_delivery_kind_has_specific_repair_and_does_not_freeze(csv_path):
    spec={'goal':'Train regression','modeling':{'task_type':'regression','target':'c2','features':['c1'],
          'prediction_delivery':'pred','metadata_delivery':'meta','metrics_delivery':'metrics'}}
    plan=[{'id':id,'kind':kind,'description':id} for id,kind in [('pred','table'),('meta','table'),('metrics','metric')]]
    model=ScriptedModel(responses=[call(),call('set_task_plan','bad',{'deliverables':plan,'task_spec':spec}),answer()])
    session=create_session(csv_path,model)
    report=session.invoke('Train regression')
    result=next(e['result'] for e in report['execution_ledger'] if e['name']=='set_task_plan')
    assert result['error']['code']=='invalid_task_spec'
    assert "'meta'" in result['message'] and "kind='metric'" in result['message']
    assert session.task_spec is None and session.deliverables==[]


def test_wrong_nesting_is_repairable_within_existing_plan_budget(csv_path):
    plan=[{'id':'overview','kind':'explanation','description':'Overview from actual profile'}]
    bad={'deliverables':plan,'task_spec':{'goal':'Overview'},'modeling':{'private_value':'SENTINEL'}}
    good={'deliverables':plan,'task_spec':{'goal':'Overview'}}
    model=ScriptedModel(responses=[call(),call('set_task_plan','bad',bad),call('set_task_plan','fixed',good),answer()])
    session=create_session(csv_path,model)
    report=session.invoke('概览字段')
    result=next(e['result'] for e in report['execution_ledger'] if e['name']=='set_task_plan')
    assert result['argument_errors'][0]['location']==['modeling']
    assert result['argument_errors'][0]['type']=='extra_forbidden'
    assert 'SENTINEL' not in str(result)
    assert report['status']=='completed'
    assert session.harness.tool_counts['set_task_plan']==2


def test_repeated_bad_plans_still_exhaust_original_budget(csv_path):
    bad={'deliverables':[{'id':'overview','kind':'explanation','description':'Overview'}],'wrong':'value'}
    model=ScriptedModel(responses=[call(),*[call('set_task_plan',f'bad-{n}',bad) for n in range(4)]])
    session=create_session(csv_path,model)
    report=session.invoke('概览字段')
    assert report['status']=='error' and report['error']['code']=='tool_budget_exhausted'
    assert session.task_spec is None


def test_two_distinct_admission_errors_allow_one_final_correction(csv_path):
    plan=[{'id':'overview','kind':'explanation','description':'Use profile evidence'}]
    bad={'deliverables':plan,'unknown':'value'}
    bad_type={'deliverables':plan,'task_spec':{'goal':'overview','assumptions':'must be a list'}}
    good={'deliverables':plan,'task_spec':{'goal':'overview'}}
    model=ScriptedModel(responses=[call(),call('set_task_plan','first',bad),call('set_task_plan','second',bad_type),call('set_task_plan','fixed',good),answer()])
    session=create_session(csv_path,model)
    report=session.invoke('概览字段')
    assert report['status']=='completed'
    assert session.harness.tool_counts['set_task_plan']==3
    assert session.harness.model_calls==5 and session.harness.model_limit==12


def test_older_good_delivery_cannot_hide_retained_failed_check(csv_path,monkeypatch):
    checked=[]
    def verify(*args):
        checked.append(True)
        return {'passed':len(checked)==1,'results':[]}
    monkeypatch.setattr('labweaver.runtime.verification.verify_artifacts',verify)
    delegate=lambda cid:call('delegate_analysis',cid,{'instruction':'Compute groups','deliverable_ids':['table']})
    model=ScriptedModel(responses=[call(),call('set_task_plan','plan',{'deliverables':plan()}),delegate('one'),delegate('two'),answer('calculation')])
    session=create_session(csv_path,model,analysis_model=ReceiptModel())
    inject_executor(session,monkeypatch)
    report=session.invoke('统计各组')
    assert not report['missing_deliverables']
    assert report['status']=='error' and report['error']['code']=='verification_failed'
    assert any(not v['passed'] for v in report['verification'])


def test_failed_verification_can_receive_bounded_delivery_repair(csv_path,monkeypatch):
    checked=[]
    def verify(*args):
        checked.append(True)
        return {'passed':len(checked)>1,'results':[]}
    monkeypatch.setattr('labweaver.runtime.verification.verify_artifacts',verify)
    delegate=lambda cid:call('delegate_analysis',cid,{'instruction':'Compute groups','deliverable_ids':['table']})
    model=ScriptedModel(responses=[call(),call('set_task_plan','plan',{'deliverables':plan()}),delegate('bad'),answer('calculation'),delegate('fixed'),answer('calculation')])
    session=create_session(csv_path,model,analysis_model=ReceiptModel())
    calls=inject_executor(session,monkeypatch)
    report=session.invoke('统计各组')
    assert report['status']=='completed',report.get('error')
    assert len(calls)==2 and len(report['completion_feedback'])==1
    assert all(v['passed'] for v in report['verification'])
