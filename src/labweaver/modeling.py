"""Modeling policy and artifact interchange contract, not an algorithm template."""

PLANNING_POLICY = """
用 set_task_plan.task_spec 记录目标、数据范围、默认/确认口径及可检查的数值关系；约定冻结后不能通过删要求修复失败。
分类类别数应依据完整列的 distinct_count，不能凭概览中的少量样例判断二分类或多分类。
建模必须填写 task_spec.modeling 对象，不能只在 goal/assumptions 写自然语言配置。prediction_delivery、metadata_delivery、metrics_delivery 分别引用计划中的测试预测表、拆分元数据、评估指标ID。建模独立检查由宿主提供，checks 通常留空；只有用户明确要求的表-指标同一数值关系才声明额外 checks，不能把训练集数量等同于测试预测表行数。
表格监督建模时填写 modeling（目标/特征用 cN，task_type classification/regression），并规划预测表、元数据、指标和必要图表。目标、时间/实体依赖或疑似泄漏字段不明确时先澄清。标识列默认排除并说明。普通独立样本默认 seed=42，test_size=0.2；分类 split=stratified，回归 random。不能合理分层时询问，不静默降级。
默认一个主要模型加训练集多数类/均值基线。用户要求比较时最多两候选，在训练部分内部验证选型，测试只用于最终评估。每任务最多2张图，选择能表达主要结果的图，不扩展额外分析。建模执行限120秒，禁止静默抽样或降低要求。Pipeline预处理只拟合训练集；缺失目标排除，重复处理按口径。实际可用 sklearn；方法仍由代码 Agent 编写。
报告从登记成果解释指标，并说明 verification 的 passed/failed/not_checked/not_applicable。无独立检查的方法仍可用，但不要称为已核验；模型不如基线也可以是有效实验。不能声称核验已证明训练全程无泄漏。
"""

ANALYSIS_POLICY = """
环境另含 scikit-learn 1.7.2。df.attrs['record_ids'] 是 Python list，与原始行顺序对应的稳定源ID；直接赋给新列，在过滤/拆分前保存，不能重置后重新生成ID。
字段 distinct_count 来自全量原始非空字符串；样例不代表全部类别。分类保留目标中的全部真实类别，不能根据样例或主 Agent 未经核对的二分类推测丢弃类别。
若 task_spec.modeling 非空，按其冻结口径生成算法（Pipeline/ColumnTransformer仅拟合训练集），并遵循以下成果协议：
1. prediction_delivery 的表固定列 source_record_id,y_true,y_pred,baseline_pred，覆盖每个测试样本一次。分类标签保留原始字符串；回归显式数值化，非法数值报错。
2. metadata_delivery JSON 包含 target,features,split,seed,test_size,duplicate_policy，与约定一致；train_ids（整个训练部分）、test_ids、excluded_ids（目标空白或按约定去重）、可选 validation_ids（训练部分子集）；model 与 preprocessing 方法说明。ID顺序来自原始快照。随机/分层拆分用 sklearn train_test_split(原顺序有效ID,test_size,random_state=seed,stratify=原字符串标签或None)。drop_exact 按全部原始列保留首次，源ID不参与重复判断。
3. metrics_delivery JSON: {model:{...},baseline:{...},undefined_reason:"如有未定义值的原因"}。回归每项 mae,rmse,r2；测试<2或真实值恒定时 r2=null。分类每项 accuracy,macro_f1,labels,confusion_matrix,per_class；labels=所有有效目标原字符串的字典序，per_class=[{label,precision,recall,f1,support}]。分母0时值null（sklearn zero_division=np.nan），macro_f1对已定义F1取平均，JSON不允许NaN。混淆矩阵按labels顺序。
预测表与指标 JSON 保留原始计算精度，不提前 round 或格式化；仅在面向用户的表格与报告中格式化展示。准确率独立复算的绝对容差为 1e-8。
基线只用train_ids：分类多数类，同频时原字符串字典序优先；回归训练均值。比较候选最多2个，validation_ids不触及测试集，选型后可用全训练集重拟合。记录选择依据。
可信核验器会在独立容器重新计算。收到 verification.failed 时修正具体差异，不能伪造检查、改变约定或只改声明为通过。自身断言不是独立证据。未覆盖的方法仍可执行，解释其边界。
"""


def artifact_contracts(task_spec):
    """Machine-readable IO requirements; no estimator or calculations are supplied."""
    model=task_spec.get('modeling')
    if not model: return []
    metrics = ({'accuracy':'number','macro_f1':'number or null','labels':['original string labels, sorted'],
                'confusion_matrix':'2D integer array in labels order','per_class':[{'label':'string','precision':'number or null','recall':'number or null','f1':'number or null','support':'integer'}]}
               if model['task_type']=='classification' else {'mae':'number','rmse':'number','r2':'number or null'})
    return [
        {'deliverable_id':model['prediction_delivery'],'kind':'table','required_columns':['source_record_id','y_true','y_pred','baseline_pred']},
        {'deliverable_id':model['metadata_delivery'],'kind':'metric','required_json':{
            **{k:model[k] for k in ['target','features','split','seed','test_size','duplicate_policy']},
            'train_ids':['original rNNNNNN string IDs'],'test_ids':['original rNNNNNN string IDs'],
            'excluded_ids':['missing target / agreed duplicate source IDs; [] if none'],
            'model':'method description','preprocessing':'training-only transformations description'}},
        {'deliverable_id':model['metrics_delivery'],'kind':'metric','required_json':{'model':metrics,'baseline':metrics,'undefined_reason':'explain undefined values if any'}},
    ]
