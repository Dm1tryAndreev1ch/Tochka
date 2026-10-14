import {test, expect} from '@playwright/test';
import fs from 'node:fs';

const env = Object.fromEntries(fs.readFileSync('.env', 'utf8').split('\n')
  .filter(line => line.includes('=') && !line.startsWith('#'))
  .map(line => {const at = line.indexOf('='); return [line.slice(0, at), line.slice(at + 1)];}));
let adminCookies, operatorCookies;
test.beforeAll(async ({playwright, baseURL}) => {
  test.skip(baseURL !== 'http://127.0.0.1:7861', 'Requires the disposable UI regression server');
  for (const role of ['admin', 'operator']) {
    const api = await playwright.request.newContext({baseURL});
    const health = await api.get('/api/v1/health/ready');
    expect((await health.json()).error).toContain('отдельном стенде');
    expect((await api.post('/api/v1/operator/login', {
      data: {role, password: env[role === 'admin' ? 'ADMIN_PASSWORD' : 'OPERATOR_PASSWORD']},
    })).ok()).toBeTruthy();
    const {cookies} = await api.storageState();
    if (role === 'admin') adminCookies = cookies; else operatorCookies = cookies;
    await api.dispose();
  }
});

async function enter(page, context, view, cookies = adminCookies) {
  await context.addCookies(cookies);
  await page.goto('/operator#' + view);
  await expect(page.locator('#desk')).toBeVisible();
  await expect(page.locator('#view-content')).not.toHaveAttribute('aria-busy', 'true');
}

test('completed conversations lead to the protected archive', async ({page, context}) => {
  await enter(page, context, 'sessions');
  await page.locator('#session-filter').selectOption('ended');
  await page.locator('[data-session]').first().click();
  await expect(page.locator('#open-session-archive')).toBeVisible();
  await page.locator('#open-session-archive').click();
  await expect(page.locator('#archive-purpose')).toBeVisible();
});

for (const width of [390, 1500]) {
  test(`archive transcript, audio and export require purpose at ${width}px`, async ({page, context}) => {
    await page.setViewportSize({width, height: 1000});
    await enter(page, context, 'archive');
    await page.locator('[data-text]').first().click();
    await expect(page.locator('#toast')).toContainText('Выберите цель доступа');
    await expect(page.locator('#archive-detail')).toBeHidden();
    await page.locator('#archive-purpose').selectOption('quality_review');
    await page.locator('[data-text]').first().click();
    await expect(page.locator('#archive-detail')).toContainText('Тестовая расшифровка');
    const response = page.waitForResponse(r => r.url().includes('/audio?purpose=quality_review'));
    await page.locator('[data-audio]').first().click();
    expect((await response).headers()['cache-control']).toBe('no-store');
    await expect(page.locator('#archive-player')).toBeVisible();
    await expect.poll(() => page.locator('#archive-player').evaluate(audio => audio.readyState)).toBeGreaterThanOrEqual(2);
    const downloaded = page.waitForEvent('download');
    await page.locator('[data-export]').first().click();
    const download = await downloaded;
    expect(download.suggestedFilename()).toMatch(/^conversation-.*\.wav$/);
    const wav = fs.readFileSync(await download.path());
    expect(wav.subarray(0, 4).toString()).toBe('RIFF');
    expect(wav.readUInt16LE(22)).toBe(2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    await page.screenshot({path: `artifacts/ui-archive-${width}.png`, fullPage: true});
  });
}

test('operator cannot read or export archived dialogue', async ({page, context}) => {
  await enter(page, context, 'sessions', operatorCookies);
  await expect(page.locator('[data-view=archive]')).toBeHidden();
  await page.locator('#session-filter').selectOption('ended');
  await page.locator('[data-session]').first().click();
  await expect(page.locator('#session-detail')).toContainText('Для доступа к архиву');
  await expect(page.locator('#open-session-archive')).toHaveCount(0);
  expect((await page.request.get('/api/v1/operator/archive')).status()).toBe(403);
});
