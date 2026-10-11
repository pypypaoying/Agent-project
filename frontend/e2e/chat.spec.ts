import { expect, test, type Page } from '@playwright/test';
import path from 'node:path';

const token = 'labweaver-playwright-local-token';
async function start(page: Page, filename: string, bytes: string) {
  await page.goto(`/#token=${token}`);
  await page.getByRole('button', { name: '新建对话', exact: true }).click();
  await expect(page.getByRole('button', { name: '新建对话', exact: true })).toBeEnabled();
  await page.getByLabel('选择附件').setInputFiles({ name: filename, mimeType: 'text/csv', buffer: Buffer.from(bytes) });
  await expect(page.getByText(filename, { exact: true })).toBeVisible();
}

async function send(page: Page, task: string) {
  await page.getByRole('textbox', { name: '任务说明' }).fill(task);
  await page.getByRole('button', { name: '发送任务', exact: true }).click();
}

async function latestRun(page: Page) {
  const testId = await page.locator('.run-card').last().getAttribute('data-testid');
  const response = await page.request.get(`/api/v1/runs/${testId!.slice(4)}`, { headers: { Authorization: `Bearer ${token}` } });
  expect(response.ok()).toBe(true);
  return response.json();
}

test('CSV overview is genuinely profiled, streamed, persisted and downloadable', async ({ page }) => {
  await start(page, '中文问卷.csv', '编号,评分,备注\n001,4,NA\n002,2,\n');
  const responses: string[] = [];
  page.on('response', response => { if (response.url().includes('/api/v1/')) responses.push(response.url()); });
  await send(page, '概览这个 CSV，不需要统计或图表');
  await expect(page.getByText('已完成', { exact: true })).toBeVisible();
  await expect(page.getByText(/已读取完整 CSV：2 行/)).toBeVisible();
  const run = await latestRun(page);
  expect(run.events.some((event: { type: string; payload: { name?: string } }) => event.type === 'tool_end' && event.payload.name === 'profile_csv')).toBe(true);
  // Read the actual serialized SSE response, including persisted evidence, without rerunning the graph.
  const stream = await page.request.get(`/api/v1/runs/${run.id}/events?after=0`, { headers: { Authorization: `Bearer ${token}` } });
  expect(stream.headers()['content-type']).toContain('text/event-stream');
  expect(await stream.text()).toContain('"tool_end"');
  await page.getByRole('button', { name: '查看执行面板' }).click();
  await expect(page.getByText('profile_csv', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: '关闭执行面板' }).click();
  await page.reload();
  const history = page.locator('.conversation-item').first();
  await history.click();
  await expect(page.getByText(/已读取完整 CSV：2 行/)).toBeVisible();
  await page.getByText(/报告、代码与全部文件/).click();
  const downloadPromise = page.waitForEvent('download');
  await page.locator('.all-files .file-download').first().click();
  const download = await downloadPromise;
  expect(await download.failure()).toBeNull();
  expect(responses.every(url => !url.includes(token))).toBe(true);
  await expect(page).not.toHaveURL(/token=/);
});

test('format clarification resumes the real graph once, then another task uses the frozen input', async ({ page }) => {
  await start(page, '歧义分隔符.csv', 'a,b;c\n1,2;3\n4,5;6\n');
  await send(page, '概览这个 CSV');
  const question = page.getByRole('region', { name: 'Agent 澄清问题' });
  await expect(question).toBeVisible();
  await question.getByRole('textbox', { name: '澄清回复' }).fill('1');
  await question.getByRole('button', { name: '回复并继续' }).click();
  await expect(page.getByText('已完成', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '添加文件' })).toBeDisabled();
  await send(page, '再概览这个 CSV');
  await expect(page.getByText('已完成', { exact: true })).toHaveCount(2);
});

for (const example of [
  { file: 'composite.csv', task: '拆分组合编码，交叉统计数量并绘制分组柱状图', expected: '交叉统计' },
  { file: 'dates.csv', task: '解析日期并按日汇总数量，计算峰值谷值差值，绘制趋势图', expected: '数量趋势' },
  { file: 'cleaning.csv', task: '删除重复记录，将数量缺失填充为0，派生双倍数量并导出，报告处理变化', expected: '' },
]) {
  test(`actual Docker analysis through UI: ${example.file}`, async ({ page }, testInfo) => {
    test.skip(process.env.LABWEAVER_REQUIRE_DOCKER !== '1', 'Set LABWEAVER_REQUIRE_DOCKER=1 with the sandbox image to run real Docker acceptance.');
    await page.goto(`/#token=${token}`);
    await page.getByRole('button', { name: '新建对话', exact: true }).click();
    await expect(page.getByRole('button', { name: '新建对话', exact: true })).toBeEnabled();
    await page.getByLabel('选择附件').setInputFiles(path.resolve('..', 'tests', 'fixtures', example.file));
    await expect(page.getByText(example.file, { exact: true })).toBeVisible();
    await send(page, example.task);
    await expect(page.getByText('已完成', { exact: true })).toBeVisible({ timeout: 90_000 });
    await expect(page.locator('table').first()).toBeVisible();
    if (example.expected) await expect(page.locator('img.chart')).toBeVisible();
    const run = await latestRun(page);
    const table = run.artifacts.find((artifact: { kind: string }) => artifact.kind === 'table');
    const preview = await page.request.get(`/api/v1/artifacts/${table.id}/preview?limit=20`, { headers: { Authorization: `Bearer ${token}` } });
    const actual = await preview.json();
    if (example.file === 'composite.csv') {
      expect(actual.columns).toEqual(['类别', 'East', 'West']);
      expect(actual.rows).toEqual([['A', '6', '3'], ['B', '5', '1']]);
    } else if (example.file === 'dates.csv') {
      expect(actual.columns).toEqual(['日期', '数量']);
      expect(actual.rows).toEqual([['2026-12-01', '5'], ['2026-12-02', '4'], ['2026-12-03', '3']]);
      expect(run.artifacts.find((artifact: { kind: string }) => artifact.kind === 'metric').value).toMatchObject({ 峰值: 5, 谷值: 3, 差值: 2 });
    } else {
      expect(actual.row_count).toBe(3);
      expect(actual.rows).toEqual([['001', '2', 'A', '4'], ['002', '0', 'B', '0'], ['003', '4', 'A', '8']]);
      expect(run.artifacts.find((artifact: { kind: string }) => artifact.kind === 'metric').value).toMatchObject({ 原始行数: 4, 处理后行数: 3, 删除重复: 1, 填充缺失: 1 });
    }
    expect(run.events.some((event: { type: string; payload: { record?: { status: string } } }) => event.type === 'execution_end')).toBe(true);
    const script = run.downloads.find((file: { name: string }) => file.name.endsWith('.py'));
    expect(script).toBeTruthy();
    const code = await page.request.get(`/api/v1/files/${script.id}/content`, { headers: { Authorization: `Bearer ${token}` } });
    expect(await code.text()).toContain('load_dataset()');
    await expect(page.getByRole('button', { name: '新建对话', exact: true })).toBeEnabled();
    const composer = await page.getByRole('textbox', { name: '任务说明' }).boundingBox();
    expect(composer && composer.y + composer.height <= page.viewportSize()!.height).toBeTruthy();
    await page.screenshot({ path: testInfo.outputPath('desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 820, height: 700 });
    await page.emulateMedia({ colorScheme: 'dark' });
    await expect(page.locator('.new-chat')).toHaveCSS('background-color', 'rgb(29, 30, 32)');
    await page.screenshot({ path: testInfo.outputPath('small-dark.png'), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.getByRole('button', { name: '查看执行面板' }).click();
    await expect(page.getByText('执行 Python', { exact: true })).toBeVisible();
    await expect(page.getByText('执行结果', { exact: true })).toBeVisible();
  });
}
