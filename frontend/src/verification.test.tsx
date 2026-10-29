import {render,screen} from '@testing-library/react';
import {describe,it,expect} from 'vitest';
import {Verification} from './verification';

describe('Verification coverage',()=>{
  it('shows selected features separately from the target',()=>{
    render(<Verification run={{id:'model',status:'completed',task_spec:{goal:'预测目标',modeling:{target:'c4',features:['c2','c3'],split:'random',seed:42,test_size:0.2}}}}/>);
    expect(screen.getByText('特征：c2、c3')).toBeTruthy();
    expect(screen.getByText(/目标 c4/)).toBeTruthy();
  });
  it('does not upgrade old history to verified',()=>{
    render(<Verification run={{id:'old',status:'completed'}}/>);
    expect(screen.getByText('核验覆盖 · 未核验')).toBeTruthy();
  });
  it('separates failed checks from completion and unknown usage',()=>{
    render(<Verification run={{id:'r',status:'error',verification:[{passed:false,results:[{check:'prediction_metrics',status:'failed',required:true,reason:'Wrong MAE'},{check:'training_leakage',status:'not_checked',required:false,reason:'Full snapshot accessible'}]}],token_usage:{main:[{call:1,role:'main',usage:null}],analysis:[]}}}/>);
    expect(screen.getByText('指标复算 · 失败')).toBeTruthy();
    expect(screen.getByText('训练泄漏边界 · 未检查')).toBeTruthy();
    expect(screen.getByText(/1 次请求用量未知/)).toBeTruthy();
  });
  it('counts unsuccessful requests without invented token usage',()=>{
    render(<Verification run={{id:'failed-request',status:'error',model_calls:2,analysis_model_calls:1,token_usage:{main:[{call:1,role:'main',usage:{input_tokens:10,output_tokens:5,total_tokens:15}}],analysis:[]}}}/>);
    expect(screen.getByText(/2 次请求用量未知/)).toBeTruthy();
    expect(screen.getByText(/15 已记录/)).toBeTruthy();
  });
});
