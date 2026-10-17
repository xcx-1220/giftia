/**
 * 推送中转（Cloudflare Worker，免费额度 100 万次/天）
 *
 * 为什么需要它：艾拉的服务器在**大陆**，连不上 Google 的推送服务
 * （`fcm.googleapis.com` —— 安卓/Chrome 的 Web Push 全走它）。实测服务器上
 * `curl https://fcm.googleapis.com` 6 秒超时、`google.com` 同样超时。
 *
 * 但服务器**能连 Cloudflare**（网站本来就是走 Cloudflare 隧道出去的）。
 * 所以：服务器 → 这个 Worker → fcm。Worker 在 Cloudflare 的网络上，出得去。
 *
 * 安全：
 *   · 必须带 `X-Relay-Secret`（和 Worker 的环境变量 RELAY_SECRET 一致），
 *     否则 403 —— 不然它就是个公开的转发器，会被别人拿去用。
 *   · 目标域名走白名单，只允许各大浏览器的推送服务，不是任意 URL 的代发。
 *
 * 部署（不需要 API token，控制台贴代码就行）：
 *   1. Cloudflare → Workers & Pages → Create → Worker
 *   2. 名字取 `aila-push-relay` → Deploy
 *   3. Edit code → 把本文件全部内容粘进去 → Deploy
 *   4. Settings → Variables and Secrets → 加一个 **Secret**：
 *        RELAY_SECRET = 一串你随手生成的长字符串（≥32 位）
 *   5. 记下 Worker 的地址，形如：
 *        https://aila-push-relay.<你的子域>.workers.dev
 *   6. 把地址和那串密钥告诉负责部署的人（写进服务器的 .env）
 */

const ALLOWED_HOSTS = [
  'fcm.googleapis.com',                      // Chrome / 安卓
  'updates.push.services.mozilla.com',       // Firefox
  'web.push.apple.com',                      // Safari（iOS 16.4+ / macOS）
  'push.apple.com',
  'notify.windows.com',                      // Edge（Windows）
  'wns2-par02p.notify.windows.com',
  'wns2-by3p.notify.windows.com',
];

function hostAllowed(host) {
  const h = host.toLowerCase();
  return ALLOWED_HOSTS.some((d) => h === d || h.endsWith('.' + d));
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== '/push') {
      return new Response('not found', { status: 404 });
    }
    if (request.method !== 'POST') {
      return new Response('method not allowed', { status: 405 });
    }
    // 1) 调用方必须是我们的服务器
    const secret = request.headers.get('X-Relay-Secret') || '';
    if (!env.RELAY_SECRET || secret !== env.RELAY_SECRET) {
      return new Response('forbidden', { status: 403 });
    }
    // 2) 目标是"某个浏览器的推送服务"
    const target = url.searchParams.get('to') || '';
    let parsed;
    try {
      parsed = new URL(target);
    } catch (e) {
      return new Response('bad target', { status: 400 });
    }
    if (parsed.protocol !== 'https:' || !hostAllowed(parsed.hostname)) {
      return new Response('host not allowed', { status: 400 });
    }
    // 3) 原样转发 VAPID 头和空载荷
    const headers = {
      Authorization: request.headers.get('X-Push-Authorization') || '',
      TTL: request.headers.get('X-Push-TTL') || '86400',
      Urgency: request.headers.get('X-Push-Urgency') || 'normal',
      'Content-Length': '0',
    };
    let upstream;
    try {
      upstream = await fetch(target, { method: 'POST', headers, body: '' });
    } catch (err) {
      return new Response(
        JSON.stringify({ status: 0, error: String(err) }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    }
    // 把推送服务的状态码原样带回去（404/410 = 这个订阅失效了，服务端会清掉）
    return new Response(
      JSON.stringify({ status: upstream.status }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    );
  },
};
