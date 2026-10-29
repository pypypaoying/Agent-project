"""Public deterministic integration fixture; run ONLY inside the Docker sandbox."""
import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.model_selection import train_test_split
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import accuracy_score, f1_score, precision_recall_fscore_support, confusion_matrix, mean_absolute_error, mean_squared_error, r2_score
from helper import load_dataset,emit_metric,emit_table,emit_figure

spec = json.loads('__SPEC__')
df=load_dataset()
ids=df.attrs['record_ids']
df.index=ids
target=spec['target']
features=spec['features']
eligible=df[target].str.strip().ne('')
clean=df.loc[eligible].copy()
train,test=train_test_split(clean.index.tolist(),test_size=spec['test_size'],random_state=spec['seed'],stratify=clean[target] if spec['split']=='stratified' else None)
X=clean[features].copy()
X['c2']=pd.to_numeric(X['c2'])
X['c3']=pd.to_numeric(X['c3'])
prep=ColumnTransformer([('num',Pipeline([('impute',SimpleImputer()),('scale',StandardScaler())]),['c2','c3']),('cat',OneHotEncoder(handle_unknown='ignore'),['c4'])])
classification=spec['task_type']=='classification'
estimator=LogisticRegression(max_iter=300,random_state=42) if classification else Ridge()
pipe=Pipeline([('preprocess',prep),('model',estimator)])
y=clean[target] if classification else pd.to_numeric(clean[target])
pipe.fit(X.loc[train],y.loc[train])
pred=pipe.predict(X.loc[test])
truth=y.loc[test]
if classification:
    counts=y.loc[train].value_counts()
    base=sorted(counts.index,key=lambda x:(-counts[x],x))[0]
    labels=sorted(y.unique().tolist())
    def metrics(p):
        precision,recall,f1,support=precision_recall_fscore_support(truth,p,labels=labels,zero_division=np.nan)
        return dict(accuracy=accuracy_score(truth,p),macro_f1=f1_score(truth,p,labels=labels,average='macro',zero_division=np.nan),labels=labels,
                    confusion_matrix=confusion_matrix(truth,p,labels=labels).tolist(),
                    per_class=[dict(label=l,precision=float(a),recall=float(b),f1=float(c),support=int(d)) for l,a,b,c,d in zip(labels,precision,recall,f1,support)])
else:
    base=float(y.loc[train].mean())
    def metrics(p):
        return dict(mae=mean_absolute_error(truth,p),rmse=mean_squared_error(truth,p)**.5,r2=r2_score(truth,p) if len(truth)>1 and truth.var()>0 else None)
baseline=[base]*len(test)
predictions=pd.DataFrame(dict(source_record_id=test,y_true=truth.tolist(),y_pred=pred,baseline_pred=baseline))
def safe(obj):
    if isinstance(obj,dict): return {k:safe(v) for k,v in obj.items()}
    if isinstance(obj,list): return [safe(v) for v in obj]
    if isinstance(obj,float) and not np.isfinite(obj): return None
    return obj
summary=safe(dict(model=metrics(pred),baseline=metrics(baseline),undefined_reason='No predicted samples for a class gives undefined precision; zero denominators are null.'))
emit_table(predictions,spec['prediction_delivery'],'测试集逐行预测')
emit_metric({**spec,'train_ids':train,'test_ids':test,'excluded_ids':df.index[~eligible].tolist(),'model':type(estimator).__name__,'preprocessing':'Training-only ColumnTransformer pipeline'},spec['metadata_delivery'],'建模方法与拆分')
emit_metric(summary,spec['metrics_delivery'],'模型与基线指标')
fig,ax=plt.subplots()
if classification:
    metric=summary['model']['accuracy']; base_metric=summary['baseline']['accuracy']
    plot=pd.DataFrame({'模型':['主模型','基线'],'准确率':[metric,base_metric]})
    ax.bar(plot['模型'],plot['准确率']); ax.set_ylabel('准确率')
else:
    plot=pd.DataFrame({'真实值':truth.to_numpy(),'预测值':pred})
    ax.scatter(plot['真实值'],plot['预测值']); ax.set_xlabel('真实值'); ax.set_ylabel('预测值')
ax.set_title('测试集表现'); fig.tight_layout()
emit_figure(fig,plot,'chart','测试集表现')
