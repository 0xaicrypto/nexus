/**
 * Playwright configuration for Heurion E2E tests
 */
import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  testMatch: '**/*.spec.ts',
  timeout: 30000,
  expect: { timeout: 10000 },
  retries: 1,
  use: {
    // E2E runs on VPS via SSH, hitting Nginx directly to skip Cloudflare.
    // Chromium --host-rules maps staging.heurion.org → localhost (self-signed SSL).
    baseURL: process.env.BASE_URL || 'https://staging.heurion.org',
    headless: true,
    viewport: { width: 1280, height: 800 },
    ignoreHTTPSErrors: true,
  },
  projects: [
    {
      name: 'chromium',
      use: {
        browserName: 'chromium',
        launchOptions: {
          args: ['--host-rules=MAP staging.heurion.org localhost'],
        },
      },
    },
  ],
})
