import { expect, test } from '@playwright/test';

// A persisted response fixture exercises document layout only. Docker analysis
// acceptance remains in chat.spec.ts and is never replaced by this UI fixture.
test('saved report presentation renders readable sections, tables and frozen duration', async ({ page }, testInfo) => {
  const run = {
    id: 'report-layout', task: '比较各部门记录数量，并解释数据条件。', status: 'completed',
    final_answer: '# 部门数据分析报告\n\n## 核心结论\n\n**A 部门记录最多，共 15 条，占总记录的 60%。** 本次比较按 CSV 中的原始部门标签分组，未合并类别。\n\n## 数据与方法\n\n- 本次使用 25 条记录、2 个字段。\n- 部门与数量均无空白值；数量保持原始整数口径。\n- 各部门采用记录计数进行比较。\n\n## 统计结果\n\n| 部门 | 记录数 | 占比 |\n| --- | ---: | ---: |\n| A | 15 | 60% |\n| B | 10 | 40% |\n\n## 解释与限制\n\n记录数量反映本文件中的分布，**不能据此推断部门绩效**。未提供外部资料，因此结论仅基于 CSV 字段与本次指定的口径。',
    timing: { source: 'host_monotonic', active: false, active_seconds: 62, wall_seconds: 62, model_seconds: 45, model_calls: 4, current_models: [] },
    artifacts: [{ id: 'metric-layout', kind: 'metric', label: '数据预处理统计', value: { original_rows: 25, missing_total: 0, class_counts: { A: 15, B: 10 } }, files: [], presentation: { blocks: [{ type: 'table', columns: ['指标', '值'], rows: [['原始记录数', '25'], ['缺失数量', '0']] }, { type: 'table', title: '类别分布', columns: ['部门', '记录数'], rows: [['A', '15'], ['B', '10']] }] } }],
    events: [], downloads: [],
  };
  await page.route('**/api/v1/conversations', route => route.fulfill({ json: { conversations: [{ id: 'layout', title: '部门分析报告', frozen: true }] } }));
  await page.route('**/api/v1/conversations/layout', route => route.fulfill({ json: { conversation: { id: 'layout', title: '部门分析报告', frozen: true }, files: [{ id: 'fixture-csv', name: '报告布局.csv', role: 'csv' }], messages: [], runs: [run] } }));
  await page.goto('/#token=labweaver-playwright-local-token');
  await page.getByRole('button', { name: '部门分析报告', exact: true }).click();
  await expect(page).toHaveTitle('AnaliyMaster · 数据分析');
  await expect(page.getByLabel('任务执行用时')).toHaveText('执行用时 1分 02秒');
  const report = page.getByRole('region', { name: '分析报告正文' });
  await expect(report.getByRole('heading', { name: '部门数据分析报告' })).toBeVisible();
  await expect(report.getByRole('table')).toContainText('A1560%');
  const metric = page.getByRole('region', { name: '数据预处理统计' });
  await expect(metric.getByRole('table').first()).toContainText('原始记录数25缺失数量0');
  await expect(metric.locator('pre')).toHaveCount(0);
  await page.locator('.chat-scroll').evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({ path: testInfo.outputPath('report-desktop.png'), animations: 'disabled' });
  await page.setViewportSize({ width: 820, height: 700 });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.screenshot({ path: testInfo.outputPath('report-small-dark.png'), animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await report.getByRole('table').scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('report-table-dark.png'), animations: 'disabled' });
  await metric.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('report-metrics-dark.png'), animations: 'disabled' });
  await metric.getByText('查看原始 JSON').click();
  await expect(metric.locator('pre')).toContainText('original_rows');
  await page.getByRole('button', { name: '查看执行面板' }).click();
  await expect(page.getByRole('complementary', { name: '执行面板' })).toContainText('模型请求累计');
  await expect(page.getByRole('complementary', { name: '执行面板' })).toContainText('45秒 · 4 次');
});
