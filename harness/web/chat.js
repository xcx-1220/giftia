'use strict';

const byId = id => document.getElementById(id);
const api = '/api/web';
const timeline = byId('timeline');
const composer = byId('composer');
const message = byId('message');
const source = byId('source');
const historyDialog = byId('history-dialog');
const statuses = {
  queued: '等待处理', running: '处理中', committing: '正在保存',
  ready: '已就绪', failed: '失败', cancelled: '已取消',
};
let conversation = null;
let selection = null;
let items = [];
let pending = null;
let openAttachment = null;
let sending = false;
let upload = null;
let retryFile = null;
let lastSnapshot = null;
let refreshVersion = 0;
let sessionVersion = 0;
let historyVersion = 0;
let historyBefore = null;
let legacyView = null;
let legacyHistoryVersion = 0;
let sourceVersion = 0;
let isPartner = false;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function button(label, action, className = 'quiet') {
  const element = node('button', className, label);
  element.type = 'button';
  element.addEventListener('click', action);
  return element;
}

function avatar(className) {
  const image = node('img', className);
  image.src = '/icons/icon-192.png';
  image.alt = '';
  return image;
}

function setDock(open) {
  byId('attachment-dock').classList.toggle('is-open', open);
  byId('open-files').setAttribute('aria-expanded', String(open));
}

byId('open-files').addEventListener('click', () => setDock(byId('open-files').getAttribute('aria-expanded') !== 'true'));
byId('close-files').addEventListener('click', () => {
  setDock(false);
  byId('open-files').focus();
});

async function request(path, options = {}) {
  const { json, ...init } = options;
  const headers = new Headers(init.headers);
  if (conversation) headers.set('X-Conversation', conversation);
  if (json) {
    headers.set('Content-Type', 'application/json');
    init.body = JSON.stringify(json);
  }
  const response = await fetch(api + path, {
    ...init, headers, cache: 'no-store', signal: init.signal ?? AbortSignal.timeout(8000),
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || '无法完成操作。');
    error.status = response.status;
    throw error;
  }
  return result;
}

function setSession(connected) {
  byId('login').hidden = connected;
  byId('chat').hidden = !connected;
  byId('logout').hidden = !connected;
  byId('open-history').hidden = !connected;
  byId('open-files').hidden = !connected;
  byId('identity').hidden = !connected;
  if (connected) return;
  byId('push').hidden = true;
  if (upload) upload.abort();
  byId('identity').textContent = '';
  isPartner = false;
  setDock(false);
  byId('notice').textContent = '';
  message.value = '';
  if (source.open) source.close();
  if (historyDialog.open) historyDialog.close();
  clearHistory();
  openAttachment = null;
  byId('source-text').textContent = '';
  byId('source-title').textContent = '来源';
  sessionVersion++;
  conversation = selection = pending = retryFile = lastSnapshot = legacyView = null;
  items = [];
  render();
}

function clearHistory() {
  historyVersion++;
  legacyHistoryVersion++;
  historyBefore = null;
  byId('history-items').replaceChildren();
  byId('history-more').hidden = true;
  byId('history-more').disabled = false;
  byId('history-status').textContent = '';
  byId('legacy-history').hidden = true;
  byId('legacy-status').textContent = '';
  byId('legacy-items').replaceChildren();
}

function oldRecord(key) {
  try {
    return JSON.parse(localStorage.getItem(key));
  } catch {
    return null;
  }
}

async function loadLegacyHistory() {
  const view = legacyView;
  if (!view) return;
  const version = legacyHistoryVersion;
  const entries = byId('legacy-items');
  const status = byId('legacy-status');
  const history = oldRecord(`ella.history.${view}`);
  const oldPending = oldRecord(`ella.pending.${view}`);
  if (Array.isArray(history)) {
    for (const item of history.slice(-200).reverse()) {
      if (!item || !['me', 'her'].includes(item.role) || typeof item.text !== 'string') continue;
      const row = node('li', 'history-row');
      row.append(node('div', 'caption', item.role === 'me' ? '你' : '艾拉'), node('p', '', item.text));
      entries.append(row);
    }
  }
  const id = oldPending?.request_id;
  const pendingIsValid = typeof id === 'string' && /^[a-zA-Z0-9_-]{16,80}$/.test(id);
  byId('legacy-history').hidden = !entries.children.length && !pendingIsValid;
  if (!pendingIsValid) return;
  status.textContent = '正在核对旧请求…';
  try {
    const result = await request(`/legacy-result/${id}`);
    if (version !== legacyHistoryVersion || view !== legacyView || !historyDialog.open) return;
    status.textContent = '已找到旧请求的回复。';
    const row = node('li', 'history-row');
    row.append(node('div', 'caption', '旧请求回执'), node('p', '', result.text), node('p', '', result.reply));
    entries.prepend(row);
  } catch (error) {
    if (version === legacyHistoryVersion && view === legacyView && historyDialog.open) {
      status.textContent = error.status === 404 ? '这条旧请求尚未确认，已保留原记录。' : error.message;
    }
  }
}

async function loadHistory() {
  const version = ++historyVersion;
  const more = byId('history-more');
  const status = byId('history-status');
  more.disabled = true;
  status.textContent = '正在读取…';
  try {
    const page = await request('/history' + (historyBefore === null ? '' : '?before=' + historyBefore));
    if (version !== historyVersion || !historyDialog.open) return;
    for (const item of page.messages) {
      const row = node('li', 'history-row');
      row.append(node('div', 'caption', `${item.role === 'user' ? '你' : '艾拉'} · ${item.ts}`),
        node('p', '', item.text));
      byId('history-items').append(row);
    }
    historyBefore = page.before;
    more.hidden = historyBefore === null;
    status.textContent = byId('history-items').children.length ? '' : '暂无普通聊天。';
  } catch (error) {
    if (version !== historyVersion) return;
    if (error.status === 401 || error.status === 409) clearHistory();
    status.textContent = error.message;
  } finally {
    if (version === historyVersion) more.disabled = false;
  }
}

byId('open-history').addEventListener('click', () => {
  clearHistory();
  historyDialog.showModal();
  loadHistory();
  loadLegacyHistory();
});
byId('history-more').addEventListener('click', loadHistory);
historyDialog.addEventListener('close', clearHistory);

function updateSelection(id) {
  selection = id;
  if (conversation) sessionStorage.setItem('isla-pdf-' + conversation, id ?? '');
  render();
}

function chooseInitialAttachment() {
  const stored = sessionStorage.getItem('isla-pdf-' + conversation);
  if (stored !== null) return stored || null;
  const previous = items.findLast(item => item.kind === 'turn' && item.attachmentId);
  return previous?.attachmentId ?? null;
}

async function refresh() {
  const version = ++refreshVersion;
  try {
    const state = await request('/state');
    if (version !== refreshVersion) return;
    if (conversation !== state.conversation) {
      if (conversation !== null) setSession(false);
      conversation = state.conversation;
      items = state.items;
      selection = chooseInitialAttachment();
    }
    isPartner = state.role === '搭档';
    const matchedView = /^[a-f0-9]{20}$/.test(state.legacyView || '') ? state.legacyView : null;
    if (legacyView !== matchedView) {
      if (historyDialog.open) historyDialog.close();
      legacyView = matchedView;
    }
    const signature = JSON.stringify(state);
    if (signature !== lastSnapshot) {
      items = state.items;
      lastSnapshot = signature;
      if (openAttachment && !items.some(item => item.kind === 'attachment' && item.id === openAttachment)) {
        source.close();
        byId('source-text').textContent = '';
        openAttachment = null;
      }
      if (pending && items.some(item => item.kind === 'turn' && item.id === pending.requestId)) {
        if (message.value.trim() === pending.text) message.value = '';
        pending = null;
      }
      render();
    }
    byId('identity').textContent = state.production ? state.role : state.role + ' · 隔离会话';
    byId('push').hidden = !(state.pushEnabled && 'PushManager' in window && 'serviceWorker' in navigator);
    byId('connection').textContent = '';
    setSession(true);
  } catch (error) {
    if (version !== refreshVersion) return;
    if (error.status === 401) {
      if (byId('login').hidden) setSession(false);
      byId('connection').textContent = '';
    } else {
      byId('connection').textContent = '连接中断，正在重连';
      if (conversation === null) byId('login').hidden = false;
    }
  }
}

async function change(item, action) {
  const kind = item.kind === 'turn' ? 'turns' : 'attachments';
  try {
    await request(`/${kind}/${item.id}/${action}`, { method: 'POST' });
    if (action === 'delete' && selection === item.id) updateSelection(null);
    await refresh();
  } catch (error) {
    byId('notice').textContent = error.message;
  }
}

function fileCard(item) {
  const card = node('article', 'file');
  card.dataset.selected = item.id === selection;
  const heading = node('div', 'file-head');
  const fileIcon = node('span', 'file-icon', 'PDF');
  fileIcon.setAttribute('aria-hidden', 'true');
  const details = node('div');
  details.append(node('span', 'file-name', item.fileName),
    node('p', 'state', item.status === 'ready' ? `PDF · ${item.pageCount} 页` : statuses[item.status]));
  heading.append(fileIcon, details);
  card.append(heading);
  const actions = node('div', 'file-actions');
  if (item.status === 'ready' && item.id !== selection) {
    actions.append(button('引用这份 PDF', () => {
      updateSelection(item.id);
      setDock(false);
      message.focus();
    }));
  } else if (['queued', 'running'].includes(item.status)) {
    actions.append(button('取消提取', () => change(item, 'cancel')));
  } else if (item.status === 'failed') {
    card.append(node('p', 'error', item.error));
    actions.append(button('重试提取', () => change(item, 'retry')));
  }
  if (item.status === 'ready' || item.status === 'failed') {
    actions.append(button('删除 PDF', () => {
      if (window.confirm('删除这份 PDF 及其问答记录？')) change(item, 'delete');
    }));
  }
  if (actions.children.length) card.append(actions);
  return card;
}

async function readSource(citation) {
  const version = sessionVersion;
  const readVersion = ++sourceVersion;
  openAttachment = citation.attachmentId;
  byId('source-title').textContent = `${citation.fileName} · 第 ${citation.page} 页`;
  byId('source-text').textContent = '正在读取…';
  if (!source.open) source.showModal();
  try {
    const page = await request(`/attachments/${citation.attachmentId}/pages/${citation.page}`);
    if (version === sessionVersion && readVersion === sourceVersion && openAttachment === citation.attachmentId) {
      byId('source-text').textContent = page.text;
    }
  } catch (error) {
    if (version === sessionVersion && readVersion === sourceVersion && openAttachment === citation.attachmentId) {
      byId('source-text').textContent = error.message;
    }
  }
}

source.addEventListener('close', () => {
  sourceVersion++;
  openAttachment = null;
  byId('source-text').textContent = '';
});

function formattedReply(item) {
  const reply = node('p', 'reply');
  const citations = new Map(item.citations.map(citation => [citation.page, citation]));
  let cursor = 0;
  for (const match of item.reply.matchAll(/\[p\.([1-9][0-9]{0,3})\]/g)) {
    const citation = citations.get(Number(match[1]));
    if (!citation) continue;
    const end = match.index + match[0].length;
    const punctuation = item.reply.slice(end).match(/^[。！？、，；：,.!?;:]+/)?.[0] ?? '';
    const mark = node('span', 'citation-wrap');
    mark.append(button(`第 ${citation.page} 页`, () => readSource(citation), 'citation'), punctuation);
    reply.append(item.reply.slice(cursor, match.index), mark);
    cursor = end + punctuation.length;
  }
  reply.append(item.reply.slice(cursor));
  return reply;
}

function turnCard(item, documents) {
  const section = node('article', 'turn');
  const user = node('div', 'user');
  if (item.attachmentId) {
    const fileName = documents.get(item.attachmentId)?.fileName ?? item.citations?.[0]?.fileName ?? 'PDF';
    user.append(node('p', 'caption', fileName));
  }
  user.append(node('p', '', item.text));
  section.append(user);
  if (item.tools.length) {
    const tools = node('ul', 'tools');
    tools.setAttribute('aria-label', '工具状态');
    const last = new Map(item.tools.map(tool => [tool.callId, tool]));
    const labels = { running: '我正在处理…', ready: '处理好了', failed: '这一步没完成' };
    const chatTools = {
      isla_calculate: { running: '我算一下…', ready: '算好了', failed: '这一步没算出来' },
      isla_convert_units: { running: '我换算一下…', ready: '换算好了', failed: '这一步没换算出来' },
      sample_reference: { running: '我找一下参考资料…', ready: '参考资料找到了', failed: '这次没查到参考资料' },
      todo_write: { running: '我理一下步骤…', ready: '步骤已更新', failed: '这次没能更新步骤' },
    };
    for (const tool of last.values()) {
      const text = (Object.hasOwn(chatTools, tool.name) && chatTools[tool.name][tool.status]) || labels[tool.status];
      if (!text) continue;
      const chip = node('li', '', text);
      chip.dataset.status = tool.status;
      tools.append(chip);
    }
    section.append(tools);
  }
  if (item.todos?.length) {
    const progress = node('section', 'task-progress');
    progress.setAttribute('aria-label', '任务进度');
    const working = ['queued', 'running', 'committing'].includes(item.status);
    progress.append(node('h3', '', working ? '我在按这几步处理' : '这轮的步骤记录'));
    const steps = node('ol');
    const states = { pending: '待处理', in_progress: working ? '进行中' : '进行到这里', completed: '已完成' };
    for (const step of item.todos) {
      const row = node('li');
      row.dataset.status = step.status;
      row.append(node('span', '', step.content), node('span', 'step-state', states[step.status]));
      steps.append(row);
    }
    progress.append(steps);
    section.append(progress);
  }
  if (item.status === 'ready') {
    const assistant = node('div', 'assistant');
    assistant.append(avatar('reply-avatar'), formattedReply(item));
    section.append(assistant);
  } else {
    const label = item.status === 'running' ? '艾拉正在回复…' : statuses[item.status];
    const status = node('div', 'state', item.error ?? label);
    if (['queued', 'running'].includes(item.status)) status.append(button('取消回复', () => change(item, 'cancel')));
    if (item.status === 'failed') status.append(button('重试回复', () => change(item, 'retry')));
    section.append(status);
  }
  return section;
}

function render() {
  const documents = new Map(items.filter(item => item.kind === 'attachment').map(item => [item.id, item]));
  const files = byId('files');
  files.replaceChildren(...[...documents.values()].reverse().map(fileCard));
  files.hidden = documents.size === 0;
  byId('file-empty').hidden = documents.size > 0;
  const turns = items.filter(item => item.kind === 'turn');
  const atBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 140;
  if (turns.length) {
    timeline.replaceChildren(...turns.map(item => turnCard(item, documents)));
    if (atBottom) timeline.scrollTop = timeline.scrollHeight;
  } else {
    const empty = node('div', 'empty');
    const starters = node('div', 'starters');
    starters.append(button('读一份 PDF', () => byId('pdf').click(), 'starter'));
    if (isPartner) starters.append(button('做个小计算', () => {
      if (!message.value) message.value = '帮我算一下 ';
      message.focus();
    }, 'starter'));
    empty.append(avatar('welcome-avatar'), node('h2', '', '和艾拉聊聊'),
      node('p', '', '今天想聊些什么？'), starters);
    timeline.replaceChildren(empty);
  }
  const selected = documents.get(selection);
  const selectionBox = byId('selection');
  selectionBox.hidden = !selection;
  selectionBox.replaceChildren();
  if (selection) {
    const label = selected ? `${selected.fileName} · ${statuses[selected.status]}` : '附件暂不可用';
    const attachment = node('span', '', label);
    if (selected?.status === 'ready') attachment.append(node('span', 'caption', ` · PDF · ${selected.pageCount} 页`));
    selectionBox.append(attachment,
      button('切回普通聊天', () => updateSelection(null)));
  }
  byId('send').disabled = sending || Boolean(selection && selected?.status !== 'ready');
  const uploadControl = byId('upload-control');
  uploadControl.hidden = !upload && !retryFile;
  uploadControl.textContent = upload ? '取消上传' : '重试上传';
  byId('attach').disabled = Boolean(upload);
}

async function uploadPDF(file) {
  const version = sessionVersion;
  const controller = new AbortController();
  upload = controller;
  retryFile = null;
  byId('notice').textContent = '正在上传 PDF…';
  render();
  try {
    const result = await request('/attachments', {
      method: 'POST', body: file, signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
      headers: { 'Content-Type': 'application/pdf', 'X-File-Name': encodeURIComponent(file.name) },
    });
    if (version !== sessionVersion) return;
    byId('notice').textContent = '';
    updateSelection(result.id);
    await refresh();
  } catch (error) {
    if (version !== sessionVersion) return;
    if (controller.signal.aborted) {
      byId('notice').textContent = '已停止上传。';
    } else {
      retryFile = error.status === 413 || error.status === 415 ? null : file;
      byId('notice').textContent = error.status ? error.message : '上传中断，请重试。';
    }
  } finally {
    if (upload === controller) upload = null;
    render();
  }
}

byId('attach').addEventListener('click', () => byId('pdf').click());
byId('pdf').addEventListener('change', event => {
  const file = event.target.files[0];
  event.target.value = '';
  if (file) uploadPDF(file);
});
byId('upload-control').addEventListener('click', () => {
  if (upload) upload.abort();
  else if (retryFile) uploadPDF(retryFile);
});

composer.addEventListener('submit', async event => {
  event.preventDefault();
  const text = message.value.trim();
  if (!text || sending || byId('send').disabled) return;
  const attachmentId = items.some(item => item.kind === 'attachment' && item.id === selection) ? selection : null;
  if (pending?.text !== text || pending.attachmentId !== attachmentId) {
    pending = { requestId: crypto.randomUUID(), attachmentId, text };
  }
  const version = sessionVersion;
  sending = true;
  byId('notice').textContent = '';
  render();
  try {
    await request('/turns', { method: 'POST', json: pending });
    if (version !== sessionVersion) return;
    if (message.value.trim() === text) message.value = '';
    pending = null;
    await refresh();
    timeline.scrollTop = timeline.scrollHeight;
  } catch (error) {
    if (version === sessionVersion) byId('notice').textContent = error.status ? error.message : '未确认发送，再次发送可重试。';
  } finally {
    sending = false;
    render();
  }
});

byId('login').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const token = byId('token').value.trim();
    await request('/login', { method: 'POST', json: { token } });
    byId('token').value = '';
    byId('login-error').textContent = '';
    await refresh();
  } catch (error) {
    byId('login-error').textContent = error.message;
  }
});
byId('logout').addEventListener('click', async () => {
  try {
    await request('/logout', { method: 'POST' });
    refreshVersion++;
    setSession(false);
  } catch (error) {
    byId('notice').textContent = error.message;
  }
});

byId('push').addEventListener('click', async () => {
  try {
    const [{publicKey}, registration] = await Promise.all([request('/push/status'), navigator.serviceWorker.ready]);
    const encoded = publicKey.replace(/-/g, '+').replace(/_/g, '/');
    const key = Uint8Array.from(atob(encoded), char => char.charCodeAt(0));
    const subscription = await registration.pushManager.getSubscription() ||
      await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
    await request('/push/subscribe', {method: 'POST', json: subscription.toJSON()});
    byId('notice').textContent = '此设备已订阅通知。';
  } catch (error) {
    byId('notice').textContent = error.message;
  }
});

window.addEventListener('online', refresh);
window.addEventListener('offline', () => { byId('connection').textContent = '连接中断，正在重连'; });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
async function poll() {
  await refresh();
  setTimeout(poll, document.hidden ? 2500 : 750);
}
poll();

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js', { updateViaCache: 'none' }).catch(() => {});
}
