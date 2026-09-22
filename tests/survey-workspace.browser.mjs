import {chromium, expect} from '@playwright/test';
import {createServer} from 'vite';
import {mkdir,writeFile} from 'node:fs/promises';
import assert from 'node:assert/strict';

const backend = new URL(process.env.SURVEY_TEST_ORIGIN || '');
assert.equal(backend.hostname, '127.0.0.1');
assert.equal(backend.protocol, 'http:');
const accounts = JSON.parse(process.env.SURVEY_TEST_ACCOUNTS || '{}');
const cases = JSON.parse(process.env.SURVEY_TEST_CASES || '{}');
const password = process.env.SURVEY_TEST_PASSWORD;
assert.ok(password && accounts['acceptance-a'] && cases['delete-recovery']);
process.env.VITE_BACKEND_URL = '/api';
process.env.VITE_API_URL = '/api';
process.env.VITE_PROXY_TARGET = backend.origin;
process.env.VITE_USE_LOCAL_API_PROXY = 'true';
process.env.VITE_BACKEND_BOOTSTRAP_GATE_ENABLED = 'true';
const server = await createServer({cacheDir: '.vercel/survey-workspace-cache',
    server: {host: '127.0.0.1', port: 0}, logLevel: 'error'});
const evidence = 'test-evidence/survey-workspace';
const results = [];
let browser;
try {
  await mkdir(evidence, {recursive: true});
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch(process.platform === 'win32' ? {channel: 'chrome', headless: true} : {headless: true});
  for (const [width, scenario] of [[1440, 'close'], [820, 'delete'], [390, 'close-recovery'], [320, 'delete-recovery']]) {
    const context = await browser.newContext({viewport: {width, height: 1000}, reducedMotion: 'reduce'});
    // The only interception is a network fence; no synthetic API responses.
    await context.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1'
      ? route.continue() : route.abort('blockedbyclient'));
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    const errors = [], mutationRequests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      const path = new URL(request.url()).pathname;
      if (request.method() === 'DELETE' || (request.method() === 'POST' && /\/surveys\/\d+\/close$/.test(path))) {
        mutationRequests.push({method: request.method(), path});
      }
    });
    const control = async mode => {
      const response = await context.request.post(backend.origin + '/__survey_acceptance__/control', {
        headers: {'X-Acceptance-Token': process.env.SURVEY_TEST_CONTROL}, data: {mode}});
      assert.equal(response.status(), 200);
      return response.json();
    };
    const before = await control('normal');
    await page.goto(origin + '/login?next=%2Fperfil%3Fsection%3Dgeneral');
    await page.getByPlaceholder('Correo electrónico', {exact: true}).fill(accounts['acceptance-a'].email);
    await page.getByPlaceholder('Contraseña', {exact: true}).fill(password);
    await page.getByRole('button', {name: 'Iniciar Sesión', exact: true}).click();
    await page.waitForURL(/\/perfil/);
    await page.getByRole('textbox', {name: 'Nombre legal o institucional'}).waitFor();
    await page.goto(origin + '/admin/encuestas?tenant_slug=acceptance-a');
    await expect(page.getByRole('heading', {name: 'Centro de participación ciudadana'})).toBeVisible();
    if (width === 390) await page.evaluate(() => document.documentElement.classList.add('dark'));
    const item = cases[scenario];
    const search = page.getByRole('searchbox', {name: 'Buscar instrumentos'});
    await search.fill(item.title);
    const closing = scenario.startsWith('close');
    const triggerName = closing ? 'Cerrar participación' : 'Borrar borrador';
    const trigger = page.getByRole('button', {name: triggerName, exact: true});
    await expect(trigger).toBeEnabled();
    await trigger.click();
    const dialog = page.getByRole('alertdialog');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText(item.title);
    const bounds = await dialog.boundingBox();
    assert.ok(bounds && bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= width + 1 && bounds.y + bounds.height <= 1001);
    await page.screenshot({path: `${evidence}/confirmation-${width}.png`, fullPage: true});
    if (scenario.endsWith('recovery')) await control('fail-next-readback');
    const confirmed = page.waitForResponse(response => {
      const path = new URL(response.url()).pathname;
      return closing ? response.request().method() === 'POST' && path === `/api/v2/surveys/${item.id}/close`
        : response.request().method() === 'DELETE' && path.endsWith('/' + item.id);
    });
    const confirm = dialog.getByRole('button', {name: closing ? 'Cerrar definitivamente' : 'Eliminar', exact: true});
    await confirm.focus();
    await page.keyboard.press('Enter');
    assert.equal((await confirmed).status(), 200);
    if (scenario.endsWith('recovery')) {
      await expect(page.getByText('Listado pendiente de actualización', {exact: true})).toBeVisible();
      await expect(page.getByRole('button', {name: 'Nueva encuesta'})).toBeDisabled();
      await page.screenshot({path: `${evidence}/recovery-${width}.png`, fullPage: true});
      const committed = await control('normal');
      assert.equal(committed.mutations.length - before.mutations.length, 1);
      await page.getByRole('button', {name: 'Actualizar listado', exact: true}).click();
      await expect(page.getByText('Listado pendiente de actualización', {exact: true})).toHaveCount(0);
    }
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole('button', {name: triggerName, exact: true})).toHaveCount(0);
    const listing = await context.request.get(origin + '/api/admin/encuestas?tenant_slug=acceptance-a');
    assert.equal(listing.status(), 200);
    const list = await listing.json();
    const row = list.encuestas.find(record => record.id === item.id);
    if (closing) {
      assert.ok(row && row.estado === 'cerrada');
      assert.equal(row.admin_lifecycle.capabilities.can_close, false);
    } else assert.equal(row, undefined);
    assert.equal((await context.request.get(origin + '/api/me?tenant_slug=acceptance-a')).status(), 200);
    const after = await control('normal');
    assert.equal(after.mutations.length - before.mutations.length, 1);
    assert.equal(mutationRequests.length, 1);
    assert.deepEqual(errors, []);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({path: `${evidence}/completed-${width}.png`, fullPage: true});
    await page.reload();
    await page.getByRole('searchbox', {name: 'Buscar instrumentos'}).fill(item.title);
    await expect(page.getByRole('button', {name: triggerName, exact: true})).toHaveCount(0);
    results.push({width, scenario, mutationCount: 1, originalLogin: true, serverReadBack: true,
      sessionPreserved: true, readRecoveryDidNotRepeatWrite: true});
    await context.close();
  }
  const report = {fullSpaRouter: true, fullFlaskApp: true, apiResponsesMocked: false,
    syntheticAccountsAndSqlite: true, readBackFailureInjectedLocally: true, results};
  await writeFile(`${evidence}/results.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} catch (error) {
  for (const context of browser?.contexts() || []) for (const page of context.pages()) {
    await page.screenshot({path: `${evidence}/failure.png`, fullPage: true}).catch(() => {});
  }
  throw error;
} finally {
  await browser?.close();
  await server.close();
}
