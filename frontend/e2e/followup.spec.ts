import { expect, test } from '@playwright/test';
import { spawn } from 'node:child_process';
import path from 'node:path';

test('live model selection survives reopening the same CSV conversation and submitting again', async ({ page }) => {
  const root = path.resolve('..');
  const child = spawn('uv', ['run', '--frozen', '--extra', 'web', 'python', path.join(root, 'tests/fixtures/live_followup_service.py')], {
    cwd: root, windowsHide: true, detached: process.platform !== 'win32', env: { ...process.env, PYTHONUTF8: '1' }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let service: { url: string; token: string } | undefined;
  let buffer = '';
  let errors = '';
  child.stderr.on('data', chunk => { errors = (errors + chunk.toString()).slice(-8000); });
  const ready = new Promise<{ url: string; token: string }>((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`Loopback test service did not start: ${errors}`)), 30000);
    child.on('error', reject);
    child.stdout.on('data', chunk => {
      buffer += chunk.toString();
      const line = buffer.split(/\r?\n/).find(value => value.startsWith('{'));
      if (line) { try { const info = JSON.parse(line); clearTimeout(timeout); resolve(info); } catch { /* Wait for a complete line. */ } }
    });
    child.once('exit', code => { clearTimeout(timeout); reject(new Error(`Loopback test service exited before readiness: ${code}: ${errors}`)); });
  });
  try {
    const { url, token } = await ready;
    service = { url, token };
    await expect.poll(async () => {
      try { return (await page.request.get(url)).status(); } catch { return 0; }
    }, { timeout: 30000 }).toBe(200);
    const tasks: Record<string, unknown>[] = [];
    page.on('request', request => {
      if (request.method() === 'POST' && /\/conversations\/[^/]+\/runs$/.test(request.url())) tasks.push(request.postDataJSON());
    });
    await page.goto(`${url}/#token=${token}`);
    await page.getByRole('button', { name: '新建对话', exact: true }).click();
    await expect(page.getByRole('button', { name: '新建对话', exact: true })).toBeEnabled();
    await page.getByLabel('选择附件').setInputFiles({ name: '连续对话.csv', mimeType: 'text/csv', buffer: Buffer.from('id,value\n001,2\n002,3\n') });
    await expect(page.getByText('连续对话.csv', { exact: true })).toBeVisible();
    const prompt = '只概览字段与缺失情况，不需要计算或绘图。';
    await page.getByLabel('任务说明').fill(prompt);
    await page.getByRole('button', { name: '发送任务' }).click();
    await expect(page.getByText('已完成', { exact: true })).toHaveCount(1, { timeout: 60000 });
    const headers = { Authorization: `Bearer ${token}` };
    const list = (await (await page.request.get(`${url}/api/v1/conversations`, { headers })).json()).conversations;
    const id = list[0].id;
    const before = await (await page.request.get(`${url}/api/v1/conversations/${id}`, { headers })).json();
    expect(before.model_selection).toHaveProperty('provider_name');
    expect(before.model_selection).toHaveProperty('base_url');
    // Refresh and reopen the stored conversation: this hydrates the rich model
    // description that the old client spread into writable task parameters.
    await page.reload();
    await page.getByRole('button', { name: list[0].title, exact: true }).click();
    await page.getByLabel('任务说明').fill(prompt);
    await page.getByRole('button', { name: '发送任务' }).click();
    await expect(page.getByText('已完成', { exact: true })).toHaveCount(2, { timeout: 60000 });
    expect(tasks).toHaveLength(2);
    for (const task of tasks) {
      expect(Object.keys(task).sort()).toEqual(['model_id', 'provider_id', 'request_id', 'task']);
      expect(task.model_id).toBe('model-a');
    }
    expect(tasks[0].request_id).not.toBe(tasks[1].request_id);
    expect(tasks[1].provider_id).toBe(before.model_selection.provider_id);
    const after = await (await page.request.get(`${url}/api/v1/conversations/${id}`, { headers })).json();
    expect(after.files[0].id).toBe(before.files[0].id);
    expect(after.runs[1].core_task_id).not.toBe(after.runs[0].core_task_id);
    expect(after.runs[1].status).toBe('completed');
    await expect(page.getByText('请求字段缺失、类型错误或包含不支持的参数。')).toHaveCount(0);
  } catch (error) {
    throw new Error(`${String(error)}\nLoopback service diagnostics: ${errors}`);
  } finally {
    if (child.exitCode === null && service) {
      const headers = { Authorization: `Bearer ${service.token}` };
      const { url } = service;
      try {
        const conversations = (await (await page.request.get(`${url}/api/v1/conversations`, { headers, timeout: 5000 })).json()).conversations;
        for (const conversation of conversations) {
          const detail = await (await page.request.get(`${url}/api/v1/conversations/${conversation.id}`, { headers, timeout: 5000 })).json();
          for (const run of detail.runs.filter((run: { status: string }) => ['running', 'awaiting_input', 'cancelling'].includes(run.status))) {
            await page.request.post(`${url}/api/v1/runs/${run.id}/cancel`, { headers, data: {}, timeout: 5000 });
            await expect.poll(async () => (await (await page.request.get(`${url}/api/v1/runs/${run.id}`, { headers })).json()).status, { timeout: 10000 }).toMatch(/completed|error|cancelled|interrupted/);
          }
        }
        await page.request.post(`${url}/api/v1/application/shutdown`, { headers, data: {}, timeout: 5000 });
      } catch { /* Hard timeout below also covers a crashed test service. */ }
    }
    await new Promise<void>(resolve => {
      if (child.exitCode !== null) { resolve(); return; }
      const killTimeout = setTimeout(() => {
        if (!child.pid) return;
        // uv owns the Python service: kill only this test-owned process tree.
        if (process.platform === 'win32') {
          const terminator = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
          terminator.on('error', () => child.kill());
        } else {
          try { process.kill(-child.pid, 'SIGKILL'); } catch { child.kill(); }
        }
      }, 15000);
      const exitTimeout = setTimeout(resolve, 20000);
      child.once('exit', () => { clearTimeout(killTimeout); clearTimeout(exitTimeout); resolve(); });
    });
  }
});
