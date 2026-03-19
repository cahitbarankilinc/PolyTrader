const state = {
  cities: [],
  events: [],
  series: null,
};

const citySelect = document.getElementById('citySelect');
const dateSelect = document.getElementById('dateSelect');
const intervalSelect = document.getElementById('intervalSelect');
const reloadButton = document.getElementById('reloadButton');
const statsGrid = document.getElementById('statsGrid');
const chartTitle = document.getElementById('chartTitle');
const chartSubtitle = document.getElementById('chartSubtitle');
const chartSvg = document.getElementById('chartSvg');
const chartTooltip = document.getElementById('chartTooltip');
const legend = document.getElementById('legend');
const marketTableBody = document.getElementById('marketTableBody');
const tableHint = document.getElementById('tableHint');
const windowSizeSelect = document.getElementById('windowSizeSelect');
const panSlider = document.getElementById('panSlider');
const panHint = document.getElementById('panHint');

const palette = ['#66d9ef', '#ffd166', '#ef476f', '#06d6a0', '#a78bfa', '#f97316', '#22c55e', '#f43f5e', '#38bdf8', '#eab308', '#fb7185'];

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return await res.json();
}

async function loadCities() {
  const data = await fetchJson('/api/tracker/cities');
  state.cities = data.cities || [];
  citySelect.innerHTML = state.cities.map((city) => `<option value="${escapeHtml(city.city_slug)}">${escapeHtml(city.city)}</option>`).join('');
}

async function loadEvents() {
  const city = citySelect.value;
  if (!city) return;
  const data = await fetchJson(`/api/tracker/events?city=${encodeURIComponent(city)}`);
  state.events = data.events || [];
  dateSelect.innerHTML = state.events.map((event) => `<option value="${escapeHtml(event.target_date)}">${escapeHtml(event.target_date)}</option>`).join('');
}

async function loadSeries() {
  const city = citySelect.value;
  const date = dateSelect.value;
  const interval = intervalSelect.value;
  if (!city || !date) return;
  state.series = await fetchJson(`/api/tracker/series?city=${encodeURIComponent(city)}&date=${encodeURIComponent(date)}&interval=${encodeURIComponent(interval)}`);
  state.panStart = 0;
  syncPanControls();
  render();
}

function render() {
  renderStats();
  renderChart();
  renderTable();
}

function renderStats() {
  const series = state.series;
  if (!series) return;
  const markers = series.forecast_markers || [];
  const cards = [
    ['Snapshot', series.snapshot_count || 0],
    ['Forecast snapshot', series.forecast_snapshot_count || 0],
    ['Market line', (series.market_series || []).length],
    ['Kırmızı çizgi', markers.length],
  ];
  statsGrid.innerHTML = cards.map(([label, value]) => `<article class="panel stat-card"><span class="muted">${escapeHtml(label)}</span><strong>${escapeHtml(String(value))}</strong></article>`).join('');
  chartTitle.textContent = series.event_title || 'Event grafiği';
  chartSubtitle.textContent = `${series.target_date} · interval ${series.interval} · source ${series.source_url_actual || series.source_url_expected || '—'}`;
}

function renderChart() {
  const series = state.series;
  if (!series) return;
  const marketSeries = series.market_series || [];
  const allPoints = marketSeries.flatMap((line) => line.points || []);
  if (!allPoints.length) {
    chartSvg.innerHTML = '';
    legend.innerHTML = '';
    syncPanControls();
    return;
  }

  const width = 1200;
  const height = 520;
  const margin = { top: 20, right: 24, bottom: 42, left: 52 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;
  const timestamps = [...new Set(allPoints.map((point) => point.ts))].sort();
  const visible = visibleTimestamps(timestamps);
  const minTs = Date.parse(visible[0]);
  const maxTs = Date.parse(visible[visible.length - 1]);
  const visibleSet = new Set(visible);
  const xFor = (ts) => margin.left + ((Date.parse(ts) - minTs) / Math.max(1, maxTs - minTs)) * innerWidth;
  const yFor = (value) => margin.top + innerHeight - (Number(value || 0) / 100) * innerHeight;

  const horizontalGrid = [0, 20, 40, 60, 80, 100].map((value) => {
    const y = yFor(value);
    return `<line x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}" class="grid-line" />
      <text x="${margin.left - 10}" y="${y + 4}" class="axis-label axis-left">${value}</text>`;
  }).join('');

  const verticalTicks = visible.filter((_, index) => index % Math.max(1, Math.floor(visible.length / 6)) === 0 || index === visible.length - 1).map((ts) => {
    const x = xFor(ts);
    return `<line x1="${x}" y1="${margin.top}" x2="${x}" y2="${height - margin.bottom}" class="grid-line vertical" />
      <text x="${x}" y="${height - 14}" text-anchor="middle" class="axis-label">${formatTime(ts)}</text>`;
  }).join('');

  const lines = marketSeries.map((line, index) => {
    const color = palette[index % palette.length];
    const points = (line.points || []).filter((point) => visibleSet.has(point.ts));
    if (!points.length) return '';
    const d = points.map((point, pointIndex) => `${pointIndex === 0 ? 'M' : 'L'} ${xFor(point.ts)} ${yFor(point.yes_probability_cents)}`).join(' ');
    const circles = points.map((point) => {
      const payload = encodeURIComponent(JSON.stringify({ type: 'market', label: line.label, point }));
      return `<circle class="point-dot" cx="${xFor(point.ts)}" cy="${yFor(point.yes_probability_cents)}" r="4" fill="${color}" data-payload="${payload}"></circle>`;
    }).join('');
    return `<path d="${d}" fill="none" stroke="${color}" stroke-width="2.4"></path>${circles}`;
  }).join('');

  const markers = (series.forecast_markers || []).filter((marker) => visibleSet.has(marker.ts)).map((marker) => {
    const x = xFor(marker.ts);
    const payload = encodeURIComponent(JSON.stringify({ type: 'marker', marker }));
    return `<line x1="${x}" y1="${margin.top}" x2="${x}" y2="${height - margin.bottom}" class="forecast-marker" data-payload="${payload}"></line>`;
  }).join('');

  chartSvg.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${height}" fill="transparent"></rect>
    ${horizontalGrid}
    ${verticalTicks}
    <line x1="${margin.left}" y1="${height - margin.bottom}" x2="${width - margin.right}" y2="${height - margin.bottom}" class="axis-line"></line>
    <line x1="${margin.left}" y1="${margin.top}" x2="${margin.left}" y2="${height - margin.bottom}" class="axis-line"></line>
    ${markers}
    ${lines}
  `;

  legend.innerHTML = marketSeries.map((line, index) => `<div class="legend-item"><span class="legend-color" style="background:${palette[index % palette.length]}"></span>${escapeHtml(line.label)}</div>`).join('');
  syncPanControls(timestamps, visible);
}

function renderTable() {
  const series = state.series;
  if (!series) return;
  const rows = (series.market_series || []).map((line) => ({ label: line.label, point: (line.points || []).at(-1) })).filter((row) => row.point);
  tableHint.textContent = rows.length ? `Son aggregated nokta gösteriliyor (${series.interval})` : '';
  marketTableBody.innerHTML = rows.map(({ label, point }) => `
    <tr>
      <td>${escapeHtml(label)}</td>
      <td>${formatCents(point.yes_probability_cents)}</td>
      <td>${formatCents(point.no_probability_cents)}</td>
      <td>${formatCents(point.yes_best_bid_sell_cents)}</td>
      <td>${formatCents(point.yes_best_ask_buy_cents)}</td>
      <td>${formatCents(point.no_best_bid_sell_cents)}</td>
      <td>${formatCents(point.no_best_ask_buy_cents)}</td>
    </tr>
  `).join('');
}

function visibleTimestamps(timestamps) {
  if (!timestamps.length) return [];
  const requested = state.windowSize === 'all' ? timestamps.length : Number(state.windowSize || 24);
  const size = Math.max(1, Math.min(timestamps.length, requested));
  const maxStart = Math.max(0, timestamps.length - size);
  const start = Math.max(0, Math.min(state.panStart || 0, maxStart));
  state.panStart = start;
  return timestamps.slice(start, start + size);
}

function syncPanControls(timestamps = [], visible = []) {
  const total = timestamps.length;
  const size = state.windowSize === 'all' ? total : Math.max(1, Math.min(total || 1, Number(state.windowSize || 24)));
  const maxStart = Math.max(0, total - size);
  panSlider.max = String(maxStart);
  panSlider.value = String(Math.max(0, Math.min(state.panStart || 0, maxStart)));
  panSlider.disabled = maxStart === 0;
  if (!visible.length) {
    panHint.textContent = '';
    return;
  }
  panHint.textContent = `${formatTime(visible[0])} → ${formatTime(visible[visible.length - 1])}`;
}

function formatCents(value) {
  if (value == null) return '—';
  return `${value}¢`;
}

function formatTime(ts) {
  const d = new Date(ts);
  return d.toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }) + ' UTC';
}

function escapeHtml(value) {
  return String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

function showTooltip(event) {
  const payloadRaw = event.target?.dataset?.payload;
  if (!payloadRaw) {
    chartTooltip.classList.add('hidden');
    return;
  }
  const payload = JSON.parse(decodeURIComponent(payloadRaw));
  chartTooltip.classList.remove('hidden');
  chartTooltip.style.left = `${event.offsetX + 18}px`;
  chartTooltip.style.top = `${event.offsetY + 18}px`;
  if (payload.type === 'marker') {
    const title = payload.marker.marker_kind === 'baseline' ? 'Forecast başlangıç noktası' : 'Forecast değişimi';
    chartTooltip.innerHTML = `
      <div class="tooltip-title">${escapeHtml(title)}</div>
      <div>${escapeHtml(payload.marker.ts || '')}</div>
      <div>Top 5 ortalama: <strong>${payload.marker.top5_avg_c}°C</strong></div>
      <div>Günün max'ı: <strong>${payload.marker.day_max_c}°C</strong></div>
    `;
    return;
  }
  const point = payload.point || {};
  chartTooltip.innerHTML = `
    <div class="tooltip-title">${escapeHtml(payload.label || '')}</div>
    <div>${escapeHtml(point.actual_ts || point.ts || '')}</div>
    <div>YES: <strong>${formatCents(point.yes_probability_cents)}</strong></div>
    <div>NO: <strong>${formatCents(point.no_probability_cents)}</strong></div>
    <div>YES bid/ask: ${formatCents(point.yes_best_bid_sell_cents)} / ${formatCents(point.yes_best_ask_buy_cents)}</div>
    <div>NO bid/ask: ${formatCents(point.no_best_bid_sell_cents)} / ${formatCents(point.no_best_ask_buy_cents)}</div>
  `;
}

chartSvg.addEventListener('mousemove', (event) => {
  if (state.dragging) {
    const deltaX = event.clientX - state.dragOriginX;
    const total = [...new Set((state.series?.market_series || []).flatMap((line) => (line.points || []).map((point) => point.ts)))].length;
    const requested = state.windowSize === 'all' ? total : Number(state.windowSize || 24);
    const size = Math.max(1, Math.min(total || 1, requested));
    const maxStart = Math.max(0, total - size);
    const stepPx = 28;
    const deltaSteps = Math.round(-deltaX / stepPx);
    state.panStart = Math.max(0, Math.min(maxStart, state.dragOriginStart + deltaSteps));
    renderChart();
    return;
  }
  showTooltip(event);
});
chartSvg.addEventListener('mouseleave', () => { chartTooltip.classList.add('hidden'); state.dragging = false; });
chartSvg.addEventListener('mousedown', (event) => {
  state.dragging = true;
  state.dragOriginX = event.clientX;
  state.dragOriginStart = state.panStart || 0;
  chartSvg.classList.add('dragging');
});
window.addEventListener('mouseup', () => { state.dragging = false; chartSvg.classList.remove('dragging'); });
panSlider.addEventListener('input', () => {
  state.panStart = Number(panSlider.value || 0);
  renderChart();
});
windowSizeSelect.addEventListener('change', () => {
  state.windowSize = windowSizeSelect.value;
  state.panStart = 0;
  renderChart();
});
reloadButton.addEventListener('click', loadSeries);
citySelect.addEventListener('change', async () => { await loadEvents(); await loadSeries(); });
dateSelect.addEventListener('change', loadSeries);
intervalSelect.addEventListener('change', loadSeries);

(async function init() {
  await loadCities();
  await loadEvents();
  await loadSeries();
})().catch((error) => {
  console.error(error);
  chartSubtitle.textContent = 'Dashboard yüklenemedi';
});
