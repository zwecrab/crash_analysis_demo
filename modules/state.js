'use strict';

/* ── State ─────────────────────────────────────────────── */
export const S = {
  tStartMs: null,
  tEndMs: null,
  curMs: null,
  playing: false,
  timer: null,
  speedMult: 60,
  tickMs: 50,
  lastWall: 0,
  bounds: null,
  circle: null,
  mask: null,
  circleCenter: null,
  radiusM: 500,
  trajs: {},
  cacheStart: null,
  cacheEnd: null,
  prefetching: false,
  sampleSec: 2,
  accidents: [],
  accMarkers: [],
  accidentVins: {},
  accMode: 'persist',
  mapStyle: 'dark',
  charts: {},
  eventLabels: {},
  collisionLabels: {},
  markers: new Map(),
  statusCounts: { normal: 0, accel: 0, brake: 0, turn: 0, collision: 0 },
  severityFilter: 'all',
  typeFilter: 'all',
  panelCollapsed: false,
  focusedVin: null,
  focusCenterMs: null,
  focusWaypoints: null,
  focusPolyline: null,
  focusMarkers: [],
  dailyCrashes: null,
  dailyEvents: null,
  lastRiskDay: null,
  mode: 'full', // 'full' | 'road' | 'heatmap'
  road: null,
  roadRects: [],
  roadLabels: [],
  roadMask: null,
  activeSection: null,
  sectionAnalytics: {},
  sectionDaily: { A: null, B: null, C: null },
  lastSectionRiskDay: { A: null, B: null, C: null },
  heatLayer: null,
  heatZonePoly: null,
  heatmapPoints: null,
  heatmapCapped: false,
  heatEventType: 0,
  heatSpeedBracket: 0,
  heatHour: 24,
  staticMode: false
};

// Global base layer reference inside Leaflet map
export let baseTileLayer = null;
export function setBaseTileLayer(layer) {
  baseTileLayer = layer;
}

// ±N minutes of single-vehicle track shown during investigation
export const FOCUS_WINDOW_MIN = 3;

// 5 minutes of data time — enough to see an event flash past at any speed.
export const ACC_FLASH_WINDOW_MS = 5 * 60 * 1000;

/* ── Circle geometry helper ────────────────────────────── */
export function circleLatLngs(lat, lng, radiusM) {
  const n = 72, pts = [];
  for (let i = 0; i <= n; i++) {
    const a = (i / n) * 2 * Math.PI;
    const dlat = (radiusM * Math.cos(a)) / 111000;
    const dlng = (radiusM * Math.sin(a)) / (111000 * Math.cos((lat * Math.PI) / 180));
    pts.push([lat + dlat, lng + dlng]);
  }
  return pts;
}

/* ── Helpers ───────────────────────────────────────────── */
export const iso = ms => new Date(ms).toISOString().slice(0, 19);

export const fmtDate = ms =>
  ms
    ? new Date(ms)
        .toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })
        .toUpperCase()
    : '—';

export const fmtStamp = ms => {
  if (!ms) return '—';
  const d = new Date(ms);
  return fmtDate(ms) + ' | ' + d.toTimeString().slice(0, 8);
};

export const dirLabel = d => {
  if (d == null || isNaN(d)) return '—';
  const D = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  return D[Math.round(d / 22.5) % 16];
};

export const evtLabel = e => S.eventLabels[e] || 'Event ' + e;
export const colLabel = c => S.collisionLabels[c] || 'Type ' + c;
export const sevColor = s => (s === 'high' ? '#dc2626' : s === 'medium' ? '#f97316' : '#f59e0b');
export const riskLevel = s =>
  s <= 3 ? { l: 'LOW', c: 'risk-low' } : s <= 6 ? { l: 'MEDIUM', c: 'risk-medium' } : { l: 'HIGH', c: 'risk-high' };

export function arrowSvg(dir, color, size) {
  size = size || 16;
  return `<div style="width:${size}px;height:${size}px;transform:rotate(${dir || 0}deg);transition:transform .3s">
    <svg viewBox="0 0 20 20" width="${size}" height="${size}">
      <polygon points="10,2 17,17 10,13 3,17" fill="${color}" stroke="rgba(0,0,0,.3)" stroke-width="1"/>
    </svg></div>`;
}

export function vehicleColor(vin, evt, prevSpd, curSpd) {
  if (vin in S.accidentVins) return '#dc2626';
  const e = evt == null ? null : Number(evt);
  if (e === 1) return '#0d9488'; // Sudden Accel → teal
  if (e === 2) return '#f59e0b'; // Harsh Braking → amber
  if (e === 3) return '#8b5cf6'; // Sharp Turn → purple
  if (prevSpd != null && curSpd != null) {
    const d = curSpd - prevSpd;
    if (d > 5) return '#0d9488';
    if (d < -5) return '#f59e0b';
  }
  return '#3b82f6';
}

export function getDist(lat1, lon1, lat2, lon2) {
  const R = 6371e3;
  const p1 = (lat1 * Math.PI) / 180, p2 = (lat2 * Math.PI) / 180;
  const dp = ((lat2 - lat1) * Math.PI) / 180, dl = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dp / 2) * Math.sin(dp / 2) +
    Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// Standard ray-cast point-in-polygon for a closed ring [[lat,lon],...].
export function pointInPolygon(lat, lon, poly) {
  if (!poly || poly.length < 3) return false;
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const yi = poly[i][0], xi = poly[i][1], yj = poly[j][0], xj = poly[j][1];
    const intersect = yi > lat !== yj > lat && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}
