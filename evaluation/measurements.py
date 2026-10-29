"""Definitions shared by live collection and scoring saved records."""


def measurements(report):
    children=report.get('analysis_runs',[])
    executions=[e for run in children for e in run.get('execution_ledger',[]) if e.get('name')=='execute_python']
    failures=sum(e.get('result',{}).get('status')=='error' for e in executions)
    main_failures=sum(e.get('result',{}).get('status')=='error' for e in report.get('execution_ledger',[]))
    retries=len(report.get('model_diagnostics',[]))+sum(len(r.get('model_diagnostics',[])) for r in children)
    usage=report.get('token_usage') or {}
    calls=[*usage.get('main',[]),*usage.get('analysis',[])]
    known=[c['usage'] for c in calls if c.get('usage') is not None]
    expected=report.get('model_calls',0)+report.get('analysis_model_calls',0)
    return {'code_attempts':len(report.get('code_executions',[])),
            'execution_failures':failures,'main_tool_failure_count':main_failures,'model_response_retries':retries,
            'no_internal_repair':not (failures or main_failures or retries or report.get('completion_feedback')),
            'known_total_tokens':sum(c['total_tokens'] for c in known),
            'unknown_usage_calls':max(0,expected-len(known))}


def trial_measurements(reports):
    parts=[measurements(report) for report in reports]
    combined={key:sum(p[key] for p in parts) for key in parts[0] if key!='no_internal_repair'}
    combined['no_internal_repair']=all(p['no_internal_repair'] for p in parts)
    combined['model_calls']=sum(r.get('model_calls',0) for r in reports)
    combined['analysis_model_calls']=sum(r.get('analysis_model_calls',0) for r in reports)
    return combined
