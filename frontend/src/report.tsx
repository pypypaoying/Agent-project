import { useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ArtifactPresentation, PresentationBlock, PresentationScalar } from './types';

const MAX_ROWS = 20;
const MAX_COLUMNS = 12;
const MAX_BLOCKS = 32;
const MAX_TEXT_BYTES = 65536;
const MAX_CELL_TEXT = 1000;
const isScalar = (value: unknown): value is PresentationScalar => value == null || ['string', 'number', 'boolean'].includes(typeof value);
const byteLength = (value: string) => new TextEncoder().encode(value).length;

/** Old one-paragraph answers gain whitespace only; authored Markdown stays intact. */
export function readableReportText(value: string): string {
  if (value.length < 320 || /\n|[`\[\]*<>]|^\s*[#>-]\s/.test(value)) return value;
  const sentences = value.split(/(?<=[。！？；])/u).filter(Boolean);
  if (sentences.length < 2) return value;
  const paragraphs: string[] = [];
  let current = '';
  for (const sentence of sentences) {
    if (current && current.length + sentence.length > 180) { paragraphs.push(current); current = ''; }
    current += sentence;
  }
  if (current) paragraphs.push(current);
  return paragraphs.join('\n\n');
}

/** Presentation rounds only the visible number; downloadable data remains unchanged. */
export function formatReportValue(value: unknown): string {
  if (value == null) return '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return '非有限值';
    return Number.isInteger(value) ? String(value) : Number(value.toPrecision(6)).toString();
  }
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return `${value.length} 项${value.length ? ` · ${value.slice(0, 5).map(item => isScalar(item) ? formatReportValue(item) : Array.isArray(item) ? `${item.length} 项` : '对象').join('、')}${value.length > 5 ? '…' : ''}` : ''}`;
  if (typeof value === 'object') return `对象（${Object.keys(value).length} 项）`;
  return String(value);
}

export function Markdown({ children }: { children: string }) {
  return <div className="prose"><ReactMarkdown skipHtml remarkPlugins={[remarkGfm]} components={{
    a: ({ href, children: linkText }) => href && /^https?:\/\//.test(href) ? <a href={href} target="_blank" rel="noopener noreferrer">{linkText}</a> : <span>{linkText}</span>,
    // Images come only from registered, authenticated artifacts, never from model prose.
    img: ({ alt }) => <span>{alt ?? ''}</span>,
    table: ({ children: cells }) => <div className="report-table-scroll"><table>{cells}</table></div>,
  }}>{readableReportText(children)}</ReactMarkdown></div>;
}

/** Old history records have no server presentation. Read their exact values without deriving new claims. */
export function fallbackPresentation(value: unknown): ArtifactPresentation {
  const blocks: PresentationBlock[] = [];
  let truncated = false;
  let usedBytes = 0;
  const boundedText = (text: string) => { if (text.length > MAX_CELL_TEXT) { truncated = true; return `${text.slice(0, MAX_CELL_TEXT)}…`; } return text; };
  const add = (block: PresentationBlock) => {
    const safe = block.type === 'table' ? { ...block, title: block.title ? boundedText(block.title) : undefined, columns: block.columns.map(boundedText), rows: block.rows.map(row => row.map(cell => typeof cell === 'string' ? boundedText(cell) : cell)) } : block;
    const size = byteLength(JSON.stringify(safe));
    if (blocks.length < MAX_BLOCKS && usedBytes + size <= MAX_TEXT_BYTES - 2048) { blocks.push(safe); usedBytes += size; }
    else truncated = true;
  };
  const table = (title: string | undefined, columns: string[], rows: PresentationScalar[][], isTruncated = false) => add({ type: 'table', title, columns, rows, truncated: isTruncated });
  const visit = (current: unknown, title?: string, depth = 0) => {
    if (blocks.length >= MAX_BLOCKS) { truncated = true; return; }
    if (isScalar(current)) { table(title, ['指标', '值'], [[title ?? '结果', current]]); return; }
    if (Array.isArray(current)) {
      if (current.length === 0) { add({ type: 'paragraph', text: `${title ?? '结果'}：没有记录。` }); return; }
      add({ type: 'paragraph', text: `${title ?? '列表'}：共 ${current.length} 项，以下为预览。` });
      if (current.every(Array.isArray)) {
        const width = current.reduce((largest, row) => Math.max(largest, row.length), 0);
        const columns = Array.from({ length: Math.min(width, MAX_COLUMNS) }, (_, i) => `第 ${i + 1} 列`);
        table(title, columns, current.slice(0, MAX_ROWS).map(row => columns.map((_, i) => isScalar(row[i]) ? row[i] ?? null : formatReportValue(row[i]))), current.length > MAX_ROWS || width > MAX_COLUMNS);
        return;
      }
      if (current.every(item => item && typeof item === 'object' && !Array.isArray(item))) {
        const keys = [...new Set(current.slice(0, MAX_ROWS).flatMap(item => Object.keys(item)))];
        const columns = keys.slice(0, MAX_COLUMNS);
        table(title, columns, current.slice(0, MAX_ROWS).map(item => columns.map(key => isScalar(item[key]) ? item[key] ?? null : formatReportValue(item[key]))), current.length > MAX_ROWS || keys.length > MAX_COLUMNS);
        return;
      }
      table(title, ['位置', '值'], current.slice(0, MAX_ROWS).map((item, index) => [index + 1, isScalar(item) ? item : formatReportValue(item)]), current.length > MAX_ROWS);
      return;
    }
    if (current && typeof current === 'object') {
      const entries = Object.entries(current);
      const narrative = entries.find(([key, item]) => key === 'report_markdown' && typeof item === 'string' && byteLength(item) <= MAX_TEXT_BYTES);
      if (narrative) add({ type: 'markdown', text: narrative[1] as string });
      const remaining = entries.filter(([key]) => key !== narrative?.[0]);
      if (!remaining.length) { if (!narrative) add({ type: 'paragraph', text: `${title ?? '结果'}：没有记录。` }); return; }
      if (depth >= 3) { table(title, ['字段', '值'], remaining.slice(0, MAX_ROWS).map(([key, item]) => [key, isScalar(item) ? item : formatReportValue(item)]), remaining.length > MAX_ROWS); return; }
      const scalars = remaining.filter(([, item]) => isScalar(item));
      if (scalars.length) table(title, ['指标', '值'], scalars.slice(0, MAX_ROWS).map(([key, item]) => [key, item as PresentationScalar]), scalars.length > MAX_ROWS);
      for (const [key, item] of remaining.filter(([, item]) => !isScalar(item))) visit(item, title ? `${title} · ${key}` : key, depth + 1);
      return;
    }
    add({ type: 'paragraph', text: formatReportValue(current) });
  };
  visit(value);
  return { blocks, note: `浮点数仅在页面中显示最多 6 位有效数字，完整精度保留在 JSON 文件。${truncated ? '部分结构未展开；完整内容请下载 JSON。' : ''}` };
}

function validPresentation(value: ArtifactPresentation | undefined): value is ArtifactPresentation {
  return Boolean(value && byteLength(JSON.stringify(value)) <= MAX_TEXT_BYTES && Array.isArray(value.blocks) && value.blocks.length <= MAX_BLOCKS && value.blocks.every(block => {
    if (!block || typeof block !== 'object') return false;
    if (block.type === 'paragraph' || block.type === 'markdown') return typeof block.text === 'string' && byteLength(block.text) <= MAX_TEXT_BYTES;
    return block.type === 'table' && Array.isArray(block.columns) && block.columns.length <= MAX_COLUMNS && block.columns.every(column => typeof column === 'string') && Array.isArray(block.rows) && block.rows.length <= MAX_ROWS && block.rows.every(row => Array.isArray(row) && row.length <= MAX_COLUMNS && row.every(isScalar));
  }));
}

function PresentationTable({ block }: { block: Extract<PresentationBlock, { type: 'table' }> }) {
  return <section className="report-data-section">
    {block.title && <h4>{block.title}</h4>}
    <div className="report-table-scroll"><table>
      <thead><tr>{block.columns.map((column, index) => <th key={index}>{column}</th>)}</tr></thead>
      <tbody>{block.rows.map((row, index) => <tr key={index}>{row.map((cell, column) => <td key={column}>{formatReportValue(cell)}</td>)}</tr>)}</tbody>
    </table></div>
    {block.truncated && <p className="report-note">这里展示部分行或列；完整内容与精度请下载 JSON。</p>}
  </section>;
}

function RawJson({ value }: { value: unknown }) {
  const [open, setOpen] = useState(false);
  const raw = useMemo(() => open ? JSON.stringify(value, null, 2) ?? 'null' : '', [open, value]);
  const tooLong = raw.length > MAX_TEXT_BYTES;
  return <details className="raw-json" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>查看原始 JSON</summary>
    {open && <><pre><code>{tooLong ? raw.slice(0, MAX_TEXT_BYTES) : raw}</code></pre>{tooLong && <p className="report-note">原始内容较大，此处仅展示开头；完整 JSON 请使用成果卡上的下载按钮。</p>}</>}
  </details>;
}

export function MetricOutput({ value, presentation, previewOnly = false }: { value: unknown; presentation?: ArtifactPresentation; previewOnly?: boolean }) {
  const document = useMemo(() => validPresentation(presentation) ? presentation : fallbackPresentation(value), [presentation, value]);
  return <div className="report-data">
    {document.blocks.map((block, index) => block.type === 'table' ? <PresentationTable key={index} block={block} /> : block.type === 'markdown' ? <Markdown key={index}>{block.text}</Markdown> : <p className="report-paragraph" key={index}>{block.text}</p>)}
    {document.note && <p className="report-note">{document.note}</p>}
    {previewOnly ? <p className="report-note">页面展示可读预览，完整指标与精度请使用成果卡上的 JSON 下载按钮。</p> : <RawJson value={value} />}
  </div>;
}
