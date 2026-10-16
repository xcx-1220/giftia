/* 艾拉 · 前端逻辑（原生 JS，零依赖、零 CDN）
 *
 * 后端契约（同源，cookie 自动带）：
 *   POST /api/login  {pass}  -> 200 {ok:true} | 401 {error}
 *   POST /api/chat   {text}  -> 200 {reply, parts:[...]} | 401 | 429 | 504
 *   GET  /api/health         -> 200 {ok:true}
 *
 * 三处最关键的设计，都在下面各自的注释里写了"为什么"：
 *   1. 分段出现（speakParts）—— 节奏靠停顿，不靠动画
 *   2. 键盘遮挡（setupViewport）—— dvh 兜底 + visualViewport 精确修正
 *   3. 历史只留 100 条 + 不缓存 /api/（后者在 sw.js 里）
 */
'use strict';

(function () {
  /* ---------------- 常量 ---------------- */
  var KEY_PASS    = 'ella.pass';
  var KEY_HISTORY = 'ella.history';
  var KEY_FIRST   = 'ella.first';

  var MAX_HISTORY = 100;      // localStorage 不是数据库，只留最近 100 条
  var MAX_PARTS   = 6;        // 万一后端切太多段，这里兜一下，别刷屏

  // 她每两段之间的停顿：500~900ms 随机。
  // 固定值会听出机械感，随机区间才像人在打字。
  var PART_MIN = 500;
  var PART_MAX = 900;

  var TIMEOUT_CHAT  = 45000;  // 后端自己会给 504；这个只是防止请求永远挂着
  var TIMEOUT_SHORT = 10000;  // 登录 / 健康检查

  /* ---------------- DOM ---------------- */
  function $(id) { return document.getElementById(id); }

  var gateEl   = $('gate');
  var gateForm = $('gate-form');
  var gatePass = $('gate-pass');
  var gateBtn  = $('gate-btn');
  var gateMsg  = $('gate-msg');

  var chatEl   = $('chat');
  var streamEl = $('stream');
  var bannerEl = $('banner');
  var subEl    = $('topbar-sub');
  var menuBtn  = $('menu-btn');
  var menuEl   = $('menu');
  var formEl   = $('composer');
  var inputEl  = $('input');
  var sendEl   = $('send');

  /* ---------------- 小工具 ---------------- */

  // localStorage 在无痕模式/被禁用时会直接抛错；读写都包一层，
  // 坏掉的存储不该把整个聊天界面带崩，最差就是"这次不记住"。
  var store = {
    get: function (k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { window.localStorage.setItem(k, v); } catch (e) {} },
    del: function (k) { try { window.localStorage.removeItem(k); } catch (e) {} }
  };

  function randInt(min, max) {
    return min + Math.floor(Math.random() * (max - min + 1));
  }

  function wait(ms) {
    return new Promise(function (resolve) { window.setTimeout(resolve, ms); });
  }

  function makeError(status, message) {
    var err = new Error(message || ('HTTP ' + status));
    err.status = status;   // 0 / 无 status 都表示"网络层就断了"
    return err;
  }

  var reduceMotion = !!(window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  /* ---------------- 请求 ---------------- */

  function api(path, body, timeout) {
    var opts = {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin',   // 同源，cookie 自动带；写出来是为了不用猜
      headers: {},
      cache: 'no-store'             // 聊天的响应绝不能被 HTTP 缓存拦住
    };

    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }

    var timer = null;
    var ctl = null;
    if (typeof window.AbortController === 'function' && timeout) {
      ctl = new window.AbortController();
      opts.signal = ctl.signal;
      timer = window.setTimeout(function () { ctl.abort(); }, timeout);
    }

    function done() { if (timer) { window.clearTimeout(timer); timer = null; } }

    return window.fetch(path, opts).then(function (res) {
      return res.text().then(function (raw) {
        var data = {};
        try { data = raw ? JSON.parse(raw) : {}; } catch (e) { data = {}; }
        if (!res.ok) {
          throw makeError(res.status, data && data.error ? data.error : '');
        }
        return data;
      });
    }).then(function (value) { done(); return value; },
            function (err) { done(); throw err; });
  }

  /* ---------------- 历史 ---------------- */

  var history = loadHistory();

  function loadHistory() {
    var raw;
    try { raw = JSON.parse(store.get(KEY_HISTORY) || '[]'); } catch (e) { raw = []; }
    if (!Array.isArray(raw)) return [];
    return raw.filter(function (m) {
      return m && (m.role === 'her' || m.role === 'me') && typeof m.text === 'string';
    }).slice(-MAX_HISTORY);
  }

  function saveHistory() {
    // 超了就从最早的开始丢。只存 {role, text, ts}，
    // 不存任何 DOM 状态，免得以后改样式被旧数据卡住。
    if (history.length > MAX_HISTORY) history = history.slice(-MAX_HISTORY);
    store.set(KEY_HISTORY, JSON.stringify(history));
  }

  /* ---------------- 渲染 ---------------- */

  // 全角括号优先；顺手兼容半角，免得后端偶尔写成 (笑)
  var ASIDE_RE = /（[^（）]*）|\([^()]*\)/;

  // 把一段话拆成「台词 + 括号旁白」：旁白用更淡的斜体，
  // 如果整段都只是旁白，就干脆不给它气泡（读起来像剧本里的动作提示）。
  function buildBubble(text) {
    var bubble = document.createElement('div');
    bubble.className = 'bubble';

    var rest = String(text);
    var onlyAside = false;
    var spoke = false;   // 有没有出现过"真正说的话"
    var guard = 0;

    while (rest && guard++ < 40) {
      var m = ASIDE_RE.exec(rest);
      if (!m) break;
      if (m.index > 0) {
        bubble.appendChild(document.createTextNode(rest.slice(0, m.index)));
        spoke = true;
      }
      var span = document.createElement('span');
      span.className = 'aside';
      span.textContent = m[0];
      bubble.appendChild(span);
      onlyAside = true;
      rest = rest.slice(m.index + m[0].length);
    }

    if (rest) {
      bubble.appendChild(document.createTextNode(rest));
      spoke = true;
    }

    if (onlyAside && !spoke) bubble.className = 'bubble bubble--aside';
    return bubble;
  }

  function renderRow(msg) {
    var li = document.createElement('li');
    li.className = 'msg msg--' + (msg.role === 'me' ? 'me' : 'her');
    li.appendChild(buildBubble(msg.text));
    return li;
  }

  function appendMessage(msg, opts) {
    opts = opts || {};
    var animate = opts.animate !== false;
    var persist = opts.persist !== false;

    var li = renderRow(msg);
    // 从历史里恢复的气泡不播动画：一屏 100 条一起飘进来太吵
    if (!animate) li.className += ' msg--static';
    streamEl.appendChild(li);

    if (persist) { history.push(msg); saveHistory(); }
    scrollToEnd();
    return li;
  }

  function addNote(text) {
    var li = document.createElement('li');
    li.className = 'note';
    li.textContent = text;
    streamEl.appendChild(li);
    scrollToEnd();
  }

  /* ---------------- 滚动 ---------------- */

  function nearBottom() {
    return streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight < 90;
  }

  function scrollToEnd(force) {
    // 用户正翻上去看老消息时不把他拽回来；只有本来就在底部才自动跟。
    if (!force && !nearBottom()) return;
    var top = streamEl.scrollHeight;
    if (reduceMotion || typeof streamEl.scrollTo !== 'function') {
      streamEl.scrollTop = top;
      return;
    }
    streamEl.scrollTo({ top: top, behavior: 'smooth' });
  }

  /* ---------------- "正在输入" ---------------- */

  var typingEl = null;

  function showTyping() {
    if (typingEl) return;
    var li = document.createElement('li');
    li.className = 'typing';
    li.setAttribute('aria-hidden', 'true');   // 纯装饰，读屏不用念

    var box = document.createElement('div');
    box.className = 'typing__bubble';
    for (var i = 0; i < 3; i++) {
      var dot = document.createElement('span');
      dot.className = 'typing__dot';
      box.appendChild(dot);
    }
    li.appendChild(box);
    streamEl.appendChild(li);
    typingEl = li;
    scrollToEnd();
  }

  function hideTyping() {
    if (typingEl && typingEl.parentNode) typingEl.parentNode.removeChild(typingEl);
    typingEl = null;
  }

  /* ---------------- 顶栏 / 提示条 ---------------- */

  function dayCount() {
    var first = parseInt(store.get(KEY_FIRST) || '', 10);
    if (!first || isNaN(first)) {
      first = Date.now();
      store.set(KEY_FIRST, String(first));
    }
    var days = Math.floor((Date.now() - first) / 86400000) + 1;
    return days > 0 ? days : 1;
  }

  function refreshSubtitle() {
    if (!subEl) return;
    subEl.textContent = 'Giftia · Type-07 · 第 ' + dayCount() + ' 天';
  }

  function showBanner(text) {
    if (!bannerEl) return;
    bannerEl.textContent = text;
    bannerEl.hidden = false;
  }

  function hideBanner() {
    if (!bannerEl) return;
    bannerEl.hidden = true;
    bannerEl.textContent = '';
  }

  /* ---------------- 页面切换 ---------------- */

  function setGateMsg(text, bad) {
    gateMsg.textContent = text || '';
    gateMsg.className = bad ? 'gate__msg gate__msg--bad' : 'gate__msg';
  }

  function showGate(msg, bad) {
    chatEl.hidden = true;
    gateEl.hidden = false;
    closeMenu();
    setGateMsg(msg || '', !!bad);
    // 手机上自动聚焦会直接把键盘顶起来，反而挡住了居中的卡片，所以不自动聚焦
  }

  function enterChat() {
    gateEl.hidden = true;
    chatEl.hidden = false;
    setGateMsg('');
    closeMenu();
    scrollToEnd(true);
    refreshSubtitle();
  }

  // 401 只在这里处理一次：清掉本地口令、退回口令页。
  // 不做自动重试 —— 口令不对时重试只会一直撞墙。
  function requireLogin(msg) {
    store.del(KEY_PASS);
    hideBanner();
    showGate(msg || '', true);
  }

  /* ---------------- 发送 & 分段出现 ---------------- */

  var busy = false;          // 一次只处理一轮，防重复提交
  var greetingOn = false;    // 首次进入的招呼还在播

  function setComposerEnabled(on) {
    inputEl.disabled = !on;
    sendEl.disabled = !on;
    // 她说完之后把光标交回输入框（iOS 上 disabled 会丢焦点，所以这里补回来）
    if (on) { try { inputEl.focus({ preventScroll: true }); } catch (e) { inputEl.focus(); } }
  }

  function normalizeParts(data) {
    var list = [];
    if (data && Array.isArray(data.parts)) list = data.parts.slice();
    // parts 缺失或全是空串时退回 reply，保证一定说点什么
    if (!list.length && data && typeof data.reply === 'string') list = [data.reply];
    return list.filter(function (p) {
      return typeof p === 'string' && p.replace(/\s+/g, '') !== '';
    }).slice(0, MAX_PARTS);
  }

  // ★ 分段出现：这就是"像真人一条条发"的地方。
  //
  // 必须串行 —— 每段之前先亮"正在输入"，停 500~900ms 再让气泡落下来。
  // 用 Promise.all 或直接 forEach 追加的话，几段会同时出现，
  // 那就又变成"一次性刷出来"的机器人了：节奏感来自停顿，不来自动画。
  function speakParts(parts, isCancelled, persist) {
    var chain = Promise.resolve();

    parts.forEach(function (part) {
      chain = chain.then(function () {
        if (isCancelled && isCancelled()) return;
        showTyping();
        return wait(randInt(PART_MIN, PART_MAX));
      }).then(function () {
        if (isCancelled && isCancelled()) return;
        hideTyping();
        appendMessage({ role: 'her', text: part, ts: Date.now() },
          { animate: true, persist: persist !== false });
      });
    });

    return chain.then(function () { hideTyping(); });
  }

  function handleChatError(err) {
    var status = err && err.status;

    if (err && err.empty) {
      addNote('（她这次什么都没说）');
      return;
    }
    if (status === 401) {
      requireLogin('口令好像过期了，再对一次。');
      return;
    }
    if (status === 429) {
      addNote('（她那边有点忙，等一下再跟她说吧）');
      return;
    }
    if (status === 504) {
      addNote('（她愣了一下，没接上话。再说一次？）');
      return;
    }
    if (status) {
      addNote('（出了点问题 ' + status + '，等会儿再试）');
      return;
    }
    // 没有状态码 = 网络层就断了（离线或被超时中断）
    addNote('（她好像没收到，等会儿再说一次）');
    showBanner('现在连不上后端。之前的消息还能看，等她那边好了再说话。');
  }

  function send() {
    if (busy) return;

    var text = inputEl.value.replace(/^\s+|\s+$/g, '');
    if (!text) return;

    busy = true;
    greetingOn = false;              // 用户开口了，招呼就不必继续演
    if (typingEl) hideTyping();
    setComposerEnabled(false);       // ★ 整段回复播完才解锁，她还在说时不会又追问一句

    inputEl.value = '';
    autoGrow();
    hideBanner();

    appendMessage({ role: 'me', text: text, ts: Date.now() }, { animate: true });
    showTyping();                    // 等网络这段时间也得有反馈，不然像卡住了

    api('/api/chat', { text: text }, TIMEOUT_CHAT)
      .then(function (data) {
        var parts = normalizeParts(data);
        if (!parts.length) {
          var empty = makeError(0, 'empty reply');
          empty.empty = true;
          throw empty;
        }
        hideTyping();
        return speakParts(parts, null);
      })
      .catch(function (err) {
        hideTyping();
        handleChatError(err);
      })
      .then(function () {
        busy = false;
        setComposerEnabled(true);
      });
  }

  /* ---------------- 首次进入的招呼 ---------------- */

  // 本地的问候，不走后端、也不写进历史（persist=false）：
  // 第一次打开时她不该只是一片空白，但这句话不是"她说过的记录"。
  function playGreeting() {
    greetingOn = true;
    speakParts([
      '（她抬起头，把手里那杯温水往你这边推了推）',
      '我是艾拉。Giftia，Type-07。',
      '……今天，想说点什么？'
    ], function () { return !greetingOn; }, false);
  }

  /* ---------------- 键盘遮挡 ---------------- */

  function setupViewport() {
    var vv = window.visualViewport;
    if (!vv) return;   // 不支持就算了：CSS 里的 100dvh + safe-area 已经够用

    var apply = function () {
      // 键盘弹起时 visualViewport 会变矮，而布局视口（100dvh）在 iOS Safari 上
      // 往往纹丝不动，于是输入框就被键盘盖住了。
      // 把真实可视高度写进 --vvh，整屏跟着缩，输入框自然停在键盘上方。
      document.documentElement.style.setProperty('--vvh', Math.round(vv.height) + 'px');

      // iOS 有时会把整个可视视口往上推（offsetTop > 0），
      // 底部就会露出一条空白；页面本身不该滚，按回去。
      if (vv.offsetTop) window.scrollTo(0, 0);

      // 键盘刚弹起时把消息流重新贴到底，别让最后几条藏在键盘后面
      if (document.activeElement === inputEl) scrollToEnd(true);
    };

    vv.addEventListener('resize', apply);
    vv.addEventListener('scroll', apply);
    apply();
  }

  /* ---------------- 输入框自适应高度 ---------------- */

  function autoGrow() {
    inputEl.style.height = 'auto';
    var max = Math.round(7.5 * 22);   // 和 CSS 里的 max-height 对齐
    inputEl.style.height = Math.min(inputEl.scrollHeight, max) + 'px';
  }

  /* ---------------- 右上角菜单 ---------------- */

  function openMenu() {
    menuEl.hidden = false;
    menuBtn.setAttribute('aria-expanded', 'true');
  }

  function closeMenu() {
    menuEl.hidden = true;
    menuBtn.setAttribute('aria-expanded', 'false');
  }

  function clearStream() {
    while (streamEl.firstChild) streamEl.removeChild(streamEl.firstChild);
  }

  function onMenuClick(event) {
    var target = event.target;
    var btn = target && target.closest ? target.closest('[data-act]') : null;
    if (!btn) return;

    var act = btn.getAttribute('data-act');
    closeMenu();

    if (act === 'clear') {
      if (!window.confirm('清空这段对话？她说过的话就不在了。')) return;
      history = [];
      saveHistory();
      clearStream();
      addNote('（她把刚才的话收了起来）');
    } else if (act === 'forget') {
      // 只忘口令，不删聊天记录 —— 记录是"记忆"，口令只是一把钥匙
      store.del(KEY_PASS);
      showGate('口令忘掉了，再输一次。', false);
    }
  }

  /* ---------------- 口令页 ---------------- */

  function setGateBusy(on) {
    gateBtn.disabled = on;
    gatePass.disabled = on;
    gateBtn.textContent = on ? '……' : '进去';
    if (!on) { try { gatePass.focus({ preventScroll: true }); } catch (e) { gatePass.focus(); } }
  }

  function onGateSubmit(event) {
    event.preventDefault();
    var pass = gatePass.value.replace(/^\s+|\s+$/g, '');
    if (!pass || gateBtn.disabled) return;

    setGateBusy(true);
    setGateMsg('');

    api('/api/login', { pass: pass }, TIMEOUT_SHORT)
      .then(function () {
        store.set(KEY_PASS, pass);   // 存下来，下次自动登录
        gatePass.value = '';
        enterChat();
        if (!history.length) playGreeting();
      })
      .catch(function (err) {
        if (err && err.status === 401) {
          setGateMsg('口令不对。', true);
        } else if (err && err.status) {
          setGateMsg('进不去（' + err.status + '），等会儿再试。', true);
        } else {
          setGateMsg('连不上后端，确认一下服务起来了没有。', true);
        }
      })
      .then(function () { setGateBusy(false); });
  }

  /* ---------------- 启动 ---------------- */

  function restoreHistory() {
    if (!history.length) return;
    history.forEach(function (m) {
      appendMessage(m, { animate: false, persist: false });
    });
    scrollToEnd(true);
  }

  function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) return;
    // 直接用 file:// 双击打开预览时没有 SW，硬注册会报错，跳过
    if (window.location.protocol !== 'http:' && window.location.protocol !== 'https:') return;
    navigator.serviceWorker.register('sw.js').catch(function () {
      // 注册失败只是没有离线加速，聊天照常，不必打扰用户
    });
  }

  function bindEvents() {
    gateForm.addEventListener('submit', onGateSubmit);
    formEl.addEventListener('submit', function (e) { e.preventDefault(); send(); });

    inputEl.addEventListener('input', autoGrow);

    // 回车发送，Shift+回车换行。
    // isComposing 是中文输入法选字时的回车，不能当成发送。
    inputEl.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229) return;
      e.preventDefault();
      send();
    });

    menuBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (menuEl.hidden) openMenu(); else closeMenu();
    });
    menuEl.addEventListener('click', onMenuClick);
    document.addEventListener('click', function (e) {
      if (menuEl.hidden) return;
      if (menuEl.contains(e.target) || menuBtn.contains(e.target)) return;
      closeMenu();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeMenu();
    });

    // 只在这两个事件上动一下，不做轮询：
    // 掉线提示条、以及回到线上时顺手确认后端在不在
    window.addEventListener('offline', function () {
      showBanner('网络断了。等她那边能连上再说。');
    });
    window.addEventListener('online', function () {
      api('/api/health', undefined, TIMEOUT_SHORT).then(function () {
        hideBanner();
      }).catch(function () {
        showBanner('网络回来了，但后端还没应答。');
      });
    });
  }

  function boot() {
    bindEvents();
    restoreHistory();
    refreshSubtitle();
    setupViewport();
    registerServiceWorker();

    var saved = store.get(KEY_PASS);
    if (!saved) {
      showGate('');
      return;
    }

    // 本地存过口令：先进聊天页（cookie 往往还有效，不必让她干等一次往返），
    // 再在后台静默登录一次核对。成功就什么都不用做。
    enterChat();
    if (!history.length) playGreeting();

    api('/api/login', { pass: saved }, TIMEOUT_SHORT)
      .then(function () {
        hideBanner();
      })
      .catch(function (err) {
        if (err && err.status === 401) {
          requireLogin('口令过期了，再对一次。');
          return;
        }
        if (!err || !err.status) {
          showBanner('现在连不上后端。消息还能看，等她那边好了再说话。');
          return;
        }
        showBanner('后端回了 ' + err.status + '，可能还没准备好。');
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
