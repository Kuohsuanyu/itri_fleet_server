/* 預警規則:門檻設定、自訂通知內容、觸發中清單、告警歷史。 */
'use strict';

renderShell('預警規則');

const OP_TEXT = {
  lt: '小於', gt: '大於', outside: '超出範圍', inside: '落在範圍',
  eq: '等於', ne: '不等於', offline: '離線超過', stale: '資料停止',
};

let liveValues = {};   // robot_id -> {field/topic -> value}, for the preview

function describeRule(r) {
  const subject = r.source === 'presence' ? '上下線' : (r.key || '?');
  if (r.op === 'offline') return `離線超過 ${r.value ?? 60}s`;
  if (r.op === 'stale') return '連線仍在但資料停止';
  if (r.op === 'eq' || r.op === 'ne') return `${subject} ${OP_TEXT[r.op]} "${r.text_value}"`;
  if (r.op === 'outside' || r.op === 'inside')
    return `${subject} ${OP_TEXT[r.op]} ${r.value}~${r.value2}`;
  return `${subject} ${OP_TEXT[r.op] || r.op} ${r.value}`;
}

/* ------------------------------------------------------------------ rules */

async function loadRules() {
  let d;
  try { d = await api('/api/alerts/rules'); }
  catch (e) { $('rules-empty').textContent = '讀取失敗:' + e.message; return; }

  $('chan-list').textContent = d.channels.length
    ? '啟用的管道:' + d.channels.join(', ')
    : '⚠ config.yaml 沒有啟用任何通知管道';
  const sel = $('test-chan');
  if (sel.options.length !== d.channels.length) {
    sel.innerHTML = d.channels.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  }
  $('key-options').innerHTML = (d.fields || []).map((f) => `<option value="${esc(f)}">`).join('');

  const body = $('rules-body');
  body.innerHTML = '';
  $('rules-empty').style.display = d.count ? 'none' : 'block';
  for (const r of d.rules) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input type="checkbox" data-act="toggle" data-id="${r.id}" ${r.enabled ? 'checked' : ''}></td>
      <td>${esc(r.name)}</td>
      <td class="mono dim">${esc(r.robot_id || '全部')}</td>
      <td class="mono">${esc(describeRule(r))}</td>
      <td class="detail">${r.message_template ? esc(r.message_template) : '<span class="dim">自動</span>'}</td>
      <td class="mono dim">${r.for_seconds}s</td>
      <td class="mono dim">${r.clear_value ?? '–'}</td>
      <td><span class="badge ${r.severity === 'critical' ? 'bad' : 'pending'}">${esc(r.severity)}</span></td>
      <td class="right"><div class="row-actions">
        <button class="btn danger" data-act="delrule" data-id="${r.id}">刪除</button>
      </div></td>`;
    body.appendChild(tr);
  }
}

$('rules-body').addEventListener('change', async (e) => {
  const el = e.target.closest('[data-act="toggle"]');
  if (!el) return;
  try {
    await api(`/api/alerts/rules/${el.dataset.id}`,
      { method: 'PATCH', body: JSON.stringify({ enabled: el.checked }) });
    toast(el.checked ? '已啟用' : '已停用');
  } catch (err) { toast(err.message, true); loadRules(); }
});

$('rules-body').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-act="delrule"]');
  if (!btn) return;
  if (!confirm('刪除這條規則?')) return;
  try {
    await api(`/api/alerts/rules/${btn.dataset.id}`, { method: 'DELETE' });
    toast('已刪除'); loadRules();
  } catch (err) { toast(err.message, true); }
});

/* -------------------------------------------------------------- rule form */

function syncRuleForm() {
  const op = $('r-op').value;
  $('wrap-v2').hidden = !(op === 'outside' || op === 'inside');
  $('wrap-text').hidden = !(op === 'eq' || op === 'ne');
  $('r-key').disabled = $('r-source').value === 'presence';
  showLiveValue();
  showPreview();
}

/** Shows the subject's current value next to the field, so a threshold can be
 *  chosen against reality instead of guesswork. */
function showLiveValue() {
  const key = $('r-key').value.trim();
  const robot = $('r-robot').value;
  const el = $('r-live');
  if (!key) { el.textContent = ''; return; }
  const pool = robot ? [liveValues[robot]] : Object.values(liveValues);
  const vals = pool.filter(Boolean).map((m) => m[key]).filter((v) => v !== undefined);
  el.textContent = vals.length
    ? `目前 ${vals.slice(0, 3).map(fmtVal).join(' / ')}${vals.length > 3 ? ' …' : ''}`
    : '(目前沒有這個項目的資料)';
}

function showPreview() {
  const tpl = $('r-msg').value.trim();
  const key = $('r-key').value.trim() || '狀態';
  const value = (() => {
    const pool = Object.values(liveValues).map((m) => m[key]).filter((v) => v !== undefined);
    return pool.length ? fmtVal(pool[0]) : '42';
  })();
  const vars = {
    robot: 'AMR-07', id: 'amr-07', key, value,
    limit: $('r-value').value || '20', limit2: $('r-value2').value || '50',
    rule: $('r-name').value || '規則名稱', severity: $('r-sev').value,
  };
  let out;
  if (!tpl) {
    const op = $('r-op').value;
    out = op === 'stale' ? `${vars.robot} 連線仍在但資料停止`
        : op === 'offline' ? `${vars.robot} 離線`
      : op === 'outside' ? `${vars.robot} ${key}=${value} 超出範圍 ${vars.limit}~${vars.limit2}`
      : `${vars.robot} ${key}=${value} ${OP_TEXT[op] || op} ${vars.limit}`;
    $('r-preview').textContent = '預覽(自動產生):' + out;
    return;
  }
  try {
    out = tpl.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m));
    const unknown = out.match(/\{(\w+)\}/g);
    $('r-preview').textContent = '預覽:' + out +
      (unknown ? `   ⚠ 未知的變數 ${unknown.join(' ')}` : '');
  } catch {
    $('r-preview').textContent = '⚠ 通知內容格式有誤';
  }
}

for (const id of ['r-op', 'r-source', 'r-key', 'r-msg', 'r-value', 'r-value2',
                  'r-name', 'r-sev', 'r-robot']) {
  const el = $(id);
  el.addEventListener('input', showPreview);
  el.addEventListener('change', syncRuleForm);
}

$('btn-newrule').onclick = () => {
  $('rulebox').hidden = !$('rulebox').hidden;
  syncRuleForm();
  if (!$('rulebox').hidden) $('r-name').focus();
};
$('btn-rulecancel').onclick = () => { $('rulebox').hidden = true; };

$('rulebox').onsubmit = async (e) => {
  e.preventDefault();
  const num = (id) => ($(id).value === '' ? null : Number($(id).value));
  try {
    await api('/api/alerts/rules', {
      method: 'POST',
      body: JSON.stringify({
        name: $('r-name').value.trim(),
        robot_id: $('r-robot').value || null,
        source: $('r-source').value,
        key: $('r-key').value.trim(),
        op: $('r-op').value,
        value: num('r-value'),
        value2: num('r-value2'),
        text_value: $('r-text').value.trim() || null,
        for_seconds: num('r-for') ?? 10,
        clear_value: num('r-clear'),
        severity: $('r-sev').value,
        cooldown_min: num('r-cool') ?? 15,
        message_template: $('r-msg').value.trim() || null,
      }),
    });
    $('rulebox').hidden = true;
    ['r-name', 'r-value', 'r-value2', 'r-msg', 'r-clear'].forEach((i) => { $(i).value = ''; });
    toast('規則已建立');
    loadRules();
  } catch (err) { toast(err.message, true); }
};

$('btn-test').onclick = async () => {
  const channel = $('test-chan').value;
  if (!channel) { toast('config.yaml 沒有啟用任何管道', true); return; }
  try {
    await api('/api/alerts/test', { method: 'POST', body: JSON.stringify({ channel }) });
    toast(`已透過 ${channel} 送出測試通知`);
  } catch (err) { toast(`${channel} 失敗:${err.message}`, true); }
};

/* --------------------------------------------------------- active/history */

async function loadActive() {
  let d, dev = { count: 0 };
  try { d = await api('/api/alerts/active'); } catch { return; }
  try { dev = await api('/api/push/devices'); } catch { /* db may be down */ }

  const body = $('active-body');
  body.innerHTML = '';
  $('active-empty').style.display = d.open.length ? 'none' : 'block';
  for (const a of d.open) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><span class="badge ${a.severity === 'critical' ? 'bad' : 'pending'}">${esc(a.severity)}</span></td>
      <td class="mono">${esc(a.robot)}</td>
      <td>${esc(a.message)}</td>
      <td class="mono dim">${a.for_s.toFixed(0)}s</td>`;
    body.appendChild(tr);
  }
  $('a-fired').textContent = d.fired;
  $('a-resolved').textContent = d.resolved;
  $('a-devices').textContent = dev.count ?? '–';
  $('a-failed').textContent = d.notify_failures;
  $('a-note').textContent = d.last_error
    ? '最後一次通知錯誤:' + d.last_error : '通知管道運作正常。';
}

async function loadHistory() {
  let d;
  try { d = await api('/api/alerts/history?limit=100'); } catch { return; }
  const body = $('hist-body');
  body.innerHTML = '';
  $('hist-empty').style.display = d.count ? 'none' : 'block';
  for (const a of d.alerts) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="mono dim">${new Date(a.started_at).toLocaleString('zh-TW', { hour12: false })}</td>
      <td class="mono dim">${a.resolved_at
        ? new Date(a.resolved_at).toLocaleTimeString('zh-TW', { hour12: false })
        : '<span class="badge pending">進行中</span>'}</td>
      <td class="mono">${esc(a.robot_id)}</td>
      <td><span class="badge ${a.severity === 'critical' ? 'bad' : 'pending'}">${esc(a.severity)}</span></td>
      <td>${esc(a.message)}</td>
      <td class="dim">${(a.notified || []).join(', ') || '–'}</td>`;
    body.appendChild(tr);
  }
}

/* Live values feed the "目前 x" hint and the preview. */
async function loadLive() {
  try {
    const fleet = await api('/api/fleet');
    liveValues = {};
    const sel = $('r-robot');
    const cur = sel.value;
    sel.innerHTML = '<option value="">全部</option>' +
      fleet.robots.map((r) => `<option value="${esc(r.id)}">${esc(r.id)}</option>`).join('');
    sel.value = cur;
    for (const r of fleet.robots) {
      liveValues[r.id] = {
        battery: r.battery, state: r.state, v: r.vel.v, w: r.vel.w,
        temp: r.temp, wifi: r.wifi, odom: r.odom,
        errors: (r.errors || []).length, age: r.age,
      };
    }
    const t = await api('/api/topics');
    for (const row of t.topics) {
      (liveValues[row.robot_id] ||= {})[row.topic] = row.last_value;
    }
  } catch { /* ignore */ }
  showLiveValue();
}

/* Arriving from the topic browser: prefill and open the editor. */
const q = new URLSearchParams(location.search);
if (q.get('topic')) {
  $('rulebox').hidden = false;
  $('r-source').value = 'topic';
  $('r-key').value = q.get('topic');
  $('r-name').value = q.get('topic');
  if (q.get('robot')) $('r-robot').value = q.get('robot');
  syncRuleForm();
}

loadRules();
loadActive();
loadHistory();
loadLive();
pingDb();
setInterval(() => { loadActive(); loadLive(); }, 10000);
setInterval(loadHistory, 30000);
