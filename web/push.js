/* PWA install + Web Push subscription, shared by the dashboard and admin page.
 *
 * Why this instead of a native app: Web Push needs no app store and no Apple
 * Developer membership. Android Chrome pushes to an installed PWA directly;
 * iOS 16.4+ does the same once the site is added to the Home Screen.
 */
'use strict';

window.ITRIPush = (() => {
  const b64ToU8 = (b64) => {
    const pad = '='.repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
  };

  const isStandalone = () =>
    window.matchMedia('(display-mode: standalone)').matches ||
    window.navigator.standalone === true;

  const isIOS = () =>
    /iP(hone|ad|od)/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

  async function register() {
    if (!('serviceWorker' in navigator)) return null;
    try {
      return await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    } catch (e) {
      console.warn('service worker registration failed', e);
      return null;
    }
  }

  /** What the UI should say, without asking for permission yet. */
  async function status() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      // Safari on iOS only exposes PushManager to home-screen web apps
      if (isIOS() && !isStandalone()) {
        return { state: 'needs-install',
                 hint: 'iOS 需要先「加入主畫面」才能開啟通知' };
      }
      return { state: 'unsupported', hint: '這個瀏覽器不支援 Web Push' };
    }
    if (Notification.permission === 'denied') {
      return { state: 'denied', hint: '通知權限被封鎖,請到瀏覽器設定解除' };
    }
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = reg && (await reg.pushManager.getSubscription());
    return sub ? { state: 'on' } : { state: 'off' };
  }

  async function enable() {
    const reg = (await navigator.serviceWorker.getRegistration()) || (await register());
    if (!reg) throw new Error('service worker 無法註冊');

    const perm = await Notification.requestPermission();
    if (perm !== 'granted') throw new Error('使用者未允許通知');

    const res = await fetch('/api/push/key');
    if (!res.ok) throw new Error('伺服器未設定 Web Push (VAPID)');
    const { public_key } = await res.json();

    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: b64ToU8(public_key),
    });

    const body = sub.toJSON();
    body.label = `${navigator.platform || 'device'} · ${new Date().toLocaleDateString('zh-TW')}`;
    const save = await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!save.ok) throw new Error('伺服器拒絕訂閱:' + (await save.text()));
    return await save.json();
  }

  async function disable() {
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = reg && (await reg.pushManager.getSubscription());
    if (!sub) return;
    await fetch('/api/push/unsubscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint: sub.endpoint }),
    });
    await sub.unsubscribe();
  }

  return { register, status, enable, disable, isStandalone, isIOS };
})();

navigator.serviceWorker && window.ITRIPush.register();
