"""Aggregate all planned attempts; failure denominators are never discarded."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import sys


def summarize(folder):
    runs=[json.loads(p.read_text(encoding='utf-8')) for p in Path(folder).glob('*/summary.json')]
    if not runs: return {'runs':0}
    attained=[r for r in runs if r.get('attained')]
    measured=[r['elapsed_seconds'] for r in runs if isinstance(r.get('elapsed_seconds'),(int,float))]
    attempted=[r for r in runs if r.get('code_attempts',0)>0]
    repaired=[r for r in attempted if r.get('execution_failures',0)>0]
    by_case={}
    for r in runs:
        item=by_case.setdefault(r['case'],{'runs':0,'attained':0})
        item['runs']+=1; item['attained']+=bool(r.get('attained'))
    return {'runs':len(runs),'attained':len(attained),'attainment_rate':len(attained)/len(runs),
            'first_attempt_attained':sum(bool(r.get('attained')) and r.get('no_internal_repair',False) for r in runs),
            'delivery_complete':sum(r.get('status')=='completed' and not r.get('missing_deliverables') for r in runs),
            'execution_failure_runs':len(repaired),'repaired_and_attained':sum(bool(r.get('attained')) for r in repaired),
            'median_seconds':statistics.median(measured) if measured else None,
            'p95_seconds':sorted(measured)[min(len(measured)-1,int(.95*len(measured)))] if measured else None,
            'failures':dict(Counter(error for r in runs for error in r.get('errors',[]))), 'by_case':by_case,
            'usage_unknown_runs':sum(r.get('unknown_usage_calls',0)>0 or not r.get('token_usage') for r in runs),
            'usage_call_count_unmeasured_runs':sum('unknown_usage_calls' not in r for r in runs),
            'known_total_tokens':sum(r.get('known_total_tokens',0) for r in runs),
            'unknown_usage_calls':sum(r.get('unknown_usage_calls',0) for r in runs)}


if __name__=='__main__':
    print(json.dumps(summarize(sys.argv[1]),ensure_ascii=False,indent=2))
