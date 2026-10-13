import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from './api';
import type { ModelSelection, Provider, ProviderCatalog, ProviderUpdate } from './types';

export function providerReady(provider: Provider): boolean { return Boolean(provider.has_key && provider.base_url && provider.models.length); }
export function defaultSelection(catalog: ProviderCatalog): ModelSelection | null {
  return catalog.default_provider_id && catalog.default_model_id ? { provider_id: catalog.default_provider_id, model_id: catalog.default_model_id } : null;
}
function Chevron() { return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="m7 10 5 5 5-5" /></svg>; }

export function ModelPicker({ catalog, selection, locked, offline, onSelect, onConfigure }: {
  catalog: ProviderCatalog | null; selection: ModelSelection | null; locked: boolean; offline: boolean;
  onSelect: (selection: ModelSelection) => void; onConfigure: (providerId?: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const selected = catalog?.providers.find(provider => provider.id === selection?.provider_id);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  useEffect(() => { if (locked || offline) setOpen(false); }, [locked, offline]);
  return <div className="model-picker" ref={root}>
    <button ref={trigger} type="button" className="model-trigger" aria-label="选择模型" aria-haspopup="menu" aria-expanded={open} disabled={locked || offline || !catalog} title={offline ? '离线演示使用确定性模型' : locked ? '当前任务结束后可切换模型' : selected ? `${selected.name} · ${selection?.model_id}` : '选择模型供应商'} onClick={() => setOpen(!open)}>
      <span>{offline ? '离线演示' : selection?.model_id || '加载模型…'}</span><Chevron />
    </button>
    {open && catalog && <div className="model-menu" role="menu" aria-label="模型供应商和模型" onKeyDown={event => {
      if (event.key === 'Escape') { setOpen(false); trigger.current?.focus(); }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); const items = Array.from(root.current!.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"], [role="menuitem"]'));
        const current = items.indexOf(document.activeElement as HTMLButtonElement); const direction = event.key === 'ArrowDown' ? 1 : -1;
        items[(current + direction + items.length) % items.length]?.focus();
      }
    }}>
      {catalog.providers.map(provider => <section className="model-group" key={provider.id} aria-label={provider.name}><div className="model-group-label">{provider.name}{!providerReady(provider) && <small>未配置</small>}</div>
        {provider.models.length ? provider.models.map(model => <button type="button" role="menuitemradio" aria-checked={selection?.provider_id === provider.id && selection.model_id === model} key={model} className="model-option" onClick={() => { setOpen(false); if (providerReady(provider)) onSelect({ provider_id: provider.id, model_id: model }); else onConfigure(provider.id); }}><span>{model}</span>{providerReady(provider) ? selection?.provider_id === provider.id && selection.model_id === model ? <span className="model-check">✓</span> : null : <small>去配置</small>}</button>) : <button type="button" role="menuitem" className="model-option" onClick={() => { setOpen(false); onConfigure(provider.id); }}>配置模型</button>}
      </section>)}
      <button type="button" role="menuitem" className="model-settings-link" onClick={() => { setOpen(false); onConfigure(); }}>管理模型供应商</button>
    </div>}
  </div>;
}

export function ProviderSettings({ catalog, initialProviderId, locked, loadError, onUpdated, onBack }: {
  catalog: ProviderCatalog | null; initialProviderId?: string; locked: boolean; loadError?: string;
  onUpdated: (catalog: ProviderCatalog) => void; onBack: () => void;
}) {
  const [providerId, setProviderId] = useState<string | null>(initialProviderId ?? catalog?.default_provider_id ?? null);
  const [name, setName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [models, setModels] = useState('');
  const [model, setModel] = useState('');
  const [key, setKey] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const provider = catalog?.providers.find(item => item.id === providerId);
  const disabled = locked || busy || !catalog;

  useEffect(() => {
    const current = catalog?.providers.find(item => item.id === providerId);
    setName(current?.name ?? ''); setBaseUrl(current?.base_url ?? ''); setModels(current?.models.join('\n') ?? ''); setModel(current?.default_model_id ?? '');
    setKey(''); setClearKey(false);
  }, [providerId, catalog]);

  const modelList = () => [...new Set(models.split(/\r?\n/).map(value => value.trim()).filter(Boolean))];
  const unsaved = () => !provider || Boolean(key || clearKey || name.trim() !== provider.name || baseUrl.trim() !== provider.base_url || models !== provider.models.join('\n') || model.trim() !== provider.default_model_id);
  const safeError = (reason: unknown) => reason instanceof ApiError && reason.status === 401 ? '访问令牌已失效，请用启动器重新打开应用。' : reason instanceof ApiError && [400, 409, 422].includes(reason.status) ? '配置未保存或操作未完成，请检查字段；活动任务结束后再修改配置。' : '操作未完成，请检查服务状态与供应商接口。密钥不会回显。';
  const validate = (): ProviderUpdate | null => {
    const available = modelList();
    if (!name.trim() || !available.length || !model.trim() || !available.includes(model.trim())) { setError('请填写供应商名称、至少一个模型，并将默认模型加入模型列表。'); return null; }
    if (name.trim().length > 80 || available.length > 50 || available.some(value => value.length > 128) || model.trim().length > 128) { setError('供应商名称最多 80 个字符，最多 50 个模型，每个模型名称最多 128 个字符。'); return null; }
    try { const url = new URL(baseUrl.trim()); if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error(); }
    catch { setError('Base URL 需为 HTTP(S) 地址，不包含密钥、用户名、查询参数或片段。'); return null; }
    if (key && (key.trim().length < 8 || key.trim().length > 8192)) { setError('API Key 长度应为 8–8192 个字符。'); return null; }
    return { name: name.trim(), base_url: baseUrl.trim(), models: available, default_model_id: model.trim(), ...(key.trim() ? { api_key: key.trim() } : {}), ...(clearKey ? { clear_key: true } : {}) };
  };
  const save = async () => {
    if (disabled) return;
    const values = validate(); if (!values) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const body = provider?.kind === 'env' ? { ...(values.api_key ? { api_key: values.api_key } : {}), ...(values.clear_key ? { clear_key: true } : {}) } : values;
      const saved = await api.saveProvider(providerId, body); setKey(''); setClearKey(false);
      const result = await api.providers(); onUpdated(result);
      if (!providerId) setProviderId(saved.id);
      setNotice('配置已保存。可获取模型列表或测试所选模型。');
    } catch (reason) { setKey(''); setError(safeError(reason)); } finally { setBusy(false); }
  };
  const refresh = async () => {
    if (!provider || disabled) return;
    if (unsaved()) { setError('请先保存当前配置，再获取模型列表。'); return; }
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await api.refreshProviderModels(provider.id); const current = await api.providers(); onUpdated(current);
      setNotice(`接口返回 ${result.models.length} 个模型。模型列表也可手动填写。`);
    } catch (reason) { setError(safeError(reason)); } finally { setBusy(false); }
  };
  const test = async () => {
    if (!provider || disabled) return;
    if (unsaved()) { setError('请先保存当前配置，再测试模型连接。'); return; }
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await api.testProvider(provider.id, model.trim());
      setNotice(result.ok ? result.tool_call_supported === false ? '接口请求成功，但模型未完成工具调用验证；请选用支持工具调用的模型。' : '模型接口请求成功。实际分析仍以任务中的执行证据为准。' : '模型测试未通过，请检查已保存配置。');
    } catch (reason) { setError(safeError(reason)); } finally { setBusy(false); }
  };
  const makeDefault = async () => {
    if (!provider || disabled || !provider.models.includes(model.trim())) return;
    if (unsaved()) { setError('请先保存配置，再设为应用默认模型。'); return; }
    setBusy(true); setError('');
    try { onUpdated(await api.defaultProvider({ provider_id: provider.id, model_id: model.trim() })); setNotice('已设为应用默认模型。'); }
    catch (reason) { setError(safeError(reason)); } finally { setBusy(false); }
  };

  return <main className="settings-main"><header className="settings-header"><div><h1>模型供应商</h1><p>主 Agent 与代码 Agent 使用本次选择的同一模型。</p></div><button type="button" className="outline-button" onClick={onBack}>返回聊天</button></header>
    {locked && <p className="notice">有任务运行或等待回复，完成或停止后可修改供应商配置。</p>}
    {loadError && <p className="notice settings-error" role="alert">{loadError}</p>}
    {!catalog && !loadError && <p className="notice">正在加载供应商配置。请先确认本地服务与访问令牌有效。</p>}
    <div className="settings-content"><aside className="provider-navigation" aria-label="供应商列表">{catalog?.providers.map(item => <button type="button" key={item.id} className={providerId === item.id ? 'selected' : ''} disabled={busy} onClick={() => { setProviderId(item.id); setNotice(''); setError(''); }}><strong>{item.name}</strong><small>{item.has_key ? '已配置密钥' : '未配置密钥'}</small></button>)}<button type="button" className="add-provider" disabled={disabled} onClick={() => { setProviderId(null); setNotice(''); setError(''); }}>＋ 添加兼容供应商</button></aside>
      <section className="provider-form" aria-label="供应商配置"><h2>{provider ? provider.name : '添加兼容供应商'}</h2><p className="muted">支持 OpenAI 兼容接口。模型名称可从接口获取，也可手动填写。</p>
        {error && <p role="alert" className="settings-error">{error}</p>}{notice && <p role="status" className="settings-notice">{notice}</p>}
        <form autoComplete="off" onKeyDown={event => { if (event.key === 'Enter' && (event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault(); }} onSubmit={event => { event.preventDefault(); void save(); }}>
          {provider?.kind === 'env' && <p className="field-help">该供应商引用现有后端配置，名称、地址和模型由配置文件管理。需要修改这些字段时，可添加新的兼容供应商。</p>}
          <label>供应商名称<input aria-label="供应商名称" value={name} disabled={disabled || provider?.kind === 'env'} onChange={event => setName(event.target.value)} maxLength={80} /></label>
          <label>Base URL<input aria-label="Base URL" value={baseUrl} disabled={disabled || provider?.kind === 'env'} onChange={event => setBaseUrl(event.target.value)} placeholder="https://api.example.com/v1" spellCheck={false} /></label>
          <label>API Key <span className="key-state">{provider?.has_key ? '已配置' : '未配置'}</span><input aria-label="API Key" type="password" autoComplete="new-password" value={key} disabled={disabled || clearKey} onChange={event => setKey(event.target.value)} placeholder={provider?.has_key ? '留空保留现有密钥' : '输入供应商 API Key'} spellCheck={false} /></label>
          <p className="field-help">密钥只写入本机后端，不回显、不进入聊天记录或浏览器存储。{provider?.key_source === 'env' ? '当前密钥引用后端环境配置。' : ''}{catalog?.credential_persistence === 'dpapi' ? '新密钥使用 Windows 当前用户加密保存。' : catalog?.credential_persistence === 'session_only' ? '当前环境仅在服务运行期间保存新密钥，服务重启后需重新填写。' : ''}</p>
          {provider?.has_key && <label className="clear-key"><input type="checkbox" checked={clearKey} disabled={disabled} onChange={event => { setClearKey(event.target.checked); setKey(''); }} />保存时清除现有密钥</label>}
          <label>模型列表<textarea aria-label="模型列表" value={models} disabled={disabled || provider?.kind === 'env'} onChange={event => setModels(event.target.value)} rows={4} placeholder="每行一个模型名称" spellCheck={false} /></label>
          <p className="field-help">每行一个模型，最多 50 个；模型名称最多 128 个字符。</p>
          <label>默认模型<input aria-label="默认模型" value={model} disabled={disabled || provider?.kind === 'env'} onChange={event => setModel(event.target.value)} list="provider-model-options" spellCheck={false} maxLength={128} /><datalist id="provider-model-options">{modelList().map(item => <option key={item} value={item} />)}</datalist></label>
          <div className="provider-actions"><button type="submit" className="primary-button" disabled={disabled}>保存配置</button><button type="button" className="outline-button" disabled={disabled || !provider?.has_key || provider.kind === 'env'} onClick={() => void refresh()}>获取模型列表</button><button type="button" className="outline-button" disabled={disabled || !provider?.has_key || !model.trim()} onClick={() => void test()}>测试连接</button><button type="button" className="text-button" disabled={disabled || !provider?.has_key} onClick={() => void makeDefault()}>设为应用默认</button></div>
        </form>
      </section>
    </div>
  </main>;
}
