// Minimal service worker: makes the site installable and keeps the app shell available; never caches API/photo data.
const SHELL = 'facematch-shell-v3', FILES = ['/', '/static/style.css', '/static/app.js', '/static/theme.js', '/static/icon.svg'];
self.addEventListener('install', e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || !(FILES.includes(u.pathname))) return;
  e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open(SHELL).then(ch => ch.put(e.request, c)); return r; }).catch(() => caches.match(e.request)));
});
