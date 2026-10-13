import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir: './tests/browser', testMatch: 'voices-regression.spec.js', timeout: 90000, workers: 1,
  use: {browserName: 'chromium', screenshot: 'only-on-failure', trace: 'off'},
  outputDir: 'artifacts/voice-regression-browser', reporter: [['list']],
});
