import {test, expect} from '@playwright/test';
import fs from 'node:fs';

const env = Object.fromEntries(fs.readFileSync('.env', 'utf8').split('\n')
  .filter(line => line.includes('=') && !line.startsWith('#'))
  .map(line => { const at = line.indexOf('='); return [line.slice(0, at), line.slice(at + 1)]; }));
let adminCookies, operatorCookies;
test.beforeAll(async ({playwright, baseURL}) => {
  for (const role of ['admin', 'operator']) {
    const api = await playwright.request.newContext({baseURL});
    const response = await api.post('/api/v1/operator/login', {
      data: {role, password: env[role === 'admin' ? 'ADMIN_PASSWORD' : 'OPERATOR_PASSWORD']},
    });
    expect(response.ok()).toBeTruthy();
    const {cookies} = await api.storageState();
    if (role === 'admin') adminCookies = cookies; else operatorCookies = cookies;
    await api.dispose();
  }
});

async function enter(page, context, view = 'overview', cookies = adminCookies) {
  await context.addCookies(cookies);
  await page.goto('/operator#' + view);
  await expect(page.locator('#desk')).toBeVisible();
  await expect(page.locator('#view-content')).not.toHaveAttribute('aria-busy', 'true');
}
async function noOverflow(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
}

test('marketing remains public, responsive and downloads a local brief', async ({page, baseURL}) => {
  const requests = [];
  page.on('request', request => requests.push(request.url()));
  await page.setViewportSize({width: 390, height: 844});
  await page.goto('/landing');
  await expect(page.locator('h1')).toContainText('Входящий звонок');
  await expect(page.locator('img[alt="Точка"]').first()).toBeVisible();
  await expect(page.locator('.hero-phone img')).toBeVisible();
  expect(await page.locator('.hero-phone img').evaluate(img => img.complete && img.naturalWidth > 0)).toBeTruthy();
  await page.locator('.mobile-nav summary').click();
  await page.locator('.mobile-nav a[href="#tech"]').click();
  await expect(page.locator('.mobile-nav')).not.toHaveAttribute('open', '');
  await noOverflow(page);
  await page.locator('[data-step="2"]').click();
  await expect(page.locator('#stage-detail h3')).toHaveText('Помощник в рабочем ритме');
  await expect(page.locator('[data-step="2"]')).toHaveAttribute('aria-pressed', 'true');
  await page.locator('[data-step="2"]').press('ArrowRight');
  await expect(page.locator('#stage-detail h3')).toHaveText('Больше точек контакта');
  await page.locator('[data-step="3"]').press('Home');
  await expect(page.locator('#stage-detail h3')).toHaveText('Сначала — ваша задача');
  await page.locator('#lead [name=name]').fill('Проверка интерфейса');
  await page.locator('#lead [name=contact]').fill('example@example.com');
  await page.locator('#lead [name=company]').fill('Тестовый локальный бриф');
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', {name: 'Скачать бриф'}).click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toBe('tochka-brief.txt');
  expect(fs.readFileSync(await download.path(), 'utf8')).toContain('Тестовый локальный бриф');
  await expect(page.locator('#lmsg')).toContainText('Данные не отправлены');
  expect(requests.filter(url => url.includes('/api/'))).toEqual([]);
  expect(requests.filter(url => /^https?:/.test(url) && !url.startsWith(baseURL + '/'))).toEqual([]);
  await page.goto('/landing');
  await page.screenshot({path: 'artifacts/tochka-landing-mobile.png', fullPage: true});
  await page.setViewportSize({width: 1440, height: 1000});
  await noOverflow(page);
  await page.screenshot({path: 'artifacts/tochka-landing-desktop.png', fullPage: true});
});

test('all cabinet sections use real APIs and navigation survives reload and back', async ({page, context}) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({width: 1500, height: 1050});
  await enter(page, context);
  for (const view of ['requests', 'calendar', 'sessions', 'agents', 'knowledge', 'services', 'channels', 'company', 'staff', 'usage', 'archive', 'crm', 'audit', 'system', 'overview']) {
    await page.locator(`[data-view="${view}"]`).click();
    await expect(page.locator('#view-content')).not.toHaveAttribute('aria-busy', 'true');
    await expect(page.locator(`[data-view="${view}"]`)).toHaveAttribute('aria-current', 'page');
    await expect(page.locator('#view-content h1')).toBeVisible();
    await expect(page.locator('#retry-view')).toHaveCount(0);
    await noOverflow(page);
  }
  await page.screenshot({path: 'artifacts/tochka-cabinet-overview.png', fullPage: true});
  await page.locator('[data-view=agents]').click();
  await expect(page.locator('[data-agent]').first()).toBeVisible();
  await page.reload();
  await expect(page.locator('#view-content h1')).toHaveText('Агенты');
  await page.locator('[data-view=company]').click();
  await expect(page.locator('#company-form')).toBeVisible();
  await page.goBack();
  await expect(page.locator('#view-content h1')).toHaveText('Агенты');
  await page.setViewportSize({width: 390, height: 844});
  await page.locator('#menu-toggle').click();
  await page.locator('[data-view=overview]').click();
  await expect(page.locator('#view-content h1')).toHaveText('Всё под контролем');
  await expect(page.locator('#menu-toggle')).toHaveAttribute('aria-expanded', 'false');
  await noOverflow(page);
  await page.screenshot({path: 'artifacts/tochka-cabinet-mobile.png', fullPage: true});
  for (const view of ['calendar', 'sessions', 'agents', 'knowledge', 'services', 'channels', 'company', 'staff', 'usage', 'archive', 'crm', 'audit', 'system']) {
    await page.locator('#menu-toggle').click();
    await page.locator(`[data-view="${view}"]`).click();
    await expect(page.locator('#view-content')).not.toHaveAttribute('aria-busy', 'true');
    await expect(page.locator('#view-content h1')).toBeVisible();
    await noOverflow(page);
  }
  expect(errors).toEqual([]);
});

test('draft knowledge persists after reload and can be archived without publication', async ({page, context, baseURL}) => {
  await enter(page, context, 'knowledge');
  const title = 'Проверка кабинета ' + Date.now();
  let id;
  try {
    await page.locator('#new-knowledge').click();
    await page.getByLabel('Название', {exact: true}).fill(title);
    await page.getByLabel('Проверенные сведения').fill('Тестовый черновик интерфейса. Не публиковать.');
    await page.route('**/api/v1/operator/knowledge', route => route.request().method() === 'POST'
      ? route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({error: {message: 'Проверка ошибки сохранения'}})})
      : route.continue());
    await page.getByRole('button', {name: 'Сохранить черновик'}).click();
    await expect(page.locator('#toast')).toContainText('Проверка ошибки сохранения');
    await expect(page.getByLabel('Название', {exact: true})).toHaveValue(title);
    await expect(page.getByRole('button', {name: 'Сохранить черновик'})).toBeEnabled();
    await page.unroute('**/api/v1/operator/knowledge');
    const created = page.waitForResponse(response => response.url().endsWith('/operator/knowledge') && response.request().method() === 'POST');
    await page.getByRole('button', {name: 'Сохранить черновик'}).click();
    const response = await created;
    expect(response.status()).toBe(201);
    id = (await response.json()).id;
    await expect(page.locator('#editor-dialog')).not.toBeVisible();
    await page.reload();
    await page.locator('#knowledge-search').fill(title);
    const card = page.locator('.knowledge-card').filter({hasText: title});
    await expect(card).toContainText('Черновик');
    await card.getByRole('button', {name: 'Редактировать'}).click();
    await page.getByLabel('Проверенные сведения').fill('Изменённый тестовый черновик. Не публиковать.');
    await page.getByRole('button', {name: 'Сохранить черновик'}).click();
    await expect(page.locator('#editor-dialog')).not.toBeVisible();
    await page.locator('#knowledge-search').fill(title);
    await expect(card).toContainText('Изменённый тестовый черновик');
    await card.getByRole('button', {name: 'В архив', exact: true}).click();
    await expect(page.locator('#toast')).toContainText('Документ больше не используется');
    await page.locator('#knowledge-status').selectOption('archive');
    await page.locator('#knowledge-search').fill(title);
    await expect(card).toContainText('Архив');
  } finally {
    if (id) await page.request.delete('/api/v1/operator/knowledge/' + id, {headers: {Origin: baseURL}});
  }
});

test('API failure offers retry and a delayed old view cannot replace navigation', async ({page, context}) => {
  await context.addCookies(adminCookies);
  await page.route('**/api/v1/operator/agents', route => route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({error: {message: 'Тестовая недоступность'}})}));
  await page.goto('/operator#agents');
  await expect(page.locator('#retry-view')).toBeVisible();
  await page.unroute('**/api/v1/operator/agents');
  await page.locator('#retry-view').click();
  await expect(page.locator('[data-agent]').first()).toBeVisible();
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/api/v1/operator/knowledge', async route => { await gate; await route.continue().catch(() => {}); });
  await page.locator('[data-view=knowledge]').click();
  await page.locator('[data-view=company]').click();
  release();
  await expect(page.locator('#company-form')).toBeVisible();
  await expect(page.locator('#view-content h1')).toHaveText('Компания');
  await expect(page.locator('[data-view=company]')).toHaveAttribute('aria-current', 'page');
});

test('operator receives only permitted controls', async ({page, context}) => {
  await enter(page, context, 'staff', operatorCookies);
  await expect(page.locator('[data-view=staff]')).toBeHidden();
  await expect(page.locator('#view-content h1')).toHaveText('Всё под контролем');
  await page.locator('[data-view=agents]').click();
  await expect(page.locator('.knowledge-card').first()).toBeVisible();
  await expect(page.locator('[data-agent]')).toHaveCount(0);
  await page.locator('[data-view=knowledge]').click();
  await expect(page.locator('#knowledge-items')).toBeVisible();
  await expect(page.locator('#new-knowledge')).toHaveCount(0);
  const forbidden = await page.request.get('/api/v1/operator/staff');
  expect(forbidden.status()).toBe(403);
});
