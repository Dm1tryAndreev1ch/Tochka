import {test,expect} from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import {X509Certificate} from 'node:crypto';
const config=JSON.parse(fs.readFileSync('.runtime/mobile.json','utf8'));
test.use({baseURL:config.url,ignoreHTTPSErrors:true,viewport:{width:390,height:844},isMobile:true,hasTouch:true,permissions:['microphone'],launchOptions:{args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream',`--use-file-for-fake-audio-capture=${path.resolve('artifacts/voice-test-input.wav')}`]}});

test('mobile HTTPS UI, both voice choices and sample playback',async({page})=>{
 await page.goto('/');
 expect(await page.evaluate(()=>isSecureContext)).toBe(true);
 expect(await page.evaluate(()=>Boolean(navigator.mediaDevices?.getUserMedia))).toBe(true);
 await expect(page.getByRole('button',{name:/Геннадий/})).toHaveAttribute('aria-pressed','true');
 await page.getByRole('button',{name:/Мария/}).click();
 await expect(page.getByRole('button',{name:/Мария/})).toHaveAttribute('aria-pressed','true');
 await page.getByRole('button',{name:'Послушать голос'}).click();
 await expect.poll(()=>page.locator('#preview-audio').evaluate(a=>a.duration)).toBeGreaterThan(1);
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
 await page.screenshot({path:'artifacts/mobile-https-voices.png',fullPage:true});
 await page.getByRole('link',{name:'Подключить телефон ↗'}).click();
 await expect(page.getByRole('heading',{name:'Разговор с телефона'})).toBeVisible();
 await expect(page.getByAltText('QR-код подключения телефона')).toBeVisible();
});

test('onboarding serves the exact CA and no private key or business APIs',async({request})=>{
 const cert=await request.get(config.setup_url+'/voice-desk-ca.cer');expect(cert.ok()).toBe(true);
 const x509=new X509Certificate(await cert.body());
 expect(x509.fingerprint256.replaceAll(':','')).toBe(config.ca_sha256);
 const profile=await request.get(config.setup_url+'/voice-desk.mobileconfig');expect(profile.ok()).toBe(true);
 expect(await profile.text()).toContain('com.apple.security.root');
 expect((await request.get(config.setup_url+'/rootCA-key.pem')).status()).toBe(404);
 expect((await request.get(config.setup_url+'/api/v1/operator/requests')).status()).toBe(404);
});

test('female persona receives actual synthesized RTC audio over HTTPS',async({page})=>{
 await expect.poll(async()=>{const r=await page.request.get('/api/v1/health/ready');const h=await r.json();return h.natural_voice_ready&&!h.voice_busy},{timeout:120000}).toBe(true);
 await page.goto('/');await page.getByRole('button',{name:/Мария/}).click();await page.getByRole('checkbox').check();
 const creation=page.waitForResponse(r=>r.url().endsWith('/api/v1/sessions')&&r.request().method()==='POST');
 await page.getByRole('button',{name:'Начать разговор',exact:false}).click();
 expect((await (await creation).json()).voice_profile).toBe('female');
 await expect(page.locator('#end-call')).toBeVisible({timeout:30000});
 await expect(page.locator('.bubble.user')).toContainText(/стоит.*консультац/i,{timeout:40000});
 await expect(page.locator('.bubble.assistant').last()).toContainText('30',{timeout:30000});
 const peak=await page.evaluate(async()=>{const audio=document.querySelector('#remote-audio');const ctx=new AudioContext();const analyser=ctx.createAnalyser();ctx.createMediaStreamSource(audio.srcObject).connect(analyser);const values=new Float32Array(analyser.fftSize);let peak=0;for(let i=0;i<100;i++){analyser.getFloatTimeDomainData(values);peak=Math.max(peak,...values.map(Math.abs));await new Promise(r=>setTimeout(r,30))}await ctx.close();return peak;});
 expect(peak).toBeGreaterThan(.001);
 await page.getByRole('button',{name:'Завершить',exact:true}).click();
});
