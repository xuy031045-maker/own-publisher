'use strict';

// Exercise the actual browser-side summary code without platform access.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const elements = new Map();
const element = key => {
  if (!elements.has(key)) elements.set(key, {
    value:'', textContent:'', innerHTML:'', hidden:false, disabled:false,
    addEventListener() {}, querySelectorAll() { return []; }
  });
  return elements.get(key);
};
const state = {
  summary:{new_count:1, review_count:1, latest_new_id:10, latest_review_id:11},
  delayed:null, listReads:0, notifications:[]
};
const context = {
  $:element, URLSearchParams, window:{addEventListener() {}}, document:{hidden:false},
  location:{hash:'#compose'}, setInterval() {}, setTimeout() {}, clearTimeout() {},
  notify(message) { state.notifications.push(message); }, escapeHtml(value) { return String(value); },
  async api(url) {
    if (url === '/api/inbox/summary') {
      if (state.delayed) { const result = state.delayed; state.delayed = null; return result; }
      return {...state.summary};
    }
    if (url.startsWith('/api/inbox/conversations?')) {
      state.listReads++;
      return {conversations:[], total:0};
    }
    throw new Error('Unexpected route: ' + url);
  }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/inbox.js','utf8'), context);
const refresh = () => vm.runInContext('refreshInboxSummary()', context);

async function main() {
  await refresh();
  context.location.hash = '#inbox';
  assert.equal(state.notifications.length,0);

  // One old alert was acknowledged while a new one arrived: count stays at 1.
  state.summary.latest_new_id = 12;
  await refresh();
  assert.equal(state.notifications.length,1);
  assert.equal(state.listReads,1);
  assert.equal(element('#inbox-badge').textContent,'1 新 · 1 待核对');
  await refresh();
  assert.equal(state.notifications.length,1);
  assert.equal(state.listReads,1);

  // An unseen review replaced another one; the conversation list must refresh.
  state.summary.latest_review_id = 13;
  await refresh();
  assert.equal(state.notifications.length,1);
  assert.equal(state.listReads,2);

  // A delayed older response cannot replace a newer summary or cause a second alert.
  let resolveOld;
  state.delayed = new Promise(resolve => { resolveOld = resolve; });
  const oldRequest = refresh();
  state.summary.latest_new_id = 14;
  await refresh();
  assert.equal(state.notifications.length,2);
  resolveOld({new_count:1,review_count:1,latest_new_id:12,latest_review_id:13});
  await oldRequest;
  await refresh();
  assert.equal(state.notifications.length,2);
  assert.equal(state.listReads,3);
  console.log('Inbox client notification checks passed.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
