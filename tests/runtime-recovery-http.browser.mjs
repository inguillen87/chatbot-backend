import {chromium, expect} from '@playwright/test';
import {createServer} from 'vite';
import {readFile, mkdir, writeFile} from 'node:fs/promises';
import assert from 'node:assert/strict';

const backend = process.env.CHATBOC_ACCEPTANCE_URL;
assert.ok(backend && /^http:\/\/127\.0\.0\.1:\d+$/.test(backend));
process.env.VITE_RUNTIME_RECOVERY_ENABLED = 'true';
const expected = JSON.parse(await readFile('tests/fixtures/runtime-recovery-ui.json', 'utf8'));
const server = await createServer({cacheDir: '.vercel/recovery-http-cache',
  server: {host: '127.0.0.1', port: 0}, logLevel: 'error'});
let browser;
const results = [];
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch(process.platform === 'win32' ? {channel: 'chrome', headless: true} : {headless: true});
  await mkdir('.vercel/runtime-recovery-http-evidence', {recursive: true});
  for (const [width, scenario] of [[1440, 'unavailable'], [820, 'mismatch'], [390, 'offline'], [320, 'slow']]) {
    const context = await browser.newContext({viewport: {width, height: 900}, reducedMotion: 'reduce'});
    const control = async mode => {
      const response = await context.request.post(backend + '/__acceptance__/control', {
        headers: {'X-Acceptance-Token': process.env.CHATBOC_ACCEPTANCE_CONTROL}, data: {mode}});
      assert.equal(response.status(), 200, 'Disposable fault control failed');
    };
    await control('normal');
    const login = await context.request.post(origin + '/auth/login', {
      data: {email: process.env.CHATBOC_ACCEPTANCE_EMAIL, password: process.env.CHATBOC_ACCEPTANCE_PASSWORD}});
    assert.equal(login.status(), 200, 'Original password login failed');
    assert.ok((await login.json()).token, 'Original login did not issue signed token');
    const me = () => context.request.get(origin + '/api/me?tenant_slug=acceptance-a');
    assert.equal((await me()).status(), 200, 'Original cookie session unavailable');
    assert.ok((await context.cookies(origin)).length > 0, 'Privacy test requires existing session cookies');
    const page = await context.newPage();
    const errors = [], requestHeaders = [], copyResponses = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      const path = new URL(request.url()).pathname;
      if (path.startsWith('/api/')) requestHeaders.push(request.allHeaders().then(headers => ({
        path, method: request.method(), cookie: headers.cookie ?? null, authorization: headers.authorization ?? null})));
    });
    page.on('response', response => {
      if (new URL(response.url()).pathname === '/api/config/runtime-recovery') copyResponses.push(response.json());
    });
    // Network fence only: no API response or authentication mocks.
    await page.route('**/*', route => {
      const host = new URL(route.request().url()).hostname;
      return ['127.0.0.1', 'localhost', '::1'].includes(host) ? route.continue() : route.abort('blockedbyclient');
    });
    await page.goto(origin + '/tests/e2e/fixtures/runtime-resume.html');
    if (width === 390) await page.evaluate(() => document.documentElement.classList.add('dark'));
    const draft = page.getByRole('textbox', {name: 'Borrador de prueba'});
    await draft.fill('Edición conservada frente al backend real');
    const bottom = page.getByTestId('workspace-bottom');
    await expect(bottom).toBeInViewport({ratio: 1});
    const initial = await bottom.boundingBox();
    assert.deepEqual(await Promise.all(copyResponses), [expected], 'Published backend contract differs from reviewed fixture');
    await control(scenario === 'offline' ? 'normal' : scenario);
    await context.setOffline(true);
    await expect(page.getByText(expected.states.offline.title, {exact: true})).toBeVisible();
    if (scenario === 'offline') {
      await page.getByRole('button', {name: expected.check_label}).click();
      await expect(page.getByText(expected.states.unavailable.title, {exact: true})).toBeVisible({timeout: 15000});
      await context.setOffline(false);
    } else {
      await context.setOffline(false);
      const phase = scenario === 'slow' ? 'waiting' : scenario;
      await expect(page.getByText(expected.states[phase].title, {exact: true})).toBeVisible({timeout: 15000});
      if (scenario !== 'slow') {
        await control('normal');
        const retry = page.getByRole('button', {name: expected.check_label});
        await retry.focus();
        await page.keyboard.press('Enter');
      }
    }
    await expect(page.getByText(expected.states.verified.title, {exact: true})).toBeVisible({timeout: 15000});
    await expect(draft).toHaveValue('Edición conservada frente al backend real');
    await expect(bottom).toBeInViewport({ratio: 1});
    const final = await bottom.boundingBox();
    assert.ok(initial && final && Math.abs(initial.y - final.y) < 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await control('normal');
    assert.equal((await me()).status(), 200, 'Original session lost after recovery');
    const requests = await Promise.all(requestHeaders);
    assert.ok(requests.length >= 2 && requests.every(row => row.method === 'GET' && row.cookie === null && row.authorization === null));
    assert.equal(requests.filter(row => row.path === '/api/config/runtime-recovery').length, 1);
    assert.deepEqual(errors, []);
    await page.screenshot({path: `.vercel/runtime-recovery-http-evidence/recovery-${width}.png`, fullPage: true});
    await page.getByRole('button', {name: expected.dismiss_label}).click();
    await expect(page.getByRole('region', {name: expected.region_label})).toHaveCount(0);
    results.push({width, scenario, recoveryRequests: requests.length, anonymousRecoveryReads: true,
      completeHeadersChecked: true, realCookieSessionPreserved: true, draftPreserved: true, bottomPreserved: true});
    await context.close();
  }
  const report = {fullFlaskApp: true, actualRecoveryComponent: true, fullSpaRouter: false,
    mockedApiResponses: false, browserNetworkEmulation: true, syntheticFaultInjection: true, results};
  await writeFile('.vercel/runtime-recovery-http-evidence/results.json', JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  await browser?.close();
  await server.close();
}
