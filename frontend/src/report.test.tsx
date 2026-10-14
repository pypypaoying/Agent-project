import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { fallbackPresentation, formatReportValue, Markdown, MetricOutput, readableReportText } from './report';
import type { ArtifactPresentation } from './types';

describe('professional report presentation', () => {
  it('renders Markdown sections, emphasis, lists and GFM tables as document elements', () => {
    render(<Markdown>{'# 分析报告\n\n## 主要发现\n\n**结果可复现**。\n\n- 检查输入口径\n- 核对真实成果\n\n| 指标 | 模型 | 基线 |\n| --- | ---: | ---: |\n| Accuracy | 0.62 | 0.42 |\n| Macro-F1 | 0.30 | 0.10 |'}</Markdown>);
    expect(screen.getByRole('heading', { name: '分析报告', level: 1 })).toBeVisible();
    expect(screen.getByRole('heading', { name: '主要发现', level: 2 })).toBeVisible();
    expect(screen.getByText('结果可复现').tagName).toBe('STRONG');
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
    expect(screen.getByRole('table')).toHaveTextContent('Accuracy0.620.42');
    expect(screen.getAllByRole('columnheader')).toHaveLength(3);
  });

  it('adds only paragraph whitespace to long old prose and preserves authored Markdown exactly', () => {
    const paragraph = `${'本次采用原始数据并保留标签，准确率为 0.6207，样本数为 1359。'.repeat(12)}最终结果已保存。`;
    const formatted = readableReportText(paragraph);
    expect(formatted).toContain('\n\n');
    expect(formatted.replaceAll('\n\n', '')).toBe(paragraph);
    const markdown = `## 方法\n\n${paragraph}\n\n| 指标 | 值 |\n| --- | --- |\n| Accuracy | 0.6207 |`;
    expect(readableReportText(markdown)).toBe(markdown);
    expect(readableReportText(`**重点**：${paragraph}`)).toBe(`**重点**：${paragraph}`);
  });

  it('keeps GFM prose passive, including reference links and model images', () => {
    const { container } = render(<Markdown>{'## 来源\n\n![模型图片][image]\n\n[危险引用][danger]\n\n[官方文档](https://example.com/docs)\n\n[image]: https://external.invalid/track.png\n[danger]: javascript:alert(1)\n\n<script>window.injected=true</script>\n<img src="https://external.invalid/track.png">'}</Markdown>);
    expect(container.querySelector('img,script,iframe,[onerror],a[href^="javascript:"]')).toBeNull();
    expect(screen.getByRole('link', { name: '官方文档' })).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('formats exact zero, integer counts, booleans and visible decimal precision', () => {
    expect(formatReportValue(0)).toBe('0');
    expect(formatReportValue(1599)).toBe('1599');
    expect(formatReportValue(0.6206896551724138)).toBe('0.62069');
    expect(formatReportValue(false)).toBe('否');
    expect(formatReportValue(null)).toBe('—');
    expect(formatReportValue('001')).toBe('001');
  });

  it('uses host presentation instead of displaying nested metric JSON by default', () => {
    render(<MetricOutput value={{ nested: { original_exact_value: 0.6206896551724138 } }} presentation={{ blocks: [{ type: 'paragraph', text: '统计来自真实执行结果。' }, { type: 'table', title: '测试结果', columns: ['指标', '值'], rows: [['Accuracy', '0.62069'], ['未识别数量', 0]] }], note: '完整精度保留在 JSON。' }} />);
    expect(screen.getByText('统计来自真实执行结果。')).toBeVisible();
    expect(screen.getByRole('heading', { name: '测试结果' })).toBeVisible();
    expect(screen.getByRole('table')).toHaveTextContent('Accuracy0.62069未识别数量0');
    expect(screen.queryByText(/original_exact_value/)).not.toBeInTheDocument();
  });

  it('preserves exact JSON only inside a closed expandable view', () => {
    const { container } = render(<MetricOutput value={{ precision: 0.6206896551724138 }} />);
    const detail = container.querySelector('details')!;
    expect(detail).not.toHaveAttribute('open');
    expect(container.querySelector('pre')).toBeNull();
    detail.open = true;
    fireEvent(detail, new Event('toggle'));
    expect(container.querySelector('pre')).toHaveTextContent('0.6206896551724138');
    expect(screen.getByRole('table')).toHaveTextContent('0.62069');
  });

  it('renders old nested records and arrays as bounded tables with a precision note', () => {
    render(<MetricOutput value={{ before: { rows: 1599, missing: 0 }, after: { rows: 1359, removed: 240 }, evaluation: [{ label: 'A', accuracy: 0.625 }, { label: 'B', accuracy: 0.5 }] }} />);
    expect(screen.getByRole('heading', { name: 'before' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'after' })).toBeVisible();
    expect(screen.getAllByRole('table')).toHaveLength(3);
    const evaluations = screen.getAllByRole('table')[2];
    expect(within(evaluations).getByRole('columnheader', { name: 'accuracy' })).toBeVisible();
    expect(within(evaluations).getByText('0.625')).toBeVisible();
    expect(screen.getByText(/完整精度保留/)).toBeVisible();
    expect(document.querySelector('pre')).toBeNull();
  });

  it('does not turn full weight matrices into unbounded report rows or columns', () => {
    const presentation = fallbackPresentation({ weights: Array.from({ length: 32 }, (_, row) => Array.from({ length: 16 }, (_, col) => row * 16 + col)) });
    const table = presentation.blocks.find(block => block.type === 'table');
    expect(table?.type === 'table' && table.columns).toHaveLength(12);
    expect(table?.type === 'table' && table.rows).toHaveLength(20);
    expect(table?.type === 'table' && table.truncated).toBe(true);
    render(<MetricOutput value={{ weights: Array.from({ length: 32 }, () => Array(16).fill(1)) }} />);
    expect(screen.getByText(/这里展示部分行或列/)).toBeVisible();
  });

  it('retains narrative Markdown and zero metrics from old report artifacts', () => {
    render(<MetricOutput value={{ report_markdown: '## 解释\n\n**结论**：补充独立验证。', missing: 0 }} />);
    expect(screen.getByRole('heading', { name: '解释' })).toBeVisible();
    expect(screen.getByText('missing')).toBeVisible();
    expect(screen.getByText('0', { exact: true })).toBeVisible();
    expect(screen.queryByText('report_markdown')).not.toBeInTheDocument();
  });

  it('falls back to values when an invalid presentation would bypass display limits', () => {
    render(<MetricOutput value={{ rows: 9 }} presentation={{ blocks: [{ type: 'table', columns: Array(13).fill('field'), rows: [] }] }} />);
    expect(screen.getByRole('table')).toHaveTextContent('rows9');
  });

  it('falls back safely when old presentation metadata has a null block', () => {
    render(<MetricOutput value={{ rows: 9 }} presentation={{ blocks: [null] } as unknown as ArtifactPresentation} />);
    expect(screen.getByRole('table')).toHaveTextContent('rows9');
  });

  it('directs large results to full JSON download instead of presenting an absent value as null', () => {
    render(<MetricOutput value={undefined} previewOnly presentation={{ blocks: [{ type: 'table', columns: ['参数', '数量'], rows: [['weights', 1024]] }] }} />);
    expect(screen.getByRole('table')).toHaveTextContent('weights1024');
    expect(screen.getByText(/页面展示可读预览，完整指标与精度/)).toBeVisible();
    expect(screen.queryByText('查看原始 JSON')).not.toBeInTheDocument();
    expect(document.querySelector('pre')).toBeNull();
  });
});
