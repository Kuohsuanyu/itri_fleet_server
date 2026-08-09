/* Service worker: Web Push receiver for the installed dashboard.
 *
 * Caching is deliberately minimal. This is a live monitoring tool -- serving a
 * stale fleet view from cache would be worse than showing an offline message,
 * so only the shell is cached and every API/WebSocket call goes to the network.
 */
'use strict';

const SHELL = 'itri-shell-v1';
const SHELL_FILES = ['/static/style.css', '/static/admin.css', '/static/app.js'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // never serve fleet data from cache
  if (url.pathname.startsWith('/api/') || url.pathname === '/ws') return;
  if (e.request.method !== 'GET') return;
  if (!SHELL_FILES.includes(url.pathname)) return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(SHELL).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});

self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data && e.data.text() }; }

  const critical = d.severity === 'critical' && !d.resolved;
  e.waitUntil(self.registration.showNotification(d.title || 'ITRI Fleet', {
    body: d.body || '',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    tag: d.resolved ? 'resolved' : (d.tag || 'alert'),
    renotify: critical,
    requireInteraction: critical,     // stays on screen until acknowledged
    vibrate: critical ? [200, 100, 200, 100, 200] : [150],
    timestamp: (d.ts || Date.now() / 1000) * 1000,
    data: { url: '/' },
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if ('focus' in c) { c.navigate(target); return c.focus(); }
      }
      return self.clients.openWindow(target);
    })
  );
});
