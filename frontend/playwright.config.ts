import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 25_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:8765', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], ...(process.env.LABWEAVER_BROWSER_CHANNEL ? { channel: process.env.LABWEAVER_BROWSER_CHANNEL } : {}) } }],
  webServer: {
    command: 'cd .. && uv run --extra web labweaver serve --offline --data-dir .cache/web-e2e --port 8765',
    url: 'http://127.0.0.1:8765',
    reuseExistingServer: !process.env.CI,
    timeout: 90_000,
    env: { LABWEAVER_ACCESS_TOKEN: 'labweaver-playwright-local-token', LANGSMITH_TRACING: 'false' },
  },
});
