"""Host-owned verifier orchestration; model output cannot select verifier code."""
import copy
import hashlib
import json
from pathlib import Path

from labweaver.contracts import VerificationResult
from labweaver.runtime.artifacts import csv_table

BOUNDARY = "核验覆盖指定记录与数值关系；未自动证明全部方法、训练无泄漏、图形编码或自然语言结论。"


def verification_view(verification):
    """Keep model feedback bounded; the full independent evidence stays in the record."""
    view=copy.deepcopy({k:v for k,v in verification.items() if k!='execution'})
    for result in view.get('results',[]):
        if len(json.dumps(result.get('evidence',{}),ensure_ascii=False).encode())>4096:
            result['evidence']={'notice':'Full independent values saved in the execution record; evidence preview omitted.'}
    return view


def verify_artifacts(executor, snapshot, task_spec, assets):
    if not task_spec.get("modeling") and not task_spec.get("checks"):
        return {"version":1,"passed":True,"boundary":BOUNDARY,"results":[
            VerificationResult(check="artifact_integrity",status="passed",required=True,reason="Host validated file formats, full row counts, hashes and source ownership",evidence={"artifact_ids":list(assets)}).model_dump(),
            VerificationResult(check="numeric_correctness",status="not_checked",reason="No independent numeric contract applies to this method").model_dump(),
            VerificationResult(check="semantic_review",status="not_checked",reason="Main Agent review remains fallible; it is not an independent proof").model_dump(),
            VerificationResult(check="modeling",status="not_applicable",reason="No modeling contract").model_dump()]}
    records=[]
    for asset in assets.values():
        meta=asset["metadata"]
        value={k:copy.deepcopy(meta.get(k)) for k in ["id","kind","deliverable_id","value"]}
        if meta["kind"]=="table":
            value["columns"],value["rows"]=csv_table(asset["files"]["table.csv"])
        records.append(value)
    program=Path(__file__).with_name("verification_program.py").read_text(encoding="utf-8")
    execution, outputs=executor.execute(program,snapshot,[{"id":"verification","kind":"metric","description":"Trusted independent checks"}],
        verification_input={"task_spec":task_spec,"artifacts":records})
    if execution["status"]!="completed" or len(outputs)!=1:
        results=[VerificationResult(check="verifier_execution",status="failed",required=True,reason="Independent verifier failed",evidence={"error":execution.get("error")}).model_dump()]
    else:
        results=[VerificationResult.model_validate(item).model_dump() for item in next(iter(outputs.values()))["metadata"]["value"]]
    results.insert(0,VerificationResult(check="artifact_integrity",status="passed",required=True,
        reason="Host validated file formats, full row counts, hashes and source ownership", evidence={"artifact_ids":list(assets),"source_sha256":snapshot.source["sha256"]}).model_dump())
    return {"version":1,"program_sha256":hashlib.sha256(program.encode()).hexdigest(),"results":results,
            "passed":not any(r["required"] and r["status"]=="failed" for r in results),
            "execution":execution,"boundary":BOUNDARY}
