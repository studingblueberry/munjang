// 서버(Termux)가 꺼져 있어도 앱 화면(/)이 열리도록 마지막으로 받은 화면을 보관한다.
// 글 데이터는 여기가 아니라 IndexedDB 에 있다. (localhost / https 에서만 서비스 워커가 동작)
const C = 'munjang-shell-v1';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin || u.pathname !== '/') return;
  e.respondWith(
    fetch(e.request)
      .then(r => { const c = r.clone(); caches.open(C).then(x => x.put('/', c)); return r; })
      .catch(() => caches.match('/'))
  );
});
