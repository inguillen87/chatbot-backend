const state = {
  tenantId: '',
  scope: 'municipio',
  filters: {
    from: null,
    to: null,
    canal: [],
    categoria: [],
    estado: [],
    agente: [],
    zona: [],
    etiqueta: [],
    bbox: null,
  },
  charts: new Map(),
  map: null,
  heatLayer: null,
  clusterLayer: null,
  drawControl: null,
  selectionLayer: null,
  mapContainerId: null,
};

const API_BASE = '/analytics';

const JUNIN_CENTER = { lat: -33.008818, lon: -68.485079 };
const JUNIN_RADIUS_KM = 3.8;
const DEMO_CATEGORIES = ['Iluminación', 'Residuos', 'Bacheo', 'Seguridad', 'Arbolado', 'Pluvial'];
const DEMO_STATUSES = ['nuevo', 'en_progreso', 'derivado', 'resuelto'];

function init() {
  const main = document.querySelector('.app-main');
  state.scope = main?.dataset.scope || 'municipio';
  state.tenantId = main?.dataset.tenant || new URLSearchParams(window.location.search).get('tenant_id') || '';
  if (!state.tenantId) {
    console.warn('No tenant_id provided. Use ?tenant_id=<id>');
  }
  bindScopeButtons();
  bindFilterActions();
  bindExports();
  bindThemeToggle();
  bindShareView();
  hydrateFiltersFromUrl();
  loadDashboard();
}

function bindScopeButtons() {
  document.querySelectorAll('.scope-button').forEach((button) => {
    button.addEventListener('click', () => {
      const scope = button.dataset.scope;
      if (scope === state.scope) return;
      state.scope = scope;
      document.querySelectorAll('.scope-button').forEach((b) => b.setAttribute('aria-selected', b === button ? 'true' : 'false'));
      document.querySelectorAll('.dashboard').forEach((section) => {
        section.classList.toggle('hidden', section.dataset.scope !== scope);
      });
      loadDashboard();
    });
  });
}

function bindFilterActions() {
  document.getElementById('apply-filters')?.addEventListener('click', () => {
    updateFiltersFromInputs();
    loadDashboard();
  });
  document.getElementById('reset-filters')?.addEventListener('click', () => {
    resetFilters();
    loadDashboard();
  });
}

function bindExports() {
  document.addEventListener('click', (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const exportKey = target.dataset.export;
    if (!exportKey) return;
    const format = target.dataset.format || 'csv';
    if (format === 'csv') {
      exportCsv(exportKey);
    } else if (format === 'png') {
      exportPng(exportKey);
    }
  });
}

function bindThemeToggle() {
  const toggle = document.getElementById('toggle-theme');
  if (!toggle) return;
  toggle.addEventListener('click', () => {
    const html = document.documentElement;
    const current = html.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    toggle.textContent = next === 'dark' ? 'Modo claro' : 'Modo oscuro';
  });
}

function bindShareView() {
  const shareBtn = document.getElementById('share-view');
  if (!shareBtn) return;
  shareBtn.addEventListener('click', async () => {
    updateFiltersFromInputs();
    const params = buildParams();
    params.set('scope', state.scope);
    params.set('tenant_id', state.tenantId);
    const url = `${window.location.origin}${window.location.pathname}?${params.toString()}`;
    try {
      await navigator.clipboard.writeText(url);
      shareBtn.textContent = 'Copiado';
      setTimeout(() => (shareBtn.textContent = 'Compartir vista'), 2000);
    } catch (err) {
      console.warn('Clipboard error', err);
      window.prompt('Copiá la URL', url);
    }
  });
}

function hydrateFiltersFromUrl() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('from')) state.filters.from = params.get('from');
  if (params.get('to')) state.filters.to = params.get('to');
  for (const key of ['canal', 'categoria', 'estado', 'agente', 'zona', 'etiqueta']) {
    if (params.get(key)) {
      state.filters[key] = params.get(key).split(',').filter(Boolean);
    }
  }
  if (params.get('bbox')) state.filters.bbox = params.get('bbox');
  populateInputs();
}

function populateInputs() {
  const { from, to, canal, categoria, estado, agente, zona, etiqueta, bbox } = state.filters;
  if (from) document.getElementById('date-from').value = from;
  if (to) document.getElementById('date-to').value = to;
  if (bbox) document.getElementById('filter-bbox').value = bbox;
  setSelectValues('filter-canal', canal);
  setSelectValues('filter-categoria', categoria);
  setSelectValues('filter-estado', estado);
  setSelectValues('filter-agente', agente);
  setSelectValues('filter-zona', zona);
  setSelectValues('filter-etiqueta', etiqueta);
}

function setSelectValues(id, values) {
  const select = document.getElementById(id);
  if (!select) return;
  Array.from(select.options).forEach((option) => {
    option.selected = values.includes(option.value);
  });
}

function updateFiltersFromInputs() {
  const dateFrom = document.getElementById('date-from');
  const dateTo = document.getElementById('date-to');
  state.filters.from = dateFrom?.value || null;
  state.filters.to = dateTo?.value || null;
  state.filters.canal = getMultiValues('filter-canal');
  state.filters.categoria = getMultiValues('filter-categoria');
  state.filters.estado = getMultiValues('filter-estado');
  state.filters.agente = getMultiValues('filter-agente');
  state.filters.zona = getMultiValues('filter-zona');
  state.filters.etiqueta = getMultiValues('filter-etiqueta');
  const bbox = document.getElementById('filter-bbox');
  state.filters.bbox = bbox?.value || null;
}

function getMultiValues(id) {
  const select = document.getElementById(id);
  if (!select) return [];
  return Array.from(select.selectedOptions).map((opt) => opt.value);
}

function resetFilters() {
  state.filters = {
    from: null,
    to: null,
    canal: [],
    categoria: [],
    estado: [],
    agente: [],
    zona: [],
    etiqueta: [],
    bbox: null,
  };
  populateInputs();
  if (state.selectionLayer) {
    state.selectionLayer.clearLayers();
  }
}

function buildParams(extra = {}) {
  const params = new URLSearchParams();
  if (state.tenantId) params.set('tenant_id', state.tenantId);
  if (state.filters.from) params.set('from', state.filters.from);
  if (state.filters.to) params.set('to', state.filters.to);
  for (const [key, values] of Object.entries(state.filters)) {
    if (['from', 'to', 'bbox'].includes(key)) continue;
    if (Array.isArray(values) && values.length) params.set(key, values.join(','));
  }
  if (state.filters.bbox) params.set('bbox', state.filters.bbox);
  params.set('scope', state.scope);
  for (const [key, value] of Object.entries(extra)) {
    if (value !== undefined && value !== null && value !== '') {
      params.set(key, value);
    }
  }
  return params;
}

async function fetchJson(path, params = new URLSearchParams()) {
  const url = `${API_BASE}/${path}?${params.toString()}`;
  const response = await fetch(url, { credentials: 'include', cache: 'no-store' });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`HTTP ${response.status}: ${text}`);
  }
  return response.json();
}

async function loadDashboard() {
  if (!state.tenantId) return;
  const params = buildParams();
  let summary;
  try {
    summary = await fetchJson('summary', params);
  } catch (error) {
    console.warn('Analytics summary unavailable, using demo payload.', error);
    summary = createDemoSummary(state.scope);
  }
  renderSummary(summary);
  if (state.scope === 'municipio') {
    renderDemografia(summary.demografia || null);
  } else {
    renderDemografia(null);
  }

  const tasks = [];
  if (state.scope === 'municipio') {
    tasks.push(
      loadTimeseries('municipio-timeseries'),
      loadBreakdowns(),
      loadTopMunicipio(),
      loadGeo(),
    );
  } else if (state.scope === 'pyme') {
    tasks.push(
      loadTimeseries('pyme-timeseries'),
      loadPymeBreakdowns(),
      loadGeo(),
      loadPymeTables(),
    );
  } else if (state.scope === 'operaciones') {
    tasks.push(loadOperations(), loadTimeseries('municipio-timeseries'));
  }
  if (tasks.length) {
    await Promise.allSettled(tasks);
  }
}

function dailySeed(offset = 0) {
  const base = Number.parseInt(new Date().toISOString().slice(0, 10).replace(/-/g, ''), 10);
  return base + offset;
}

function createSeededRandom(seed) {
  let value = seed % 2147483647;
  if (value <= 0) {
    value += 2147483646;
  }
  return () => {
    value = (value * 16807) % 2147483647;
    return (value - 1) / 2147483646;
  };
}

function createDemoSummary(scope) {
  const offset = scope === 'pyme' ? 97 : scope === 'operaciones' ? 137 : 11;
  const random = createSeededRandom(dailySeed(offset));
  if (scope === 'municipio') {
    const tickets = Math.round(280 + random() * 220);
    const abiertos = Math.round(tickets * (0.22 + random() * 0.2));
    const backlog = Math.round(random() * 80);
    const automatizado = Math.round(25 + random() * 35);
    const primerContacto = Math.round(45 + random() * 40);
    const nps = Math.round(-5 + random() * 55);
    const csat = Math.round((3.6 + random() * 1.2) * 100) / 100;
    const ttaP50 = Math.round(8 + random() * 15);
    const ttaP90 = ttaP50 + Math.round(10 + random() * 18);
    const ttrP50 = Math.round(120 + random() * 160);
    const ttrP90 = ttrP50 + Math.round(150 + random() * 220);
    const respuestas = Math.max(Math.round(180 + random() * 160), 1);
    const encuestasDelta = Number((random() * 40 - 20).toFixed(2));
    const ticketsDelta = Number((random() * 28 - 14).toFixed(2));
    const ticketsPrev = Math.max(Math.round(tickets / (1 + ticketsDelta / 100)), 1);
    const encuestasPrev = Math.max(Math.round(respuestas / (1 + encuestasDelta / 100)), 1);
    const generoFem = Math.round(respuestas * (0.45 + random() * 0.1));
    const generoMasc = Math.round(respuestas * (0.4 + random() * 0.08));
    const generoNb = Math.max(respuestas - generoFem - generoMasc, 0);
    const ageBuckets = ['18-24', '25-34', '35-44', '45-54', '55-64', '65+'];
    const demoAges = {};
    let remaining = respuestas;
    ageBuckets.forEach((bucket, index) => {
      const weight = 0.3 - index * 0.03 + random() * 0.05;
      const value = Math.max(Math.round(respuestas * Math.max(weight, 0.05)), 0);
      demoAges[bucket] = value;
      remaining -= value;
    });
    if (remaining > 0) {
      demoAges['18-24'] += remaining;
    }
    const barrioLabels = ['Centro', 'Sur', 'Norte', 'Este', 'Oeste'];
    const demoBarrios = barrioLabels.map((label, idx) => ({ label, value: Math.max(Math.round(respuestas * (0.12 + random() * 0.1) * (1 - idx * 0.1)), 1) }));
    return {
      totals: {
        tickets,
        tickets_abiertos: abiertos,
        tickets_cerrados: Math.max(tickets - abiertos, 0),
        backlog,
        automatizado_pct: automatizado,
        primer_contacto_pct: primerContacto,
        nps,
        csat,
        encuestas: respuestas,
        cierre_pct: tickets ? Math.round(((tickets - abiertos) / tickets) * 10000) / 100 : 0,
        tta_promedio_min: Math.round((ttaP50 + ttaP90) / 2),
        ttr_promedio_min: Math.round((ttrP50 + ttrP90) / 2),
        tickets_periodo_anterior: ticketsPrev,
        tickets_variacion_pct: ticketsDelta,
        encuestas_periodo_anterior: encuestasPrev,
        encuestas_variacion_pct: encuestasDelta,
      },
      sla: {
        tta: { p50: ttaP50, p90: ttaP90, p95: ttaP90 + Math.round(random() * 10) },
        ttr: { p50: ttrP50, p90: ttrP90, p95: ttrP90 + Math.round(random() * 30) },
      },
      extras: {},
      demografia: {
        genero: { femenino: generoFem, masculino: generoMasc, no_binario: generoNb },
        rango_etario: demoAges,
        edad: {
          promedio: Math.round((28 + random() * 18) * 10) / 10,
          mediana: 35,
          p90: 58,
          muestra: respuestas,
        },
        territorio: { barrios: demoBarrios },
      },
    };
  }
  if (scope === 'pyme') {
    const tickets = Math.round(120 + random() * 90);
    const pedidos = Math.round(80 + random() * 140);
    const ticketMedio = Math.round((random() * 4500 + 6200) * 100) / 100;
    const conversion = Math.round(5 + random() * 15);
    const retencion = Math.round(30 + random() * 25);
    const automatizado = Math.round(15 + random() * 30);
    const hora = `${String(8 + Math.floor(random() * 10)).padStart(2, '0')}:00`;
    const nps = Math.round(-10 + random() * 60);
    const csat = Math.round((3.8 + random() * 1) * 100) / 100;
    const encuestas = Math.max(Math.round(60 + random() * 45), 1);
    const ticketsDelta = Number((random() * 26 - 13).toFixed(2));
    const pedidosDelta = Number((random() * 30 - 15).toFixed(2));
    const encuestasDelta = Number((random() * 36 - 18).toFixed(2));
    const ttaProm = Math.round(10 + random() * 18);
    const ttrProm = Math.round(90 + random() * 140);
    return {
      totals: {
        tickets,
        tickets_cerrados: Math.max(tickets - Math.round(tickets * 0.25), 0),
        pedidos,
        ticket_medio: ticketMedio,
        conversion_pct: conversion,
        retencion_30: retencion,
        retencion_60: Math.max(retencion - 5, 0),
        retencion_90: Math.max(retencion - 9, 0),
        automatizado_pct: automatizado,
        hora_pico: hora,
        nps,
        csat,
        encuestas,
        tickets_abiertos: Math.round(tickets * 0.25),
        backlog: Math.round(tickets * 0.25),
        cierre_pct: tickets ? Math.round(((tickets - Math.round(tickets * 0.25)) / tickets) * 10000) / 100 : 0,
        tta_promedio_min: ttaProm,
        ttr_promedio_min: ttrProm,
        tickets_periodo_anterior: Math.max(Math.round(tickets / (1 + ticketsDelta / 100)), 1),
        tickets_variacion_pct: ticketsDelta,
        pedidos_periodo_anterior: Math.max(Math.round(pedidos / (1 + pedidosDelta / 100)), 1),
        pedidos_variacion_pct: pedidosDelta,
        encuestas_periodo_anterior: Math.max(Math.round(encuestas / (1 + encuestasDelta / 100)), 1),
        encuestas_variacion_pct: encuestasDelta,
      },
      sla: {},
      extras: {},
      demografia: null,
    };
  }
  const tickets = Math.round(200 + random() * 160);
  const abiertos = Math.round(tickets * (0.3 + random() * 0.2));
  const violaciones = Math.round(5 + random() * 25);
  const primer = Math.round(40 + random() * 45);
  const automatizado = Math.round(20 + random() * 35);
  const ticketsDelta = Number((random() * 22 - 11).toFixed(2));
  const ttaProm = Math.round(9 + random() * 16);
  const ttrProm = Math.round(95 + random() * 155);
  return {
    totals: {
      tickets,
      abiertos,
      tickets_cerrados: Math.max(tickets - abiertos, 0),
      cierre_pct: tickets ? Math.round(((tickets - abiertos) / tickets) * 10000) / 100 : 0,
      violaciones_sla: violaciones,
      primer_contacto_pct: primer,
      automatizado_pct: automatizado,
      tta_promedio_min: ttaProm,
      ttr_promedio_min: ttrProm,
      tickets_periodo_anterior: Math.max(Math.round(tickets / (1 + ticketsDelta / 100)), 1),
      tickets_variacion_pct: ticketsDelta,
    },
    sla: {},
    extras: {},
    demografia: null,
  };
}

function createDemoTimeseries(scope) {
  const offset = scope === 'pyme' ? 59 : scope === 'operaciones' ? 103 : 27;
  const random = createSeededRandom(dailySeed(offset));
  const days = 21;
  const today = new Date();
  const base = scope === 'pyme' ? 120 : scope === 'operaciones' ? 160 : 240;
  const series = [];
  for (let index = days - 1; index >= 0; index -= 1) {
    const date = new Date(today);
    date.setDate(today.getDate() - index);
    const variance = (random() - 0.5) * 0.4;
    const value = Math.max(1, Math.round(base * (0.75 + variance) + random() * 35));
    series.push({
      date: date.toISOString().slice(0, 10),
      group: 'total',
      value,
    });
  }
  return series;
}

function createDemoBreakdown(dimension, scope) {
  const offset =
    dimension === 'canal' ? 71 : dimension === 'estado' ? 83 : scope === 'pyme' ? 67 : 45;
  const random = createSeededRandom(dailySeed(offset));
  let labels;
  if (dimension === 'canal') {
    labels = scope === 'pyme' ? ['WhatsApp', 'Instagram', 'Web', 'Sucursal'] : ['WhatsApp', 'Web', 'Presencial', 'Llamadas'];
  } else if (dimension === 'estado') {
    labels = scope === 'pyme' ? ['nuevo', 'pagado', 'en_proceso', 'entregado'] : DEMO_STATUSES;
  } else if (dimension === 'productos') {
    labels = ['Canasta básica', 'Turnos médicos', 'Turismo', 'Eventos', 'Pagos'];
  } else {
    labels = scope === 'pyme'
      ? ['Ventas online', 'Delivery', 'Turnos', 'Reservas', 'Reclamos']
      : DEMO_CATEGORIES;
  }
  const breakdown = labels.map((label, index) => ({
    label,
    value: Math.max(1, Math.round((index + 1) * 12 + random() * 90)),
  }));
  breakdown.sort((a, b) => b.value - a.value);
  return { breakdown };
}

function createDemoTop(category, scope) {
  const random = createSeededRandom(dailySeed(category === 'productos' ? 89 : 75));
  let labels;
  if (category === 'productos') {
    labels = ['Menú ejecutivo', 'Supermercado', 'Farmacia', 'Limpieza', 'Regalería', 'Electrónica'];
  } else {
    labels = ['Centro', 'Norte', 'Sur', 'Este', 'Oeste', 'San Martín', 'Belgrano', 'La Colonia', 'Godoy Cruz', 'Ciudad'];
  }
  const items = labels.map((label, index) => ({
    label,
    value: Math.max(1, Math.round(30 + random() * (scope === 'pyme' ? 60 : 110) - index * 4)),
  }));
  items.sort((a, b) => b.value - a.value);
  return { items };
}

function createDemoTemplates() {
  const random = createSeededRandom(dailySeed(111));
  const templates = [
    'Seguimiento pedido',
    'Promoción semanal',
    'Encuesta satisfacción',
    'Recordatorio pago',
  ].map((template) => {
    const sent = Math.round(180 + random() * 260);
    const responded = Math.round(sent * (0.18 + random() * 0.32));
    const ctr = sent ? Number(((responded / sent) * 100).toFixed(1)) : 0;
    return {
      template,
      sent,
      responded,
      ctr,
    };
  });
  return { templates };
}

function createDemoCohorts() {
  const random = createSeededRandom(dailySeed(129));
  const cohorts = [];
  const baseDate = new Date();
  for (let index = 0; index < 4; index += 1) {
    const date = new Date(baseDate);
    date.setMonth(baseDate.getMonth() - index);
    const label = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
    const size = Math.round(80 + random() * 140);
    const retention30 = Math.round(30 + random() * 40);
    const retention60 = Math.max(retention30 - Math.round(10 + random() * 15), 5);
    const retention90 = Math.max(retention60 - Math.round(5 + random() * 12), 3);
    cohorts.push({
      cohort: label,
      size,
      retention30,
      retention60,
      retention90,
    });
  }
  return { cohorts };
}

function createDemoOperations() {
  const random = createSeededRandom(dailySeed(151));
  const aging = {
    '0-1 días': Math.round(45 + random() * 25),
    '2-3 días': Math.round(30 + random() * 22),
    '4-7 días': Math.round(18 + random() * 15),
    '8+ días': Math.round(8 + random() * 10),
  };
  const agents = ['García', 'Rodríguez', 'Pérez', 'López', 'Fernández'].map((apellido, index) => ({
    agente: `Agente ${apellido}`,
    tickets: Math.round(35 + random() * 28 - index * 3),
    respuesta_promedio_min: Math.round(18 + random() * 25 + index * 2),
  }));
  const queue = {
    'Guardia Urbana': Math.round(22 + random() * 18),
    'Espacios Verdes': Math.round(18 + random() * 14),
    'Obras Públicas': Math.round(26 + random() * 16),
    'Servicios Generales': Math.round(15 + random() * 12),
  };
  return { extras: { aging, agents, queue } };
}

function renderSummary(summary) {
  if (state.scope === 'municipio') {
    const totals = summary.totals || {};
    const items = [
      { label: 'Tickets', value: totals.tickets },
      { label: 'Δ Tickets', value: totals.tickets_variacion_pct, suffix: '%' },
      { label: 'Cerrados', value: totals.tickets_cerrados },
      { label: '% Cierre', value: totals.cierre_pct, suffix: '%' },
      { label: 'Abiertos', value: totals.tickets_abiertos },
      { label: 'Backlog', value: totals.backlog },
      { label: 'TTA prom', value: totals.tta_promedio_min, suffix: 'min' },
      { label: 'TTR prom', value: totals.ttr_promedio_min, suffix: 'min' },
      { label: 'TTA P90', value: summary.sla?.tta?.p90, suffix: 'min' },
      { label: 'TTR P90', value: summary.sla?.ttr?.p90, suffix: 'min' },
      { label: '% Automatizado', value: totals.automatizado_pct, suffix: '%' },
      { label: '% 1er contacto', value: totals.primer_contacto_pct, suffix: '%' },
      { label: 'Encuestas', value: totals.encuestas },
      { label: 'NPS', value: totals.nps },
      { label: 'CSAT', value: totals.csat },
    ];
    renderKpis('municipio-kpis', items);
    renderSla('municipio-sla', summary.sla);
  } else if (state.scope === 'pyme') {
    const totals = summary.totals || {};
    const items = [
      { label: 'Tickets', value: totals.tickets },
      { label: 'Δ Tickets', value: totals.tickets_variacion_pct, suffix: '%' },
      { label: 'Cerrados', value: totals.tickets_cerrados },
      { label: '% Cierre', value: totals.cierre_pct, suffix: '%' },
      { label: 'Pedidos', value: totals.pedidos },
      { label: 'Δ Pedidos', value: totals.pedidos_variacion_pct, suffix: '%' },
      { label: 'Ticket medio', value: totals.ticket_medio, prefix: '$' },
      { label: '% Conversión', value: totals.conversion_pct, suffix: '%' },
      { label: 'Retención 30', value: totals.retencion_30, suffix: '%' },
      { label: 'Retención 60', value: totals.retencion_60, suffix: '%' },
      { label: 'Retención 90', value: totals.retencion_90, suffix: '%' },
      { label: '% Automatizado', value: totals.automatizado_pct, suffix: '%' },
      { label: 'TTA prom', value: totals.tta_promedio_min, suffix: 'min' },
      { label: 'TTR prom', value: totals.ttr_promedio_min, suffix: 'min' },
      { label: 'Hora pico', value: totals.hora_pico },
      { label: 'Encuestas', value: totals.encuestas },
      { label: 'NPS', value: totals.nps },
      { label: 'CSAT', value: totals.csat },
    ];
    renderKpis('pyme-kpis', items);
  } else {
    const totals = summary.totals || {};
    const items = [
      { label: 'Tickets', value: totals.tickets },
      { label: 'Δ Tickets', value: totals.tickets_variacion_pct, suffix: '%' },
      { label: 'Cerrados', value: totals.tickets_cerrados },
      { label: '% Cierre', value: totals.cierre_pct, suffix: '%' },
      { label: 'Abiertos', value: totals.abiertos },
      { label: 'Violaciones SLA', value: totals.violaciones_sla },
      { label: 'TTA prom', value: totals.tta_promedio_min, suffix: 'min' },
      { label: 'TTR prom', value: totals.ttr_promedio_min, suffix: 'min' },
      { label: '% 1er contacto', value: totals.primer_contacto_pct, suffix: '%' },
      { label: '% Automatizado', value: totals.automatizado_pct, suffix: '%' },
    ];
    renderKpis('operaciones-kpis', items);
  }
}

function renderKpis(containerId, items) {
  const container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = '';
  items.filter(Boolean).forEach((item) => {
    const card = document.createElement('div');
    card.className = 'kpi-card';
    const label = document.createElement('span');
    label.textContent = item.label;
    const value = document.createElement('strong');
    let text = item.value ?? '—';
    if (typeof text === 'number' && !Number.isNaN(text)) {
      text = formatNumber(text);
      if (item.suffix) text += item.suffix;
      if (item.prefix) text = `${item.prefix}${text}`;
    }
    value.textContent = text;
    card.append(label, value);
    container.append(card);
  });
}

function renderSla(containerId, sla) {
  const container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = '';
  ['tta', 'ttr'].forEach((metric) => {
    if (!sla?.[metric]) return;
    const wrapper = document.createElement('div');
    wrapper.innerHTML = `<h4>${metric.toUpperCase()}</h4><p>P50: ${sla[metric].p50 ?? '—'} min · P90: ${sla[metric].p90 ?? '—'} min · P95: ${sla[metric].p95 ?? '—'} min</p>`;
    container.append(wrapper);
  });
}

async function loadTimeseries(canvasId) {
  const params = buildParams();
  let data;
  try {
    data = await fetchJson('timeseries', params);
  } catch (error) {
    console.warn('Timeseries unavailable, generating demo data.', error);
    data = { series: [] };
  }
  let series = Array.isArray(data?.series) ? data.series : [];
  if (!series.length) {
    series = createDemoTimeseries(state.scope);
  }
  if (!series.length) {
    updateEmptyState(canvasId, false, 'Sin datos de evolución');
    return;
  }
  updateEmptyState(canvasId, true);
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const grouped = groupSeries(series);
  const datasets = Object.entries(grouped).map(([group, entries], index) => ({
    label: group,
    data: entries.map((entry) => ({ x: entry.date, y: entry.value })),
    tension: 0.35,
    fill: false,
    borderColor: palette(index),
    backgroundColor: palette(index, 0.3),
  }));
  renderChart(canvas, {
    type: 'line',
    data: {
      datasets,
    },
    options: {
      responsive: true,
      scales: {
        x: {
          type: 'category',
          ticks: { autoSkip: true, maxTicksLimit: 10 },
        },
        y: {
          beginAtZero: true,
        },
      },
      plugins: {
        legend: { display: datasets.length > 1 },
      },
    },
  });
}

async function loadBreakdowns() {
  let categoria;
  try {
    categoria = await fetchJson('breakdown', buildParams({ dimension: 'categoria' }));
  } catch (error) {
    console.warn('Categoría breakdown unavailable, using demo data.', error);
    categoria = { breakdown: [] };
  }
  let categoriasData = Array.isArray(categoria?.breakdown) ? categoria.breakdown : [];
  if (!categoriasData.length) {
    categoriasData = createDemoBreakdown('categoria', state.scope).breakdown;
  }
  renderBarChart('municipio-top-categorias', categoriasData);
  updateFilterOptions('filter-categoria', categoriasData);

  let canales;
  try {
    canales = await fetchJson('breakdown', buildParams({ dimension: 'canal' }));
  } catch (error) {
    console.warn('Canal breakdown unavailable, using demo data.', error);
    canales = { breakdown: [] };
  }
  let canalesData = Array.isArray(canales?.breakdown) ? canales.breakdown : [];
  if (!canalesData.length) {
    canalesData = createDemoBreakdown('canal', state.scope).breakdown;
  }
  renderDonutChart('municipio-top-canales', canalesData);
  updateFilterOptions('filter-canal', canalesData);

  let estados;
  try {
    estados = await fetchJson('breakdown', buildParams({ dimension: 'estado' }));
  } catch (error) {
    console.warn('Estado breakdown unavailable, using demo data.', error);
    estados = { breakdown: [] };
  }
  let estadosData = Array.isArray(estados?.breakdown) ? estados.breakdown : [];
  if (!estadosData.length) {
    estadosData = createDemoBreakdown('estado', state.scope).breakdown;
  }
  updateFilterOptions('filter-estado', estadosData);

  let zonas;
  try {
    zonas = await fetchJson('top', buildParams({ category: 'barrios', limit: 20 }));
  } catch (error) {
    console.warn('Zonas top unavailable, using demo data.', error);
    zonas = { items: [] };
  }
  let zonasData = Array.isArray(zonas?.items) ? zonas.items : [];
  if (!zonasData.length) {
    zonasData = createDemoTop('barrios', state.scope).items;
  }
  updateFilterOptions('filter-zona', zonasData);
}

async function loadTopMunicipio() {
  let zonas;
  try {
    zonas = await fetchJson('top', buildParams({ category: 'barrios', limit: 10 }));
  } catch (error) {
    console.warn('Top zonas unavailable, using demo data.', error);
    zonas = { items: [] };
  }
  let items = Array.isArray(zonas?.items) ? zonas.items : [];
  if (!items.length) {
    items = createDemoTop('barrios', state.scope).items.slice(0, 10);
  }
  renderTable('municipio-top-zonas', items, ['label', 'value']);
}

async function loadPymeBreakdowns() {
  let productos;
  try {
    productos = await fetchJson('top', buildParams({ category: 'productos', limit: 10 }));
  } catch (error) {
    console.warn('Top productos unavailable, using demo data.', error);
    productos = { items: [] };
  }
  let productosItems = Array.isArray(productos?.items) ? productos.items : [];
  if (!productosItems.length) {
    productosItems = createDemoTop('productos', 'pyme').items.slice(0, 10);
  }
  renderBarChart('pyme-top-productos', productosItems);

  let conversion;
  try {
    conversion = await fetchJson('breakdown', buildParams({ dimension: 'canal' }));
  } catch (error) {
    console.warn('Conversión por canal unavailable, using demo data.', error);
    conversion = { breakdown: [] };
  }
  let conversionData = Array.isArray(conversion?.breakdown) ? conversion.breakdown : [];
  if (!conversionData.length) {
    conversionData = createDemoBreakdown('canal', 'pyme').breakdown;
  }
  renderDonutChart('pyme-conversion-canal', conversionData);
  updateFilterOptions('filter-canal', conversionData);
}

async function loadPymeTables() {
  let templates;
  try {
    templates = await fetchJson('whatsapp/templates', buildParams());
  } catch (error) {
    console.warn('Templates analytics unavailable, using demo data.', error);
    templates = { templates: [] };
  }
  let templatesRows = Array.isArray(templates?.templates) ? templates.templates : [];
  if (!templatesRows.length) {
    templatesRows = createDemoTemplates().templates;
  }
  renderTable('pyme-templates', templatesRows, ['template', 'sent', 'responded', 'ctr']);

  let cohorts;
  try {
    cohorts = await fetchJson('cohorts', buildParams());
  } catch (error) {
    console.warn('Cohorts analytics unavailable, using demo data.', error);
    cohorts = { cohorts: [] };
  }
  let cohortRows = Array.isArray(cohorts?.cohorts) ? cohorts.cohorts : [];
  if (!cohortRows.length) {
    cohortRows = createDemoCohorts().cohorts;
  }
  renderTable('pyme-cohorts', cohortRows, ['cohort', 'size', 'retention30', 'retention60', 'retention90']);
}

async function loadOperations() {
  let data;
  try {
    data = await fetchJson('operations', buildParams());
  } catch (error) {
    console.warn('Operaciones analytics unavailable, using demo data.', error);
    data = { extras: {} };
  }
  let extras = data?.extras || {};
  if (!extras.aging && !extras.agents && !extras.queue) {
    extras = createDemoOperations().extras;
  }
  renderBarChart('operaciones-aging', Object.entries(extras.aging || {}).map(([label, value]) => ({ label, value })));
  renderTable('operaciones-agentes', extras.agents, ['agente', 'tickets', 'respuesta_promedio_min']);
  renderBarChart('operaciones-sla', Object.entries(extras.queue || {}).map(([label, value]) => ({ label, value })));
}

async function loadGeo() {
  const params = buildParams();
  try {
    const [heat, points] = await Promise.all([
      fetchJson('geo/heatmap', params),
      fetchJson('geo/points', buildParams({ limit: 1000 })),
    ]);
    const cells = Array.isArray(heat?.cells) ? heat.cells : [];
    const pointList = Array.isArray(points?.points) ? points.points : [];
    if (!cells.length && !pointList.length) {
      const fallback = generateDemoGeoDataset(state.scope);
      updateMap(fallback.cells, fallback.points);
      return;
    }
    updateMap(cells, pointList);
  } catch (error) {
    console.warn('Geo analytics unavailable, rendering demo data.', error);
    const fallback = generateDemoGeoDataset(state.scope);
    updateMap(fallback.cells, fallback.points);
  }
}

function renderBarChart(canvasId, items) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const data = items || [];
  renderChart(canvas, {
    type: 'bar',
    data: {
      labels: data.map((item) => item.label || '—'),
      datasets: [
        {
          label: '#',
          data: data.map((item) => item.value || 0),
          backgroundColor: data.map((_, index) => palette(index, 0.6)),
          borderRadius: 12,
        },
      ],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true } },
    },
  });
}

function renderDonutChart(canvasId, items) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const data = items || [];
  renderChart(canvas, {
    type: 'doughnut',
    data: {
      labels: data.map((item) => item.label || '—'),
      datasets: [
        {
          data: data.map((item) => item.value || 0),
          backgroundColor: data.map((_, index) => palette(index, 0.8)),
        },
      ],
    },
    options: {
      responsive: true,
      plugins: { legend: { position: 'bottom' } },
    },
  });
}

function renderTable(elementId, rows = [], columns = []) {
  const table = document.getElementById(elementId);
  if (!table) return;
  if (!rows || !rows.length) {
    table.innerHTML = '<tbody><tr><td>No hay datos</td></tr></tbody>';
    return;
  }
  if (!columns.length) columns = Object.keys(rows[0]);
  const thead = `<thead><tr>${columns.map((col) => `<th>${col}</th>`).join('')}</tr></thead>`;
  const tbody = `<tbody>${rows
    .map(
      (row) =>
        `<tr>${columns
          .map((col) => `<td>${formatCell(row[col])}</td>`)
          .join('')}</tr>`
    )
    .join('')}</tbody>`;
  table.innerHTML = `${thead}${tbody}`;
}

function formatCell(value) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') return formatNumber(value);
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
}

function renderChart(canvas, config) {
  if (state.charts.has(canvas.id)) {
    state.charts.get(canvas.id).destroy();
  }
  canvas.style.display = '';
  const container = canvas.parentElement;
  if (container) {
    const placeholder = container.querySelector('.empty-state');
    if (placeholder) {
      placeholder.remove();
    }
  }
  const context = canvas.getContext('2d');
  if (!config.data.labels && config.data.datasets?.[0]?.data?.[0]?.x !== undefined) {
    const labels = Array.from(
      new Set(
        config.data.datasets
          .flatMap((dataset) => dataset.data.map((point) => (typeof point === 'object' ? point.x : point)))
      )
    );
    config.data.labels = labels;
  }
  const chart = new window.Chart(context, config);
  state.charts.set(canvas.id, chart);
}

function updateEmptyState(canvasId, hasData, message = 'Sin datos disponibles') {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const container = canvas.parentElement;
  if (!container) return;
  if (hasData) {
    canvas.style.display = '';
    const placeholder = container.querySelector('.empty-state');
    if (placeholder) placeholder.remove();
    return;
  }
  if (state.charts.has(canvasId)) {
    state.charts.get(canvasId).destroy();
    state.charts.delete(canvasId);
  }
  canvas.style.display = 'none';
  let placeholder = container.querySelector('.empty-state');
  if (!placeholder) {
    placeholder = document.createElement('p');
    placeholder.className = 'empty-state';
    container.appendChild(placeholder);
  }
  placeholder.textContent = message;
}

function renderDemografia(data) {
  const resumenContainer = document.getElementById('municipio-demografia-resumen');
  if (!data) {
    updateEmptyState('municipio-demografia-genero', false, 'Sin datos de género');
    updateEmptyState('municipio-demografia-edad', false, 'Sin datos etarios');
    updateEmptyState('municipio-demografia-barrios', false, 'Sin datos por barrio');
    if (resumenContainer) {
      resumenContainer.innerHTML = '<p class="empty-state">Sin datos demográficos suficientes</p>';
    }
    return;
  }

  const generoEntries = Object.entries(data.genero || {}).map(([label, value]) => ({ label, value }));
  updateEmptyState('municipio-demografia-genero', generoEntries.length > 0, 'Sin datos de género');
  if (generoEntries.length) {
    renderDonutChart('municipio-demografia-genero', generoEntries);
  }

  const ageOrder = ['0-12', '13-17', '18-24', '25-34', '35-44', '45-54', '55-64', '65+'];
  const ageCounts = data.rango_etario || {};
  const ageItems = ageOrder
    .map((bucket) => ({ label: bucket, value: ageCounts[bucket] || 0 }))
    .filter((item) => item.value > 0);
  updateEmptyState('municipio-demografia-edad', ageItems.length > 0, 'Sin datos etarios');
  if (ageItems.length) {
    renderBarChart('municipio-demografia-edad', ageItems);
  }

  const barriosItems = Array.isArray(data.territorio?.barrios) ? data.territorio.barrios.slice(0, 8) : [];
  updateEmptyState('municipio-demografia-barrios', barriosItems.length > 0, 'Sin datos por barrio');
  if (barriosItems.length) {
    renderBarChart('municipio-demografia-barrios', barriosItems);
  }

  if (resumenContainer) {
    const edad = data.edad || {};
    const metrics = [
      {
        label: 'Promedio',
        value:
          edad.promedio != null ? formatNumber(Math.round(Number(edad.promedio) * 10) / 10) : '—',
      },
      { label: 'Mediana', value: edad.mediana != null ? formatNumber(Number(edad.mediana)) : '—' },
      { label: 'P90', value: edad.p90 != null ? formatNumber(Number(edad.p90)) : '—' },
      { label: 'Muestra', value: edad.muestra != null ? edad.muestra : 0 },
    ];
    if (!metrics.length) {
      resumenContainer.innerHTML = '<p class="empty-state">Sin datos demográficos suficientes</p>';
    } else {
      resumenContainer.innerHTML = metrics
        .map(
          (metric) =>
            `<div class="metric"><span>${metric.label}</span><strong>${metric.value}</strong></div>`
        )
        .join('');
    }
  }
}

function groupSeries(series) {
  const grouped = {};
  series.forEach((point) => {
    const group = point.group || 'total';
    if (!grouped[group]) grouped[group] = [];
    grouped[group].push(point);
  });
  Object.values(grouped).forEach((entries) => entries.sort((a, b) => (a.date > b.date ? 1 : -1)));
  return grouped;
}

function palette(index, alpha = 1) {
  const colors = [
    [56, 189, 248],
    [110, 231, 183],
    [249, 115, 22],
    [244, 114, 182],
    [129, 140, 248],
    [251, 191, 36],
  ];
  const color = colors[index % colors.length];
  return `rgba(${color[0]}, ${color[1]}, ${color[2]}, ${alpha})`;
}

function randomPointAroundJunin(random) {
  const angle = random() * Math.PI * 2;
  const radius = 0.25 + random() * JUNIN_RADIUS_KM;
  const deltaLat = (radius * Math.cos(angle)) / 111;
  const denom = 111 * Math.cos((JUNIN_CENTER.lat * Math.PI) / 180) || 1;
  const deltaLon = (radius * Math.sin(angle)) / denom;
  return {
    lat: JUNIN_CENTER.lat + deltaLat,
    lon: JUNIN_CENTER.lon + deltaLon,
  };
}

function buildCategoryMix(total, random) {
  const desired = Math.max(1, Math.round(random() * 3));
  const picked = [];
  while (picked.length < desired) {
    const candidate = DEMO_CATEGORIES[Math.floor(random() * DEMO_CATEGORIES.length)];
    if (!picked.includes(candidate)) {
      picked.push(candidate);
    }
  }
  let remaining = total;
  const mix = {};
  picked.forEach((category, index) => {
    if (index === picked.length - 1) {
      mix[category] = Math.max(1, remaining);
    } else {
      const share = Math.max(1, Math.round(random() * remaining * 0.6));
      mix[category] = share;
      remaining -= share;
    }
  });
  return mix;
}

function generateDemoGeoDataset(scope = 'municipio') {
  const offset = scope === 'pyme' ? 77 : scope === 'operaciones' ? 133 : 21;
  const random = createSeededRandom(dailySeed(offset));
  const cellCount = scope === 'pyme' ? 22 : 30;
  const pointCount = scope === 'pyme' ? 70 : 90;
  const cells = [];
  for (let index = 0; index < cellCount; index += 1) {
    const coords = randomPointAroundJunin(random);
    const count = Math.max(4, Math.round((scope === 'pyme' ? 4 : 8) + random() * 24));
    cells.push({
      cell_id: `demo-${scope}-${index}`,
      count,
      centroid_lat: Number(coords.lat.toFixed(6)),
      centroid_lon: Number(coords.lon.toFixed(6)),
      categories: buildCategoryMix(count, random),
      fuente: 'demo',
    });
  }
  if (cells.length) {
    const maxCount = Math.max(...cells.map((cell) => cell.count || 0)) || 1;
    cells.forEach((cell) => {
      cell.intensity = Number(((cell.count || 0) / maxCount).toFixed(4));
    });
  }
  const points = [];
  for (let index = 0; index < pointCount; index += 1) {
    const coords = randomPointAroundJunin(random);
    const categoria = DEMO_CATEGORIES[Math.floor(random() * DEMO_CATEGORIES.length)];
    const estado = DEMO_STATUSES[Math.floor(random() * DEMO_STATUSES.length)];
    const point = {
      lat: Number(coords.lat.toFixed(6)),
      lon: Number(coords.lon.toFixed(6)),
      categoria,
      estado,
      fuente: 'demo',
    };
    if (scope === 'pyme') {
      point.total = Number((random() * 90000 + 5000).toFixed(2));
    } else {
      point.count = Math.max(1, Math.round(random() * 4));
    }
    points.push(point);
  }
  return { cells, points };
}

function formatNumber(value) {
  if (typeof value !== 'number' || Number.isNaN(value)) return value;
  if (Math.abs(value) >= 1000) return value.toLocaleString('es-AR');
  return Number(value).toLocaleString('es-AR', { maximumFractionDigits: 2 });
}

function updateFilterOptions(selectId, values) {
  const select = document.getElementById(selectId);
  if (!select || !values) return;
  const existing = new Set(Array.from(select.options).map((opt) => opt.value));
  values.forEach((value) => {
    const label = typeof value === 'object' ? value.label : value;
    const optionValue = typeof value === 'object' ? value.label : value;
    if (!optionValue || existing.has(optionValue)) return;
    const option = document.createElement('option');
    option.value = optionValue;
    option.textContent = label || optionValue;
    select.append(option);
    existing.add(optionValue);
  });
}

function ensureMap() {
  const desiredId = state.scope === 'pyme' ? 'pyme-map' : 'municipio-map';
  if (state.map && state.mapContainerId === desiredId) return;
  const container = document.getElementById(desiredId);
  if (!container) return;
  if (state.map) {
    state.map.off();
    state.map.remove();
  }
  state.map = L.map(container, {
    center: [JUNIN_CENTER.lat, JUNIN_CENTER.lon],
    zoom: 13,
    zoomControl: true,
  });
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(state.map);
  state.heatLayer = L.heatLayer([], { radius: 25, blur: 15, maxZoom: 17 }).addTo(state.map);
  state.clusterLayer = L.markerClusterGroup().addTo(state.map);
  state.selectionLayer = L.featureGroup().addTo(state.map);
  if (state.drawControl) {
    state.map.removeControl(state.drawControl);
  }
  state.drawControl = new L.Control.Draw({
    draw: {
      polyline: false,
      polygon: false,
      circle: false,
      circlemarker: false,
      marker: false,
      rectangle: {
        shapeOptions: {
          color: '#38bdf8',
          weight: 2,
        },
      },
    },
    edit: { featureGroup: state.selectionLayer, edit: false },
  });
  state.map.addControl(state.drawControl);
  state.map.on(L.Draw.Event.CREATED, (event) => {
    state.selectionLayer.clearLayers();
    state.selectionLayer.addLayer(event.layer);
    const bounds = event.layer.getBounds();
    const bbox = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()].join(',');
    state.filters.bbox = bbox;
    const bboxInput = document.getElementById('filter-bbox');
    if (bboxInput) bboxInput.value = bbox;
    loadDashboard();
  });
  state.mapContainerId = desiredId;
}

function updateMap(cells, points) {
  ensureMap();
  if (!state.map) return;
  const heatPoints = cells.map((cell) => {
    const intensity = typeof cell.intensity === 'number' ? cell.intensity : cell.count;
    const normalized = Math.max(0, Math.min(1, intensity || 0));
    return [cell.centroid_lat, cell.centroid_lon, normalized];
  });
  state.heatLayer.setLatLngs(heatPoints);
  state.clusterLayer.clearLayers();
  points.forEach((point) => {
    const marker = L.marker([point.lat, point.lon]);
    marker.bindPopup(`<strong>${point.categoria || point.estado || 'Dato'}</strong><br/>${formatCell(point.total || point.count || '')}`);
    state.clusterLayer.addLayer(marker);
  });
  if (!state.map.hasLayer(state.clusterLayer)) {
    state.map.addLayer(state.clusterLayer);
  }
  if (cells.length || points.length) {
    const bounds = [];
    cells.forEach((cell) => bounds.push([cell.centroid_lat, cell.centroid_lon]));
    points.forEach((point) => bounds.push([point.lat, point.lon]));
    if (bounds.length) state.map.fitBounds(bounds, { padding: [40, 40] });
  }
}

function exportCsv(key) {
  const chart = state.charts.get(key);
  if (chart) {
    const rows = [];
    const labels = chart.data.labels || chart.data.datasets[0]?.data.map((point) => point.x);
    rows.push(['label', ...chart.data.datasets.map((ds) => ds.label || 'dataset')]);
    labels.forEach((label, idx) => {
      const values = chart.data.datasets.map((ds) => {
        const point = ds.data[idx];
        if (typeof point === 'object') return point.y ?? point;
        return point;
      });
      rows.push([label, ...values]);
    });
    downloadCsv(`${key}.csv`, rows);
    return;
  }
  const table = document.getElementById(key);
  if (table) {
    const rows = Array.from(table.querySelectorAll('tr')).map((tr) => Array.from(tr.children).map((cell) => cell.textContent));
    downloadCsv(`${key}.csv`, rows);
  }
}

function downloadCsv(filename, rows) {
  const csv = rows.map((row) => row.map((value) => `"${String(value ?? '').replace(/"/g, '""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.setAttribute('download', filename);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

function exportPng(key) {
  const chart = state.charts.get(key);
  if (!chart) return;
  const link = document.createElement('a');
  link.href = chart.toBase64Image('image/png', 1);
  link.download = `${key}.png`;
  link.click();
}

window.addEventListener('DOMContentLoaded', init);
