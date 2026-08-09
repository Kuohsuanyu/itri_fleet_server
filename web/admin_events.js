/* 事件記錄:稽核軌跡瀏覽。 */
'use strict';

renderShell('事件記錄');

let evSeverity = '';

async function loadEvents() {
  const params = new URLSearchParams({ limit: '300' });
  if (evSeverity) params.set('severity', evSeverity);
  const robot = $('ev-robot').value.trim();
  const kind = $('ev-kind').value.trim();
  if (robot) params.set('robot_id', robot);
  if (kind) params.set('kind', kind);

  let data;
  try {
    data = await api('/api/events?' + params);
  } catch (e) {
    $('events-empty').textContent = '讀取失敗:' + e.message;
    $('events-empty').style.display = 'block';
    return;
  }
  const body = $('events-body');
  body.innerHTML = '';
  $('events-empty').style.display = data.count ? 'none' : 'block';

  for (const ev of data.events) {
    const cls = ev.severity === 'critical' ? 'bad'
      : ev.severity === 'warn' ? 'pending' : 'mute';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="mono dim">${new Date(ev.ts).toLocaleString('zh-TW', { hour12: false })}</td>
      <td class="mono">${esc(ev.robot_id || '–')}</td>
      <td>${esc(ev.kind)}</td>
      <td><span class="badge ${cls}">${esc(ev.severity)}</span></td>
      <td class="detail">${ev.detail ? esc(JSON.stringify(ev.detail, null, 0)) : '–'}</td>`;
    body.appendChild(tr);
  }
}

$('ev-filter').addEventListener('click', (e) => {
  const btn = e.target.closest('.seg');
  if (!btn) return;
  evSeverity = btn.dataset.s;
  $('ev-filter').querySelectorAll('.seg').forEach((b) => b.classList.toggle('active', b === btn));
  loadEvents();
});
$('ev-refresh').onclick = loadEvents;
for (const id of ['ev-robot', 'ev-kind']) {
  $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') loadEvents(); });
}

loadEvents();
pingDb();
setInterval(loadEvents, 20000);
