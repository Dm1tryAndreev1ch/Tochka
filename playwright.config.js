import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir:'./tests/browser',timeout:90000,workers:1,
  use:{baseURL:'http://127.0.0.1:7860',browserName:'chromium',screenshot:'only-on-failure',trace:'off'},
  outputDir:'artifacts/browser-tests',reporter:[['list'],['json',{outputFile:'artifacts/browser-results.json'}]],
});
