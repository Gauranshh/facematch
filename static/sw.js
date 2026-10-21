// Service worker: lets the site install like an app and open instantly. It caches only the app shell — never photos or API data.
const SHELL = 'facematch-shell-v4', FILES = ['/', '/offline', '/static/style.css', '/static/app.js', '/static/theme.js', '/static/icon.svg', '/static/icons/icon-192.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin) return;
  if (e.request.mode === 'navigate' && (u.pathname === '/' )) {           // open the shell; if the server is unreachable show the offline page
    e.respondWith(fetch(e.request).catch(() => caches.match('/offline')));
    return;
  }
  if (FILES.includes(u.pathname) || u.pathname.startsWith('/static/fonts/')) {
    e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open(SHELL).then(ch => ch.put(e.request, c)); return r; }).catch(() => caches.match(e.request)));
  }
});
