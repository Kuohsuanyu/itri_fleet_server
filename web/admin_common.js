/* Shared helpers for the admin pages.
 *
 * The admin surface is split into real pages rather than one long scroll, so
 * each page loads only what it needs and nothing gets buried below the fold.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* non-JSON error */ }
  if (!res.ok) throw new Error(data?.detail || data?.error || text || `HTTP ${res.status}`);
  return data;
}

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'toast show' + (isError ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 3400);
}

function fmtTime(iso) {
  if (!iso) return '–';
  const d = new Date(iso);
  const age = (Date.now() - d) / 1000;
  if (age < 60) return `${age.toFixed(0)} 秒前`;
  if (age < 3600) return `${(age / 60).toFixed(0)} 分前`;
  if (age < 86400) return `${(age / 3600).toFixed(0)} 小時前`;
  return d.toLocaleString('zh-TW', { hour12: false });
}

function fmtBytes(n) {
  if (n == null) return '–';
  if (n < 1024) return n + ' B';
  const u = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toFixed(n < 10 ? 2 : 1) + ' ' + u[i];
}

const fmtVal = (v) => {
  if (v == null) return '–';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(3);
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
};

/* The top bar, drawer and notification prompt come from shell.js, shared with
 * the dashboard so the two never drift apart. */
const renderShell = (title) => ITRIShell.render({ title, live: false });

/** Every admin page pings this so the header shows database health. */
async function pingDb() {
  const dot = $('db-dot');
  const text = $('db-text');
  try {
    const s = await api('/api/storage');
    if (dot) dot.className = 'dot up';
    const msg = `資料庫 ${fmtBytes(s.database_bytes)} · 保留 ${s.retention_days} 天`;
    if (text) text.textContent = msg;
    ITRIShell.setStatus(msg);
  } catch (e) {
    if (dot) dot.className = 'dot';
    if (text) text.textContent = '資料庫離線';
    ITRIShell.setStatus('資料庫離線\n' + e.message);
  }
}
