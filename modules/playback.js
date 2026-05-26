'use strict';

import { S, iso, fmtStamp, evtLabel, colLabel, arrowSvg, vehicleColor, getDist, dirLabel } from './state.js';
import { map, updateSectionRiskForCurrentDay } from './map.js';
import { activeBbox, fetchTrajectory, sectionFor } from './api.js';
import { clearAccMarkers, renderAccidents, applyFocusStyling } from './collisions.js';
import { updateRiskForCurrentDay } from './charts.js';

/* ── Timeline Display and Toast Helper ──────────────────── */
export function setTime(ms) {
  S.curMs = Math.max(S.tStartMs, Math.min(S.tEndMs, ms));
  const pct = (S.curMs - S.tStartMs) / (S.tEndMs - S.tStartMs);
  const tl = document.getElementById('timeline');
  if (tl) tl.value = Math.round(pct * 1000);
  const disp = document.getElementById('ts-display');
  if (disp) disp.textContent = fmtStamp(S.curMs);
}

export function showToast(msg, color) {
  let t = document.getElementById('traj-toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'traj-toast';
    t.style.cssText =
      'position:fixed;top:54px;left:50%;transform:translateX(-50%);' +
      'background:#1e293b;color:#f8fafc;font-size:12px;padding:6px 14px;border-radius:6px;' +
      'z-index:9999;pointer-events:none;border:1px solid rgba(255,255,255,.15);transition:opacity .4s';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.borderColor = color || 'rgba(255,255,255,.15)';
  t.style.opacity = '1';
  clearTimeout(t._hide);
  t._hide = setTimeout(() => {
    t.style.opacity = '0';
  }, 4000);
}

/* ── Asynchronous Trajectory Window Pre-fetching ────────── */
export async function loadTrajectoryWindow(centerMs) {
  const halfWin = S.speedMult <= 30 ? 120000 : S.speedMult <= 60 ? 300000 : S.speedMult <= 300 ? 900000 : 1800000;
  const tS = centerMs - halfWin, tE = centerMs + halfWin;
  if (S.cacheStart && S.cacheEnd && tS >= S.cacheStart && tE <= S.cacheEnd) return;
  S.prefetching = true;
  try {
    const d = await fetchTrajectory(tS, tE);
    S.trajs = d.trajectories || {};
    S.cacheStart = tS;
    S.cacheEnd = tE;
    const n = Object.keys(S.trajs).length;
    console.log(`[trajectory] ${n} vehicles loaded for window ${new Date(tS).toISOString()} → ${new Date(tE).toISOString()}`);
    if (n === 0) showToast('⚠ No vehicle data in this time window — try scrubbing forward', '#f59e0b');
  } catch (e) {
    console.error('trajectory fetch:', e);
    showToast('✕ Failed to load vehicle data: ' + e.message, '#dc2626');
  }
  S.prefetching = false;
}

/* ── Chronological Path Interpolation (ray-casting helper) ── */
export function interp(wps, tMs) {
  if (!wps || !wps.length) return null;
  const t0 = new Date(wps[0].t).getTime();
  if (tMs < t0) return null;

  let i = 0;
  while (i < wps.length - 1 && new Date(wps[i + 1].t).getTime() <= tMs) i++;
  const a = wps[i], tA = new Date(a.t).getTime();

  if (i >= wps.length - 1) {
    const el = (tMs - tA) / 1000;
    const disappearAfter = Math.max(60, S.sampleSec * 20);
    if (el > disappearAfter || el < 0) return null;
    const sm = (a.s || 0) / 3.6, dr = (a.d * Math.PI) / 180;
    return {
      lat: a.la + (sm * el * Math.cos(dr)) / 111000,
      lon: a.lo + (sm * el * Math.sin(dr)) / (111000 * Math.cos((a.la * Math.PI) / 180)),
      dir: a.d,
      spd: a.s,
      evt: a.e,
      col: a.c,
      prevSpd: a.s
    };
  }

  const b = wps[i + 1], tB = new Date(b.t).getTime(), gap = tB - tA, frac = Math.max(0, Math.min(1, (tMs - tA) / gap));

  let spdA = a.s, spdB = b.s;
  if (spdA == null || spdB == null) {
    const dM = getDist(a.la, a.lo, b.la, b.lo);
    const calcSpd = (dM / (gap / 1000)) * 3.6;
    if (spdA == null) spdA = calcSpd;
    if (spdB == null) spdB = calcSpd;
  }

  if (gap > 10000) {
    const el = (tMs - tA) / 1000, sm = (spdA || 0) / 3.6, dr = (a.d * Math.PI) / 180;
    let dDir = b.d - a.d;
    if (dDir > 180) dDir -= 360;
    if (dDir < -180) dDir += 360;
    return {
      lat: a.la + (sm * el * Math.cos(dr)) / 111000,
      lon: a.lo + (sm * el * Math.sin(dr)) / (111000 * Math.cos((a.la * Math.PI) / 180)),
      dir: a.d + dDir * frac,
      spd: spdA,
      evt: a.e,
      col: a.c,
      prevSpd: spdA
    };
  }

  let dDir = b.d - a.d;
  if (dDir > 180) dDir -= 360;
  if (dDir < -180) dDir += 360;
  return {
    lat: a.la + (b.la - a.la) * frac,
    lon: a.lo + (b.lo - a.lo) * frac,
    dir: a.d + dDir * frac,
    spd: spdA + (spdB - spdA) * frac,
    evt: a.e,
    col: a.c,
    prevSpd: spdA
  };
}

/* ── Render Frame ───────────────────────────────────────── */
export function renderFrame() {
  if (!S.curMs) return;
  if (S.staticMode) {
    S.markers.forEach((m, vin) => {
      m.remove();
    });
    S.markers.clear();
    S.statusCounts = { normal: 0, accel: 0, brake: 0, turn: 0, collision: 0 };
    updateStatusUI();
    clearAccMarkers();
    updateRiskForCurrentDay();
    if (S.mode === 'road') updateSectionRiskForCurrentDay();
    return;
  }
  const counts = { normal: 0, accel: 0, brake: 0, turn: 0, collision: 0 };
  const seen = new Set();
  for (const [vin, wps] of Object.entries(S.trajs)) {
    const p = interp(wps, S.curMs);
    if (!p) {
      if (S.markers.has(vin)) {
        S.markers.get(vin).remove();
        S.markers.delete(vin);
      }
      continue;
    }
    if (S.mode === 'road') {
      const sec = sectionFor(p.lat, p.lon);
      const inRoad = sec !== null;
      const inActive = !S.activeSection || sec === S.activeSection;
      if (!inRoad || !inActive) {
        if (S.markers.has(vin)) {
          S.markers.get(vin).remove();
          S.markers.delete(vin);
        }
        continue;
      }
    } else if (S.circleCenter && getDist(p.lat, p.lon, S.circleCenter.lat, S.circleCenter.lng) > S.radiusM) {
      if (S.markers.has(vin)) {
        S.markers.get(vin).remove();
        S.markers.delete(vin);
      }
      continue;
    }
    seen.add(vin);
    const col = vehicleColor(vin, p.evt, p.prevSpd, p.spd);
    const isCol = vin in S.accidentVins && S.curMs >= new Date(S.accidentVins[vin]).getTime();
    if (isCol) counts.collision++;
    else if (col === '#0d9488') counts.accel++;
    else if (col === '#f59e0b') counts.brake++;
    else if (col === '#8b5cf6') counts.turn++;
    else counts.normal++;

    const icon = L.divIcon({
      className: '',
      html: arrowSvg(p.dir, col),
      iconSize: [16, 16],
      iconAnchor: [8, 8]
    });
    const spdStr = p.spd != null && !isNaN(p.spd) ? Math.round(p.spd) + ' km/h' : '—';
    const dirStr = p.dir != null && !isNaN(p.dir) ? `${Math.round(p.dir)}° ${dirLabel(p.dir)}` : '—';
    const popup =
      `<b>${vin.slice(-8)}</b><br>${spdStr} | ${dirStr}` +
      (p.evt ? `<br><span style="color:#f59e0b">${evtLabel(p.evt)}</span>` : '') +
      (p.col ? `<br><b style="color:#dc2626">${colLabel(p.col)}</b>` : '');
    if (S.markers.has(vin)) {
      const m = S.markers.get(vin);
      m.setLatLng([p.lat, p.lon]);
      m.setIcon(icon);
      m.getPopup()?.setContent(popup);
    } else {
      const m = L.marker([p.lat, p.lon], { icon, zIndexOffset: 100 }).bindPopup(popup).addTo(map);
      S.markers.set(vin, m);
    }
  }
  S.markers.forEach((m, vin) => {
    if (!seen.has(vin)) {
      m.remove();
      S.markers.delete(vin);
    }
  });
  S.statusCounts = counts;
  updateStatusUI();
  renderAccidents();
  applyFocusStyling();
  updateRiskForCurrentDay();
  if (S.mode === 'road') updateSectionRiskForCurrentDay();
}

export function updateStatusUI() {
  const c = S.statusCounts, tot = c.normal + c.accel + c.brake + c.turn + c.collision;
  document.getElementById('sp-total').textContent = tot;
  document.getElementById('sp-normal').textContent = c.normal;
  document.getElementById('sp-accel').textContent = c.accel;
  document.getElementById('sp-brake').textContent = c.brake;
  document.getElementById('sp-turn').textContent = c.turn;
  document.getElementById('sp-collision').textContent = c.collision;
}

/* ── Playback Controls ──────────────────────────────────── */
export function play() {
  if (S.playing) return;
  S.playing = true;
  S.lastWall = performance.now();
  document.getElementById('btn-play').classList.add('active');
  document.getElementById('btn-pause').classList.remove('active');
  requestAnimationFrame(tick);
}

export function tick(now) {
  if (!S.playing) return;
  const wallDelta = now - S.lastWall;
  S.lastWall = now;
  const dataDelta = wallDelta * S.speedMult;
  const next = S.curMs + dataDelta;
  if (next >= S.tEndMs) {
    setTime(S.tEndMs);
    pause();
    renderFrame();
    return;
  }
  setTime(next);
  renderFrame();
  if (S.cacheEnd && S.curMs > S.cacheEnd - 60000 && !S.prefetching) loadTrajectoryWindow(S.curMs);
  requestAnimationFrame(tick);
}

export function pause() {
  if (!S.playing) return;
  S.playing = false;
  document.getElementById('btn-play').classList.remove('active');
  document.getElementById('btn-pause').classList.add('active');
}

export function stop() {
  pause();
  document.getElementById('btn-pause').classList.remove('active');
  setTime(S.tStartMs);
  S.markers.forEach(m => m.remove());
  S.markers.clear();
  clearAccMarkers();
  S.accMarkers = [];
  renderFrame();
}
