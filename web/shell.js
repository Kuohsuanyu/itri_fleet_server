/* Shared page shell: top bar, mobile drawer, notification prompt.
 *
 * Wrapped in an IIFE so it can be loaded alongside app.js and admin_*.js
 * without colliding on their own `$` / `esc` globals.
 *
 * Layout rule: on a phone the top bar keeps only the hamburger and the bell.
 * Navigation, connection status and logout move into a slide-in drawer --
 * cramming six nav links plus status text into 390 px is how the notification
 * button ended up pushed off screen in the first place.
 */
'use strict';

window.ITRIShell = (() => {
  // Admin pages answer only on the private listener. On the public one they
  // 404, so the links are dropped rather than rendered dead -- a button that
  // always fails reads as a broken site, not as a boundary.
  // Defaults to hiding them: if /surface.js somehow did not load, showing
  // fewer options is the failure that does not produce error pages.
  const ADMIN_OK = !!(window.ITRI_SURFACE && window.ITRI_SURFACE.admin);
  const NAV = [
    ['/', '監控', '📊'],
    ['/admin/robots', '車輛', '🚚'],
    ['/admin/topics', 'Topic', '🏷'],
    ['/admin/alerts', '預警', '🔔'],
    ['/admin/events', '事件', '📋'],
    ['/admin/system', '系統', '⚙'],
  ].filter(([href]) => ADMIN_OK || !href.startsWith('/admin'));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const DISMISS_KEY = 'itri.push.dismissed';

  function render({ title = 'Fleet Console', live = false } = {}) {
    const here = location.pathname;
    const navHtml = (cls) => NAV.map(([href, label, icon]) =>
      `<a href="${href}" class="${here === href ? 'on' : ''}">` +
      (cls === 'drawer-nav' ? `<span class="ico">${icon}</span>` : '') +
      `${label}</a>`).join('');

    document.body.insertAdjacentHTML('afterbegin', `
      <header class="topbar">
        <button class="burger" id="burger" aria-label="選單">☰</button>
        <div class="brand">
          <span class="logo">ITRI</span>
          <span class="brand-sub">${esc(title)}</span>
          <nav class="nav desktop-only">${navHtml('nav')}</nav>
        </div>
        <div class="conn">
          ${live ? `<span class="dot" id="conn-dot"></span>
                    <span class="desktop-only" id="conn-text">connecting…</span>
                    <span class="sep desktop-only"></span>
                    <span class="desktop-only" id="mqtt-text">MQTT –</span>`
                 : `<span class="dot" id="db-dot"></span>
                    <span class="desktop-only" id="db-text">資料庫 –</span>`}
          <span class="sep desktop-only"></span>
          <button class="bell" id="btn-push" title="通知">🔔</button>
          <span class="sep desktop-only"></span>
          <a class="logout desktop-only" href="/logout">登出</a>
        </div>
      </header>

      <div class="scrim" id="scrim" hidden></div>
      <aside class="drawer" id="drawer" hidden>
        <div class="drawer-head">
          <span class="logo">ITRI</span>
          <button class="btn" id="drawer-close" aria-label="關閉">✕</button>
        </div>
        <nav class="drawer-nav">${navHtml('drawer-nav')}</nav>
        <div class="drawer-foot">
          <div class="drawer-status" id="drawer-status">—</div>
          <a class="btn" href="/logout">登出</a>
        </div>
      </aside>

      <div class="pushprompt" id="pushprompt" hidden>
        <div class="pp-text">
          <b>要開啟預警通知嗎?</b>
          <span id="pp-sub">車輛異常會直接推到這台裝置,不用一直盯著畫面。</span>
        </div>
        <div class="pp-actions">
          <button class="btn primary" id="pp-yes">開啟通知</button>
          <button class="btn" id="pp-no">稍後</button>
        </div>
      </div>`);

    document.body.insertAdjacentHTML('beforeend', '<div class="toast" id="toast"></div>');
    wireDrawer();
    wireBell();
    setTimeout(maybePrompt, 2500);
  }

  /* ------------------------------------------------------------- drawer */

  function openDrawer(open) {
    document.getElementById('drawer').hidden = !open;
    document.getElementById('scrim').hidden = !open;
    document.body.style.overflow = open ? 'hidden' : '';
  }

  function wireDrawer() {
    document.getElementById('burger').onclick = () => openDrawer(true);
    document.getElementById('drawer-close').onclick = () => openDrawer(false);
    document.getElementById('scrim').onclick = () => openDrawer(false);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') openDrawer(false);
    });
  }

  /** Mirrors whatever the page put in the header into the drawer footer. */
  function setStatus(text) {
    const el = document.getElementById('drawer-status');
    if (el) el.textContent = text;
  }

  /* --------------------------------------------------------------- bell */

  async function refreshBell() {
    const btn = document.getElementById('btn-push');
    if (!btn || !window.ITRIPush) return;
    const s = await ITRIPush.status();
    btn.className = 'bell';
    btn.dataset.state = s.state;
    if (s.state === 'on') { btn.textContent = '🔔'; btn.classList.add('on'); btn.title = '通知已開啟'; }
    else if (s.state === 'off') { btn.textContent = '🔔'; btn.title = '開啟通知'; }
    else { btn.textContent = '🔕'; btn.classList.add('warn'); btn.title = s.hint || '不支援'; }
  }

  function wireBell() {
    const btn = document.getElementById('btn-push');
    if (!btn) return;
    btn.onclick = async () => {
      const state = btn.dataset.state;
      try {
        if (state === 'on') {
          if (!confirm('關閉這台裝置的通知?')) return;
          await ITRIPush.disable();
          note('已關閉通知');
        } else if (state === 'needs-install') {
          alert('iOS 需要先用 Safari 開啟這個網站,按「分享」→「加入主畫面」,'
              + '再從主畫面的圖示開啟,才能開啟通知。');
        } else if (state === 'unsupported' || state === 'denied') {
          alert(btn.title || '這個瀏覽器無法開啟通知。');
        } else {
          const r = await ITRIPush.enable();
          note(`已開啟通知,目前 ${r.devices} 台裝置訂閱`);
        }
      } catch (e) {
        note('失敗:' + e.message, true);
      }
      refreshBell();
    };
    refreshBell();
  }

  /* ------------------------------------------------------------- prompt */

  /** Browsers only allow requestPermission() from a user gesture, so this asks
   *  in-page first and lets the button carry the gesture. */
  async function maybePrompt() {
    if (!window.ITRIPush) return;
    if (localStorage.getItem(DISMISS_KEY)) return;
    const s = await ITRIPush.status();
    if (s.state === 'on' || s.state === 'denied' || s.state === 'unsupported') return;

    const box = document.getElementById('pushprompt');
    if (s.state === 'needs-install') {
      document.getElementById('pp-sub').textContent =
        'iOS 請先按「分享」→「加入主畫面」,再從主畫面開啟本站即可開通知。';
      document.getElementById('pp-yes').hidden = true;
    }
    box.hidden = false;

    document.getElementById('pp-yes').onclick = async () => {
      try {
        const r = await ITRIPush.enable();
        note(`已開啟通知,目前 ${r.devices} 台裝置訂閱`);
        box.hidden = true;
      } catch (e) {
        note('失敗:' + e.message, true);
      }
      refreshBell();
    };
    document.getElementById('pp-no').onclick = () => {
      localStorage.setItem(DISMISS_KEY, String(Date.now()));
      box.hidden = true;
    };
  }

  function note(msg, isError = false) {
    const el = document.getElementById('toast');
    if (!el) { alert(msg); return; }
    el.textContent = msg;
    el.className = 'toast show' + (isError ? ' err' : '');
    clearTimeout(note._t);
    note._t = setTimeout(() => { el.className = 'toast'; }, 3400);
  }

  return { render, refreshBell, setStatus, note };
})();
