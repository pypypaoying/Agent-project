"""Benchmark acceptance independent of the production verifier and success flag."""
import csv
import io
import math
from pathlib import Path


def check_modeling(report,assets,oracle):
    errors=[]
    spec=(report.get('task_spec') or {}).get('modeling')
    if not spec: return ['missing_modeling_contract']
    deliveries={a['metadata']['deliverable_id']:a for a in assets.values()}
    try:
        predictions=list(csv.DictReader(io.StringIO(deliveries[spec['prediction_delivery']]['files']['table.csv'].decode('utf-8-sig'))))
        meta=deliveries[spec['metadata_delivery']]['metadata']['value']
        metrics=deliveries[spec['metrics_delivery']]['metadata']['value']
        source_path=Path(__file__).parent/'fixtures'/report['source']['name']
        with source_path.open(encoding='utf-8') as f:
            source=list(csv.reader(f))[1:]
        target=int(spec['target'][1:])-1
        if len(source)!=oracle['rows'] or len(predictions)!=oracle['test_rows']: errors.append('sample_counts')
        ids=[p['source_record_id'] for p in predictions]
        if len(set(ids))!=len(ids) or set(ids)!=set(meta['test_ids']) or set(ids)&set(meta['train_ids']): errors.append('test_identity')
        training=[source[int(r[1:])-1][target] for r in meta['train_ids']]
        for p in predictions:
            truth=source[int(p['source_record_id'][1:])-1][target]
            if oracle['modeling']=='classification' and truth!=p['y_true']: errors.append('source_label')
            if oracle['modeling']=='regression' and float(truth)!=float(p['y_true']): errors.append('source_value')
        if oracle['modeling']=='regression':
            actual=[float(p['y_true']) for p in predictions]; predicted=[float(p['y_pred']) for p in predictions]
            mean=sum(map(float,training))/len(training)
            if any(not math.isclose(float(p['baseline_pred']),mean,rel_tol=1e-6) for p in predictions): errors.append('train_only_baseline')
            mae=sum(abs(a-b) for a,b in zip(actual,predicted))/len(actual)
            if not math.isclose(metrics['model']['mae'],mae,rel_tol=1e-6): errors.append('independent_mae')
        else:
            majority=min(set(training),key=lambda x:(-training.count(x),x))
            if any(p['baseline_pred']!=majority for p in predictions): errors.append('train_only_baseline')
            accuracy=sum(p['y_true']==p['y_pred'] for p in predictions)/len(predictions)
            if not math.isclose(metrics['model']['accuracy'],accuracy,abs_tol=1e-8): errors.append('independent_accuracy')
        if not any(a['metadata']['kind']=='figure' for a in assets.values()): errors.append('missing_chart')
        if any(r['status']=='failed' for g in report.get('verification',[]) for r in g['results']): errors.append('failed_production_check')
        if not report.get('verification'): errors.append('missing_production_verification')
    except (KeyError,ValueError,IndexError,TypeError,OSError): errors.append('invalid_modeling_deliverables')
    return sorted(set(errors))
