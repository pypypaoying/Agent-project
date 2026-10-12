import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';
import { ModelPicker, ProviderSettings } from './providers';
import type { ProviderCatalog } from './types';

vi.mock('./api', () => ({ ApiError: class extends Error {}, api: { saveProvider: vi.fn(), providers: vi.fn(), defaultProvider: vi.fn(), refreshProviderModels: vi.fn(), testProvider: vi.fn() } }));
const catalog = (): ProviderCatalog => ({ providers: [
  { id: 'deepseek', name: 'DeepSeek', kind: 'preset', base_url: 'https://api.deepseek.com', models: ['deepseek-flash', 'deepseek-v4-pro'], default_model_id: 'deepseek-flash', has_key: true, key_source: 'env', revision: 1 },
  { id: 'custom', name: '兼容服务', kind: 'custom', base_url: 'https://example.invalid/v1', models: ['analysis-model'], default_model_id: 'analysis-model', has_key: false, revision: 1 },
], default_provider_id: 'deepseek', default_model_id: 'deepseek-flash' });
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.saveProvider).mockResolvedValue(catalog().providers[0]); vi.mocked(api.providers).mockResolvedValue(catalog()); });

describe('provider-grouped model selection', () => {
  it('shows configured models by provider and sends only identifiers to the parent', async () => {
    const select = vi.fn(); render(<ModelPicker catalog={catalog()} selection={{ provider_id: 'deepseek', model_id: 'deepseek-flash' }} locked={false} offline={false} onSelect={select} onConfigure={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: '选择模型' }));
    expect(screen.getByText('DeepSeek')).toBeVisible();
    expect(screen.getByText('兼容服务')).toBeVisible();
    await userEvent.click(screen.getByRole('menuitemradio', { name: 'deepseek-v4-pro' }));
    expect(select).toHaveBeenCalledWith({ provider_id: 'deepseek', model_id: 'deepseek-v4-pro' });
  });

  it('opens settings for an unconfigured provider instead of pretending it is ready', async () => {
    const configure = vi.fn(); const select = vi.fn(); render(<ModelPicker catalog={catalog()} selection={null} locked={false} offline={false} onSelect={select} onConfigure={configure} />);
    await userEvent.click(screen.getByRole('button', { name: '选择模型' }));
    expect(screen.getByText('未配置')).toBeVisible();
    await userEvent.click(screen.getByRole('menuitemradio', { name: 'analysis-model 去配置' }));
    expect(configure).toHaveBeenCalledWith('custom'); expect(select).not.toHaveBeenCalled();
  });

  it('locks selection for active tasks and truthfully labels offline mode', () => {
    const props = { catalog: catalog(), selection: { provider_id: 'deepseek', model_id: 'deepseek-flash' }, onSelect: vi.fn(), onConfigure: vi.fn() };
    const view = render(<ModelPicker {...props} locked offline={false} />);
    expect(screen.getByRole('button', { name: '选择模型' })).toBeDisabled();
    view.rerender(<ModelPicker {...props} locked={false} offline />);
    expect(screen.getByText('离线演示')).toBeVisible(); expect(screen.getByRole('button', { name: '选择模型' })).toBeDisabled();
  });
});

describe('write-only provider credentials', () => {
  it('saves an empty key field without replacing the existing backend key', async () => {
    const updated = vi.fn(); render(<ProviderSettings catalog={catalog()} locked={false} onUpdated={updated} onBack={vi.fn()} />);
    expect(screen.getByLabelText('API Key', { exact: true })).toHaveValue('');
    await userEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await waitFor(() => expect(api.saveProvider).toHaveBeenCalled());
    const body = vi.mocked(api.saveProvider).mock.calls[0][1];
    expect(body).not.toHaveProperty('api_key'); expect(body).not.toHaveProperty('clear_key');
  });

  it('writes a supplied key once, clears the input and never persists it in web storage', async () => {
    const storage = vi.spyOn(Storage.prototype, 'setItem');
    render(<ProviderSettings catalog={catalog()} locked={false} onUpdated={vi.fn()} onBack={vi.fn()} />);
    await userEvent.type(screen.getByLabelText('API Key', { exact: true }), 'private-key-sentinel');
    await userEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await waitFor(() => expect(api.saveProvider).toHaveBeenCalledWith('deepseek', expect.objectContaining({ api_key: 'private-key-sentinel' })));
    await waitFor(() => expect(screen.getByLabelText('API Key', { exact: true })).toHaveValue(''));
    expect(storage).not.toHaveBeenCalled();
    expect(screen.queryByText('private-key-sentinel')).not.toBeInTheDocument();
  });

  it('requires explicit key clearance and does not send a key together with clear_key', async () => {
    render(<ProviderSettings catalog={catalog()} locked={false} onUpdated={vi.fn()} onBack={vi.fn()} />);
    await userEvent.click(screen.getByRole('checkbox', { name: '保存时清除现有密钥' }));
    expect(screen.getByLabelText('API Key', { exact: true })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await waitFor(() => expect(api.saveProvider).toHaveBeenCalledWith('deepseek', expect.objectContaining({ clear_key: true })));
    expect(vi.mocked(api.saveProvider).mock.calls[0][1]).not.toHaveProperty('api_key');
  });

  it('locks settings mutations during execution or clarification', () => {
    render(<ProviderSettings catalog={catalog()} locked onUpdated={vi.fn()} onBack={vi.fn()} />);
    expect(screen.getByRole('button', { name: '保存配置' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '测试连接' })).toBeDisabled();
    expect(screen.getByLabelText('API Key', { exact: true })).toBeDisabled();
  });

  it('tests saved models through the actual API method and refuses unsaved changes', async () => {
    vi.mocked(api.testProvider).mockResolvedValue({ ok: true, model_id: 'deepseek-flash', tool_call_supported: true });
    render(<ProviderSettings catalog={catalog()} locked={false} onUpdated={vi.fn()} onBack={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: '测试连接' }));
    await waitFor(() => expect(api.testProvider).toHaveBeenCalledWith('deepseek', 'deepseek-flash'));
    expect(await screen.findByRole('status')).toHaveTextContent('模型接口请求成功');
    fireEvent.change(screen.getByRole('textbox', { name: 'Base URL' }), { target: { value: 'https://other.invalid/v1' } });
    await userEvent.click(screen.getByRole('button', { name: '测试连接' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('请先保存');
    expect(api.testProvider).toHaveBeenCalledTimes(1);
  });

  it('does not echo raw API errors that could contain the entered key', async () => {
    vi.mocked(api.saveProvider).mockRejectedValue(new Error('request private-secret-key failed'));
    render(<ProviderSettings catalog={catalog()} locked={false} onUpdated={vi.fn()} onBack={vi.fn()} />);
    await userEvent.type(screen.getByLabelText('API Key', { exact: true }), 'private-secret-key');
    await userEvent.click(screen.getByRole('button', { name: '保存配置' }));
    const alert = await screen.findByRole('alert');
    expect(alert).not.toHaveTextContent('private-secret-key');
    expect(screen.getByLabelText('API Key', { exact: true })).toHaveValue('');
  });

  it('creates a custom provider with explicit models and no browser persistence', async () => {
    render(<ProviderSettings catalog={catalog()} locked={false} onUpdated={vi.fn()} onBack={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: '＋ 添加兼容供应商' }));
    await userEvent.type(screen.getByRole('textbox', { name: '供应商名称' }), '自建服务');
    await userEvent.type(screen.getByRole('textbox', { name: 'Base URL' }), 'http://127.0.0.1:9000/v1');
    await userEvent.type(screen.getByRole('textbox', { name: '模型列表' }), 'my-analysis\nmy-reasoning');
    await userEvent.type(screen.getByRole('combobox', { name: '默认模型' }), 'my-analysis');
    await userEvent.type(screen.getByLabelText('API Key', { exact: true }), 'my-private-key');
    await userEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await waitFor(() => expect(api.saveProvider).toHaveBeenCalledWith(null, { name: '自建服务', base_url: 'http://127.0.0.1:9000/v1', models: ['my-analysis', 'my-reasoning'], default_model_id: 'my-analysis', api_key: 'my-private-key' }));
  });

  it('refreshes models through the API and blocks requests using unsaved credentials', async () => {
    vi.mocked(api.refreshProviderModels).mockResolvedValue({ models: ['new-model'] });
    render(<ProviderSettings catalog={catalog()} locked={false} onUpdated={vi.fn()} onBack={vi.fn()} />);
    await userEvent.type(screen.getByLabelText('API Key', { exact: true }), 'changed-private-key');
    await userEvent.click(screen.getByRole('button', { name: '获取模型列表' }));
    expect(api.refreshProviderModels).not.toHaveBeenCalled();
    expect(await screen.findByRole('alert')).toHaveTextContent('请先保存');
    fireEvent.change(screen.getByLabelText('API Key', { exact: true }), { target: { value: '' } });
    await userEvent.click(screen.getByRole('button', { name: '获取模型列表' }));
    await waitFor(() => expect(api.refreshProviderModels).toHaveBeenCalledWith('deepseek'));
  });

  it('respects read-only environment metadata and updates only the explicit key', async () => {
    const current = catalog(); current.providers[0].kind = 'env';
    render(<ProviderSettings catalog={current} locked={false} onUpdated={vi.fn()} onBack={vi.fn()} />);
    expect(screen.getByRole('textbox', { name: 'Base URL' })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: '模型列表' })).toBeDisabled();
    await userEvent.type(screen.getByLabelText('API Key', { exact: true }), 'replacement-key');
    await userEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await waitFor(() => expect(api.saveProvider).toHaveBeenCalledWith('deepseek', { api_key: 'replacement-key' }));
  });

  it.each(['too many', 'too long'])('rejects %s models before sending provider credentials', async (kind) => {
    render(<ProviderSettings catalog={catalog()} locked={false} onUpdated={vi.fn()} onBack={vi.fn()} />);
    const values = kind === 'too many' ? ['deepseek-flash', ...Array.from({ length: 50 }, (_, index) => `model-${index}`)] : ['deepseek-flash', 'x'.repeat(129)];
    fireEvent.change(screen.getByRole('textbox', { name: '模型列表' }), { target: { value: values.join('\n') } });
    await userEvent.click(screen.getByRole('button', { name: '保存配置' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('最多 50 个模型');
    expect(api.saveProvider).not.toHaveBeenCalled();
  });

  it('explains actual session-only credential persistence instead of claiming durable storage', () => {
    const current = catalog(); current.credential_persistence = 'session_only';
    render(<ProviderSettings catalog={current} locked={false} onUpdated={vi.fn()} onBack={vi.fn()} />);
    expect(screen.getByText(/服务重启后需重新填写/)).toBeVisible();
    expect(screen.queryByText(/Windows 当前用户加密保存/)).not.toBeInTheDocument();
  });
});
