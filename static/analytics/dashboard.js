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
  try {
    const summary = await fetchJson('summary', params);
    renderSummary(summary);
    if (state.scope === 'municipio') {
      await Promise.all([
        loadTimeseries('municipio-timeseries'),
        loadBreakdowns(),
        loadTopMunicipio(),
        loadGeo(),
      ]);
    } else if (state.scope === 'pyme') {
      await Promise.all([
        loadTimeseries('pyme-timeseries'),
        loadPymeBreakdowns(),
        loadGeo(),
        loadPymeTables(),
      ]);
    } else if (state.scope === 'operaciones') {
      await Promise.all([
        loadOperations(),
        loadTimeseries('municipio-timeseries'),
      ]);
    }
  } catch (error) {
    console.error('Analytics error', error);
  }
}

function renderSummary(summary) {
  if (state.scope === 'municipio') {
    const items = [
      { label: 'Tickets', value: summary.totals.tickets },
      { label: 'Abiertos', value: summary.totals.tickets_abiertos },
      { label: 'Backlog', value: summary.totals.backlog },
      { label: 'TTA P50', value: summary.sla.tta?.p50, suffix: 'min' },
      { label: 'TTR P90', value: summary.sla.ttr?.p90, suffix: 'min' },
      { label: '% Automatizado', value: summary.totals.automatizado_pct, suffix: '%' },
      { label: '% 1er contacto', value: summary.totals.primer_contacto_pct, suffix: '%' },
      { label: 'NPS', value: summary.totals.nps },
      { label: 'CSAT', value: summary.totals.csat },
    ];
    renderKpis('municipio-kpis', items);
    renderSla('municipio-sla', summary.sla);
  } else if (state.scope === 'pyme') {
    const items = [
      { label: 'Tickets', value: summary.totals.tickets },
      { label: 'Pedidos', value: summary.totals.pedidos },
      { label: 'Ticket medio', value: summary.totals.ticket_medio, prefix: '$' },
      { label: '% Conversión', value: summary.totals.conversion_pct, suffix: '%' },
      { label: 'Retención 30', value: summary.totals.retencion_30, suffix: '%' },
      { label: '% Automatizado', value: summary.totals.automatizado_pct, suffix: '%' },
      { label: 'Hora pico', value: summary.totals.hora_pico },
      { label: 'NPS', value: summary.totals.nps },
      { label: 'CSAT', value: summary.totals.csat },
    ];
    renderKpis('pyme-kpis', items);
  } else {
    const totals = summary.totals || {};
    const items = [
      { label: 'Tickets', value: totals.tickets },
      { label: 'Abiertos', value: totals.abiertos },
      { label: 'Violaciones SLA', value: totals.violaciones_sla },
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
  const data = await fetchJson('timeseries', params);
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const grouped = groupSeries(data.series || []);
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
  const categoria = await fetchJson('breakdown', buildParams({ dimension: 'categoria' }));
  renderBarChart('municipio-top-categorias', categoria.breakdown || []);
  const canales = await fetchJson('breakdown', buildParams({ dimension: 'canal' }));
  renderDonutChart('municipio-top-canales', canales.breakdown || []);
  updateFilterOptions('filter-categoria', categoria.breakdown);
  updateFilterOptions('filter-canal', canales.breakdown);
  const estados = await fetchJson('breakdown', buildParams({ dimension: 'estado' }));
  updateFilterOptions('filter-estado', estados.breakdown);
  const zonas = await fetchJson('top', buildParams({ category: 'barrios', limit: 20 }));
  updateFilterOptions('filter-zona', zonas.items);
}

async function loadTopMunicipio() {
  const zonas = await fetchJson('top', buildParams({ category: 'barrios', limit: 10 }));
  renderTable('municipio-top-zonas', zonas.items, ['label', 'value']);
}

async function loadPymeBreakdowns() {
  const productos = await fetchJson('top', buildParams({ category: 'productos', limit: 10 }));
  renderBarChart('pyme-top-productos', productos.items || []);
  const conversion = await fetchJson('breakdown', buildParams({ dimension: 'canal' }));
  renderDonutChart('pyme-conversion-canal', conversion.breakdown || []);
  updateFilterOptions('filter-canal', conversion.breakdown);
}

async function loadPymeTables() {
  const templates = await fetchJson('whatsapp/templates', buildParams());
  renderTable('pyme-templates', templates.templates, ['template', 'sent', 'responded', 'ctr']);
  const cohorts = await fetchJson('cohorts', buildParams());
  renderTable('pyme-cohorts', cohorts.cohorts, ['cohort', 'size', 'retention30', 'retention60', 'retention90']);
}

async function loadOperations() {
  const data = await fetchJson('operations', buildParams());
  const extras = data.extras || {};
  renderBarChart('operaciones-aging', Object.entries(extras.aging || {}).map(([label, value]) => ({ label, value })));
  renderTable('operaciones-agentes', extras.agents, ['agente', 'tickets', 'respuesta_promedio_min']);
  renderBarChart('operaciones-sla', Object.entries(extras.queue || {}).map(([label, value]) => ({ label, value })));
}

async function loadGeo() {
  const heat = await fetchJson('geo/heatmap', buildParams());
  const points = await fetchJson('geo/points', buildParams({ limit: 1000 }));
  updateMap(heat.cells || [], points.points || []);
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
    center: [-34.6037, -58.3816],
    zoom: 12,
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
  state.heatLayer.setLatLngs(cells.map((cell) => [cell.centroid_lat, cell.centroid_lon, cell.count]));
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
