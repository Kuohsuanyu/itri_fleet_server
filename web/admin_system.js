/* 系統:資料庫用量、推播裝置、外網流量、MQTT 統計。 */
'use strict';

renderShell('系統');

async function loadStorage() {
  let d;
  try {
    d = await api('/api/storage');
  } catch (e) {
    $('st-note').textContent = '資料庫離線:' + e.message;
    return;
  }
  $('st-rows').textContent = (d.telemetry_rows || 0).toLocaleString();
  $('st-size').textContent = fmtBytes(d.telemetry_bytes);
  $('st-per').textContent = d.bytes_per_row ? d.bytes_per_row + ' B' : '–';
  $('st-rate').textContent = d.observed_rows_per_s ? d.observed_rows_per_s + ' /s' : '–';
  $('st-day').textContent = d.projected_bytes_per_day
    ? (d.projected_bytes_per_day / 1e9).toFixed(2) + ' GB' : '–';
  $('st-ret').textContent = d.projected_gb_at_retention != null
    ? d.projected_gb_at_retention + ' GB' : '–';
  $('st-note').textContent =
    `保留 ${d.retention_days} 天,共 ${d.partition_count} 個分區。`
    + ` 整個資料庫 ${fmtBytes(d.database_bytes)}。過期分區以 DROP TABLE 清除,不留 bloat。`;

  const body = $('parts-body');
  body.innerHTML = '';
  for (const p of d.partitions) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td class="mono">${esc(p.part)}</td>
                    <td class="mono">${(p.approx_rows || 0).toLocaleString()}</td>
                    <td class="mono">${fmtBytes(p.bytes)}</td>`;
    body.appendChild(tr);
  }
}

async function loadDevices() {
  let d;
  try { d = await api('/api/push/devices'); } catch { return; }
  const body = $('dev-body');
  body.innerHTML = '';
  $('dev-empty').style.display = d.count ? 'none' : 'block';
  for (const s of d.devices) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${esc(s.label || '(未命名)')}</td>
      <td class="detail dim">${esc((s.user_agent || '').slice(0, 70))}</td>
      <td class="dim">${fmtTime(s.created_at)}</td>
      <td class="dim">${s.last_ok ? fmtTime(s.last_ok) : '尚未送過'}</td>`;
    body.appendChild(tr);
  }
}

async function loadMetrics() {
  let d;
  try { d = await api('/api/metrics'); } catch { return; }
  const m = d.mqtt, b = m.broker_stats || {}, h = d.history || {};

  // QoS 1 redeliveries caught before they reached the archive. A number that
  // keeps climbing means the uplink is flapping.
  $('m-dup').textContent = (m.duplicate_batches || 0).toLocaleString();
  // Agents predating the batch envelope. Those batches cannot be deduplicated.
  $('m-old').textContent = (m.unversioned_batches || 0).toLocaleString();
  const disk = h.disk || {};
  $('m-disk').textContent = disk.free_pct != null ? disk.free_pct + '%' : '–';

  // Vehicles whose clock disagrees with the server's.
  const skew = m.clock_skew_s || {};
  const bad = Object.entries(skew).filter(([, v]) => Math.abs(v) >= 2);
  $('m-clock').textContent = bad.length;
  const card = $('clock-card'), body = $('clock-body');
  if (card && body) {
    card.hidden = bad.length === 0;
    body.innerHTML = '';
    for (const [id, v] of bad.sort((a, b2) => Math.abs(b2[1]) - Math.abs(a[1]))) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td class="mono">${esc(id)}</td>` +
        `<td class="mono">${Math.abs(v).toFixed(1)} s</td>` +
        `<td>${v > 0 ? '車子比伺服器快' : '車子比伺服器慢'}</td>`;
      body.appendChild(tr);
    }
  }
  $('e-rate').textContent = fmtBytes(d.rate_bps) + '/s';
  $('e-total').textContent = fmtBytes(d.total_bytes);
  $('e-month').textContent = d.projected_gb_month + ' GB';
  $('e-clients').textContent = d.ws_clients;
  $('m-mode').textContent = m.mode === 'embedded-inproc' ? '每車憑證' : m.mode;
  $('m-conn').textContent = b.connects ?? '–';
  $('m-auth').textContent = b.auth_failures ?? '–';
  $('m-acl').textContent = b.acl_denied ?? '–';
  $('m-buf').textContent = (h.buffered ?? 0) + (h.topics_buffered ?? 0);
  $('m-drop').textContent = h.rows_dropped ?? '–';
}

/* --------------------------------------------------------- push subscribe */

async function refreshPushBtn() {
  const btn = $('btn-push');
  const s = await ITRIPush.status();
  btn.className = 'btn primary';
  if (s.state === 'on') { btn.textContent = '🔔 這台裝置已開啟(點擊關閉)'; btn.className = 'btn'; }
  else if (s.state === 'off') { btn.textContent = '🔔 在這台裝置開啟通知'; }
  else { btn.textContent = '🔕 ' + (s.hint || '不支援'); btn.className = 'btn'; }
  btn.dataset.state = s.state;
}

$('btn-push').onclick = async () => {
  const state = $('btn-push').dataset.state;
  try {
    if (state === 'on') { await ITRIPush.disable(); toast('已關閉這台裝置的通知'); }
    else if (state === 'needs-install') {
      alert('iOS 需要先用 Safari 開啟,按分享 → 加入主畫面,再從主畫面開啟才能開通知。');
      return;
    } else if (state === 'unsupported' || state === 'denied') {
      alert('這個瀏覽器無法開啟通知。'); return;
    } else {
      const r = await ITRIPush.enable();
      toast(`已開啟,目前 ${r.devices} 台裝置訂閱`);
    }
  } catch (e) { toast('失敗:' + e.message, true); }
  refreshPushBtn();
  loadDevices();
};

function refreshAll() { loadStorage(); loadDevices(); loadMetrics(); }
refreshAll();
refreshPushBtn();
pingDb();
setInterval(refreshAll, 10000);
