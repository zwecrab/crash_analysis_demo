'use strict';

import { S, baseTileLayer, setBaseTileLayer, circleLatLngs, getDist } from './state.js';
import { api, circleBbox, roadBbox, fetchAccidents, fetchAnalytics, fetchSectionAnalytics, sectionFor } from './api.js';
import { updateCharts } from './charts.js';
import { clearAccMarkers, renderCollisionList, renderAccidents } from './collisions.js';
import { loadTrajectoryWindow, renderFrame } from './playback.js';
import { renderHeatLayer } from './heatmap.js';

/* ── Leaflet Initialization ─────────────────────────────── */
export const map = L.map('map', { zoomControl: true, preferCanvas: true });

// Initialize cartoDB dark style tile layer
setBaseTileLayer(
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; OSM &copy; CARTO',
    subdomains: 'abcd',
    maxZoom: 19
  }).addTo(map)
);

/* ── Circle Analysis Zone ───────────────────────────────── */
export function createCircle(lat, lng, radius) {
  if (S.circle) S.circle.remove();
  if (S.mask) S.mask.remove();
  S.circleCenter = L.latLng(lat, lng);
  S.radiusM = radius;

  const outerRing = [[-90, -360], [-90, 360], [90, 360], [90, -360]];
  const innerRing = circleLatLngs(lat, lng, radius);
  S.mask = L.polygon([outerRing, innerRing], {
    color: 'none',
    fillColor: '#0f172a',
    fillOpacity: 0.76,
    interactive: false,
    smoothFactor: 1
  }).addTo(map);

  S.circle = L.circle([lat, lng], {
    radius,
    color: '#94a3b8',
    weight: 1.5,
    dashArray: '6 5',
    fill: false
  }).addTo(map);
}

let circleDebounce = null;
export function onCircleChange() {
  clearTimeout(circleDebounce);
  circleDebounce = setTimeout(async () => {
    clearAccMarkers();
    S.accMarkers = [];
    await Promise.allSettled([fetchAccidents(), fetchAnalytics().then(updateCharts)]);
    loadTrajectoryWindow(S.curMs);
  }, 400);
}

/* ── Road Focus Geometry management ────────────────────── */
export const SECTION_COLORS = {
  A: { stroke: '#5b8dc4', fillAlpha: 0.10 }, // Entry Blue
  B: { stroke: '#c7613c', fillAlpha: 0.10 }, // Warning Coral
  C: { stroke: '#8b8478', fillAlpha: 0.06 }  // Exit Grey
};

export function clearRoadGeometry() {
  S.roadRects.forEach(r => r.remove());
  S.roadRects = [];
  S.roadLabels.forEach(l => l.remove());
  S.roadLabels = [];
  if (S.roadMask) {
    S.roadMask.remove();
    S.roadMask = null;
  }
}

export function _polygonCentroid(poly) {
  let lat = 0, lon = 0;
  poly.forEach(p => {
    lat += p[0];
    lon += p[1];
  });
  return [lat / poly.length, lon / poly.length];
}

export function drawRoadGeometry() {
  clearRoadGeometry();
  if (!S.road) return;

  const visibleSections = S.activeSection
    ? S.road.sections.filter(s => s.id === S.activeSection)
    : S.road.sections;

  const outerRing = [[-90, -360], [-90, 360], [90, 360], [90, -360]];
  const holes = visibleSections.map(
    s =>
      s.polygon || [
        [s.lat_min, s.lon_min],
        [s.lat_min, s.lon_max],
        [s.lat_max, s.lon_max],
        [s.lat_max, s.lon_min]
      ]
  );
  S.roadMask = L.polygon([outerRing, ...holes], {
    color: 'none',
    fillColor: '#0f0d0b',
    fillOpacity: 0.78,
    interactive: false,
    smoothFactor: 1
  }).addTo(map);

  visibleSections.forEach(s => {
    const c = SECTION_COLORS[s.id] || SECTION_COLORS.A;
    const isActive = S.activeSection === s.id;
    const ring =
      s.polygon || [
        [s.lat_min, s.lon_min],
        [s.lat_min, s.lon_max],
        [s.lat_max, s.lon_max],
        [s.lat_max, s.lon_min]
      ];
    const rect = L.polygon(ring, {
      color: c.stroke,
      weight: isActive ? 2.4 : 1.6,
      fillColor: c.stroke,
      fillOpacity: c.fillAlpha,
      fill: true,
      dashArray: isActive ? null : '4 6',
      className: `section-rect section-${s.id}${isActive ? ' active' : ''}`
    }).addTo(map);
    rect.bindTooltip(`Section ${s.id} · ${s.label}`, {
      direction: 'top',
      sticky: true,
      className: 'section-tooltip'
    });

    if (S.mode === 'road') {
      rect.on('click', () => setActiveSection(S.activeSection === s.id ? null : s.id));
    }
    S.roadRects.push(rect);
  });
}

export function setActiveSection(id) {
  S.activeSection = id;
  drawRoadGeometry();

  document.querySelectorAll('.section-tile').forEach(el => {
    el.classList.toggle('active', el.dataset.section === id);
  });

  const closeBtn = document.getElementById('btn-close-section');
  if (id) {
    const sec = S.road.sections.find(s => s.id === id);
    if (sec) {
      const ring = sec.polygon || [[sec.lat_min, sec.lon_min], [sec.lat_max, sec.lon_max]];
      map.fitBounds(ring, { padding: [40, 40], animate: true, maxZoom: 19 });
    }
    if (closeBtn) {
      closeBtn.classList.remove('hidden');
      const sec2 = S.road.sections.find(s => s.id === id);
      closeBtn.setAttribute(
        'title',
        `Back to full road view (currently focused on Section ${id} · ${sec2 ? sec2.label : ''})`
      );
    }
  } else {
    const bb = roadBbox();
    map.fitBounds([[bb.lat_min, bb.lon_min], [bb.lat_max, bb.lon_max]], { padding: [60, 60], animate: true });
    if (closeBtn) closeBtn.classList.add('hidden');
  }

  S.cacheStart = null;
  S.cacheEnd = null;
  S.lastRiskDay = null;
  if (S.heatmapEnabled) {
    renderHeatLayer(S.heatmapPoints);
  }
  if (S.mode === 'road') {
    Promise.allSettled([fetchAccidents(), fetchAnalytics().then(updateCharts)]).then(() =>
      loadTrajectoryWindow(S.curMs).then(renderFrame)
    );
  }
}

export async function enterRoadMode() {
  if (!S.road) {
    try {
      S.road = await api('/api/road');
    } catch (e) {
      showToast('Failed to load road config: ' + e.message, '#b73d3d');
      return;
    }
  }
  S.mode = 'road';
  S.activeSection = null;
  document.getElementById('btn-mode-full').classList.remove('active');
  document.getElementById('btn-mode-road').classList.add('active');
  document.getElementById('section-panel').classList.remove('hidden');

  if (S.circle) {
    S.circle.remove();
    S.circle = null;
  }
  if (S.mask) {
    S.mask.remove();
    S.mask = null;
  }
  drawRoadGeometry();

  const bb = roadBbox();
  map.fitBounds([[bb.lat_min, bb.lon_min], [bb.lat_max, bb.lon_max]], { padding: [60, 60] });

  S.cacheStart = null;
  S.cacheEnd = null;
  S.lastRiskDay = null;
  renderSectionTiles();
  await Promise.allSettled([
    fetchAccidents(),
    fetchAnalytics().then(updateCharts),
    fetchSectionAnalytics()
  ]);
  await loadTrajectoryWindow(S.curMs);
  renderFrame();
}

export async function exitRoadMode() {
  S.mode = 'full';
  S.activeSection = null;
  document.getElementById('btn-mode-road').classList.remove('active');
  document.getElementById('btn-mode-full').classList.add('active');
  document.getElementById('section-panel').classList.add('hidden');
  document.getElementById('btn-close-section').classList.add('hidden');
  clearRoadGeometry();

  if (S.bounds) {
    let lat = (S.bounds.lat_min + S.bounds.lat_max) / 2,
      lng = (S.bounds.lon_min + S.bounds.lon_max) / 2,
      r = 500;
    try {
      const bs = await api('/api/blackspots');
      if (bs && bs.length) {
        lat = bs[0].lat;
        lng = bs[0].lon;
        r = bs[0].radius_m || 500;
      }
    } catch (e) {}
    createCircle(lat, lng, r);
    map.fitBounds([[S.bounds.lat_min, S.bounds.lon_min], [S.bounds.lat_max, S.bounds.lon_max]], {
      padding: [30, 30]
    });
  }
  S.cacheStart = null;
  S.cacheEnd = null;
  S.lastRiskDay = null;
  await Promise.allSettled([fetchAccidents(), fetchAnalytics().then(updateCharts)]);
  await loadTrajectoryWindow(S.curMs);
  renderFrame();
}

export function renderSectionTiles() {
  const grid = document.getElementById('section-grid');
  if (!grid || !S.road) return;
  grid.innerHTML = S.road.sections
    .map(s => {
      const c = SECTION_COLORS[s.id] || SECTION_COLORS.A;
      const a = S.sectionAnalytics[s.id];
      const isLoading = a && a._loading;
      const isError = a && a._error;
      const eb = (a && !isLoading && !isError && a.event_breakdown) || null;
      const totalCrashes = eb ? eb.collision?.count || 0 : null;
      const totalEvents = eb
        ? (eb.harsh_brake?.count || 0) + (eb.sudden_accel?.count || 0) + (eb.sharp_turn?.count || 0)
        : null;
      const noData = eb && totalCrashes === 0 && totalEvents === 0;
      let bodyHtml;
      if (isLoading) {
        bodyHtml = `<div class="section-tile-empty"><div class="section-spinner"></div>Loading analytics…</div>`;
      } else if (isError) {
        bodyHtml = `<div class="section-tile-empty section-tile-error">Failed to load<div class="section-tile-empty-sub">${a._error}</div></div>`;
      } else if (noData) {
        bodyHtml = `<div class="section-tile-empty">No data in this window<div class="section-tile-empty-sub">Pending data extraction over Kamphaeng Phet 6 Rd</div></div>`;
      } else {
        bodyHtml = `<div class="section-tile-stats">
        <div class="stat"><div class="stat-num" data-sec="${s.id}" data-stat="crashes">${
          totalCrashes != null ? totalCrashes : '—'
        }</div><div class="stat-lbl">Total<br>crashes</div></div>
        <div class="stat"><div class="stat-num" data-sec="${s.id}" data-stat="events">${
          totalEvents != null ? totalEvents.toLocaleString() : '—'
        }</div><div class="stat-lbl">Total<br>events</div></div>
        <div class="stat"><div class="stat-num stat-risk" data-sec="${s.id}" data-stat="risk-today">—</div><div class="stat-lbl">Risk<br>today</div></div>
      </div>
      <div class="section-tile-day" data-sec="${s.id}" data-stat="day-detail">— · — crashes · — events</div>`;
      }
      return `<button class="section-tile${S.activeSection === s.id ? ' active' : ''}${
        noData ? ' no-data' : ''
      }${isError ? ' has-error' : ''}" data-section="${s.id}" style="--c:${c.stroke}">
      <div class="section-tile-head">
        <span class="section-tile-id">${s.id}</span>
        <span class="section-tile-label">${s.label}</span>
      </div>
      ${bodyHtml}
    </button>`;
    })
    .join('');

  grid.querySelectorAll('.section-tile').forEach(el => {
    el.addEventListener('click', () => {
      const id = el.dataset.section;
      setActiveSection(S.activeSection === id ? null : id);
    });
  });
}

const _dayKey = ms => new Date(ms).toISOString().slice(0, 10);

export function updateSectionRiskForCurrentDay(force) {
  if (!S.road || S.curMs == null) return;
  const day = _dayKey(S.curMs);
  S.road.sections.forEach(s => {
    if (!force && S.lastSectionRiskDay[s.id] === day) return;
    S.lastSectionRiskDay[s.id] = day;
    const dd = S.sectionDaily[s.id];
    if (!dd) return;
    const c = dd.crashes[day] || 0;
    const e = dd.events[day] || 0;
    const score = Math.min(10, c * 10 + e * 0.001);
    const rounded = Math.round(score * 10) / 10;
    const lvl = score <= 3 ? 'low' : score <= 6 ? 'medium' : 'high';
    const riskEl = document.querySelector(
      `.section-tile[data-section="${s.id}"] [data-stat="risk-today"]`
    );
    if (riskEl) {
      riskEl.textContent = rounded.toFixed(1);
      riskEl.classList.remove('risk-low', 'risk-medium', 'risk-high');
      riskEl.classList.add('risk-' + lvl);
    }
    const dayEl = document.querySelector(
      `.section-tile[data-section="${s.id}"] [data-stat="day-detail"]`
    );
    if (dayEl) {
      const dateStr = new Date(S.curMs).toLocaleDateString('en-GB', {
        day: '2-digit',
        month: 'short'
      });
      dayEl.innerHTML = `<b>${dateStr}</b> · ${c} crash${c === 1 ? '' : 'es'} · ${e.toLocaleString()} event${
        e === 1 ? '' : 's'
      }`;
    }
  });
}

export function toggleMapStyle() {
  console.log('[toggleMapStyle] S.mapStyle was:', S.mapStyle, '| baseTileLayer:', baseTileLayer);
  const to = S.mapStyle === 'dark' ? 'light' : 'dark';
  console.log('[toggleMapStyle] switching to:', to);
  S.mapStyle = to;
  map.removeLayer(baseTileLayer);
  const url =
    to === 'light'
      ? 'https://{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}&hl=en'
      : 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';
  const subdomains = to === 'light' ? ['mt0', 'mt1', 'mt2', 'mt3'] : 'abcd';
  setBaseTileLayer(
    L.tileLayer(url, {
      attribution: to === 'light' ? '&copy; Google' : '&copy; OSM &copy; CARTO',
      subdomains: subdomains,
      maxZoom: 20
    }).addTo(map)
  );
  baseTileLayer.bringToBack();
  if (S.mode === 'road') {
    drawRoadGeometry();
  } else if (S.mode === 'full') {
    if (S.circleCenter) {
      createCircle(S.circleCenter.lat, S.circleCenter.lng, S.radiusM);
    }
  }
  const btn = document.getElementById('btn-map-toggle');
  if (btn) {
    btn.textContent = to === 'light' ? '🌙 Dark Mode' : '🗺️ Google Map';
    btn.classList.toggle('active', to === 'light');
  }
}

export const ROUTE_PATHS = {
  AB: [[13.840191, 100.556739], [13.840540, 100.556951]],
  BA: [[13.840540, 100.556951], [13.840191, 100.556739]],
  CA: [[13.840408, 100.556743], [13.840191, 100.556739]],
  CB: [[13.840408, 100.556743], [13.840540, 100.556951]],
  AC: [[13.840191, 100.556739], [13.840408, 100.556743]],
  BC: [[13.840540, 100.556951], [13.840408, 100.556743]]
};

export const ROUTE_LABELS = {
  AB: 'Route AB: North Entrance ➔ South Exit (Main Highway)',
  BA: 'Route BA: South Entrance ➔ North Exit (Main Highway)',
  CA: 'Route CA: Gate C Connector ➔ North Exit (High Risk Transit)',
  CB: 'Route CB: Gate C Connector ➔ South Exit (High Risk Transit)',
  AC: 'Route AC: North Entrance ➔ Gate C Connector',
  BC: 'Route BC: South Entrance ➔ Gate C Connector'
};

export const GATE_COORDS = {
  A: [[13.8402134, 100.5568106], [13.8402584, 100.5567211], [13.8401686, 100.5566678], [13.8401220, 100.5567560]],
  B: [[13.8405280, 100.5568707], [13.8404676, 100.5569855], [13.8405560, 100.5570320], [13.8406097, 100.5569150]],
  C: [[13.8404666, 100.5568276], [13.8405093, 100.5567459], [13.8403466, 100.5566593], [13.8403101, 100.5567385]]
};

let hoverRoutePolyline = null;
let hoverRouteTooltip = null;
let hoverStartPolygon = null;
let hoverEndPolygon = null;

export function drawHoverRoute(routeId) {
  clearHoverRoute();
  const path = ROUTE_PATHS[routeId];
  const label = ROUTE_LABELS[routeId];
  if (!path || routeId.length !== 2) return;

  const startGate = routeId[0];
  const endGate = routeId[1];

  // Draw start gate in green (origin)
  if (GATE_COORDS[startGate]) {
    hoverStartPolygon = L.polygon(GATE_COORDS[startGate], {
      color: '#22c55e',
      fillColor: '#22c55e',
      fillOpacity: 0.35,
      weight: 2,
      interactive: false
    }).addTo(map);
  }

  // Draw end gate in red (destination)
  if (GATE_COORDS[endGate]) {
    hoverEndPolygon = L.polygon(GATE_COORDS[endGate], {
      color: '#ef4444',
      fillColor: '#ef4444',
      fillOpacity: 0.35,
      weight: 2,
      interactive: false
    }).addTo(map);
  }

  // Draw centerline trajectory in subtle grey-white with moving dash micro-animation
  hoverRoutePolyline = L.polyline(path, {
    color: '#94a3b8',
    weight: 3.5,
    opacity: 0.85,
    className: 'animated-route-line'
  }).addTo(map);
}

export function clearHoverRoute() {
  if (hoverRoutePolyline) {
    hoverRoutePolyline.remove();
    hoverRoutePolyline = null;
  }
  if (hoverRouteTooltip) {
    hoverRouteTooltip.remove();
    hoverRouteTooltip = null;
  }
  if (hoverStartPolygon) {
    hoverStartPolygon.remove();
    hoverStartPolygon = null;
  }
  if (hoverEndPolygon) {
    hoverEndPolygon.remove();
    hoverEndPolygon = null;
  }
}

