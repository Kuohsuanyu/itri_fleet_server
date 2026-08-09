/* ITRI Fleet Console -- live view over a single WebSocket.
 *
 * The server sends a full `snapshot` on connect and `update` deltas afterwards,
 * so this file keeps the authoritative robot map locally and only re-renders
 * cards whose `rev` changed. That keeps Funnel egress low and the UI smooth
 * with a few dozen robots.
 *
 * Battery sparklines are accumulated client-side from the updates we already
 * receive -- they cost zero extra bandwidth.
 */
'use strict';

const TOKEN = new URLSearchParams(location.search).get('token');
const robots = new Map();      // id -> robot record
const spark = new Map();       // id -> [battery, …] most recent last
const SPARK_MAX = 60;
let summary = {};
let filter = 'all';

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------- websocket */

let ws = null, retry = 0, pingTimer = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws${TOKEN ? '?token=' + encodeURIComponent(TOKEN) : ''}`;
  setConn('warn', 'connecting…');
  ws = new WebSocket(url);

  ws.onopen = () => {
    retry = 0;
    setConn('up', 'live');
    clearInterval(pingTimer);
    pingTimer = setInterval(() => ws.readyState === 1 && ws.send('p'), 25000);
  };

  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.t === 'snapshot') {
      robots.clear(); spark.clear();
      m.robots.forEach(applyRobot);
      summary = m.summary; renderAll();
    } else if (m.t === 'update') {
      (m.robots || []).forEach(applyRobot);
      (m.removed || []).forEach((id) => { robots.delete(id); spark.delete(id); });
      summary = m.summary || summary; renderAll();
    } else if (m.t === 'metrics') {
      renderMetrics(m.metrics);
    }
  };

  ws.onclose = (ev) => {
    clearInterval(pingTimer);
    if (ev.code === 4401) { setConn('', 'unauthorized — 請重新登入'); return; }
    retry = Math.min(retry + 1, 6);
    setConn('', `reconnecting in ${retry}s`);
    setTimeout(connect, retry * 1000);
  };

  ws.onerror = () => ws.close();
}

function setConn(cls, text) {
  $('conn-dot').className = 'dot ' + cls;
  const el = $('conn-text');
  if (el) el.textContent = text;          // hidden on phones; the drawer shows it
  syncDrawerStatus(text);
}

let lastMqtt = 'MQTT –';
let lastConn = 'connecting…';

function syncDrawerStatus(connText) {
  if (connText) lastConn = connText;
  if (!window.ITRIShell) return;
  const s = summary || {};
  const batt = s.avg_battery == null ? '–' : s.avg_battery.toFixed(0) + '%';
  ITRIShell.setStatus(
    `連線 ${lastConn}\n${lastMqtt}\n在線 ${s.online ?? 0} / ${s.total ?? 0}\n平均電量 ${batt}`);
}

function applyRobot(r) {
  robots.set(r.id, r);
  if (r.online && r.battery != null) {
    const s = spark.get(r.id) || [];
    if (!s.length || s[s.length - 1] !== r.battery) {
      s.push(r.battery);
      if (s.length > SPARK_MAX) s.shift();
      spark.set(r.id, s);
    }
  }
}

/* ------------------------------------------------------------------ stats */

function renderStats() {
  const s = summary || {};
  $('s-online').textContent = `${s.online ?? 0} / ${s.total ?? 0}`;
  $('s-moving').textContent = s.moving ?? 0;
  $('s-charging').textContent = s.charging ?? 0;
  const f = $('s-fault');
  f.textContent = s.faulted ?? 0;
  f.classList.toggle('hot', (s.faulted ?? 0) > 0);
  $('s-batt').textContent = s.avg_battery == null ? '–' : s.avg_battery.toFixed(0) + '%';
  const mb = $('s-minbatt');
  mb.textContent = s.min_battery == null ? '–' : s.min_battery.toFixed(0) + '%';
  mb.classList.toggle('hot', (s.min_battery ?? 100) < 20);
  $('s-msgs').textContent = fmtCount(s.msg_count ?? 0);
  syncDrawerStatus();          // keep the drawer numbers current, not frozen
}

/* ------------------------------------------------------------------ cards */

function passesFilter(r) {
  switch (filter) {
    case 'online':  return r.online;
    case 'offline': return !r.online;
    case 'fault':   return (r.errors && r.errors.length) || r.state === 'error' || r.state === 'estop';
    case 'low':     return r.battery != null && r.battery < 30;
    default:        return true;
  }
}

function renderCards() {
  const box = $('cards');
  const q = $('search').value.trim().toLowerCase();
  const list = [...robots.values()]
    .filter(passesFilter)
    .filter((r) => !q || (r.id + ' ' + r.name).toLowerCase().includes(q))
    .sort((a, b) => (b.online - a.online) || a.id.localeCompare(b.id));

  $('empty').style.display = list.length ? 'none' : 'block';
  $('empty').textContent = robots.size
    ? '沒有符合條件的車輛。'
    : '尚未收到任何 MQTT 遙測。';

  const seen = new Set();
  for (const r of list) {
    seen.add(r.id);
    let el = box.querySelector(`[data-id="${CSS.escape(r.id)}"]`);
    if (!el) {
      el = document.createElement('div');
      el.dataset.id = r.id;
      box.appendChild(el);
    }
    el.className = `card s-${r.state}`;
    el.innerHTML = cardHtml(r);
  }
  box.querySelectorAll('.card').forEach((el) => { if (!seen.has(el.dataset.id)) el.remove(); });
}

function cardHtml(r) {
  const b = r.battery;
  const bcls = b == null ? '' : b < 15 ? 'crit' : b < 30 ? 'low' : '';
  const extras = [];
  if (r.temp != null) extras.push(`<div>溫度<b>${r.temp.toFixed(0)}°C</b></div>`);
  if (r.wifi != null) extras.push(`<div>訊號<b>${r.wifi.toFixed(0)} dBm</b></div>`);
  if (r.odom != null) extras.push(`<div>里程<b>${(r.odom / 1000).toFixed(1)} km</b></div>`);

  return `
    <div class="card-top">
      <div class="card-title">
        <span class="card-name">${esc(r.name)}</span>
        <span class="card-id">${esc(r.id)}</span>
      </div>
      <span class="pill ${esc(r.state)}">${esc(r.state)}</span>
    </div>

    <div class="batt-row">
      <div class="batt ${bcls}"><i style="width:${b ?? 0}%"></i></div>
      <span class="batt-n ${bcls}">${b == null ? '–' : b.toFixed(0) + '%'}</span>
      ${sparkSvg(r.id)}
    </div>

    <div class="card-grid">
      <div>速度<b>${r.vel.v.toFixed(2)} m/s</b></div>
      <div>角速度<b>${r.vel.w.toFixed(2)} rad/s</b></div>
      <div>延遲<b>${r.age == null ? '–' : r.age.toFixed(1) + 's'}</b></div>
      ${extras.join('')}
    </div>

    ${r.mission ? `<div class="mission">任務 <b>${esc(r.mission)}</b>${
        r.progress != null
          ? `<span class="prog"><i style="width:${(r.progress * 100).toFixed(0)}%"></i></span>
             <span class="prog-n">${(r.progress * 100).toFixed(0)}%</span>`
          : ''}</div>` : ''}

    ${r.errors && r.errors.length ? `<div class="errs">⚠ ${r.errors.map(esc).join(' · ')}</div>` : ''}`;
}

/** Tiny inline battery-trend line. No library, no extra requests. */
function sparkSvg(id) {
  const s = spark.get(id);
  if (!s || s.length < 3) return '<span class="spark"></span>';
  const w = 64, h = 16;
  const min = Math.min(...s), max = Math.max(...s);
  const span = Math.max(max - min, 1);
  const pts = s.map((v, i) =>
    `${(i / (s.length - 1) * w).toFixed(1)},${(h - (v - min) / span * (h - 2) - 1).toFixed(1)}`
  ).join(' ');
  const falling = s[s.length - 1] < s[0];
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
            <polyline points="${pts}" fill="none"
                      stroke="${falling ? '#fbbf24' : '#34d399'}" stroke-width="1.5"/>
          </svg>`;
}

/* ---------------------------------------------------------------- metrics */

function renderMetrics(m) {
  lastMqtt = 'MQTT ' + (m.mqtt_connected ? '已連線' : '斷線');
  const mq = $('mqtt-text');
  if (mq) mq.textContent = lastMqtt;
  $('e-rate').textContent = fmtRate(m.rate_bps);
  $('e-total').textContent = fmtBytes(m.total_bytes);
  $('e-day').textContent = m.projected_gb_day.toFixed(2) + ' GB';
  $('e-month').textContent = m.projected_gb_month.toFixed(1) + ' GB';
  $('e-clients').textContent = m.ws_clients;
  $('e-peak').textContent = fmtRate(m.peak_bps);

  // 100 GB/month is a self-imposed budget, not a Tailscale quota -- they do
  // not publish one. It just gives the bar a meaningful scale.
  const pct = Math.min(m.projected_gb_month / 100 * 100, 100);
  $('e-bar').style.width = pct + '%';
  $('e-note').textContent =
    `以目前速率推估每月 ${m.projected_gb_month.toFixed(1)} GB(參考預算 100 GB/月 = ${pct.toFixed(1)}%)` +
    ` · WS ${fmtBytes(m.ws_bytes)} / HTTP ${fmtBytes(m.http_bytes)} · ${m.ws_frames} frames`;
}

/* ------------------------------------------------------------------ utils */

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  const u = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toFixed(n < 10 ? 2 : 1) + ' ' + u[i];
}
const fmtRate = (bps) => bps < 1024 ? bps.toFixed(0) + ' B/s' : fmtBytes(bps) + '/s';
const fmtCount = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);

function renderAll() { renderStats(); renderCards(); }

/* ------------------------------------------------------------------ alerts */

async function pollAlerts() {
  let d;
  try {
    d = await (await fetch('/api/alerts/active')).json();
  } catch { return; }
  const bar = $('alertbar');
  const open = d.open || [];
  bar.hidden = open.length === 0;
  bar.innerHTML = open.map((a) => `
    <div class="alert ${a.severity === 'critical' ? 'critical' : ''}">
      <span>${a.severity === 'critical' ? '🚨' : '⚠'}</span>
      <span class="who">${esc(a.robot)}</span>
      <span>${esc(a.message)}</span>
      <span class="age">${fmtAge(a.for_s)}</span>
    </div>`).join('');
}

const fmtAge = (s) => s < 60 ? `${s.toFixed(0)}s`
  : s < 3600 ? `${(s / 60).toFixed(0)} 分` : `${(s / 3600).toFixed(1)} 小時`;

/* The bell button and the notification prompt live in shell.js, shared with
 * the admin pages -- there is no page-specific behaviour worth duplicating. */

/* -------------------------------------------------------------------- init */

$('search').oninput = renderCards;
$('filters').addEventListener('click', (e) => {
  const btn = e.target.closest('.seg');
  if (!btn) return;
  filter = btn.dataset.f;
  $('filters').querySelectorAll('.seg').forEach((b) => b.classList.toggle('active', b === btn));
  renderCards();
});

connect();
pollAlerts();
setInterval(pollAlerts, 5000);
