"""Publish aggregate facts only. Raw records and model-generated code stay local."""
import argparse
import hashlib
import json
from pathlib import Path
from summarize import summarize


def batch_identity(folder):
    """Disclose measured code/tasks and reject duplicate or mixed trial cohorts."""
    root = Path(folder)
    runs = [json.loads(p.read_text(encoding='utf-8')) for p in root.glob('*/summary.json')]
    task_hashes = sorted({r['suite_sha256'] for r in runs if r.get('suite_sha256')})
    code_hashes = sorted({r['implementation_sha256'] for r in runs if r.get('implementation_sha256')})
    pairs = [(r.get('case'), r.get('repeat')) for r in runs]
    suite = root/'suite.json'
    task_snapshot = json.loads(suite.read_text(encoding='utf-8')) if suite.exists() else None
    expected_hash = hashlib.sha256(suite.read_bytes()).hexdigest() if suite.exists() else None
    return {'suite_version': task_snapshot.get('version') if task_snapshot else None,
            'suite_sha256': task_hashes, 'implementation_sha256': code_hashes,
            'unique_trial_ids': len(pairs) == len(set(pairs)),
            'consistent_snapshot': len(task_hashes) <= 1 and len(code_hashes) <= 1
                and (expected_hash is None or task_hashes == [expected_hash]),
            'complete_repetitions': all(sorted(r.get('repeat',0) for r in runs if r.get('case')==c)==[1,2,3]
                                        for c in {r.get('case') for r in runs})}


def build(baseline, candidate):
    before, after = summarize(baseline), summarize(candidate)
    identities = {'baseline': batch_identity(baseline), 'candidate': batch_identity(candidate)}
    common_ids = sorted(before['by_case'])
    common_pass = sum(after['by_case'].get(k, {}).get('attained', 0) for k in common_ids)
    common_runs = sum(after['by_case'].get(k, {}).get('runs', 0) for k in common_ids)
    core = {k: after['by_case'].get(k, {}).get('attained', 0) for k in ['M1','M2','M3']}
    modeling_runs=sum(v['runs'] for k,v in after['by_case'].items() if k.startswith('M'))
    return {'baseline': before, 'candidate': after,
            'common': {'baseline_attained':before['attained'], 'baseline_runs':before['runs'],
                       'candidate_attained':common_pass, 'candidate_runs':common_runs},
            'core_modeling_successes':core,
            'modeling_runs':modeling_runs,
            'batch_identity': identities,
            'numeric_release_gate': before['runs']==48 and after['runs']==72 and common_runs==48
                and all(i['unique_trial_ids'] and i['consistent_snapshot'] and i['complete_repetitions'] for i in identities.values())
                and common_pass>=before['attained'] and min(core.values())>=2,
            'note':'Numeric gate only; tests, fault rejection, visual acceptance and CI must pass separately. All attempts retained.'}


def markdown(data):
    b,a=data['baseline'],data['candidate']; common=data['common']
    lines=['# 任务级评测结果','',
        f"固定公开合成数据、同一 DeepSeek 配置，计划每题三次；本表实际记录基线 {b['runs']} 次、新版 {a['runs']} 次。表内保留失败；开发调试不计入正式分母。", '',
        '| 项目 | 基线 | 新版 |','|---|---:|---:|',
        f"| 共同任务达成 | {b['attained']}/{b['runs']} | {common['candidate_attained']}/{common['candidate_runs']} |",
        f"| 新增建模任务达成 | 不支持，不比较 | {a['attained']-common['candidate_attained']}/{data['modeling_runs']} |",
        f"| 全部任务达成 | {b['attained']}/{b['runs']} | {a['attained']}/{a['runs']} |",
        f"| 无内部修正即达成 | {b['first_attempt_attained']} | {a['first_attempt_attained']} |",
        f"| 完成交付（不代表数值正确） | {b['delivery_complete']} | {a['delivery_complete']} |",
        f"| 出现代码/核验失败的运行 | {b['execution_failure_runs']} | {a['execution_failure_runs']} |",
        f"| 上述失败后修正达成 | {b['repaired_and_attained']} | {a['repaired_and_attained']} |",
        f"| 耗时中位数 / P95（秒） | {b['median_seconds']} / {b['p95_seconds']} | {a['median_seconds']} / {a['p95_seconds']} |",
        f"| 接口实际记录 token | {b['known_total_tokens']}（未知请求 {b['unknown_usage_calls']}） | {a['known_total_tokens']}（未知请求 {a['unknown_usage_calls']}） |",'',
        '| 任务 | 基线达成 | 新版达成 |','|---|---:|---:|']
    for key,val in sorted(a['by_case'].items()):
        prior=b['by_case'].get(key)
        lines.append(f"| {key} | {str(prior['attained'])+'/3' if prior else '未纳入'} | {val['attained']}/{val['runs']} |")
    identity=data.get('batch_identity',{}).get('candidate',{})
    if identity.get('suite_version')==2:
        lines+=['','本批使用任务集 v2：仅 M3 经用户批准澄清为回归随机拆分；数值判据、8道冻结题和16道共同任务不变。M3 不作为与 v1 同题提升的证据。']
    lines+=['',f"数值发布门槛：{'达到' if data['numeric_release_gate'] else '未达到'}。仍需另外核对自动回归、故障注入、图表和远端 CI。",'',
        f"边界：样本小且重复运行相关，不估计任意任务正确率；任务口径歧义也可能导致合理澄清被固定完成判据记为未达成。失败类型可重叠。基线没有 token 计量，不代表零消耗。另有基线 {b.get('usage_call_count_unmeasured_runs',0)} 次、新版 {a.get('usage_call_count_unmeasured_runs',0)} 次运行连用量请求数也未取得，不计作零消耗。", '',
        '独立指标复算不证明训练全过程无泄漏、预测来自所声明的模型或报告的所有解释正确。历史 v1 的字典行比较判据修复仅重新评分已保存成果，保留原评分；新批次独立记录，不覆盖旧失败。','']
    return '\n'.join(lines)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('baseline'); parser.add_argument('candidate'); parser.add_argument('output')
    args=parser.parse_args(); data=build(args.baseline,args.candidate)
    output=Path(args.output); output.parent.mkdir(parents=True,exist_ok=True)
    output.with_suffix('.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    output.with_suffix('.md').write_text(markdown(data),encoding='utf-8')
