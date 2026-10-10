import { chromium, expect } from '@playwright/test';
import { createServer } from 'vite';
import { mkdir, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';

const backend = new URL(process.env.ORGANIZATION_API);
assert.equal(backend.hostname, '127.0.0.1');
const accounts = JSON.parse(process.env.ORGANIZATION_ACCOUNTS);
process.env.VITE_PROXY_TARGET = backend.origin;
process.env.VITE_BACKEND_URL = '/api'; process.env.VITE_API_URL = '/api';
process.env.VITE_USE_LOCAL_API_PROXY = 'true'; process.env.VITE_BACKEND_BOOTSTRAP_GATE_ENABLED = 'true';
const server = await createServer({ cacheDir: '.vercel/organization-access-cache', server: { host: '127.0.0.1', port: 0 }, logLevel: 'error' });
const directory = 'test-evidence/organization-access';
let browser;
const results = [];
try {
  await server.listen(); await mkdir(directory, { recursive: true });
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch(process.platform === 'win32' ? { channel: 'chrome', headless: true } : { headless: true });
  for (const width of [1440, 820, 390, 320]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: 'reduce', colorScheme: width === 390 ? 'dark' : 'light' });
    await context.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort('blockedbyclient'));
    const page = await context.newPage(); page.setDefaultTimeout(30000);
    const errors = [], loginRequests = [], guideRequests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (request.url().includes('/auth/admin/login') && request.method() === 'POST') {
        const body = request.postDataJSON(); loginRequests.push({ tenant: body.tenant_slug, url: request.url(), headers: request.headers() });
      }
      if (request.url().includes('/conversation-guide')) guideRequests.push(request.url());
    });
    // Simulate an earlier public visit. It must not decide the organization of a global sign-in.
    await page.goto(origin + '/login');
    await page.evaluate(() => { localStorage.setItem('tenantSlug', 'acceptance-b'); sessionStorage.setItem('tenantSlug', 'acceptance-b'); });
    await page.reload();
    await page.getByPlaceholder('Correo electrónico', { exact: true }).fill(accounts['acceptance-a'].email);
    await page.getByPlaceholder('Contraseña', { exact: true }).fill(process.env.ORGANIZATION_PASSWORD);
    await page.getByRole('button', { name: 'Iniciar Sesión', exact: true }).click();
    await page.waitForURL(/\/perfil/);
    await page.getByTestId('admin-organization-identity').waitFor();
    assert.equal(loginRequests[0].tenant, undefined);
    assert.equal(new URL(loginRequests[0].url).searchParams.has('tenant'), false);
    assert.equal(loginRequests[0].headers['x-tenant-slug'], undefined);
    const me = await (await context.request.get(origin + '/api/me')).json();
    assert.equal(me.tenant_slug, 'acceptance-a');
    assert.equal(me.organization_profile.values.nombre_empresa, 'Gobierno de evaluación local');
    await expect(page.getByTestId('admin-organization-identity')).toContainText('Gobierno de evaluación local');
    await page.reload();
    await expect(page.getByTestId('admin-organization-identity')).toContainText('Gobierno de evaluación local');
    await page.goto(origin + '/implementacion?tenant_slug=acceptance-a');
    const setup = page.getByTestId('organization-setup-workspace');
    await setup.waitFor({ timeout: 45000 });
    await setup.getByRole('button', { name: /^3 / }).click();
    const guide = page.getByTestId('private-conversation-guide');
    await guide.waitFor();
    assert.equal(guideRequests.length, 0);
    await guide.getByRole('button', { name: 'Explorar la guía' }).click();
    await expect(guide.getByRole('heading', { name: '¿Para quién es la consulta?' })).toBeVisible();
    await guide.getByRole('button', { name: 'Para mí', exact: true }).click();
    await expect(guide.getByRole('heading', { name: '¿Sobre qué querés consultar?' })).toBeVisible();
    await guide.getByRole('button', { name: 'Documentación: CUD y CMO' }).click();
    await expect(guide.locator('[data-node="documentation"]')).toBeVisible();
    await guide.getByRole('button', { name: 'Volver al menú' }).click();
    await guide.locator('[data-node="main"]').waitFor();
    if (width === 390) await page.evaluate(() => document.documentElement.classList.add('dark'));
    await guide.evaluate(el => window.scrollTo({ top: el.getBoundingClientRect().top + scrollY - 100, behavior: 'instant' }));
    await page.screenshot({ path: `${directory}/viewport-${width}.png`, animations: 'disabled' });
    await guide.screenshot({ path: `${directory}/guide-${width}.png`, animations: 'disabled' });
    await guide.locator('summary').focus(); await page.keyboard.press('Enter');
    await expect(guide.locator('pre')).toBeVisible();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const own = await context.request.get(origin + '/api/admin/tenants/acceptance-a/conversation-guide');
    assert.equal(own.status(), 200);
    const tamper = await context.request.get(origin + '/api/admin/tenants/acceptance-b/conversation-guide');
    assert.equal(tamper.status(), 403);
    assert.equal((await context.request.get(origin + '/api/admin/tenants/acceptance-a/conversation-guide?tenant_slug=acceptance-b')).status(), 400);
    const activation = await (await context.request.get(origin + '/api/v2/tenants/acceptance-a/activation/channels')).json();
    assert.equal(activation.channels.find(c => c.id === 'whatsapp').ready, false);
    assert.deepEqual(errors, []);
    results.push({ width, globalLoginIgnoresOldPublicTenant: true, sameTenantOnReload: true, privateGuide: true,
      originalSharedRenderer: true, apiMocked: false, wrongTenantDenied: true, whatsappPending: true, keyboard: true, horizontalOverflow: false });
    await context.close();
  }
  // Independent canonical route login on a clean browser verifies the previously broken /t/:slug path.
  const canonical = await browser.newContext();
  await canonical.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort('blockedbyclient'));
  const page = await canonical.newPage();
  await page.goto(origin + '/t/acceptance-a/login');
  await page.getByPlaceholder('Correo electrónico', { exact: true }).fill(accounts['acceptance-a'].email);
  await page.getByPlaceholder('Contraseña', { exact: true }).fill(process.env.ORGANIZATION_PASSWORD);
  await page.getByRole('button', { name: 'Iniciar Sesión', exact: true }).click();
  await page.waitForURL(/\/perfil/);
  await expect(page.getByTestId('admin-organization-identity')).toHaveAttribute('data-tenant-slug', 'acceptance-a');
  await page.reload();
  await expect(page.getByTestId('admin-organization-identity')).toHaveAttribute('data-tenant-slug', 'acceptance-a');
  await page.getByRole('button', { name: /Mi cuenta/ }).click();
  await page.getByRole('menuitem', { name: 'Cerrar sesión' }).click();
  await page.goto(origin + '/login');
  await page.getByPlaceholder('Correo electrónico', { exact: true }).fill(accounts['acceptance-b'].email);
  await page.getByPlaceholder('Contraseña', { exact: true }).fill(process.env.ORGANIZATION_PASSWORD);
  await page.getByRole('button', { name: 'Iniciar Sesión', exact: true }).click();
  await page.waitForURL(/\/perfil/);
  await expect(page.getByTestId('admin-organization-identity')).toHaveAttribute('data-tenant-slug', 'acceptance-b');
  await expect(page.getByTestId('admin-organization-identity')).toContainText('Otra organización local');
  await page.goto(origin + '/implementacion?tenant_slug=acceptance-b');
  await page.getByTestId('organization-setup-workspace').waitFor();
  await page.getByTestId('organization-setup-workspace').getByRole('button', { name: /^3 / }).click();
  await expect(page.getByTestId('private-conversation-guide')).toHaveCount(0);
  await canonical.close();
  const report = { fullSpa: true, fullFlask: true, disposableData: true, canonicalLogin: true, logoutReloginDifferentOrganization: true, unconfiguredTenantHasNoGuide: true, results };
  await writeFile(`${directory}/results.json`, JSON.stringify(report, null, 2)); console.log(JSON.stringify(report));
} catch (error) {
  for (const context of browser?.contexts() || []) for (const page of context.pages()) await page.screenshot({ path: `${directory}/failure.png`, fullPage: true }).catch(() => {});
  throw error;
} finally { await browser?.close(); await server.close(); }
