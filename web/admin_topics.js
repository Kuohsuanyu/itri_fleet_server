/* Topic 瀏覽器:列出 agent 轉發過來的原始 topic,看趨勢、直接建規則。
 *
 * The chart is hand-drawn on a canvas rather than pulled from a library: it is
 * one line plot, and a charting dependency would be larger than this whole file.
 */
'use strict';

renderShell('Topic 瀏覽器');

let topics = [];
let chartTopic = null;
let chartHours = 1;

/* ------------------------------------------------------------------ table */

async function loadRobotOptions() {
  try {
    const d = await api('/api/admin/robots');
    const sel = $('t-robot');
    const cur = sel.value;
    sel.innerHTML = '<option value="">全部車輛</option>' +
      d.robots.map((r) => `<option value="${esc(r.id)}">${esc(r.id)}</option>`).join('');
    if (cur) sel.value = cur;
  } catch { /* ignore */ }
}

async function loadTopics() {
  const robot = $('t-robot').value;
  let d;
  try {
    d = await api('/api/topics' + (robot ? `?robot_id=${encodeURIComponent(robot)}` : ''));
  } catch (e) {
    $('topics-empty').textContent = '讀取失敗:' + e.message;
    $('topics-empty').style.display = 'block';
    return;
  }
  topics = d.topics;
  render();
}

function render() {
  const q = $('t-filter').value.trim().toLowerCase();
  const rows = topics.filter((t) => !q || t.topic.toLowerCase().includes(q));
  $('t-count').textContent = `${rows.length} / ${topics.length} 個 topic`;
  $('topics-empty').style.display = rows.length ? 'none' : 'block';

  const body = $('topics-body');
  body.innerHTML = '';
  for (const t of rows) {
    const fresh = (Date.now() - new Date(t.last_seen)) < 6000;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="mono">${fresh ? '<span class="live">●</span> ' : ''}${esc(t.topic)}</td>
      <td class="mono">${esc(String(t.last_value ?? '–')).slice(0, 40)}</td>
      <td class="mono dim">${t.samples.toLocaleString()}</td>
      <td class="dim">${fmtTime(t.last_seen)}</td>
      <td class="right"><div class="row-actions">
        <button class="btn" data-act="chart" data-topic="${esc(t.topic)}"
                data-robot="${esc(t.robot_id)}">圖表</button>
        <button class="btn primary" data-act="rule" data-topic="${esc(t.topic)}"
                data-robot="${esc(t.robot_id)}">設預警</button>
      </div></td>`;
    body.appendChild(tr);
  }
}

$('topics-body').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const { act, topic, robot } = btn.dataset;
  if (act === 'chart') {
    chartTopic = { topic, robot };
    openChart();
  } else {
    // hand the topic to the rule editor; it prefills source=topic and the key
    location.href = `/admin/alerts?topic=${encodeURIComponent(topic)}`
                  + `&robot=${encodeURIComponent(robot)}`;
  }
});

$('t-filter').oninput = render;
$('t-robot').onchange = loadTopics;

/* ------------------------------------------------------------------ chart */

async function openChart() {
  if (!chartTopic) return;
  $('chartpanel').hidden = false;
  $('chart-title').textContent = `${chartTopic.topic} · ${chartTopic.robot}`;
  $('chartpanel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  const start = Date.now() / 1000 - chartHours * 3600;
  const p = new URLSearchParams({
    topic: chartTopic.topic, robot_id: chartTopic.robot,
    start: String(start), limit: '2000',
  });
  let d;
  try {
    d = await api('/api/topic_history?' + p);
  } catch (e) {
    $('chart-note').textContent = '讀取失敗:' + e.message;
    return;
  }

  const pts = d.rows
    .map((r) => ({ t: new Date(r.ts).getTime(), v: r.num }))
    .filter((p2) => p2.v != null);

  if (!pts.length) {
    drawEmpty(d.count ? '這個 topic 不是數值,無法繪圖(值仍完整存在資料庫)'
                      : '這段時間沒有資料');
    $('chart-note').textContent = `${d.count} 筆樣本`;
    return;
  }
  draw(pts);
  const vs = pts.map((p2) => p2.v);
  $('chart-note').textContent =
    `${d.count} 筆 · 最小 ${Math.min(...vs).toFixed(3)} · 最大 ${Math.max(...vs).toFixed(3)}`
    + ` · 平均 ${(vs.reduce((a, b) => a + b, 0) / vs.length).toFixed(3)}`
    + (d.bucket_s ? ` · 每 ${d.bucket_s.toFixed(0)}s 平均` : '');
}

function chartCtx() {
  const cv = $('chart');
  const dpr = window.devicePixelRatio || 1;
  const w = cv.parentElement.clientWidth - 28;
  const h = 260;
  cv.style.width = w + 'px';
  cv.style.height = h + 'px';
  cv.width = w * dpr;
  cv.height = h * dpr;
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

function drawEmpty(msg) {
  const { ctx, w, h } = chartCtx();
  ctx.fillStyle = '#8697aa';
  ctx.font = '13px "Segoe UI", sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText(msg, w / 2, h / 2);
}

function draw(pts) {
  const { ctx, w, h } = chartCtx();
  const pad = { l: 54, r: 12, t: 12, b: 26 };
  const t0 = pts[0].t, t1 = pts[pts.length - 1].t || t0 + 1;
  let lo = Math.min(...pts.map((p) => p.v));
  let hi = Math.max(...pts.map((p) => p.v));
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const padY = (hi - lo) * 0.1;
  lo -= padY; hi += padY;

  const X = (t) => pad.l + (t - t0) / Math.max(t1 - t0, 1) * (w - pad.l - pad.r);
  const Y = (v) => h - pad.b - (v - lo) / (hi - lo) * (h - pad.t - pad.b);

  ctx.strokeStyle = '#1a2431';
  ctx.fillStyle = '#8697aa';
  ctx.font = '11px ui-monospace, Consolas, monospace';
  ctx.lineWidth = 1;
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4;
    const y = Y(v);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.fillText(v.toFixed(2), pad.l - 6, y + 4);
  }
  ctx.textAlign = 'center';
  for (let i = 0; i <= 4; i++) {
    const t = t0 + (t1 - t0) * i / 4;
    ctx.fillText(new Date(t).toLocaleTimeString('zh-TW', { hour12: false }).slice(0, 5),
                 X(t), h - 8);
  }

  const grad = ctx.createLinearGradient(0, pad.t, 0, h - pad.b);
  grad.addColorStop(0, 'rgba(56,189,248,.28)');
  grad.addColorStop(1, 'rgba(56,189,248,0)');
  ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(X(p.t), Y(p.v)) : ctx.moveTo(X(p.t), Y(p.v))));
  ctx.lineTo(X(pts[pts.length - 1].t), h - pad.b);
  ctx.lineTo(X(pts[0].t), h - pad.b);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(X(p.t), Y(p.v)) : ctx.moveTo(X(p.t), Y(p.v))));
  ctx.strokeStyle = '#38bdf8';
  ctx.lineWidth = 1.6;
  ctx.stroke();
}

$('chart-range').addEventListener('click', (e) => {
  const btn = e.target.closest('.seg');
  if (!btn) return;
  chartHours = Number(btn.dataset.h);
  $('chart-range').querySelectorAll('.seg').forEach((b) => b.classList.toggle('active', b === btn));
  openChart();
});
$('chart-close').onclick = () => { $('chartpanel').hidden = true; chartTopic = null; };
window.addEventListener('resize', () => { if (chartTopic) openChart(); });

/* ------------------------------------------------------------------- init */

loadRobotOptions().then(loadTopics);
pingDb();
setInterval(() => { if ($('t-live').checked) loadTopics(); }, 5000);
