/* 艾拉 · Service Worker
 *
 * 只做一件事：把静态壳（HTML/CSS/JS/图标/清单）留在本地，第二次打开时瞬间出现，
 * 不用等网络。聊天内容一律不碰 —— 那是实时数据，缓存会让她"卡在过去"。
 *
 * 想更新壳的时候，改下面的 VERSION；activate 里会把不认识的缓存全删掉。
 */
const VERSION = 'v1';
const CACHE = 'ella-shell-' + VERSION;

// 用相对路径：整个 web/ 可以挂在任意子路径下（比如 /static/ella/）
const SHELL = [
  './',
  './index.html',
  './style.css',
  './app.js',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // 逐个 add，且 cache:'reload' 绕过 HTTP 缓存，保证装进去的是刚部署的版本。
    // 用 allSettled 的语义（catch 掉单个失败）：一张图标 404 不该让整个 SW 装不上。
    await Promise.all(SHELL.map((url) =>
      cache.add(new Request(url, { cache: 'reload' })).catch(() => {})
    ));
    // 新 SW 立刻待命，配合下面的 clients.claim()，
    // 这样用户刷新一次就能拿到新壳，而不是要关掉所有标签页
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;

  // 只处理 GET；POST 的 /api/chat、/api/login 直接放行
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // 跨域的（其实也没有）不插手
  if (url.origin !== self.location.origin) return;

  // 绝不缓存 /api/：
  // 1) 登录/聊天是实时的，缓存住就会出现"她说的话永远是上一句"；
  // 2) 带 cookie 的响应也不该落到 Cache Storage 里。
  // 同时匹配 /api/ 出现在路径中间的情况（反代到子路径时）。
  if (url.pathname === '/api' || url.pathname.indexOf('/api/') !== -1) return;

  if (req.mode === 'navigate') {
    event.respondWith(navigate(req));
    return;
  }

  event.respondWith(asset(req));
});

// 页面导航：先给本地壳（瞬间出画面），同时悄悄去网络取新的存下来，下次打开就是新的
async function navigate(req) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match('./index.html');

  const fresh = fetch(req).then((res) => {
    if (res && res.ok) cache.put('./index.html', res.clone()).catch(() => {});
    return res;
  }).catch(() => null);

  if (cached) return cached;

  const res = await fresh;
  if (res) return res;

  // 第一次打开就断网，也得给一句人话，而不是浏览器的恐龙页
  return new Response('现在连不上，等一下再打开试试。', {
    status: 503,
    headers: { 'Content-Type': 'text/plain; charset=utf-8' }
  });
}

// 静态资源：本地有就直接用（ignoreSearch 让 ?v=xxx 也能命中），
// 同时在后台更新一份，下次生效 —— 也就是 stale-while-revalidate
async function asset(req) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(req, { ignoreSearch: true });

  const fresh = fetch(req).then((res) => {
    // 只存自己域名下、真正成功的响应；opaque/错误响应存了会污染缓存
    if (res && res.ok && res.type === 'basic') {
      cache.put(req, res.clone()).catch(() => {});
    }
    return res;
  }).catch(() => null);

  if (cached) return cached;

  const res = await fresh;
  if (res) return res;

  return new Response('', { status: 504, statusText: 'offline' });
}

// ── 她主动找你（Web Push）──────────────────────────────────────
// 推送本身是**空的**（无载荷）：标准载荷要 AES-128-GCM，而服务端是零依赖的
// Python，没有 AES。所以流程是：空响铃 → 这里来取内容 → 弹通知。
self.addEventListener('push', (event) => {
  event.waitUntil((async () => {
    let data = {};
    try {
      const r = await fetch('./api/push/last', { credentials: 'same-origin', cache: 'no-store' });
      if (r.ok) data = await r.json();
    } catch (e) { /* 取不到就用兜底文案 */ }
    const text = String(data.text || '她好像有话想跟你说。');
    await self.registration.showNotification(String(data.title || '艾拉'), {
      body: text.length > 120 ? text.slice(0, 118) + '…' : text,
      icon: './icons/icon-192.png',
      badge: './icons/icon-192.png',
      tag: 'aila-outreach',       // 同一条只留一个，不刷屏
      renotify: true,
      data: { url: './' },
    });
  })());
});

// 点通知 → 回到已经开着的页面，没有就打开一个
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const all = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      if ('focus' in c) return c.focus();
    }
    return clients.openWindow('./');
  })());
});
