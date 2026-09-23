import { chromium, expect } from '@playwright/test';
import { createServer } from 'vite';
import { mkdir, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';

const backend = new URL(process.env.SEGMENT_API);
assert.equal(backend.hostname, '127.0.0.1');
const cases = JSON.parse(process.env.SEGMENT_CASES);
process.env.VITE_PROXY_TARGET = backend.origin;
process.env.VITE_BACKEND_URL = '/api';
process.env.VITE_API_URL = '/api';
process.env.VITE_USE_LOCAL_API_PROXY = 'true';
process.env.VITE_BACKEND_BOOTSTRAP_GATE_ENABLED = 'true';
process.env.VITE_ENABLE_SURVEY_ANALYTICS_FALLBACK = 'false';
const server = await createServer({ cacheDir: '.vercel/segment-compare-cache', server: { host: '127.0.0.1', port: 0 }, logLevel: 'error' });
const directory = 'test-evidence/segment-compare-full';
let browser;
const results = [];
try {
  await server.listen();
  await mkdir(directory, { recursive: true });
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch(process.platform === 'win32' ? { channel: 'chrome', headless: true } : { headless: true });
  for (const width of [1440, 820, 390, 320]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: 'reduce', colorScheme: width === 390 ? 'dark' : 'light' });
    await context.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort('blockedbyclient'));
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    const errors = [], writes = [], compared = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      const url = new URL(request.url());
      if (/\/encuestas\//.test(url.pathname) && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method())) writes.push(request.method());
      if (url.pathname.endsWith('/segments/compare')) compared.push(url.searchParams);
    });
    await page.goto(origin + '/login?next=%2Fperfil%3Fsection%3Dgeneral');
    await page.getByPlaceholder('Correo electrónico', { exact: true }).fill(process.env.SEGMENT_ACCOUNT);
    await page.getByPlaceholder('Contraseña', { exact: true }).fill(process.env.SEGMENT_PASSWORD);
    await page.getByRole('button', { name: 'Iniciar Sesión', exact: true }).click();
    await page.waitForURL(/\/perfil/);
    await page.getByRole('textbox', { name: 'Nombre legal o institucional' }).waitFor();
    await page.goto(`${origin}/admin/encuestas/${cases.populated.id}/analytics?tenant_slug=acceptance-a`);
    const panel = page.getByTestId('survey-segment-compare');
    await panel.waitFor({ timeout: 45000 });
    if (width === 390) await page.evaluate(() => document.documentElement.classList.add('dark'));
    // Use server-authored labels and the real suggestions endpoint, never a mocked API.
    const api = `${origin}/api/admin/encuestas/${cases.populated.id}/analytics`;
    const suggestions = await (await context.request.get(api + '/segments/suggestions?tenant_slug=acceptance-a')).json();
    const options = Object.values(suggestions.dimensions).flat();
    const web = options.find(item => item.filters?.canal === 'web');
    const whatsapp = options.find(item => item.filters?.canal === 'whatsapp');
    assert.ok(web && whatsapp, 'Both observed channels must be selectable');
    async function select(id, label) {
      await page.locator(id).click();
      await page.getByRole('option', { name: label, exact: true }).click();
    }
    await select('#segment-a-selector', web.label);
    await select('#segment-b-selector', whatsapp.label);
    const compareUrl = api + '/segments/compare?tenant_slug=acceptance-a&a_canal=web&b_canal=whatsapp';
    const response = await context.request.get(compareUrl);
    assert.equal(response.status(), 200);
    const data = await response.json();
    assert.equal(data.basis.selected_records, 8);
    const question = data.questions.find(row => row.id === cases.populated.question_id);
    const yes = question.options.find(row => row.id === cases.populated.yes_id);
    assert.equal(yes.segment_a_percent, 75);
    assert.equal(yes.segment_b_percent, 25);
    assert.equal(yes.delta_percentage_points, 50);
    const yesRow = panel.locator(`[data-question-id="${question.id}"] [data-option-id="${yes.id}"]`);
    await expect(yesRow.getByText('75 %', { exact: true })).toBeVisible();
    await expect(yesRow.getByText('25 %', { exact: true })).toBeVisible();
    await expect(yesRow.getByText('+50 p.p.', { exact: true })).toBeVisible();
    await expect(panel).toHaveAttribute('data-selected-records', '8');
    // The response table has its own refresh button; exercise the analytics action.
    const actions = page.locator('div.sticky').filter({ has: page.getByText('Centro de acciones de analytics', { exact: true }) });
    const requestsBeforeRefresh = compared.length;
    await actions.getByRole('button', { name: 'Actualizar', exact: true }).click();
    await expect.poll(() => compared.length).toBeGreaterThan(requestsBeforeRefresh);
    await expect(panel).toHaveAttribute('data-selected-records', '8');
    await panel.evaluate(el => window.scrollTo({ top: el.getBoundingClientRect().top + scrollY - 110, behavior: 'instant' }));
    await page.screenshot({ path: `${directory}/viewport-${width}.png`, fullPage: false });
    await panel.screenshot({ path: `${directory}/comparison-${width}.png` });
    const disclosure = panel.locator('summary');
    await disclosure.focus();
    await page.keyboard.press('Enter');
    await expect(panel.getByText(data.limitations[0].detail, { exact: true })).toBeVisible();
    assert.equal(await panel.evaluate(el => el.scrollWidth > el.clientWidth), false);
    await select('#analytics-filter-barrio', 'Centro QA');
    await expect(panel).toHaveAttribute('data-selected-records', '4');
    await expect(yesRow.getByText('50 %', { exact: true })).toHaveCount(2);
    await expect(yesRow.getByText('0 p.p.', { exact: true })).toBeVisible();
    assert.ok(compared.some(params => params.get('barrio') === 'Centro QA' && params.get('a_canal') === 'web' && params.get('b_canal') === 'whatsapp'));
    const filtered = await (await context.request.get(compareUrl + '&barrio=Centro%20QA')).json();
    const filteredYes = filtered.questions.find(row => row.id === question.id).options.find(row => row.id === yes.id);
    assert.equal(filteredYes.segment_a_percent, 50);
    assert.equal(filteredYes.segment_b_percent, 50);
    assert.equal(filteredYes.delta_percentage_points, 0);
    await page.getByRole('button', { name: 'Limpiar filtros', exact: true }).click();
    await expect(panel).toHaveAttribute('data-selected-records', '8');
    await select('#segment-b-selector', web.label);
    await expect(yesRow.getByText('0 p.p.', { exact: true })).toBeVisible();
    const overlap = await (await context.request.get(compareUrl.replace('b_canal=whatsapp', 'b_canal=web'))).json();
    assert.equal(overlap.basis.overlap_records, 4);
    await expect(panel).toContainText(overlap.ui.overlap);
    await page.reload();
    await expect(panel).toHaveAttribute('data-selected-records', '8', { timeout: 45000 });
    await page.goto(`${origin}/admin/encuestas/${cases.empty.id}/analytics?tenant_slug=acceptance-a`);
    await page.getByTestId('survey-fieldwork-coverage').waitFor({ timeout: 45000 });
    await expect(panel).toHaveCount(0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, []);
    results.push({ width, dark: width === 390, originalLogin: true, apiMocked: false, comparison: '75% vs 25%, A-B=50pp', filtered: '50% vs 50%, A-B=0pp', overlap: 4, emptyHasNoStaleComparison: true, refreshRequestsNewComparison: true, reload: true, keyboard: true, horizontalOverflow: false, writes: 0 });
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
