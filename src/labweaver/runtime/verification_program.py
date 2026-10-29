"""Trusted, independent calculations. Run in a fresh container, never import generated code."""
from collections import Counter
import json
import math


def close(a, b):
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(b, (int, float)):
        return isinstance(a, (int, float)) and math.isfinite(a) and math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-8)
    if isinstance(b, dict):
        return isinstance(a, dict) and all(k in a and close(a[k], v) for k, v in b.items())
    if isinstance(b, list):
        return isinstance(a, list) and len(a)==len(b) and all(close(x,y) for x,y in zip(a,b))
    return a == b


def metrics_match(reported, expected, task_type):
    """Keep raw classification accuracy consistent with the independent oracle."""
    if not close(reported, expected):
        return False
    if task_type == "classification":
        return all(math.isclose(reported[family]["accuracy"], expected[family]["accuracy"],
                                rel_tol=0, abs_tol=1e-8)
                   for family in ("model", "baseline"))
    return True


def classification(actual, predicted, labels):
    pairs = Counter(zip(actual,predicted))
    if len(labels)>500:
        raise ValueError("Independent confusion matrix exceeds supported 500-class budget")
    matrix = [[pairs[x,y] for y in labels] for x in labels]
    per_class = []
    for i, label in enumerate(labels):
        tp = matrix[i][i]
        support = sum(matrix[i])
        guesses = sum(row[i] for row in matrix)
        per_class.append(dict(label=label, support=support,
            precision=tp/guesses if guesses else None, recall=tp/support if support else None,
            f1=2*tp/(support+guesses) if support+guesses else None))
    defined = [p["f1"] for p in per_class if p["f1"] is not None]
    return {"accuracy":sum(a==p for a,p in zip(actual,predicted))/len(actual),
            "macro_f1":sum(defined)/len(defined) if defined else None,
            "labels":labels,"confusion_matrix":matrix,"per_class":per_class}


def regression(actual, predicted):
    residual = [a-p for a,p in zip(actual,predicted)]
    mse = math.fsum(r*r for r in residual)/len(actual)
    mean = math.fsum(actual)/len(actual)
    variance = math.fsum((a-mean)**2 for a in actual)
    return {"mae":math.fsum(abs(r) for r in residual)/len(actual), "rmse":math.sqrt(mse),
            "r2":1-math.fsum(r*r for r in residual)/variance if len(actual)>=2 and variance>0 else None}


def verify(dataset, request):
    results = []
    def record(name, status, reason, *, required=False, **evidence):
        results.append(dict(check=name,status=status,reason=reason,required=required,evidence=evidence))
    def test(name, valid, reason, **evidence):
        record(name, "passed" if valid else "failed", reason, required=True, **evidence)
        return valid
    spec = request.get("task_spec") or {}
    assets = request["artifacts"]
    by_delivery = {a["deliverable_id"]:a for a in assets}
    for relation in spec.get("checks", []):
        name = "fact:"+relation["metric_delivery"]+":"+relation["metric_key"]
        try:
            table, metric = by_delivery[relation["table_delivery"]], by_delivery[relation["metric_delivery"]]
            if relation["kind"]=="row_count": value=len(table["rows"])
            else:
                values=[float(r[relation["column"]]) for r in table["rows"] if str(r[relation["column"]]).strip()]
                if not values or not all(math.isfinite(x) for x in values): raise ValueError("nonfinite or empty")
                value={"sum":math.fsum,"mean":lambda x:math.fsum(x)/len(x),"min":min,"max":max}[relation["kind"]](values)
            reported = metric["value"][relation["metric_key"]]
            test(name,close(reported,value),"Registered table relation recomputed",expected=value,reported=reported,
                 artifact_ids=[table["id"],metric["id"]])
        except (ValueError,KeyError,TypeError,OverflowError):
            test(name,False,"Missing or invalid registered inputs for declared relation")
    model = spec.get("modeling")
    record("semantic_review", "not_checked", "Main Agent narrative is a fallible semantic review, not an independent proof.")
    record("method_correctness", "not_checked", "No independent checker for arbitrary algorithms or all narrative claims.")
    record("chart_rendering", "not_checked", "Host checks image formats and paired data hashes; visual encoding is not numerically proved.")
    if not model:
        record("modeling", "not_applicable", "No modeling contract in this task.")
        return results
    record("training_leakage", "not_checked", "Generated code can access the full snapshot; checks cannot prove absence of training leakage or the claimed estimator's provenance.")
    # Report ALL shape mismatches together before numerical work. A vague
    # KeyError wastes a bounded repair on guessing which receipt is malformed.
    schema_errors=[]
    for key,fields in [('metadata_delivery',['target','features','split','seed','test_size','duplicate_policy','train_ids','test_ids','excluded_ids','model','preprocessing']),
                       ('metrics_delivery',['model','baseline'])]:
        delivery=model[key]
        value=by_delivery.get(delivery,{}).get('value')
        if not isinstance(value,dict): schema_errors.append({'deliverable_id':delivery,'expected':'JSON object','missing':fields})
        else:
            missing=[k for k in fields if k not in value]
            if missing: schema_errors.append({'deliverable_id':delivery,'missing':missing,'actual_keys':list(value)})
    delivery=model['prediction_delivery']
    prediction=by_delivery.get(delivery,{})
    columns=prediction.get('columns') or (list(prediction['rows'][0]) if prediction.get('rows') else [])
    missing=[k for k in ['source_record_id','y_true','y_pred','baseline_pred'] if k not in columns]
    if missing: schema_errors.append({'deliverable_id':delivery,'missing_columns':missing,'actual_columns':columns})
    metrics=by_delivery.get(model['metrics_delivery'],{}).get('value')
    if isinstance(metrics,dict):
        required=['accuracy','macro_f1','labels','confusion_matrix','per_class'] if model['task_type']=='classification' else ['mae','rmse','r2']
        for family in ['model','baseline']:
            block=metrics.get(family)
            missing=[k for k in required if not isinstance(block,dict) or k not in block]
            if missing: schema_errors.append({'deliverable_id':model['metrics_delivery'],'json_object':family,'missing':missing})
    if not test('modeling_schema',not schema_errors,'Use exact registered artifact keys from artifact_contracts; keep additional metrics outside the required objects',differences=schema_errors):
        return results
    try:
        prediction = by_delivery[model["prediction_delivery"]]
        metadata = by_delivery[model["metadata_delivery"]]["value"]
        metrics = by_delivery[model["metrics_delivery"]]["value"]
        target = int(model["target"][1:])-1
        source = dict(zip(dataset["record_ids"], dataset["rows"]))
        required_meta = ["target","features","split","seed","test_size","duplicate_policy"]
        test("modeling_contract",all(metadata.get(k)==model[k] for k in required_meta),"Metadata agrees with frozen task contract",expected={k:model[k] for k in required_meta})
        test("method_record",all(metadata.get(k) for k in ["model","preprocessing"]),"Model and preprocessing descriptions recorded (not independently proved).")
        train, heldout, excluded = metadata["train_ids"],metadata["test_ids"],metadata["excluded_ids"]
        validation = metadata.get("validation_ids",[])
        lists=[train,heldout,excluded,validation]
        valid_ids = all(isinstance(ids,list) and all(isinstance(i,str) and i in source for i in ids) and len(ids)==len(set(ids)) for ids in lists)
        if not test("sample_identity",valid_ids,"IDs must be unique original source records"):
            return results
        if not test("split_disjoint",bool(train) and bool(heldout) and not (set(train)&set(heldout) or set(train)&set(excluded) or set(heldout)&set(excluded)) and set(validation)<=set(train),"Train/test/excluded disjoint; validation belongs to training portion"):
            return results
        expected_excluded=[]
        seen=set()
        for rid,row in source.items():
            if not row[target].strip(): expected_excluded.append(rid)
            elif model["duplicate_policy"]=="drop_exact" and tuple(row) in seen: expected_excluded.append(rid)
            else: seen.add(tuple(row))
        test("sample_coverage",set(excluded)==set(expected_excluded) and set(train+heldout+excluded)==set(source),"All source records accounted for; exclusions follow contract",source_rows=len(source),train_rows=len(train),test_rows=len(heldout),excluded_rows=len(excluded),expected_excluded_count=len(expected_excluded))
        if model["split"] in {"random","stratified"}:
            from sklearn.model_selection import train_test_split
            excluded_set=set(expected_excluded)
            eligible=[rid for rid in source if rid not in excluded_set]
            labels=[source[i][target] for i in eligible]
            xtrain,xtest=train_test_split(eligible,test_size=model["test_size"],random_state=model["seed"],stratify=labels if model["split"]=="stratified" else None)
            test("split_policy",set(xtrain)==set(train) and set(xtest)==set(heldout),"Recreated seeded split matches metadata")
        else:
            record("split_policy","not_checked","Time/group/custom policy needs a separate domain-specific audit.")
        records=[[row[k] for k in ["source_record_id","y_true","y_pred","baseline_pred"]] for row in prediction["rows"]]
        if not test("prediction_coverage",len(records)==len(heldout) and len({r[0] for r in records})==len(records) and {r[0] for r in records}==set(heldout),"Exactly one prediction per test record",prediction_count=len(records),test_count=len(heldout)):
            return results
        ids,actual,predicted,baseline=map(list,zip(*records))
        if model["task_type"]=="regression":
            actual,predicted,baseline=[[float(v) for v in xs] for xs in [actual,predicted,baseline]]
            truth=[float(source[i][target]) for i in ids]
            training_values=[float(source[i][target]) for i in train]
            if not all(math.isfinite(v) for xs in [actual,predicted,baseline,truth,training_values] for v in xs): raise ValueError("nonfinite")
            base_value=math.fsum(training_values)/len(train)
            expected_base=[base_value]*len(ids)
            expected_metrics={"model":regression(actual,predicted),"baseline":regression(actual,expected_base)}
        else:
            truth=[source[i][target] for i in ids]
            counts=Counter(source[i][target] for i in train)
            base_value=sorted(counts,key=lambda x:(-counts[x],x))[0]
            expected_base=[base_value]*len(ids)
            labels=sorted({source[i][target] for i in train+heldout})
            test("prediction_labels",set(predicted)<=set(labels),"Predicted labels belong to original target domain")
            expected_metrics={"model":classification(actual,predicted,labels),"baseline":classification(actual,expected_base,labels)}
        test("true_labels",close(actual,truth),"Test truth matches original source target",artifact_ids=[prediction["id"]])
        test("baseline_predictions",close(baseline,expected_base),"Baseline recomputed from training targets; majority ties use lexical label order",expected_baseline=base_value)
        test("prediction_metrics",metrics_match(metrics,expected_metrics,model["task_type"]),"Metrics recomputed from full predictions; preserve raw precision (accuracy absolute tolerance 1e-8); undefined denominators yield null",expected=expected_metrics,artifact_ids=[prediction["id"],by_delivery[model["metrics_delivery"]]["id"]])
        undefined = any(v is None for family in expected_metrics.values() for v in family.values()) or any(p.get("precision") is None or p.get("recall") is None or p.get("f1") is None for family in expected_metrics.values() for p in family.get("per_class",[]))
        if undefined:
            test("undefined_metric_reason",bool(metrics.get("undefined_reason")),"Undefined metrics require a reason, not forced zero")
    except (KeyError,ValueError,TypeError,IndexError,OverflowError,ZeroDivisionError) as exc:
        test("modeling_inputs",False,"Missing/malformed modeling receipts, invalid numbers or impossible split",exception_type=type(exc).__name__,detail=str(exc)[:200])
    return results


if __name__ == "__main__":
    from pathlib import Path
    from helper import emit_metric
    dataset=json.loads(Path("/input/dataset.json").read_text())
    request=json.loads(Path("/input/verification.json").read_text())
    emit_metric(verify(dataset,request),"verification","Independent verification")
