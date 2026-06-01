'use strict';

import { S, iso, fmtStamp, evtLabel } from './state.js';
import { map } from './map.js';
import { api, fetchRouteTrips, fetchSectionAnalytics } from './api.js';
import { drawHoverRoute, clearHoverRoute } from './map.js';
import { pause, setTime, loadTrajectoryWindow, renderFrame } from './playback.js';
import { clearFocus } from './collisions.js';
import { fetchHeatmapData } from './heatmap.js';

// Local storage for routes module state
export const R = {
  activeRoute: null,
  eventFilter: 'all',
  trips: [],
  eventMarkers: [],
  focusPolyline: null,
  focusMarkers: [],
  focusedVin: null,
  focusCenterMs: null,
  focusWaypoints: null
};

/* ── Initialize Routes Panel ───────────────────────────── */
export function initRoutesPanel() {
  const filterSel = document.getElementById('route-event-filter');
  if (filterSel) {
    filterSel.addEventListener('change', function () {
      R.eventFilter = this.value;
      if (R.activeRoute) {
        refreshRouteTrips();
      }
    });
  }

  const collapseBtn = document.getElementById('btn-collapse-routes');
  if (collapseBtn) {
    collapseBtn.addEventListener('click', function () {
      const panel = document.getElementById('routes-panel');
      if (panel) {
        const isCollapsed = panel.classList.toggle('collapsed');
        collapseBtn.textContent = isCollapsed ? '▶' : '◀';
        
        const controls = document.getElementById('routes-controls');
        if (controls) {
          if (isCollapsed || !R.activeRoute) controls.classList.add('hidden');
          else controls.classList.remove('hidden');
        }
        
        setTimeout(() => map.invalidateSize(), 260);
      }
    });
  }

  const clearFocusBtn = document.getElementById('btn-clear-route-focus');
  if (clearFocusBtn) {
    clearFocusBtn.addEventListener('click', clearRouteFocus);
  }
}

/* ── Set Active Route ──────────────────────────────────── */
export async function setActiveRoute(routeId) {
  R.activeRoute = routeId;
  S.activeRoute = routeId; // sync with global state
  
  // Expand routes panel
  const panel = document.getElementById('routes-panel');
  const collapseBtn = document.getElementById('btn-collapse-routes');
  if (panel) {
    panel.classList.remove('collapsed');
    if (collapseBtn) collapseBtn.textContent = '◀';
  }

  const controls = document.getElementById('routes-controls');
  if (controls) controls.classList.remove('hidden');

  // Clear focus track if any
  clearRouteFocus();

  // Draw gates and centerline path on map
  drawHoverRoute(routeId);

  // Fetch and render trips
  await refreshRouteTrips();

  // Task 3: Sync heatmap points to this route if heatmap is enabled
  if (S.heatmapEnabled) {
    fetchHeatmapData();
  }

  // Task 1: Refresh section breakdown cards if road view mode is active
  if (S.mode === 'road') {
    fetchSectionAnalytics();
  }
}

/* ── Clear Active Route ────────────────────────────────── */
export function clearActiveRoute() {
  R.activeRoute = null;
  S.activeRoute = null; // sync with global state

  // Clear map graphics
  clearHoverRoute();
  clearRouteEventMarkers();
  clearRouteFocus();

  // Collapse routes panel or show empty state
  const panel = document.getElementById('routes-panel');
  const collapseBtn = document.getElementById('btn-collapse-routes');
  if (panel) {
    panel.classList.add('collapsed');
    if (collapseBtn) collapseBtn.textContent = '▶';
  }

  const controls = document.getElementById('routes-controls');
  if (controls) controls.classList.add('hidden');

  const listEl = document.getElementById('routes-list');
  if (listEl) {
    listEl.innerHTML = '<div class="routes-empty">Select a route in the matrix to view trips.</div>';
  }

  const countEl = document.getElementById('route-trips-count');
  if (countEl) countEl.textContent = '0';

  // Clear matrix highlights
  document.querySelectorAll('.rm-row').forEach(row => row.classList.remove('active'));

  // Task 3: Sync heatmap points by clearing route filter if heatmap is enabled
  if (S.heatmapEnabled) {
    fetchHeatmapData();
  }

  // Task 1: Refresh section breakdown cards if road view mode is active
  if (S.mode === 'road') {
    fetchSectionAnalytics();
  }
}

/* ── Refresh Trips & Markers ───────────────────────────── */
export async function refreshRouteTrips() {
  if (!R.activeRoute) return;

  const listEl = document.getElementById('routes-list');
  const countEl = document.getElementById('route-trips-count');
  if (listEl) {
    listEl.innerHTML = '<div class="routes-empty"><div class="section-spinner"></div>Loading trips…</div>';
  }

  try {
    const res = await fetchRouteTrips(R.activeRoute, R.eventFilter);
    R.trips = res.trips || [];
    
    if (countEl) countEl.textContent = R.trips.length.toLocaleString();
    
    renderRouteTripsList();
  } catch (err) {
    console.error('Failed to fetch route trips:', err);
    if (listEl) {
      listEl.innerHTML = `<div class="routes-empty" style="color:var(--sev-high)">Error loading trips:<br>${err.message || err}</div>`;
    }
  }
}

/* ── Render Trips List ─────────────────────────────────── */
function renderRouteTripsList() {
  const listEl = document.getElementById('routes-list');
  if (!listEl) return;

  if (R.trips.length === 0) {
    listEl.innerHTML = '<div class="routes-empty">No trips match the current filter.</div>';
    return;
  }

  listEl.innerHTML = R.trips
    .map((trip, idx) => {
      const vinTail = trip.vin ? trip.vin.slice(-6) : '—';
      const d = new Date(trip.t_start);
      const dateStr = d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
      const timeStr = d.toTimeString().slice(0, 8);
      const maxSpd = trip.max_speed != null ? `${Math.round(trip.max_speed)} km/h` : '—';
      
      // Determine primary event category for icon/dot styling
      let primaryClass = 'trip-normal';
      let badges = [];
      
      const hasAccel = trip.events.some(e => e.event_type === 1);
      const hasBrake = trip.events.some(e => e.event_type === 2);
      const hasTurn = trip.events.some(e => e.event_type === 3);

      if (hasBrake) { primaryClass = 'trip-brake'; badges.push('<span class="trip-badge badge-brake">Harsh Brake</span>'); }
      if (hasTurn) { primaryClass = 'trip-turn'; badges.push('<span class="trip-badge badge-turn">Sharp Turn</span>'); }
      if (hasAccel) { primaryClass = 'trip-accel'; badges.push('<span class="trip-badge badge-accel">Sudden Accel</span>'); }
      
      if (badges.length === 0) {
        badges.push('<span class="trip-badge badge-normal">Normal</span>');
      }

      const isFocused = R.focusedVin === trip.vin && R.focusCenterMs === d.getTime();

      return `
        <div class="route-trip-item${isFocused ? ' active' : ''}" data-idx="${idx}">
          <span class="trip-dot ${primaryClass}"></span>
          <div class="trip-body">
            <div class="trip-title">VIN ···${vinTail}</div>
            <div class="trip-meta">
              ${dateStr} · ${timeStr} · <b>${trip.origin} ➔ ${trip.destination}</b><span class="sep">·</span>Max: ${maxSpd}
            </div>
            <div class="trip-badge-wrap">
              ${badges.join('')}
            </div>
          </div>
        </div>
      `;
    })
    .join('');

  listEl.querySelectorAll('.route-trip-item').forEach(el => {
    el.addEventListener('click', () => {
      const idx = parseInt(el.dataset.idx, 10);
      focusTrip(R.trips[idx]);
    });
  });
}

/* ── Draw Event Markers on Map ────────────────────────── */
function drawRouteEventMarkers(trip) {
  clearRouteEventMarkers();
  if (!trip || !trip.events) return;

  // If the filter is 'normal', we don't display event markers on the map
  if (R.eventFilter === 'normal') return;

  trip.events.forEach(ev => {
    // Color coding matching state.js / dashboard legend
    let color = '#3b82f6'; // normal
    let label = evtLabel(ev.event_type);
    
    if (ev.event_type === 1) color = '#0d9488'; // Accel
    else if (ev.event_type === 2) color = '#f59e0b'; // Brake
    else if (ev.event_type === 3) color = '#8b5cf6'; // Turn

    const markerHtml = `
      <div style="width:20px;height:20px;display:flex;align-items:center;justify-content:center;
        background:rgba(255,255,255,.9);border:2px solid ${color};border-radius:50%;
        box-shadow:0 1px 5px rgba(0,0,0,.4)">
        <div style="width:8px;height:8px;background:${color};border-radius:50%"></div>
      </div>
    `;

    const popup = `
      <b style="color:${color}">${label}</b><br>
      VIN: <b>···${trip.vin.slice(-6)}</b><br>
      Time: ${ev.timestamp.replace('T', ' ').slice(0, 19)}<br>
      Speed: <b>${Math.round(ev.speed)} km/h</b>
    `;

    const icon = L.divIcon({
      className: '',
      html: markerHtml,
      iconSize: [20, 20],
      iconAnchor: [10, 10]
    });

    const m = L.marker([ev.lat, ev.lon], { icon })
      .bindPopup(popup)
      .addTo(map);

    R.eventMarkers.push(m);
  });
}

function clearRouteEventMarkers() {
  R.eventMarkers.forEach(m => {
    if (m && map.hasLayer(m)) m.remove();
  });
  R.eventMarkers = [];
}

/* ── Focus Single Vehicle Trip Trajectory ───────────────── */
export async function focusTrip(trip) {
  if (!trip) return;

  // Clear any active collision highlight to prevent dual overlay conflicts
  clearFocus();

  const tStartMs = new Date(trip.t_start).getTime();
  const tEndMs = new Date(trip.t_end).getTime();
  const tMidMs = Math.round((tStartMs + tEndMs) / 2);

  pause();
  setTime(tStartMs);
  
  R.focusedVin = trip.vin;
  S.focusedVin = trip.vin; // sync with global state for animation tracking
  R.focusCenterMs = tStartMs;

  const bar = document.getElementById('routes-focus-bar');
  const label = document.getElementById('routes-focus-label');
  if (bar) bar.classList.remove('hidden');
  if (label) {
    label.textContent = `Highlighting ···${trip.vin.slice(-6)} · ${new Date(tStartMs).toTimeString().slice(0, 8)}`;
  }

  // Calculate window size in minutes to completely cover the trip duration (min 5 minutes, max 30)
  const durationMin = Math.ceil((tEndMs - tStartMs) / (2 * 60 * 1000)) + 2;
  const winMin = Math.max(5, Math.min(30, durationMin));

  try {
    const data = await api(
      `/api/vehicle-trajectory?vin=${encodeURIComponent(trip.vin)}&t_center=${encodeURIComponent(
        new Date(tMidMs).toISOString()
      )}&window_minutes=${winMin}`
    );
    R.focusWaypoints = data.waypoints || [];
    S.focusWaypoints = data.waypoints || []; // sync with global state for animation tracking
    drawTripFocusedTrack(trip);
    drawRouteEventMarkers(trip);
  } catch (err) {
    console.error('Failed to load focus trip path:', err);
  }

  S.cacheStart = null;
  S.cacheEnd = null;
  await loadTrajectoryWindow(S.curMs);
  renderFrame();
  renderRouteTripsList();
}

function drawTripFocusedTrack(trip) {
  clearRouteFocusedTrack();

  const wps = R.focusWaypoints;
  if (!wps || !wps.length) return;

  const startTarget = new Date(trip.t_start).getTime();
  const endTarget = new Date(trip.t_end).getTime();

  let startWP = wps[0];
  let endWP = wps[wps.length - 1];
  let minStartDiff = Infinity;
  let minEndDiff = Infinity;

  wps.forEach(w => {
    const t = new Date(w.t).getTime();
    const startDiff = Math.abs(t - startTarget);
    const endDiff = Math.abs(t - endTarget);
    if (startDiff < minStartDiff) {
      minStartDiff = startDiff;
      startWP = w;
    }
    if (endDiff < minEndDiff) {
      minEndDiff = endDiff;
      endWP = w;
    }
  });

  // Filter waypoints strictly between gate crossings for highlighting
  let activeWps = wps.filter(w => {
    const t = new Date(w.t).getTime();
    return t >= startTarget && t <= endTarget;
  });
  if (activeWps.length < 2) {
    activeWps = [startWP, endWP];
  }

  const latlngs = activeWps.map(w => [w.la, w.lo]);

  // Draw coral trajectory line matching investigations
  R.focusPolyline = L.polyline(latlngs, {
    color: '#c7613c', // Claude Coral
    weight: 4,
    opacity: 0.95,
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

  R.focusMarkers.push(mk([startWP.la, startWP.lo], 'start', `Trip origin gate ${trip.origin}`));
  R.focusMarkers.push(mk([endWP.la, endWP.lo], 'end', `Trip destination gate ${trip.destination}`));

  if (latlngs.length > 1) {
    map.fitBounds(R.focusPolyline.getBounds(), { padding: [60, 60], maxZoom: 18, animate: true });
  }
}

function clearRouteFocusedTrack() {
  if (R.focusPolyline) {
    R.focusPolyline.remove();
    R.focusPolyline = null;
  }
  R.focusMarkers.forEach(m => m.remove());
  R.focusMarkers = [];
}

export function clearRouteFocus() {
  R.focusedVin = null;
  S.focusedVin = null; // sync with global state to clear animation tracking
  R.focusCenterMs = null;
  R.focusWaypoints = null;
  S.focusWaypoints = null; // sync with global state to clear animation tracking
  clearRouteFocusedTrack();
  clearRouteEventMarkers();

  const focusBar = document.getElementById('routes-focus-bar');
  if (focusBar) focusBar.classList.add('hidden');

  renderRouteTripsList();
  renderFrame();
}
