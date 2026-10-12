import { defineConfig, devices } from '@playwright/test';
const port = Number(process.env.LABWEAVER_E2E_PORT ?? 8765);
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 25_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: { baseURL, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], ...(process.env.LABWEAVER_BROWSER_CHANNEL ? { channel: process.env.LABWEAVER_BROWSER_CHANNEL } : {}) } }],
  webServer: {
    command: `cd .. && uv run --extra web labweaver serve --offline --data-dir .cache/web-e2e-${port} --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 90_000,
    env: { LABWEAVER_ACCESS_TOKEN: 'labweaver-playwright-local-token', LANGSMITH_TRACING: 'false' },
  },
});
