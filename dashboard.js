'use strict';

/* ── Imports ────────────────────────────────────────────── */
import { S, iso, fmtDate, fmtStamp, getDist } from './modules/state.js';
import { api, activeBbox, fetchAccidents, fetchAnalytics } from './modules/api.js';
import { initCharts, updateCharts, updateRiskForCurrentDay } from './modules/charts.js';
import { map, createCircle, onCircleChange, drawRoadGeometry, clearRoadGeometry, setActiveSection, enterRoadMode, exitRoadMode, toggleMapStyle } from './modules/map.js';
import { clearAccMarkers, renderCollisionList, clearFocus, renderAccidents } from './modules/collisions.js';
import { fetchHeatmapData, enterHeatmapMode, exitHeatmapMode } from './modules/heatmap.js';
import { play, pause, stop, setTime, loadTrajectoryWindow, renderFrame, showToast } from './modules/playback.js';

/* ── Precision Time Jumping (DOM) ────────────────────────── */
function _pad(n) {
  return String(n).padStart(2, '0');
}

function _toLocalDTString(ms) {
  const d = new Date(ms);
  return `${d.getFullYear()}-${_pad(d.getMonth() + 1)}-${_pad(d.getDate())}T${_pad(d.getHours())}:${_pad(d.getMinutes())}:${_pad(d.getSeconds())}`;
}

function prefillTimeJumpFromCur() {
  const inp = document.getElementById('time-jump-input');
  if (!inp || !S.curMs) return;
  inp.value = _toLocalDTString(S.curMs);
  inp.min = _toLocalDTString(S.tStartMs);
  inp.max = _toLocalDTString(S.tEndMs);
}

function doTimeJump() {
  if (S.staticMode) return;
  const inp = document.getElementById('time-jump-input');
  if (!inp || !inp.value) {
    showToast('Enter a date & time first', '#d27a3f');
    return;
  }
  const target = new Date(inp.value).getTime();
  if (isNaN(target)) {
    showToast('Invalid date/time', '#b73d3d');
    return;
  }
  if (target < S.tStartMs || target > S.tEndMs) {
    showToast(`Out of range · dataset is ${fmtStamp(S.tStartMs)} → ${fmtStamp(S.tEndMs)}`, '#d27a3f');
    return;
  }
  pause();
  setTime(target);
  S.cacheStart = null;
  S.cacheEnd = null;
  loadTrajectoryWindow(S.curMs).then(renderFrame);
  showToast(`→ Jumped to ${fmtStamp(target)}`, '#4a9080');
}

/* ── Interactive View Modes Switcher ────────────────────── */
async function switchMode(to) {
  if (S.mode === to) return;
  const from = S.mode;

  // Tear-down current view mode
  if (from === 'heatmap') await exitHeatmapMode(to);
  if (from === 'road') {
    if (to !== 'heatmap') {
      clearRoadGeometry();
      document.getElementById('section-panel').classList.add('hidden');
      S.activeSection = null;
    }
  }
  if (from === 'full' || from === 'road') {
    if (to !== 'heatmap') {
      if (S.circle) {
        S.circle.remove();
        S.circle = null;
      }
      if (S.mask) {
        S.mask.remove();
        S.mask = null;
      }
    }
  }

  // Clear tracking references and markers between modes
  S.markers.forEach(m => m.remove());
  S.markers.clear();
  clearAccMarkers();
  S.accMarkers = [];

  // Setup new mode
  if (to === 'full') await exitRoadMode();
  else if (to === 'road') await enterRoadMode();
  else if (to === 'heatmap') await enterHeatmapMode();

  // Sync button toggles
  document.getElementById('btn-mode-full').classList.toggle('active', to === 'full');
  document.getElementById('btn-mode-road').classList.toggle('active', to === 'road');
  document.getElementById('btn-mode-heatmap').classList.toggle('active', to === 'heatmap');
}

function setTimelineInteraction(enabled) {
  const ids = [
    'timeline',
    'btn-pause',
    'btn-play',
    'btn-stop',
    'btn-prev',
    'btn-next',
    'time-jump-input',
    'btn-time-jump',
    'speed-select'
  ];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    if (enabled) {
      el.removeAttribute('disabled');
      el.classList.remove('disabled');
    } else {
      el.setAttribute('disabled', 'true');
      el.classList.add('disabled');
    }
  });
}

/* ── Main Setup & Startup Sequence ──────────────────────── */
async function init() {
  const loadEl = document.getElementById('loading'),
    msg = document.getElementById('load-msg'),
    err = document.getElementById('load-err');
  try {
    initCharts();
    msg.textContent = 'Loading dataset metadata…';
    const meta = await api('/api/meta');
    if (!meta.t_start) throw new Error('Empty dataset');
    S.tStartMs = new Date(meta.t_start).getTime();
    S.tEndMs = new Date(meta.t_end).getTime();

    const suggestedMs = meta.t_suggested
      ? new Date(meta.t_suggested).getTime()
      : Math.round((S.tStartMs + S.tEndMs) / 2);
    S.curMs = Math.max(S.tStartMs, Math.min(S.tEndMs, suggestedMs));

    S.eventLabels = meta.event_labels || {};
    S.collisionLabels = meta.collision_labels || {};
    S.bounds = meta.bounds;
    document.getElementById('tl-start').textContent = fmtDate(S.tStartMs);
    document.getElementById('tl-end').textContent = fmtDate(S.tEndMs);
    document.getElementById('ts-display').textContent = fmtStamp(S.curMs);

    const b = S.bounds;
    map.setMinZoom(13);
    map.fitBounds([[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]], { padding: [30, 30] });

    let circleLat = (b.lat_min + b.lat_max) / 2,
      circleLng = (b.lon_min + b.lon_max) / 2,
      circleR = 500;
    try {
      const bs = await api('/api/blackspots');
      if (bs && bs.length) {
        circleLat = bs[0].lat;
        circleLng = bs[0].lon;
        circleR = bs[0].radius_m || 500;
      }
    } catch (e) {
      console.warn('Blackspots endpoint unavailable:', e);
    }
    createCircle(circleLat, circleLng, circleR);

    api('/api/road')
      .then(r => {
        S.road = r;
      })
      .catch(() => {});

    msg.textContent = 'Loading accidents…';
    await fetchAccidents();
    msg.textContent = 'Loading analytics…';
    const ad = await fetchAnalytics();
    updateCharts(ad);

    msg.textContent = 'Finding vehicle data…';
    await loadTrajectoryWindow(S.curMs);
    if (Object.keys(S.trajs).length === 0) {
      const stepMs = 2 * 3600 * 1000;
      let probe = S.tStartMs;
      while (probe <= S.tEndMs && Object.keys(S.trajs).length === 0) {
        probe += stepMs;
        S.curMs = Math.min(probe, S.tEndMs);
        S.cacheStart = null;
        S.cacheEnd = null;
        msg.textContent = `Scanning for traffic… ${fmtStamp(S.curMs)}`;
        await loadTrajectoryWindow(S.curMs);
      }
    }
    setTime(S.curMs);
    prefillTimeJumpFromCur();

    loadEl.classList.add('hidden');
    play();
  } catch (e) {
    msg.style.display = 'none';
    err.style.display = 'block';
    err.textContent = 'Error: ' + e.message;
    console.error(e);
  }
}

/* ── DOM Event Listeners Binding ────────────────────────── */
document.getElementById('btn-reset').addEventListener('click', async () => {
  if (S.mode === 'road') {
    const bb = S.road ? roadBbox() : null;
    if (bb) map.fitBounds([[bb.lat_min, bb.lon_min], [bb.lat_max, bb.lon_max]], { padding: [60, 60] });
    if (S.activeSection) {
      setActiveSection(null);
    }
    return;
  }
  if (!S.bounds) return;
  const b = S.bounds;
  map.fitBounds([[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]], { padding: [30, 30] });

  let lat = (b.lat_min + b.lat_max) / 2,
    lng = (b.lon_min + b.lon_max) / 2,
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
  onCircleChange();
});

document.getElementById('btn-toggle-static').addEventListener('click', function () {
  S.staticMode = !S.staticMode;
  if (S.staticMode) {
    this.classList.add('active');
    this.textContent = 'Active Data: Hidden';
    pause();
    setTimelineInteraction(false);
  } else {
    this.classList.remove('active');
    this.textContent = 'Hide Active Data';
    setTimelineInteraction(true);
  }
  renderFrame();
});

document.getElementById('timeline').addEventListener('input', e => {
  if (S.staticMode) return;
  const pct = e.target.value / 1000;
  S.curMs = S.tStartMs + pct * (S.tEndMs - S.tStartMs);
  const disp = document.getElementById('ts-display');
  if (disp) disp.textContent = fmtStamp(S.curMs);
  clearTimeout(S._slTm);
  S._slTm = setTimeout(() => {
    loadTrajectoryWindow(S.curMs).then(renderFrame);
  }, 300);
});

document.getElementById('btn-play').addEventListener('click', () => {
  if (!S.staticMode) play();
});
document.getElementById('btn-pause').addEventListener('click', () => {
  if (!S.staticMode) pause();
});
document.getElementById('btn-stop').addEventListener('click', () => {
  if (!S.staticMode) stop();
});

document.getElementById('btn-prev').addEventListener('click', () => {
  if (S.staticMode) return;
  pause();
  setTime(S.curMs - S.speedMult * 1000);
  loadTrajectoryWindow(S.curMs).then(renderFrame);
});

document.getElementById('btn-next').addEventListener('click', () => {
  if (S.staticMode) return;
  pause();
  setTime(S.curMs + S.speedMult * 1000);
  loadTrajectoryWindow(S.curMs).then(renderFrame);
});

document.getElementById('speed-select').addEventListener('change', function () {
  S.speedMult = parseInt(this.value);
  S.cacheStart = null;
  S.cacheEnd = null;
  loadTrajectoryWindow(S.curMs);
});

document.getElementById('cm-apply').addEventListener('click', async () => {
  try {
    const d = await fetchAnalytics();
    updateCharts(d);
  } catch (e) {
    console.error(e);
  }
});

document.getElementById('btn-map-toggle').addEventListener('click', toggleMapStyle);

document.getElementById('collision-severity-filter').addEventListener('change', function () {
  S.severityFilter = this.value;
  renderCollisionList();
  clearAccMarkers();
  S.accMarkers = [];
  renderAccidents();
});

document.getElementById('collision-type-filter').addEventListener('change', function () {
  S.typeFilter = this.value;
  renderCollisionList();
  clearAccMarkers();
  S.accMarkers = [];
  renderAccidents();
});

document.getElementById('btn-collapse-collisions').addEventListener('click', function () {
  S.panelCollapsed = !S.panelCollapsed;
  document.getElementById('collision-panel').classList.toggle('collapsed', S.panelCollapsed);
  this.textContent = S.panelCollapsed ? '▶' : '◀';
  setTimeout(() => map.invalidateSize(), 260);
});

document.getElementById('btn-clear-focus').addEventListener('click', clearFocus);

document.getElementById('btn-mode-full').addEventListener('click', () => switchMode('full'));
document.getElementById('btn-mode-road').addEventListener('click', () => switchMode('road'));
document.getElementById('btn-mode-heatmap').addEventListener('click', () => switchMode('heatmap'));

document.querySelectorAll('#hm-event-pills .hm-pill').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#hm-event-pills .hm-pill').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    S.heatEventType = parseInt(btn.dataset.value, 10);
    fetchHeatmapData();
  });
});

document.querySelectorAll('#hm-speed-pills .hm-pill').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#hm-speed-pills .hm-pill').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    S.heatSpeedBracket = parseInt(btn.dataset.value, 10);
    fetchHeatmapData();
  });
});

document.getElementById('hm-hour-slider').addEventListener('input', function () {
  S.heatHour = parseInt(this.value, 10);
  const disp = document.getElementById('hm-hour-display');
  if (disp)
    disp.textContent =
      S.heatHour === 24
        ? 'All hours'
        : String(S.heatHour).padStart(2, '0') + ':00 – ' + String(S.heatHour).padStart(2, '0') + ':59';
});

document.getElementById('hm-hour-slider').addEventListener('change', () => fetchHeatmapData());

document.getElementById('btn-close-section').addEventListener('click', () => {
  if (S.activeSection) setActiveSection(null);
});

document.getElementById('btn-time-jump').addEventListener('click', doTimeJump);
document.getElementById('time-jump-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    e.preventDefault();
    doTimeJump();
  }
});

/* ── Launch Dashboard ───────────────────────────────────── */
init();
