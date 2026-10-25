/* Time separators use the actual app.js renderer, including restored local history. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = []; this.attributes = {}; this.style = {}; this.value = '';
    this.scrollHeight = 0; this.scrollTop = 0; this.clientHeight = 0;
    this.classList = { add() {}, remove() {} };
  }
  get firstChild() { return this.children[0]; }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  removeChild(child) { this.children = this.children.filter(x => x !== child); }
  setAttribute(name, value) { this.attributes[name] = value; }
  focus() {}
}

const stamp = (year, month, day, hour = 0, minute = 0) => new Date(year, month - 1, day, hour, minute).getTime();
const now = stamp(2026, 10, 25, 12);
class Clock extends Date {
  constructor(...args) { super(...(args.length ? args : [now])); }
  static now() { return now; }
}
const elements = {};
const window = {
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  matchMedia: () => ({ matches: true })
};
const document = {
  getElementById: id => elements[id] || (elements[id] = new Element()),
  createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }), readyState: 'loading'
};
let source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const end = source.lastIndexOf("  if (document.readyState === 'loading')");
assert.ok(end > 0);
source = source.slice(0, end) +
  'window.test={messageTime,formatMessageTime,appendMessage,addNote,clearStream,refreshTimeLabels,restoreHistory,' +
  'getHistory:()=>history,setHistory:rows=>{history=rows;}};})();';
vm.runInNewContext(source, { window, document, navigator: {}, Date: Clock, Promise, Math, JSON, isFinite, isNaN });
const api = window.test;
const labels = () => elements.stream.children.filter(row => row.className === 'message-time');
const labelTexts = () => labels().map(row => row.children[0].textContent);
const add = (ts, text = '消息') => api.appendMessage({ role: 'me', text, ts }, { persist: false, animate: false });
let checks = 0;
function check(name, run) { run(); checks++; }

check('today', () => assert.equal(api.formatMessageTime(stamp(2026, 10, 25, 9, 5), now), '09:05'));
check('yesterday', () => assert.equal(api.formatMessageTime(stamp(2026, 10, 24, 23, 58), now), '昨天 23:58'));
check('earlier date', () => assert.equal(api.formatMessageTime(stamp(2026, 10, 20, 8, 3), now), '10月20日 08:03'));
check('previous year', () => assert.equal(api.formatMessageTime(stamp(2025, 12, 31, 8, 3), now), '2025年12月31日 08:03'));
check('yesterday across new year', () => assert.equal(api.formatMessageTime(stamp(2025, 12, 31, 23), stamp(2026, 1, 1, 12)), '昨天 23:00'));
check('older day across new year', () => assert.equal(api.formatMessageTime(stamp(2025, 12, 30, 23), stamp(2026, 1, 1, 12)), '2025年12月30日 23:00'));
check('yesterday is calendar-based over DST', () => assert.equal(api.formatMessageTime(stamp(2026, 11, 1, 0, 30), stamp(2026, 11, 2, 0, 15)), '昨天 00:30'));
check('numeric timestamp', () => assert.equal(api.messageTime(now), now));
check('ISO push timestamp', () => assert.equal(api.messageTime(new Date(now).toISOString()), now));
check('legacy local ISO timestamp', () => assert.equal(api.messageTime('2026-10-25T12:00:00'), now));
check('explicit timezone', () => assert.equal(api.messageTime('2026-10-25T12:00:00+08:00'), Date.parse('2026-10-25T04:00:00Z')));
for (const value of [undefined, null, '', 'not a date', '0', '2026', {}, NaN, Infinity, 0]) {
  check('invalid historical timestamp', () => assert.equal(api.messageTime(value), null));
}

const start = stamp(2026, 10, 25, 10);
api.clearStream(); add(start);
check('first message has a centered separator', () => assert.deepEqual(labelTexts(), ['10:00']));
check('semantic time element', () => assert.equal(labels()[0].children[0].tagName, 'TIME'));
check('machine-readable absolute timestamp', () => assert.equal(labels()[0].children[0].attributes.datetime, new Date(start).toISOString()));
add(start + 299999);
check('under five minutes', () => assert.equal(labels().length, 1));
add(start + 299999 + 300000);
check('exactly five minutes since previous message', () => assert.equal(labels().length, 2));
api.clearStream(); add(start); add(start + 240000); add(start + 480000);
check('ongoing chat does not repeat time every five wall-clock minutes', () => assert.equal(labels().length, 1));
api.addNote('连接恢复'); add(start + 500000);
check('system notes do not split a continuous chat', () => assert.equal(labels().length, 1));

api.clearStream(); add(stamp(2026, 10, 24, 23, 59)); add(stamp(2026, 10, 25, 0, 1));
check('cross-midnight separator despite a two-minute gap', () => assert.deepEqual(labelTexts(), ['昨天 23:59', '00:01']));
api.clearStream(); add(undefined, '旧记录没有时间');
check('legacy invalid time has no fabricated label', () => assert.equal(labels().length, 0));
add(start);
check('first valid time after unknown history', () => assert.deepEqual(labelTexts(), ['10:00']));
add(start - 60000);
check('out-of-order imported timestamp is visible', () => assert.equal(labels().length, 2));
api.clearStream(); add(start + 1000);
check('clearing history resets grouping', () => assert.equal(labels().length, 1));
api.refreshTimeLabels(stamp(2026, 10, 26, 12));
check('returning next day refreshes relative dates', () => assert.deepEqual(labelTexts(), ['昨天 10:00']));

api.clearStream();
const history = [{ role: 'me', text: '上次的话', ts: start }, { role: 'her', text: '我记得', ts: start + 1000 },
                 { role: 'me', text: '过了一会回来', ts: start + 600000 }];
api.setHistory(history);
const before = JSON.stringify(history);
api.restoreHistory();
check('restored history uses identical grouping', () => assert.deepEqual(labelTexts(), ['10:00', '10:10']));
check('separators never enter persisted conversation', () => assert.equal(JSON.stringify(api.getHistory()), before));
api.appendMessage({ role: 'her', text: '新回复', ts: start + 601000 }, { animate: false });
check('live reply after restoring history uses the previous timestamp', () => assert.equal(labels().length, 2));
check('only messages are saved', () => assert.equal(api.getHistory().length, 4));
console.log(`Chat time: ${checks} checks passed (${process.env.TZ || 'system timezone'}).`);
