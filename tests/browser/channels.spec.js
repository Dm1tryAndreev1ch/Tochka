import {test,expect} from '@playwright/test';
import fs from 'node:fs';
const env=Object.fromEntries(fs.readFileSync('.env','utf8').split('\n').filter(l=>l.includes('=')&&!l.startsWith('#')).map(l=>{const i=l.indexOf('=');return[l.slice(0,i),l.slice(i+1)]}));

test('administrator switches call modes and operator cannot access credentials',async({page})=>{
  await page.goto('/operator');
  await page.locator('#login-password').fill(env.ADMIN_PASSWORD);
  await page.locator('#login-form button[type=submit]').click();
  await expect(page.locator('#desk')).toBeVisible();
  await page.locator('[data-view="channels"]').click();
  await expect(page.getByRole('heading',{name:'Где принимать звонки'})).toBeVisible();
  await expect(page.locator('#telegram-auth-form')).toBeVisible();
  await expect(page.locator('#sip-credentials')).toBeVisible();
  await page.screenshot({path:'artifacts/call-channels.png',fullPage:true});
  await page.locator('[data-call-mode="sip"]').click();
  await expect(page.locator('[data-call-mode="sip"]')).toHaveAttribute('aria-pressed','true');
  const visitor=await page.context().newPage();
  try{
    await visitor.goto('/');
    await expect(visitor.locator('#health-label')).toContainText('SIP');
    await expect(visitor.locator('#start-call')).toBeDisabled();
    await visitor.locator('#chat-tab').click();
    await expect(visitor.locator('#start-call')).toBeEnabled();
  }finally{await visitor.close();await page.locator('[data-call-mode="website"]').click();}
  await expect(page.locator('[data-call-mode="website"]')).toHaveAttribute('aria-pressed','true');
  await page.locator('#logout').click();
  await page.locator('#login-role').selectOption('operator');
  await page.locator('#login-password').fill(env.OPERATOR_PASSWORD);
  await page.locator('#login-form button[type=submit]').click();
  await page.locator('[data-view="channels"]').click();
  await expect(page.locator('#sip-credentials')).toHaveCount(0);
  await expect(page.locator('#telegram-auth-form')).toHaveCount(0);
  await expect(page.locator('[data-call-mode="telegram"]')).toBeDisabled();
});
