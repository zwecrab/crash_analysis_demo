'use strict';

import { S, riskLevel } from './state.js';

/* ── Chart Configurations ───────────────────────────────── */
export const CO = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: { legend: { display: false } }
};

export function initCharts() {
  S.charts.baEvents = new Chart(document.getElementById('chart-ba-events'), {
    type: 'bar',
    data: {
      labels: ['Before', 'After'],
      datasets: [
        { data: [0, 0], backgroundColor: ['#2563eb', '#0d9488'], borderRadius: 3, barPercentage: 0.5 }
      ]
    },
    options: {
      ...CO,
      scales: {
        x: { ticks: { font: { size: 9 }, color: '#64748b' }, grid: { display: false } },
        y: { ticks: { font: { size: 9 }, color: '#64748b', maxTicksLimit: 4 }, grid: { color: '#f1f5f9' } }
      }
    }
  });

  S.charts.baRisk = new Chart(document.getElementById('chart-ba-risk'), {
    type: 'bar',
    data: {
      labels: ['Before', 'After'],
      datasets: [
        { data: [0, 0], backgroundColor: ['#2563eb', '#0d9488'], borderRadius: 3, barPercentage: 0.5 }
      ]
    },
    options: {
      ...CO,
      scales: {
        x: { ticks: { font: { size: 9 }, color: '#64748b' }, grid: { display: false } },
        y: { ticks: { font: { size: 9 }, color: '#64748b', maxTicksLimit: 4 }, grid: { color: '#f1f5f9' } }
      }
    }
  });

  S.charts.freq = new Chart(document.getElementById('chart-freq'), {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        {
          data: [],
          borderColor: '#2563eb',
          backgroundColor: 'rgba(37,99,235,.08)',
          fill: true,
          tension: 0.35,
          pointRadius: 1.5,
          borderWidth: 1.5
        }
      ]
    },
    options: {
      ...CO,
      scales: {
        x: {
          ticks: { font: { size: 8 }, color: '#64748b', maxRotation: 0, maxTicksLimit: 5 },
          grid: { display: false }
        },
        y: { ticks: { font: { size: 9 }, color: '#64748b', maxTicksLimit: 4 }, grid: { color: '#f1f5f9' } }
      }
    }
  });

  S.charts.events = new Chart(document.getElementById('chart-events'), {
    type: 'doughnut',
    data: {
      labels: ['Harsh Brake', 'Sudden Accel', 'Sharp Turn', 'Collision'],
      datasets: [
        {
          data: [1, 1, 1, 1],
          backgroundColor: ['#f59e0b', '#0d9488', '#8b5cf6', '#dc2626'],
          borderWidth: 0,
          hoverOffset: 4
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '58%',
      plugins: {
        legend: {
          position: 'right',
          labels: { font: { size: 9 }, color: '#374151', padding: 6, usePointStyle: true, pointStyleWidth: 7 }
        }
      }
    }
  });
}

/* ── Canvas Gauge Drawing ──────────────────────────────── */
export function drawGauge(score) {
  const cv = document.getElementById('gauge');
  if (!cv) return;
  const W = cv.offsetWidth || 260, H = cv.offsetHeight || 70;
  const dpr = window.devicePixelRatio || 1;
  cv.width = W * dpr;
  cv.height = H * dpr;
  const ctx = cv.getContext('2d');
  ctx.scale(dpr, dpr);
  const cx = W / 2, cy = H + 2, r = Math.min(W * 0.42, H * 1.1);

  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, 0, false);
  ctx.lineWidth = 12;
  ctx.strokeStyle = '#e2e8f0';
  ctx.stroke();

  [
    { f: Math.PI, t: Math.PI * 1.4, c: '#16a34a' },
    { f: Math.PI * 1.4, t: Math.PI * 1.7, c: '#f59e0b' },
    { f: Math.PI * 1.7, t: Math.PI * 2, c: '#ef4444' }
  ].forEach(s => {
    ctx.beginPath();
    ctx.arc(cx, cy, r, s.f, s.t, false);
    ctx.lineWidth = 12;
    ctx.strokeStyle = s.c;
    ctx.stroke();
  });

  const ang = Math.PI + (Math.min(score, 10) / 10) * Math.PI, nL = r * 0.7;
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(cx + Math.cos(ang) * nL, cy + Math.sin(ang) * nL);
  ctx.lineWidth = 2.5;
  ctx.strokeStyle = '#1e293b';
  ctx.lineCap = 'round';
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(cx, cy, 4, 0, Math.PI * 2);
  ctx.fillStyle = '#1e293b';
  ctx.fill();

  ctx.font = `500 ${Math.round(H * 0.16)}px Inter,system-ui`;
  ctx.fillStyle = '#94a3b8';
  ctx.textAlign = 'center';
  ctx.fillText('0', cx - r - 8, cy + 2);
  ctx.fillText('10', cx + r + 8, cy + 2);

  ctx.font = `700 ${Math.round(H * 0.27)}px Inter,system-ui`;
  ctx.fillStyle = '#1e293b';
  ctx.fillText(score.toFixed(1), cx, cy - r * 0.4);
}

/* ── Live Updates ───────────────────────────────────────── */
export function updateCharts(d) {
  if (d.before_after) {
    document.getElementById('ba-placeholder').style.display = 'none';
    document.getElementById('ba-row').style.display = 'flex';
    S.charts.baEvents.data.datasets[0].data = [
      d.before_after.before.crashes,
      d.before_after.after.crashes
    ];
    S.charts.baEvents.update('none');
    S.charts.baRisk.data.datasets[0].data = [
      d.before_after.before.events,
      d.before_after.after.events
    ];
    S.charts.baRisk.update('none');
  } else {
    document.getElementById('ba-placeholder').style.display = 'block';
    document.getElementById('ba-row').style.display = 'none';
  }

  if (d.crash_frequency?.length) {
    S.charts.freq.data.labels = d.crash_frequency.map(x =>
      new Date(x.date).toLocaleDateString('en-GB', { month: 'short', day: 'numeric' })
    );
    S.charts.freq.data.datasets[0].data = d.crash_frequency.map(x => x.count);
    S.charts.freq.update('none');
  }

  if (d.event_breakdown) {
    const eb = d.event_breakdown;
    S.charts.events.data.datasets[0].data = [
      eb.harsh_brake.count,
      eb.sudden_accel.count,
      eb.sharp_turn.count,
      eb.collision.count
    ];
    S.charts.events.update('none');
  }

  S.dailyCrashes = {};
  S.dailyEvents = {};
  (d.crash_frequency || []).forEach(x => {
    S.dailyCrashes[x.date] = x.count;
  });
  (d.daily_events || []).forEach(x => {
    S.dailyEvents[x.date] = x.count;
  });
  S.lastRiskDay = null; // force refresh
  updateRiskForCurrentDay();
}

const _dayKey = ms => new Date(ms).toISOString().slice(0, 10);

export function updateRiskForCurrentDay(force) {
  if (!S.dailyCrashes || S.curMs == null) return;
  const day = _dayKey(S.curMs);
  if (!force && day === S.lastRiskDay) return;
  S.lastRiskDay = day;

  const c = S.dailyCrashes[day] || 0;
  const e = S.dailyEvents[day] || 0;
  const score = Math.min(10, c * 10 + e * 0.001);
  const rounded = Math.round(score * 10) / 10;

  drawGauge(rounded);
  const lv = riskLevel(rounded);
  const el = document.getElementById('risk-label');
  if (el) {
    el.className = 'risk-label ' + lv.c;
    el.textContent = 'RISK: ' + lv.l;
  }

  const det = document.getElementById('risk-detail');
  if (det) {
    const dateStr = new Date(S.curMs).toLocaleDateString('en-GB', {
      day: '2-digit',
      month: 'short',
      year: 'numeric'
    });
    det.innerHTML =
      `<b>${dateStr}</b> &nbsp;·&nbsp; Crashes: <b>${c}</b> &nbsp;·&nbsp; Events: <b>${e.toLocaleString()}</b><br>` +
      `<span style="color:#4e7a5c">■ 0–3 Low</span> &nbsp;` +
      `<span style="color:#cf7a3f">■ 3–6 Medium</span> &nbsp;` +
      `<span style="color:#b73d3d">■ 6–10 High</span>`;
  }
}
