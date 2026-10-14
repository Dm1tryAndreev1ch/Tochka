import {defineConfig} from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser', timeout: 90000, workers: 1,
  testMatch: /(?:design-workspace|protected-workspace)\.spec\.js/,
  use: {baseURL: 'http://127.0.0.1:7861', browserName: 'chromium', screenshot: 'only-on-failure', trace: 'off'},
  outputDir: 'artifacts/ui-isolated-tests',
  reporter: [['list'], ['json', {outputFile: 'artifacts/ui-isolated-results.json'}]],
});
