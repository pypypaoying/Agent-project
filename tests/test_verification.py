"""Independent formulas, counterexamples and frozen contract validation."""
import copy
import pytest
from labweaver.contracts import TaskSpec, ModelingSpec
from labweaver.runtime.verification_program import verify, regression, classification, metrics_match
from labweaver.runtime.execution import snapshot_payload
from labweaver.tools.csv_profile import load_csv_snapshot


def case():
    dataset={"record_ids":["r000001","r000002","r000003","r000004"],"rows":[["1","1"],["2","3"],["3","5"],["4","7"]]}
    model=dict(task_type="regression",target="c2",features=["c1"],split="custom",seed=42,test_size=.5,
               duplicate_policy="keep",prediction_delivery="p",metadata_delivery="d",metrics_delivery="m")
    meta={**model,"model":"declared linear model","preprocessing":"none","train_ids":dataset["record_ids"][:2],"test_ids":dataset["record_ids"][2:],"excluded_ids":[]}
    rows=[dict(source_record_id="r000003",y_true="5",y_pred="4",baseline_pred="2"),dict(source_record_id="r000004",y_true="7",y_pred="6",baseline_pred="2")]
    metrics={"model":{"mae":1,"rmse":1,"r2":0},"baseline":{"mae":4,"rmse":(17)**.5,"r2":-16}}
    request={"task_spec":{"modeling":model},"artifacts":[{"id":"p1","deliverable_id":"p","rows":rows},
        {"id":"d1","deliverable_id":"d","value":meta},{"id":"m1","deliverable_id":"m","value":metrics}]}
    return dataset,request


def test_independent_regression_formulas():
    data,request=case()
    results=verify(data,request)
    assert not [r for r in results if r["status"]=="failed"]
    assert next(r for r in results if r["check"]=="training_leakage")["status"]=="not_checked"
    assert regression([1,1],[1,2])["r2"] is None
    assert regression([1],[1])["r2"] is None


@pytest.mark.parametrize("mutation,expected",[
    (lambda r:r["artifacts"][2]["value"]["model"].update(mae=0),"prediction_metrics"),
    (lambda r:r["artifacts"][0]["rows"].pop(),"prediction_coverage"),
    (lambda r:r["artifacts"][1]["value"]["train_ids"].append("r000003"),"split_disjoint"),
    (lambda r:r["artifacts"][0]["rows"][0].update(y_true="4"),"true_labels"),
    (lambda r:r["artifacts"][0]["rows"][0].update(baseline_pred="5"),"baseline_predictions"),
    (lambda r:r["artifacts"][0]["rows"][0].update(source_record_id="forged"),"prediction_coverage"),
    (lambda r:r["artifacts"][1]["value"].update(target="c1"),"modeling_contract"),
])
def test_mutated_evidence_is_rejected(mutation,expected):
    data,request=case(); mutation(request)
    assert any(r["check"]==expected and r["status"]=="failed" for r in verify(data,request))


def test_classification_undefined_not_silently_zero():
    result=classification(["a","b"],["a","a"],["a","b"])
    assert result["accuracy"]==.5
    assert result["macro_f1"]==pytest.approx(1/3)
    assert result["confusion_matrix"]==[[1,0],[1,0]]
    assert result["per_class"][1]["precision"] is None
    assert result["per_class"][1]["f1"]==0


def test_accuracy_precision_agrees_with_independent_benchmark_threshold():
    metrics = classification(["a", "b", "c"], ["a", "b", "b"], ["a", "b", "c"])
    expected = {"model": metrics, "baseline": metrics}
    assert metrics_match(expected, expected, "classification")
    for family in ("model", "baseline"):
        rounded = copy.deepcopy(expected)
        rounded[family]["accuracy"] = round(metrics["accuracy"], 6)
        assert not metrics_match(rounded, expected, "classification")


def test_continuous_regression_cannot_use_raw_label_stratification():
    _,request=case()
    model=request['task_spec']['modeling']
    with pytest.raises(ValueError,match='ordinary regression use random'):
        ModelingSpec(**{**model,'split':'stratified'})
    assert ModelingSpec(**{**model,'split':'custom'}).split=='custom'
    assert ModelingSpec(**{**model,'split':'random'}).split=='random'


def test_relation_checks_use_registered_full_table():
    request={"task_spec":{"checks":[dict(kind="sum",table_delivery="t",column="x",metric_delivery="m",metric_key="total")]},
             "artifacts":[dict(id="table",deliverable_id="t",rows=[{"x":"1"},{"x":"2"},{"x":""}]),dict(id="metric",deliverable_id="m",value={"total":3})]}
    assert verify({},request)[0]["status"]=="passed"
    request["artifacts"][1]["value"]["total"]=2
    assert verify({},request)[0]["status"]=="failed"


def test_task_contract_and_stable_ids(tmp_path):
    p=tmp_path/"duplicate.csv"; p.write_text("x,x\n001,NA\n001,NA\n",encoding="utf-8")
    payload=snapshot_payload(load_csv_snapshot(p))
    assert payload["record_ids"]==["r000001","r000002"]
    assert payload["rows"][0]==("001","NA")
    _,request=case()
    model=request["task_spec"]["modeling"]
    spec=TaskSpec(goal="test",modeling=model)
    with pytest.raises(ValueError): spec.validate_bindings(1,[])
    with pytest.raises(ValueError): ModelingSpec(**{**model,"features":["c2"]})


def test_unsupported_method_stays_explicitly_unchecked():
    results=verify({}, {"task_spec":{"goal":"geometric mean"},"artifacts":[]})
    assert not any(r["status"]=="failed" for r in results)
    assert any(r["check"]=="method_correctness" and r["status"]=="not_checked" for r in results)


def test_missing_target_and_exact_duplicate_exclusion_are_source_checked():
    data,request=case()
    data['rows'][3][1]=' '
    data['record_ids'].append('r000005'); data['rows'].append(data['rows'][0][:])
    request['task_spec']['modeling']['duplicate_policy']='drop_exact'
    meta=request['artifacts'][1]['value']
    meta.update(duplicate_policy='drop_exact',test_ids=['r000003'],excluded_ids=['r000004','r000005'])
    request['artifacts'][0]['rows'].pop()
    request['artifacts'][2]['value']={'model':{'mae':1,'rmse':1,'r2':None},'baseline':{'mae':3,'rmse':3,'r2':None},'undefined_reason':'One test sample: R² undefined'}
    results=verify(data,request)
    assert not any(r['status']=='failed' for r in results)
    meta['excluded_ids']=['r000004']
    assert any(r['check']=='sample_coverage' and r['status']=='failed' for r in verify(data,request))


def test_string_labels_and_baseline_tie_are_not_coerced():
    data,request=case()
    for row,label in zip(data['rows'],['02','01','01','02']): row[1]=label
    request['task_spec']['modeling']['task_type']='classification'
    request['artifacts'][0]['rows']=[
        dict(source_record_id='r000003',y_true='01',y_pred='01',baseline_pred='01'),
        dict(source_record_id='r000004',y_true='02',y_pred='01',baseline_pred='01')]
    value=classification(['01','02'],['01','01'],['01','02'])
    request['artifacts'][2]['value']={'model':value,'baseline':copy.deepcopy(value),'undefined_reason':'No class 02 predictions'}
    assert not any(r['status']=='failed' for r in verify(data,request))
    request['artifacts'][0]['rows'][0]['y_true']='1'
    assert any(r['check']=='true_labels' and r['status']=='failed' for r in verify(data,request))


def test_schema_feedback_lists_all_mismatches_before_retraining():
    data,request=case()
    del request['artifacts'][1]['value']['train_ids']
    del request['artifacts'][1]['value']['model']
    del request['artifacts'][2]['value']['model']['mae']
    results=verify(data,request)
    mismatch=next(r for r in results if r['check']=='modeling_schema')
    assert mismatch['status']=='failed'
    assert len(mismatch['evidence']['differences'])==2
