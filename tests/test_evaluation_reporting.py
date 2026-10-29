"""Release accounting must not accept duplicate or mixed-revision trials."""
import hashlib
import importlib.util
import json
from pathlib import Path


def report_module(monkeypatch):
    root=Path(__file__).resolve().parents[1]/'evaluation'
    monkeypatch.syspath_prepend(str(root))
    spec=importlib.util.spec_from_file_location('evaluation_report',root/'report.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_duplicate_trials_and_mixed_snapshots_are_not_a_complete_cohort(tmp_path,monkeypatch):
    report=report_module(monkeypatch)
    suite=tmp_path/'suite.json'
    suite.write_text('{"version":2,"cases":[]}',encoding='utf-8')
    digest=hashlib.sha256(suite.read_bytes()).hexdigest()
    for repeat in [1,2,3]:
        output=tmp_path/str(repeat);output.mkdir()
        (output/'summary.json').write_text(json.dumps(dict(case='M3',repeat=repeat,suite_sha256=digest,implementation_sha256='one')),encoding='utf-8')
    identity=report.batch_identity(tmp_path)
    assert identity['suite_version']==2 and identity['consistent_snapshot']
    assert identity['unique_trial_ids'] and identity['complete_repetitions']
    last=tmp_path/'3/summary.json'
    value=json.loads(last.read_text());value.update(repeat=2,implementation_sha256='two')
    last.write_text(json.dumps(value),encoding='utf-8')
    identity=report.batch_identity(tmp_path)
    assert not identity['unique_trial_ids'] and not identity['complete_repetitions']
    assert not identity['consistent_snapshot']
