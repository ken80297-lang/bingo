// Exercise the actual page with controlled API responses, including a failure.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
(async () => {
  const browser = await chromium.launch({ headless: true });
  const html = fs.readFileSync(path.join(__dirname, '../static/shadow_ranking.html'), 'utf8');
  const item = { target_issue: '115059939', frozen_at: '2026-10-23T03:26:00Z',
    status: 'waiting_draw', top5: [14,3,11,2,9], top6: [14,3,11,2,9,6],
    top5_hits: null, top6_hits: null, matched_numbers: [] };
  const payload = { items: [item], verified_count: 0, waiting_count: 1,
    excluded: 0, top5_mean: null, top6_mean: null, same20_top5_expected: null,
    same20_top6_expected: null, as_of: '2026-10-23T03:27:00Z' };
  for (const width of [390, 768]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    let fail = false;
    await page.route('http://shadow.test/**', route => route.fulfill(
      route.request().url().endsWith('/api/top-k-shadow')
        ? { status: fail ? 503 : 200, contentType: 'application/json', body: JSON.stringify(payload) }
        : { contentType: 'text/html', body: html }));
    await page.goto('http://shadow.test/shadow-ranking');
    await page.waitForFunction(() => document.getElementById('state').textContent.startsWith('更新於'));
    if (await page.locator('#latest .ball').count() !== 6) throw Error('Expected only six picks');
    if (JSON.stringify(await page.locator('#latest .ball').allTextContents()) !== JSON.stringify(['02','03','06','09','11','14'])) throw Error('Picks must display in numeric order');
    if (!(await page.locator('#latest').innerText()).includes('等待驗證')) throw Error('Pending state missing');
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw Error('Page overflow');
    const original = await page.locator('#latest').innerText();
    fail = true;
    await page.getByRole('button', { name: '重新整理' }).click();
    await page.waitForFunction(() => document.getElementById('state').textContent === '更新失敗');
    if (await page.locator('#latest').innerText() !== original) throw Error('Previous data lost');
    if (errors.length) throw Error(errors.join('\n'));
    console.log(`${width}px: frozen picks, pending status, layout and failed-refresh retention passed`);
    await page.close();
  }
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
