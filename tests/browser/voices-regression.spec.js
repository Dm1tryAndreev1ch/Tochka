import {test, expect, chromium} from '@playwright/test';
import path from 'node:path';

for (const [profile, name] of [['male', 'Геннадий'], ['female', 'Мария']]) {
  test(`HTTPS real microphone and streamed ${name} reply`, async () => {
    const browser = await chromium.launch({args: [
      '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
      `--use-file-for-fake-audio-capture=${path.resolve('artifacts/voice-regression-input.wav')}`,
    ]});
    const context = await browser.newContext({ignoreHTTPSErrors: true,
      viewport: {width: 390, height: 844}, isMobile: true, hasTouch: true});
    const page = await context.newPage();
    try {
      await page.goto('https://192.168.1.119:8443/');
      await expect(page.locator('#health-label')).toContainText('на связи', {timeout: 30000});
      await page.locator(`[data-voice="${profile}"]`).click();
      await page.locator('#consent').check();
      const created = page.waitForResponse(r => r.url().endsWith('/api/v1/sessions') && r.request().method() === 'POST');
      await page.locator('#start-call').click();
      const session = await (await created).json();
      expect(session.voice_profile).toBe(profile);
      await expect(page.locator('#call-controls')).toBeVisible({timeout: 30000});
      const heard = await page.evaluate(async () => {
        const element = document.querySelector('#remote-audio');
        for (let i = 0; i < 100 && !element.srcObject; i++) await new Promise(r => setTimeout(r, 100));
        if (!element.srcObject) return false;
        const audio = new AudioContext();
        const source = audio.createMediaStreamSource(element.srcObject);
        const analyser = audio.createAnalyser();source.connect(analyser);await audio.resume();
        const samples = new Float32Array(analyser.fftSize);
        try {
          for (let i = 0; i < 160; i++) {
            analyser.getFloatTimeDomainData(samples);
            if (samples.some(v => Math.abs(v) > .005)) return true;
            await new Promise(r => setTimeout(r, 100));
          }
          return false;
        } finally {source.disconnect();await audio.close();}
      });
      expect(heard).toBe(true);
      await expect(page.locator('#transcript')).toContainText(/какой ваш адрес/i, {timeout: 45000});
      await expect(page.locator('#transcript')).toContainText(/Минск/, {timeout: 15000});
      await page.locator('#end-call').click();
      await expect(page.locator('#call-controls')).toBeHidden();
    } finally {
      if (await page.locator('#end-call').isVisible().catch(() => false)) await page.locator('#end-call').click();
      await context.close();await browser.close();
    }
  });
}
