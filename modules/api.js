'use strict';

import { S, iso, pointInPolygon } from './state.js';
import { updateTypeFilterOptions, renderCollisionList } from './collisions.js';
import { renderSectionTiles, updateSectionRiskForCurrentDay } from './map.js';

/* ── API core ───────────────────────────────────────────── */
export async function api(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/* ── Bounding Box Helpers ───────────────────────────────── */
export function circleBbox() {
  const c = S.circleCenter, r = S.radiusM;
  if (!c) return { lat_min: 0, lat_max: 0, lon_min: 0, lon_max: 0 };
  const dLa = r / 111000;
  const dLo = r / (111000 * Math.cos((c.lat * Math.PI) / 180));
  return { lat_min: c.lat - dLa, lat_max: c.lat + dLa, lon_min: c.lng - dLo, lon_max: c.lng + dLo };
}

export function roadBbox() {
  if (!S.road || !S.road.sections.length) return circleBbox();
  let secs = S.road.sections;
  if (S.activeSection) secs = secs.filter(s => s.id === S.activeSection);
  let la_lo = Infinity, la_hi = -Infinity, lo_lo = Infinity, lo_hi = -Infinity;
  secs.forEach(s => {
    if (s.lat_min < la_lo) la_lo = s.lat_min;
    if (s.lat_max > la_hi) la_hi = s.lat_max;
    if (s.lon_min < lo_lo) lo_lo = s.lon_min;
    if (s.lon_max > lo_hi) lo_hi = s.lon_max;
  });
  return { lat_min: la_lo, lat_max: la_hi, lon_min: lo_lo, lon_max: lo_hi };
}

export function activeBbox() {
  return S.mode === 'road' ? roadBbox() : circleBbox();
}

export function sectionFor(lat, lon) {
  if (!S.road) return null;
  for (const s of S.road.sections) {
    if (s.polygon && s.polygon.length) {
      if (pointInPolygon(lat, lon, s.polygon)) return s.id;
    } else if (lat >= s.lat_min && lat <= s.lat_max && lon >= s.lon_min && lon <= s.lon_max) {
      return s.id;
    }
  }
  return null;
}

/* ── Trajectory and Analytics Fetchers ──────────────────── */
export async function fetchTrajectory(tStart, tEnd) {
  const bb = activeBbox();
  const samp = S.speedMult <= 30 ? 1 : S.speedMult <= 60 ? 2 : S.speedMult <= 300 ? 5 : 10;
  S.sampleSec = samp; // keep state in sync
  const p = new URLSearchParams({
    t_start: iso(tStart),
    t_end: iso(tEnd),
    lat_min: bb.lat_min,
    lat_max: bb.lat_max,
    lon_min: bb.lon_min,
    lon_max: bb.lon_max,
    sample_sec: samp
  });
  return api('/api/trajectory?' + p);
}

export async function fetchAccidents() {
  const bb = activeBbox();
  const p = new URLSearchParams({
    lat_min: bb.lat_min,
    lat_max: bb.lat_max,
    lon_min: bb.lon_min,
    lon_max: bb.lon_max,
    t_start: iso(S.tStartMs),
    t_end: iso(S.tEndMs)
  });
  const d = await api('/api/accidents?' + p);
  S.accidents = d.accidents || [];
  S.accidentVins = d.accident_vins || {};
  updateTypeFilterOptions();
  renderCollisionList();
}

export async function fetchAnalytics() {
  const bb = activeBbox();
  const p = new URLSearchParams({
    lat_min: bb.lat_min,
    lat_max: bb.lat_max,
    lon_min: bb.lon_min,
    lon_max: bb.lon_max,
    t_start: iso(S.tStartMs),
    t_end: iso(S.tEndMs)
  });
  const cm = document.getElementById('cm-date').value;
  if (cm) p.set('countermeasure_date', cm);
  return api('/api/analytics?' + p);
}

export async function fetchRouteMatrix() {
  const p = new URLSearchParams({
    t_start: iso(S.tStartMs),
    t_end: iso(S.tEndMs)
  });
  return api('/api/route-matrix?' + p);
}

export async function fetchSectionAnalytics() {
  if (!S.road) return;
  const cm = document.getElementById('cm-date').value;
  // Mark every section as loading up-front
  S.road.sections.forEach(s => {
    S.sectionAnalytics[s.id] = { _loading: true };
  });
  renderSectionTiles();

  await Promise.allSettled(
    S.road.sections.map(async s => {
      const p = new URLSearchParams({
        lat_min: s.lat_min,
        lat_max: s.lat_max,
        lon_min: s.lon_min,
        lon_max: s.lon_max,
        t_start: iso(S.tStartMs),
        t_end: iso(S.tEndMs)
      });
      if (cm) p.set('countermeasure_date', cm);
      const t0 = performance.now();
      try {
        const d = await api('/api/analytics?' + p);
        S.sectionAnalytics[s.id] = d;
        const dc = {}, de = {};
        (d.crash_frequency || []).forEach(x => {
          dc[x.date] = x.count;
        });
        (d.daily_events || []).forEach(x => {
          de[x.date] = x.count;
        });
        S.sectionDaily[s.id] = { crashes: dc, events: de };
        S.lastSectionRiskDay[s.id] = null;
        console.log(`[analytics] section ${s.id} ✓ ${Math.round(performance.now() - t0)}ms`);
      } catch (e) {
        console.error(`[analytics] section ${s.id} ✕`, e);
        S.sectionAnalytics[s.id] = { _error: e.message || 'Request failed' };
      }
      renderSectionTiles();
      updateSectionRiskForCurrentDay();
    })
  );
}
