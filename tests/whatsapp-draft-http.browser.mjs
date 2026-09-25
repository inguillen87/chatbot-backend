import {chromium, expect} from '@playwright/test';
import {createServer} from 'vite';
import {mkdir,writeFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const backend = new URL(process.env.PROFILE_BACKEND_ORIGIN || '');
assert.equal(backend.hostname, '127.0.0.1');
assert.equal(backend.protocol, 'http:');
const accounts = JSON.parse(process.env.PROFILE_TEST_ACCOUNTS || '{}');
const password = process.env.PROFILE_TEST_PASSWORD;
assert.ok(password && accounts['acceptance-a']);
process.env.VITE_BACKEND_URL = '/api';
process.env.VITE_API_URL = '/api';
process.env.VITE_PROXY_TARGET = backend.origin;
process.env.VITE_USE_LOCAL_API_PROXY = 'true';
process.env.VITE_BACKEND_BOOTSTRAP_GATE_ENABLED = 'true';
const server = await createServer({cacheDir: '.vercel/whatsapp-http-cache',
  server: {host: '127.0.0.1', port: 0}, logLevel: 'error'});
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  await mkdir('.vercel/whatsapp-http-evidence', {recursive: true});
  browser = await chromium.launch(process.platform === 'win32' ? {channel: 'chrome', headless: true} : {headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
  // Network restriction, not a response mock. No provider or customer origins allowed.
  await context.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1'
    ? route.continue() : route.abort('blockedbyclient'));
  const page = await context.newPage();
  page.setDefaultTimeout(45000);
  const errors = [], creations = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (request.method() === 'POST' && /\/template-packs\/[^/]+\/drafts$/.test(new URL(request.url()).pathname)) {
      creations.push(request.allHeaders().then(headers => ({path: new URL(request.url()).pathname,
        operationKeyPresent: typeof headers['idempotency-key'] === 'string'})));
    }
  });
  await page.goto(origin + '/login?next=%2Fperfil%3Fsection%3Dgeneral');
  await page.getByPlaceholder('Correo electrónico', {exact: true}).fill(accounts['acceptance-a'].email);
  await page.getByPlaceholder('Contraseña', {exact: true}).fill(password);
  await page.getByRole('button', {name: 'Iniciar Sesión', exact: true}).click();
  await page.waitForURL(/\/perfil/);
  await page.getByRole('textbox', {name: 'Nombre legal o institucional'}).waitFor();
  const catalogPath = '/api/admin/whatsapp/template-packs';
  const catalogResponse = page.waitForResponse(response => new URL(response.url()).pathname === catalogPath
    && response.request().method() === 'GET' && response.status() === 200);
  await page.goto(origin + '/perfil/plantillas-respuesta?tenant_slug=acceptance-a');
  const catalog = await (await catalogResponse).json();
  assert.equal(catalog.tenant.slug, 'acceptance-a');
  assert.equal(catalog.capabilities.materialize_local_draft, true);
  const pack = catalog.packs.find(item => item.vertical === 'municipio');
  assert.ok(pack && pack.templates.length > 0);
  const panel = page.getByTestId('whatsapp-template-packs');
  await expect(panel).toBeVisible();
  await panel.getByLabel('Conjunto de plantillas').selectOption(pack.vertical);
  const create = panel.getByRole('button', {name: catalog.frontend_contract.copy.materialize, exact: true});
  await expect(create).toBeEnabled();
  const written = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === catalogPath + '/municipio/drafts');
  // Same browser task: a second click cannot schedule another transaction.
  await create.evaluate(button => { button.click(); button.click(); });
  const response = await written;
  assert.equal(response.status(), 201);
  const receipt = await response.json();
  assert.equal(receipt.provider_calls_performed, false);
  assert.equal(receipt.created_count, pack.templates.length);
  await expect(panel.getByRole('button', {name: catalog.frontend_contract.copy.materialized, exact: true})).toBeDisabled();
  await page.reload();
  await expect(panel.getByRole('button', {name: catalog.frontend_contract.copy.materialized, exact: true})).toBeDisabled();
  const results = [];
  for (const [width, dark] of [[1440, false], [820, false], [390, true], [320, false]]) {
    await page.setViewportSize({width, height: 1000});
    await page.evaluate(value => document.documentElement.classList.toggle('dark', value), dark);
    await panel.scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await expect(panel.getByRole('button', {name: catalog.frontend_contract.copy.materialized, exact: true})).toBeDisabled();
    await page.screenshot({path: `.vercel/whatsapp-http-evidence/panel-${width}.png`, fullPage: true});
    results.push({width, dark, persistedDraftVisible: true, horizontalOverflow: false});
  }
  const writes = await Promise.all(creations);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].operationKeyPresent, true);
  assert.deepEqual(errors, []);
  assert.equal((await context.request.get(origin + '/api/me?tenant_slug=acceptance-a')).status(), 200);
  const report = {actualSpaRoute: true, fullFlaskApp: true, mockedApiResponses: false,
    syntheticAccountsAndDatabase: true, observedDraftPosts: writes.length,
    passwordSessionPreserved: true, localDraftNotProviderApproval: true, results};
  await writeFile('.vercel/whatsapp-http-evidence/results.json', JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
  await context.close();
} finally {
  await browser?.close();
  await server.close();
}
