import { chromium, expect } from '@playwright/test';
import { createServer } from 'vite';
import { mkdir, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';

const backend = new URL(process.env.FIELDWORK_API);
assert.equal(backend.hostname, '127.0.0.1');
const cases = JSON.parse(process.env.FIELDWORK_CASES);
process.env.VITE_PROXY_TARGET = backend.origin;
process.env.VITE_BACKEND_URL = '/api';
process.env.VITE_API_URL = '/api';
process.env.VITE_USE_LOCAL_API_PROXY = 'true';
process.env.VITE_BACKEND_BOOTSTRAP_GATE_ENABLED = 'true';
// Production always disables the optional development-only synthetic fallback.
process.env.VITE_ENABLE_SURVEY_ANALYTICS_FALLBACK = 'false';
const server = await createServer({ cacheDir: '.vercel/fieldwork-full-cache', server: { host: '127.0.0.1', port: 0 }, logLevel: 'error' });
const directory = 'test-evidence/fieldwork-full';
let browser;
const results = [];
try {
  await server.listen();
  await mkdir(directory, { recursive: true });
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch(process.platform === 'win32' ? { channel: 'chrome', headless: true } : { headless: true });
  for (const width of [1440, 820, 390, 320]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
    await context.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort('blockedbyclient'));
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    const errors = [], writes = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (/\/encuestas\//.test(new URL(request.url()).pathname) && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method())) writes.push(request.method());
    });
    await page.goto(origin + '/login?next=%2Fperfil%3Fsection%3Dgeneral');
    await page.getByPlaceholder('Correo electrónico', { exact: true }).fill(process.env.FIELDWORK_ACCOUNT);
    await page.getByPlaceholder('Contraseña', { exact: true }).fill(process.env.FIELDWORK_PASSWORD);
    await page.getByRole('button', { name: 'Iniciar Sesión', exact: true }).click();
    await page.waitForURL(/\/perfil/);
    await page.getByRole('textbox', { name: 'Nombre legal o institucional' }).waitFor();
    const url = `${origin}/admin/encuestas/${cases.populated.id}/analytics?tenant_slug=acceptance-a`;
    await page.goto(url);
    const panel = page.getByTestId('survey-fieldwork-coverage');
    await panel.waitFor({ timeout: 45000 });
    if (width === 390) await page.evaluate(() => document.documentElement.classList.add('dark'));
    const endpoint = `${origin}/api/admin/encuestas/${cases.populated.id}/analytics/resumen?tenant_slug=acceptance-a`;
    const response = await context.request.get(endpoint);
    assert.equal(response.status(), 200);
    const payload = (await response.json()).fieldwork_coverage;
    assert.equal(payload.basis.selected_records, 8);
    const campaign = payload.dimensions.find(row => row.id === 'campaign');
    assert.equal(campaign.recorded_count, 2);
    assert.equal(campaign.coverage_percent, 25);
    await expect(panel.getByRole('heading', { name: payload.ui.heading, exact: true })).toBeVisible();
    await expect(panel.locator('[data-dimension="campaign"] h4')).toHaveText(campaign.label);
    await panel.evaluate(el => window.scrollTo({ top: el.getBoundingClientRect().top + scrollY - 110, behavior: 'instant' }));
    await page.screenshot({ path: `${directory}/viewport-${width}.png`, fullPage: false });
    await panel.screenshot({ path: `${directory}/coverage-${width}.png` });
    const disclosure = panel.locator('summary');
    await disclosure.focus();
    await page.keyboard.press('Enter');
    await expect(panel.getByText(payload.limitations[0].detail, { exact: true })).toBeVisible();
    assert.equal(await panel.evaluate(el => el.scrollWidth > el.clientWidth), false);
    await page.locator('#analytics-filter-barrio').click();
    await page.getByRole('option', { name: 'Centro QA', exact: true }).click();
    await expect.poll(async () => {
      const summary = await page.locator('[data-testid="survey-fieldwork-coverage"]').getAttribute('data-selected-records');
      return summary;
    }).toBe('4');
    const filteredResponse = await context.request.get(endpoint + '&barrio=Centro%20QA');
    const filtered = (await filteredResponse.json()).fieldwork_coverage;
    assert.equal(filtered.scope.filtered, true);
    assert.equal(filtered.dimensions.find(row => row.id === 'campaign').coverage_percent, 50);
    await page.getByRole('button', { name: 'Limpiar filtros', exact: true }).click();
    await expect(panel).toHaveAttribute('data-selected-records', '8');
    await page.reload();
    await expect(panel).toHaveAttribute('data-selected-records', '8', { timeout: 45000 });
    await page.goto(`${origin}/admin/encuestas/${cases.empty.id}/analytics?tenant_slug=acceptance-a`);
    await expect(panel).toHaveAttribute('data-selected-records', '0', { timeout: 45000 });
    await expect(panel.getByText(payload.ui.empty, { exact: true })).toBeVisible();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal((await context.request.get(origin + '/api/me?tenant_slug=acceptance-a')).status(), 200);
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, []);
    results.push({ width, dark: width === 390, originalLogin: true, apiMocked: false, coverage: '2/8=25%', filtered: '2/4=50%', emptyHasNoRate: true, reload: true, keyboard: true, horizontalOverflow: false, writes: 0 });
    await context.close();
  }
  const report = { fullSpa: true, fullFlask: true, disposableData: true, results };
  await writeFile(`${directory}/results.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} catch (error) {
  for (const context of browser?.contexts() || []) for (const page of context.pages()) await page.screenshot({ path: `${directory}/failure.png`, fullPage: true }).catch(() => {});
  throw error;
} finally {
  await browser?.close();
  await server.close();
}
