/* 車輛註冊:建立、發 token、撤銷、刪除。 */
'use strict';

renderShell('車輛註冊');

async function loadRobots() {
  let data;
  try {
    data = await api('/api/admin/robots');
  } catch (e) {
    $('robots-empty').textContent = '讀取失敗:' + e.message;
    $('robots-empty').style.display = 'block';
    return;
  }
  const body = $('robots-body');
  body.innerHTML = '';
  $('robot-count').textContent = `${data.count} 台已註冊`;
  $('robots-empty').style.display = data.count ? 'none' : 'block';

  for (const r of data.robots) {
    let cred, credCls;
    if (r.revoked_at)         { cred = '已撤銷';   credCls = 'bad'; }
    else if (r.enrolled)      { cred = '已發憑證'; credCls = 'ok'; }
    else if (r.pending_token) { cred = '待登記';   credCls = 'pending'; }
    else                      { cred = '無憑證';   credCls = 'mute'; }

    const live = r.live;
    const liveHtml = live
      ? `<span class="badge ${live.online ? 'ok' : 'mute'}">${esc(live.online ? live.state : 'offline')}</span>`
      : '<span class="dim">–</span>';

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="mono">${esc(r.id)}</td>
      <td>${esc(r.name)}</td>
      <td><span class="badge ${credCls}">${cred}</span></td>
      <td>${liveHtml}</td>
      <td class="mono">${live?.battery != null ? live.battery.toFixed(0) + '%' : '<span class="dim">–</span>'}</td>
      <td class="dim">${fmtTime(r.last_seen)}</td>
      <td class="dim">${(r.tags || []).map(esc).join(', ') || '–'}</td>
      <td class="right"><div class="row-actions">
        <button class="btn" data-act="token" data-id="${esc(r.id)}" data-name="${esc(r.name)}">重發 token</button>
        <button class="btn danger" data-act="revoke" data-id="${esc(r.id)}">撤銷</button>
        <button class="btn danger" data-act="delete" data-id="${esc(r.id)}">刪除</button>
      </div></td>`;
    body.appendChild(tr);
  }
}

$('robots-body').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const { act, id, name } = btn.dataset;
  try {
    if (act === 'token') {
      showToken(name || id, await api(`/api/admin/robots/${encodeURIComponent(id)}/token`,
                                      { method: 'POST' }));
      toast(`已為 ${id} 產生新 token`);
    } else if (act === 'revoke') {
      if (!confirm(`撤銷 ${id} 的憑證?\n\n該車會立刻無法發送資料,歷史紀錄保留。`)) return;
      const r = await api(`/api/admin/robots/${encodeURIComponent(id)}/revoke`, { method: 'POST' });
      toast(`${id} 已撤銷,踢掉 ${r.sessions_killed} 條連線`);
    } else if (act === 'delete') {
      if (!confirm(`刪除 ${id} 的註冊資料?\n\n遙測歷史不會被刪除。`)) return;
      await api(`/api/admin/robots/${encodeURIComponent(id)}`, { method: 'DELETE' });
      toast(`${id} 已刪除`);
    }
    loadRobots();
  } catch (err) { toast(err.message, true); }
});

$('btn-new').onclick = () => {
  $('newbox').hidden = !$('newbox').hidden;
  if (!$('newbox').hidden) $('f-name').focus();
};
$('btn-cancel').onclick = () => { $('newbox').hidden = true; };

$('newbox').onsubmit = async (e) => {
  e.preventDefault();
  const name = $('f-name').value.trim();
  if (!name) return;
  try {
    const created = await api('/api/admin/robots', {
      method: 'POST',
      body: JSON.stringify({ name, id: $('f-id').value.trim() || undefined,
                             ttl_minutes: Number($('f-ttl').value) || 30 }),
    });
    $('newbox').hidden = true;
    $('f-name').value = ''; $('f-id').value = '';
    showToken(created.name, created);
    loadRobots();
  } catch (err) { toast(err.message, true); }
};

function showToken(name, t) {
  $('tb-name').textContent = name;
  const steps = Array.isArray(t.install?.steps) ? t.install.steps : [String(t.install)];
  $('tb-cmd').textContent = steps.join('\n');
  $('tb-exp').textContent = new Date(t.expires_at).toLocaleTimeString('zh-TW', { hour12: false });
  $('tokenbox').hidden = false;
  $('tokenbox').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

$('tb-close').onclick = () => { $('tokenbox').hidden = true; };
$('tb-copy').onclick = async () => {
  try {
    await navigator.clipboard.writeText($('tb-cmd').textContent);
    toast('已複製到剪貼簿');
  } catch {
    // clipboard needs a secure context; select it so Ctrl+C still works
    const range = document.createRange();
    range.selectNodeContents($('tb-cmd'));
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    toast('已選取,請按 Ctrl+C');
  }
};

loadRobots();
pingDb();
setInterval(loadRobots, 15000);
