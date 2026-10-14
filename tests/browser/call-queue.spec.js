import {test,expect,chromium} from '@playwright/test';

test('second connected caller waits without transcription and is promoted',async()=>{
 const browser=await chromium.launch({args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream']});
 const first=await browser.newContext(),second=await browser.newContext();
 const a=await first.newPage(),b=await second.newPage();
 try{
  for(const page of [a,b]){await page.goto('http://127.0.0.1:7860/');await expect(page.locator('#health-label')).not.toContainText('Проверяем');await page.locator('#consent').check();}
  await a.locator('#start-call').click();await expect(a.locator('#call-controls')).toBeVisible({timeout:30000});
  await b.locator('#start-call').click();await expect(b.locator('#call-controls')).toBeVisible({timeout:30000});
  await expect(b.locator('#call-title')).toContainText('очереди',{timeout:15000});
  const before=await b.locator('#transcript').innerText();
  await b.waitForTimeout(1000);expect(await b.locator('#transcript').innerText()).toBe(before);
  await a.locator('#end-call').click();
  await expect(b.locator('#call-title')).not.toContainText('очереди',{timeout:20000});
 }finally{
  for(const page of [a,b])if(await page.locator('#end-call').isVisible().catch(()=>false))await page.locator('#end-call').click();
  await first.close();await second.close();await browser.close();
 }
});
