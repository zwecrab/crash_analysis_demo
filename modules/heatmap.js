'use strict';

import { S } from './state.js';
import { api, sectionFor } from './api.js';
import { map } from './map.js';

/* ── Heatmap Corridor Boundary Calculation ────────────────── */
export const HEATMAP_ZONE = (() => {
  const S0 = [13.8390065, 100.556055], E0 = [13.8412935, 100.557258];
  const mLat = 111000, mLon = 111000 * Math.cos((13.8396 * Math.PI) / 180);
  const dx = (E0[0] - S0[0]) * mLat, dy = (E0[1] - S0[1]) * mLon;
  const len = Math.sqrt(dx * dx + dy * dy);
  const ux = dx / len, uy = dy / len;
  const vx = -uy, vy = ux;
  const W = 17.25; // 34.5m total corridor width / 2
  const corners = [
    [S0[0] + (vx * W) / mLat, S0[1] + (vy * W) / mLon],
    [E0[0] + (vx * W) / mLat, E0[1] + (vy * W) / mLon],
    [E0[0] - (vx * W) / mLat, E0[1] - (vy * W) / mLon],
    [S0[0] - (vx * W) / mLat, S0[1] - (vy * W) / mLon]
  ];
  const lats = corners.map(c => c[0]), lons = corners.map(c => c[1]);
  return {
    polygon: corners,
    bbox: { lat_min: Math.min(...lats), lat_max: Math.max(...lats), lon_min: Math.min(...lons), lon_max: Math.max(...lons) },
    bearing: Math.round((Math.atan2(dy, dx) * 180) / Math.PI),
    center: [(S0[0] + E0[0]) / 2, (S0[1] + E0[1]) / 2]
  };
})();

export const HEAT_GRADIENTS = {
  0: { 0.2: '#3b82f6', 0.5: '#f59e0b', 1.0: '#dc2626' }, // all
  1: { 0.2: '#ccfbf1', 0.5: '#0d9488', 1.0: '#065f46' }, // accel
  2: { 0.2: '#fef3c7', 0.5: '#f59e0b', 1.0: '#b45309' }, // brake
  3: { 0.2: '#ede9fe', 0.5: '#8b5cf6', 1.0: '#5b21b6' }  // turn
};

/* ── Heatmap Operations ─────────────────────────────────── */
export async function fetchHeatmapData() {
  const bb = HEATMAP_ZONE.bbox;
  const hour = S.heatHour === 24 ? -1 : S.heatHour;
  const p = new URLSearchParams({
    lat_min: bb.lat_min,
    lat_max: bb.lat_max,
    lon_min: bb.lon_min,
    lon_max: bb.lon_max,
    event_type: S.heatEventType,
    speed_bracket: S.heatSpeedBracket,
    hour: hour
  });

  // Task 3: If a specific bidirectional route is active, pass it to filter event transitions
  if (S.activeRoute) {
    p.append('route', S.activeRoute);
  }

  const loadEl = document.getElementById('hm-loading');
  const cntEl = document.getElementById('hm-point-count');
  if (loadEl) loadEl.classList.remove('hidden');
  try {
    const d = await api('/api/heatmap?' + p);
    S.heatmapPoints = d.points;
    S.heatmapCapped = d.capped;
    renderHeatLayer(d.points);
  } catch (e) {
    console.error('[heatmap] fetch failed:', e);
    if (cntEl) cntEl.textContent = 'Failed to load';
  } finally {
    if (loadEl) loadEl.classList.add('hidden');
  }
}

export function renderHeatLayer(points) {
  if (S.heatLayer) {
    S.heatLayer.remove();
    S.heatLayer = null;
  }
  if (!points || !points.length) {
    const cntEl = document.getElementById('hm-point-count');
    if (cntEl) cntEl.textContent = '0 events';
    return;
  }

  // Task 3: Section Crop on-click
  let displayPoints = points;
  if (S.activeSection) {
    displayPoints = points.filter(p => sectionFor(p[0], p[1]) === S.activeSection);
  }

  const cntEl = document.getElementById('hm-point-count');
  if (cntEl) {
    const total = displayPoints.length;
    let txt = total.toLocaleString() + ' event' + (total !== 1 ? 's' : '');
    if (S.heatmapCapped && !S.activeSection) txt += ' (capped at 60 000)';
    cntEl.textContent = txt;
  }

  if (!displayPoints || !displayPoints.length) return;

  // Stacks the heatmap cleanly below markers
  if (!map.getPane('heatmapPane')) {
    const pane = map.createPane('heatmapPane');
    pane.style.zIndex = 450;
    pane.style.pointerEvents = 'none';
  }

  const grad = HEAT_GRADIENTS[S.heatEventType] || HEAT_GRADIENTS[0];

  S.heatLayer = L.heatLayer(displayPoints, {
    radius: 10,
    blur: 8,
    maxZoom: 19,
    max: 30.0,
    gradient: grad,
    minOpacity: 0.15,
    pane: 'heatmapPane'
  }).addTo(map);
}

export function clearHeatLayer() {
  if (S.heatLayer) {
    S.heatLayer.remove();
    S.heatLayer = null;
  }
  if (S.heatZonePoly) {
    S.heatZonePoly.remove();
    S.heatZonePoly = null;
  }
  S.heatmapPoints = null;
  S.heatmapCapped = false;
}

/* ── Toggle Heatmap Layer Overlay (Task 2) ────────────────── */
export async function toggleHeatmapOverlay(enable) {
  S.heatmapEnabled = !!enable;

  const btn = document.getElementById('btn-toggle-heatmap');
  const panel = document.getElementById('heatmap-panel');

  if (btn) btn.classList.toggle('active', S.heatmapEnabled);
  if (panel) panel.classList.toggle('hidden', !S.heatmapEnabled);

  if (S.heatmapEnabled) {
    await fetchHeatmapData();
  } else {
    clearHeatLayer();
  }
}
