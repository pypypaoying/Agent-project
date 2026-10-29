import type {Run} from './types';

const states = {passed:'通过',failed:'失败',not_applicable:'不适用',not_checked:'未检查'};
const explanations: Record<string,string> = {
  artifact_integrity:'检查已登记文件的格式、完整行数、哈希与输入来源。',
  semantic_review:'主 Agent 的报告解释仍可能有误，不属于独立证明。',
  method_correctness:'尚未独立验证任意分析算法或全部自然语言结论。',
  chart_rendering:'已检查图片格式及对应数据哈希，尚未自动证明图中的视觉编码正确。',
  training_leakage:'生成代码可以读取完整快照；这些检查不能证明训练全过程无泄漏，也不能证明预测确实来自声明的模型。',
  modeling_schema:'检查预测表、拆分元数据和指标是否符合约定格式。',
  modeling_contract:'将元数据中的目标、特征和拆分参数与冻结的任务约定核对。',
  method_record:'检查是否记录模型及预处理说明；记录存在不代表方法已经被证明正确。',
  sample_identity:'检查记录 ID 是否来自原始数据且没有重复。',
  split_disjoint:'检查训练、测试和排除样本是否互斥，验证集是否属于训练部分。',
  sample_coverage:'核对原始样本总数及按约定排除的记录。',
  split_policy:'按约定随机种子重新拆分并核对；自定义切分需另行审查。',
  prediction_coverage:'检查每条测试样本是否恰有一条预测。',
  prediction_labels:'检查预测类别是否属于原始目标标签集合。',
  true_labels:'将测试集真实值与原始数据逐行核对。',
  baseline_predictions:'从训练目标重新计算多数类或均值基线。',
  prediction_metrics:'根据完整逐行预测独立复算指标；未定义的指标保留为空。',
  undefined_metric_reason:'检查未定义指标是否说明原因，不能以零伪装。',
};
const names: Record<string,string> = {artifact_integrity:'文件与来源',numeric_correctness:'独立数值检查',semantic_review:'语义复核边界',method_correctness:'分析方法',chart_rendering:'图形编码',modeling:'建模核验',training_leakage:'训练泄漏边界',modeling_contract:'建模约定',method_record:'方法记录',sample_identity:'源记录身份',split_disjoint:'拆分交集',sample_coverage:'样本覆盖',split_policy:'拆分口径',prediction_coverage:'预测覆盖',prediction_labels:'预测标签',true_labels:'原始真实标签',baseline_predictions:'基线预测',prediction_metrics:'指标复算',undefined_metric_reason:'未定义指标说明',modeling_inputs:'建模成果结构',verifier_execution:'核验程序执行'};

export function Verification({run}: {run: Run}) {
  const checks=(run.verification ?? []).flatMap(group=>group.results);
  const passed=checks.filter(c=>c.status==='passed').length;
  const failed=checks.filter(c=>c.status==='failed').length;
  const unchecked=checks.filter(c=>c.status==='not_checked').length;
  const calls=[...(run.token_usage?.main ?? []),...(run.token_usage?.analysis ?? [])];
  const known=calls.filter(c=>c.usage!==null);
  const expectedCalls=Math.max(calls.length,(run.model_calls ?? 0)+(run.analysis_model_calls ?? 0));
  return <section className="verification-summary" aria-label="完成情况与核验覆盖">
    {run.task_spec && <details><summary>任务约定</summary><p>{run.task_spec.goal}</p><p>{run.task_spec.data_scope}</p>{!!run.task_spec.assumptions?.length && <ul>{run.task_spec.assumptions.map((a,i)=><li key={i}>{a}</li>)}</ul>}{run.task_spec.modeling && <><p>目标 {run.task_spec.modeling.target} · {run.task_spec.modeling.split} 拆分 · 测试比例 {run.task_spec.modeling.test_size*100}% · 种子 {run.task_spec.modeling.seed}</p><p>特征：{run.task_spec.modeling.features.join('、')}</p></>}</details>}
    <details><summary>核验覆盖 · {checks.length ? `${passed} 通过，${failed} 失败，${unchecked} 未检查` : '未核验'}</summary>
      <p>完成表示所需成果已交付。独立检查仅覆盖下列项目，不能证明训练全程无泄漏或所有结论正确。</p>
      {!checks.length && <p>此记录没有独立数值核验信息；旧历史仍可查看。</p>}
      {checks.map((c,i)=>{
        const title=names[c.check] ?? (c.check==='modeling_schema' ? '建模成果格式' : c.check.startsWith('fact:') ? '声明的数值关系' : c.check);
        const evidence=JSON.stringify(c.evidence ?? {},null,2);
        return <div className="verification-item" key={i}><strong>{title} · {states[c.status]}</strong><p>{explanations[c.check] ?? (c.check.startsWith('fact:') ? '用完整登记表格重新计算并核对声明的指标。' : c.reason)}</p>{c.status==='failed' && <span role="status">需修正此项；不能标记为核验通过。</span>}
          <details><summary>原始检查说明与证据</summary><p>{c.reason}</p>{evidence!=='{}' && <pre className="event-payload">{evidence.slice(0,4000)}{evidence.length>4000 ? '\n…仅展示前4000字符，完整证据见报告 JSON。' : ''}</pre>}</details>
        </div>;
      })}
    </details>
    {expectedCalls>0 && <p className="muted">接口 token：{known.reduce((n,c)=>n+(c.usage?.total_tokens ?? 0),0).toLocaleString()} 已记录{known.length!==expectedCalls ? `；${expectedCalls-known.length} 次请求用量未知` : ''}（未估算费用）</p>}
  </section>;
}
