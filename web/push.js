/* 让她能主动找你（Web Push）。
 *
 * **只有搭档能看到这个按钮**：访客访问 /api/push/status 会拿到 403，
 * 这里就直接什么都不做 —— 她主动联系你这件事，绝不会打扰访客。
 *
 * 为什么是"按钮"而不是自动订阅：浏览器要求订阅必须发生在**用户手势**里，
 * 偷偷订阅会被拒（而且很没礼貌）。
 *
 * 样式全用内联，不碰 style.css。
 */
(function () {
  'use strict';

  function urlB64ToUint8Array(base64) {
    const pad = '='.repeat((4 - (base64.length % 4)) % 4);
    const raw = atob((base64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  async function api(path, opts) {
    try {
      const r = await fetch(path, Object.assign({ credentials: 'same-origin', cache: 'no-store' }, opts));
      if (!r.ok) return null;
      return await r.json();
    } catch (e) {
      return null;
    }
  }

  async function main() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
    // 有些浏览器根本没有 Notification（比如 iOS Safari 16.4 以下、部分内置浏览器）。
    // 不先判存在性的话，下面那一行会抛 ReferenceError，整个 main() 直接中断 ——
    // 表现就是"按钮从来不出现"，而且控制台里一眼看不出原因（踩过）。
    if (typeof Notification === 'undefined') return;
    if (Notification.permission === 'denied') return;    // 明确拒绝过，尊重
    if (!('serviceWorker' in navigator)) return;

    const btn = document.createElement('button');
    btn.textContent = '让她能主动找你';
    btn.setAttribute('aria-label', '开启通知，让她能主动找你');
    btn.style.cssText = [
      'position:fixed', 'left:50%', 'transform:translateX(-50%)',
      'bottom:calc(env(safe-area-inset-bottom, 0px) + 92px)', 'z-index:30',
      'padding:10px 18px', 'border-radius:999px', 'border:1px solid var(--accent-edge, #8fc9c3)',
      'background:var(--card, #fff)', 'color:var(--accent-ink, #146660)',
      'font-size:13px', 'font-family:inherit', 'cursor:pointer',
      'box-shadow:var(--shadow, 0 2px 8px rgba(20,60,58,.08))',
      'opacity:0', 'transition:opacity .4s ease',
    ].join(';');

    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.textContent = '正在开启…';
      try {
        const perm = await Notification.requestPermission();
        if (perm !== 'granted') {
          btn.remove();
          return;
        }
        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlB64ToUint8Array(status.public_key),
        });
        const saved = await api('/api/push/subscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(sub.toJSON ? sub.toJSON() : sub),
        });
        if (saved) {
          btn.textContent = '好了，她会来找你的';
          setTimeout(() => btn.remove(), 1800);
        } else {
          btn.textContent = '没设置成功，回头再试';
          btn.disabled = false;
        }
      } catch (e) {
        btn.textContent = '没设置成功，回头再试';
        btn.disabled = false;
      }
    });

    document.body.appendChild(btn);
    requestAnimationFrame(() => { btn.style.opacity = '1'; });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', main, { once: true });
  } else {
    main();
  }
})();
