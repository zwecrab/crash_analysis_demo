'use strict';

import { S, FOCUS_WINDOW_MIN, ACC_FLASH_WINDOW_MS, colLabel, evtLabel, sevColor } from './state.js';
import { map } from './map.js';
import { api, activeBbox, fetchAccidents, fetchAnalytics, sectionFor } from './api.js';
import { updateCharts } from './charts.js';
import { loadTrajectoryWindow, renderFrame, pause, setTime } from './playback.js';

/* ── Clear Markers ──────────────────────────────────────── */
export function clearAccMarkers() {
  S.accMarkers.forEach(m => {
    if (m && map.hasLayer(m)) m.remove();
  });
  S.accMarkers = [];
}

export function _makeAccIcon(sc) {
  return L.divIcon({
    className: '',
    html: `<div style="width:28px;height:28px;display:flex;align-items:center;justify-content:center;
      background:rgba(220,38,38,.18);border:2px solid ${sc};border-radius:50%;
      box-shadow:0 0 10px 3px ${sc}88,0 0 0 4px rgba(220,38,38,.08)">
      <svg width="13" height="13" viewBox="0 0 10 10"><polygon points="5,0.5 9.5,9.5 0.5,9.5" fill="${sc}" stroke="#fff" stroke-width=".5"/></svg></div>`,
    iconSize: [28, 28],
    iconAnchor: [14, 14]
  });
}

/* ── Render Accidents ───────────────────────────────────── */
export function renderAccidents() {
  if (S.staticMode || S.accMode === 'hidden') {
    clearAccMarkers();
    return;
  }
  const now = S.curMs;
  S.accidents.forEach((acc, i) => {
    const accMs = new Date(acc.timestamp).getTime();
    const inPast = accMs <= now;
    const inFlashWindow = inPast && now - accMs <= ACC_FLASH_WINDOW_MS;
    const timeOk = S.accMode === 'persist' ? inPast : inFlashWindow;
    const sevOk = S.severityFilter === 'all' || acc.severity === S.severityFilter;
    const typeOk = S.typeFilter === 'all' || String(acc.collision_type) === String(S.typeFilter);

    let roadOk = true;
    if (S.mode === 'road') {
      const sec = sectionFor(acc.lat, acc.lon);
      roadOk = sec !== null && (!S.activeSection || sec === S.activeSection);
    }
    const shouldShow = timeOk && sevOk && typeOk && roadOk;

    if (!shouldShow) {
      if (S.accMarkers[i]) {
        if (map.hasLayer(S.accMarkers[i])) S.accMarkers[i].remove();
        delete S.accMarkers[i];
      }
      return;
    }
    if (S.accMarkers[i] && map.hasLayer(S.accMarkers[i])) return;

    const sc = sevColor(acc.severity);
    const popup = `<b>Collision</b><br>${acc.timestamp.replace('T', ' ').slice(0, 19)}<br>
      ${acc.collision_label}<br>Severity: <b>${acc.severity.toUpperCase()}</b><br>
      Speed: ${acc.speed != null ? acc.speed + ' km/h' : '—'}<br>G: ${acc.gx}`;
    S.accMarkers[i] = L.marker([acc.lat, acc.lon], { icon: _makeAccIcon(sc), zIndexOffset: 2000 })
      .bindPopup(popup)
      .addTo(map);
  });
}

/* ── UI Panel Wiring ────────────────────────────────────── */
export function updateTypeFilterOptions() {
  const sel = document.getElementById('collision-type-filter');
  if (!sel) return;
  const prev = sel.value;

  const seen = new Map();
  S.accidents.forEach(a => {
    if (a.collision_type != null && !seen.has(String(a.collision_type)))
      seen.set(String(a.collision_type), a.collision_label || 'Type ' + a.collision_type);
  });
  const opts = ['<option value="all">All Types</option>'];
  [...seen.entries()]
    .sort((a, b) => a[0] - b[0])
    .forEach(([t, l]) => {
      opts.push(`<option value="${t}">${l.replace(/\s*\(.*?\)\s*/, ' ').trim()}</option>`);
    });
  sel.innerHTML = opts.join('');
  sel.value = [...seen.keys(), 'all'].includes(prev) ? prev : 'all';
  S.typeFilter = sel.value;
}

export function renderCollisionList() {
  const listEl = document.getElementById('collision-list');
  const countEl = document.getElementById('collision-count');
  if (!listEl) return;

  const filtered = S.accidents.filter(a => {
    const sevOk = S.severityFilter === 'all' || a.severity === S.severityFilter;
    const typeOk = S.typeFilter === 'all' || String(a.collision_type) === String(S.typeFilter);
    let roadOk = true;
    if (S.mode === 'road') {
      const sec = sectionFor(a.lat, a.lon);
      roadOk = sec !== null && (!S.activeSection || sec === S.activeSection);
    }
    return sevOk && typeOk && roadOk;
  });

  const sorted = [...filtered].sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
  if (countEl) countEl.textContent = sorted.length;

  if (!sorted.length) {
    listEl.innerHTML = '<div class="collision-empty">No collisions match the current filters.</div>';
    return;
  }

  listEl.innerHTML = sorted
    .map(acc => {
      const origIdx = S.accidents.indexOf(acc);
      const sev = acc.severity || 'unknown';
      const d = new Date(acc.timestamp);
      const dateStr = d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
      const timeStr = d.toTimeString().slice(0, 8);
      const spd = acc.speed != null ? `${Math.round(acc.speed)} km/h` : '—';
      const g = acc.gx != null ? `${Math.abs(acc.gx).toFixed(1)}G` : '—';
      const vinTail = acc.vin ? acc.vin.slice(-6) : '—';
      const label = (acc.collision_label || 'Collision').replace(/\s*\(.*?\)\s*/, ' ').trim();
      const isFocused = S.focusedVin === acc.vin && S.focusCenterMs === d.getTime();
      return `<div class="collision-item${isFocused ? ' active' : ''}" data-idx="${origIdx}">
      <span class="collision-dot sev-${sev}"></span>
      <div class="collision-body">
        <div class="collision-title">${label}</div>
        <div class="collision-meta">${dateStr} · ${timeStr}<span class="sep">·</span>${spd}<span class="sep">·</span>${g}
          <div class="collision-vin">VIN ···${vinTail}</div>
        </div>
      </div>
      <span class="collision-severity-tag sev-${sev}">${sev}</span>
    </div>`;
    })
    .join('');

  listEl.querySelectorAll('.collision-item').forEach(el => {
    el.addEventListener('click', () => {
      const idx = parseInt(el.dataset.idx, 10);
      focusCollision(idx);
    });
  });
}

/* ── Collision Focus / Investigation Mode ────────────────── */
export async function focusCollision(idx) {
  const acc = S.accidents[idx];
  if (!acc) return;
  const tMs = new Date(acc.timestamp).getTime();

  pause();
  setTime(tMs);
  S.focusedVin = acc.vin;
  S.focusCenterMs = tMs;

  const bar = document.getElementById('collision-focus-bar');
  const label = document.getElementById('collision-focus-label');
  if (bar) bar.classList.remove('hidden');
  if (label) {
    label.textContent = `Investigating ···${acc.vin.slice(-6)} · ${new Date(tMs)
      .toTimeString()
      .slice(0, 8)} (±${FOCUS_WINDOW_MIN} min)`;
  }

  try {
    const data = await api(
      `/api/vehicle-trajectory?vin=${encodeURIComponent(acc.vin)}&t_center=${encodeURIComponent(
        acc.timestamp
      )}&window_minutes=${FOCUS_WINDOW_MIN}`
    );
    S.focusWaypoints = data.waypoints || [];
    drawFocusTrack(tMs);
  } catch (e) {
    console.error('focus track:', e);
    // showToast is alert fallback
  }

  S.cacheStart = null;
  S.cacheEnd = null;
  await loadTrajectoryWindow(S.curMs);
  renderFrame();
  renderCollisionList();
}

export function drawFocusTrack(collisionMs) {
  if (S.focusPolyline) {
    S.focusPolyline.remove();
    S.focusPolyline = null;
  }
  S.focusMarkers.forEach(m => m.remove());
  S.focusMarkers = [];

  const wps = S.focusWaypoints;
  if (!wps || !wps.length) return;
  const latlngs = wps.map(w => [w.la, w.lo]);

  S.focusPolyline = L.polyline(latlngs, {
    color: '#c7613c',
    weight: 3.5,
    opacity: 0.9,
    lineCap: 'round',
    lineJoin: 'round',
    className: 'focus-track'
  }).addTo(map);

  const mk = (latlng, cls, title) =>
    L.marker(latlng, {
      icon: L.divIcon({
        className: '',
        iconSize: [14, 14],
        iconAnchor: [7, 7],
        html: `<div class="focus-anchor ${cls}" title="${title}"></div>`
      }),
      zIndexOffset: 2500
    }).addTo(map);
  S.focusMarkers.push(mk(latlngs[0], 'start', 'Track start'));
  S.focusMarkers.push(mk(latlngs[latlngs.length - 1], 'end', 'Track end'));

  let impactIdx = 0, best = Infinity;
  wps.forEach((w, i) => {
    const d = Math.abs(new Date(w.t).getTime() - collisionMs);
    if (d < best) {
      best = d;
      impactIdx = i;
    }
  });
  S.focusMarkers.push(mk(latlngs[impactIdx], 'impact', 'Impact'));

  if (latlngs.length > 1) {
    map.fitBounds(S.focusPolyline.getBounds(), { padding: [70, 70], maxZoom: 18, animate: true });
  }
}

export function clearFocus() {
  S.focusedVin = null;
  S.focusCenterMs = null;
  S.focusWaypoints = null;
  if (S.focusPolyline) {
    S.focusPolyline.remove();
    S.focusPolyline = null;
  }
  S.focusMarkers.forEach(m => m.remove());
  S.focusMarkers = [];
  const focusBar = document.getElementById('collision-focus-bar');
  if (focusBar) focusBar.classList.add('hidden');
  S.markers.forEach(m => {
    const el = m.getElement();
    if (el) {
      el.classList.remove('vehicle-faded');
      el.classList.remove('vehicle-focused');
    }
  });
  renderCollisionList();
  renderFrame();
}

export function applyFocusStyling() {
  if (!S.focusedVin) {
    S.markers.forEach(m => {
      const el = m.getElement();
      if (el) {
        el.classList.remove('vehicle-faded');
        el.classList.remove('vehicle-focused');
      }
    });
    return;
  }
  S.markers.forEach((m, vin) => {
    const el = m.getElement();
    if (!el) return;
    if (vin === S.focusedVin) {
      el.classList.add('vehicle-focused');
      el.classList.remove('vehicle-faded');
    } else {
      el.classList.add('vehicle-faded');
      el.classList.remove('vehicle-focused');
    }
  });
}
