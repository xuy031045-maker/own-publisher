'use strict';

const inboxState = {accounts: [], conversations: [], total: 0, selected: null, thread: null,
  messages: [], messageOffset: 0, syncing: false, request: 0, summary: null, summaryRequest: 0,
  reviewAlerts: [], scheduleFinished: undefined};
const inboxTime = value => {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '' : new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'
  }).format(date);
};

async function refreshInboxAccounts() {
  const data = await api('/api/inbox/accounts');
  inboxState.accounts = data.accounts;
  const selected = $('#inbox-account').value;
  $('#inbox-account').innerHTML = '<option value="">全部账号</option>' + data.accounts.map(a =>
    `<option value="${escapeHtml(a.alias)}">${escapeHtml(a.label)} (${escapeHtml(a.alias)})</option>`).join('');
  $('#inbox-account').value = selected;
  const stateNames = {complete: '已同步', partial: '部分历史待核实', login_needed: '需要登录', failed: '同步未完成'};
  $('#inbox-accounts-status').innerHTML = data.accounts.map(a => {
    const status = !a.identity_verified ? '请先检查账号' : stateNames[a.state] || '尚未同步';
    const attention = !a.identity_verified || ['partial', 'login_needed', 'failed'].includes(a.state);
    const timing = Number.isInteger(a.elapsed_ms) ? ` · ${(a.elapsed_ms / 1000).toFixed(1)} 秒${a.reused ? ` · 复用 ${a.reused} 个会话` : ''}` : '';
    return `<span class="inbox-account-chip ${attention ? 'needs-attention' : ''}">${escapeHtml(a.label)} · ${escapeHtml(status)}${timing}</span>`;
  }).join('') || '<span class="inbox-account-chip">请先在账号管理中添加账号</span>';
  updateInboxSyncButton();
}

async function refreshInboxSummary() {
  const request = ++inboxState.summaryRequest;
  const summary = await api('/api/inbox/summary');
  if (request !== inboxState.summaryRequest) return;
  const previous = inboxState.summary;
  const badge = $('#inbox-badge');
  const total = summary.new_count + summary.review_count;
  badge.hidden = !total;
  badge.textContent = [summary.new_count ? `${summary.new_count} 新` : '',
    summary.review_count ? `${summary.review_count} 待核对` : ''].filter(Boolean).join(' · ');
  if (previous && (summary.new_count > previous.new_count ||
      (summary.latest_new_id || 0) > (previous.latest_new_id || 0))) {
    notify('收到新的私信，请查看收件箱。');
  }
  if (previous && location.hash === '#inbox' &&
      (summary.new_count !== previous.new_count || summary.review_count !== previous.review_count ||
       summary.latest_new_id !== previous.latest_new_id || summary.latest_review_id !== previous.latest_review_id)) {
    refreshConversations().catch(() => {});
  }
  inboxState.summary = summary;
}

async function refreshInboxSettings() {
  const data = await api('/api/settings');
  const schedule = data.schedule;
  if (inboxState.scheduleFinished !== undefined && inboxState.scheduleFinished !== schedule.last_finished) {
    await refreshInboxAccounts();
  }
  inboxState.scheduleFinished = schedule.last_finished;
  $('#inbox-interval').value = String(schedule.interval_minutes);
  const info = !schedule.interval_minutes ? '工作台打开时可启用' :
    schedule.state === 'running' ? '自动同步进行中' :
    ['attention', 'interrupted'].includes(schedule.state) ? schedule.detail : '工作台打开时生效';
  $('#inbox-schedule-status').textContent = info;
}
$('#inbox-interval').addEventListener('change', async event => {
  try { await post('/api/settings/schedule', {minutes: Number(event.target.value)}); await refreshInboxSettings(); notify('自动同步设置已保存。'); }
  catch (error) { notify(error.message, true); await refreshInboxSettings(); }
});

function updateInboxSyncButton() {
  const button = $('#inbox-sync');
  button.textContent = inboxState.syncing ? '同步中…' : ($('#inbox-account').value ? '快速同步此账号' : '快速同步全部');
  button.disabled = inboxState.syncing || !inboxState.accounts.length;
  $('#inbox-sync-full').disabled = button.disabled;
}

async function refreshConversations(append = false) {
  const request = ++inboxState.request;
  const offset = append ? inboxState.conversations.length : 0;
  const params = new URLSearchParams({alias: $('#inbox-account').value, query: $('#inbox-search').value.trim(),
    status: $('#inbox-filter').value, limit: '100', offset: String(offset)});
  const data = await api(`/api/inbox/conversations?${params}`);
  if (request !== inboxState.request) return;
  inboxState.conversations = append ? inboxState.conversations.concat(data.conversations) : data.conversations;
  inboxState.total = data.total;
  $('#inbox-count').textContent = `${data.total} 个`;
  $('#inbox-list').innerHTML = inboxState.conversations.map(c => `
    <button type="button" class="inbox-item ${c.id === inboxState.selected ? 'selected' : ''}" data-conversation="${c.id}">
      <span class="avatar">${escapeHtml((c.display_name || '客').slice(0, 1))}</span>
      <span class="inbox-item-content"><span class="inbox-item-top"><strong>${escapeHtml(c.display_name || '未命名会话')} ${c.important ? '<span class="inbox-important">★</span>' : ''}</strong><time>${escapeHtml(inboxTime(c.last_timestamp_ms) || c.last_time_label)}</time></span>
      <span class="inbox-item-preview">${escapeHtml(c.last_preview || '暂无文字预览')}</span>
      <span class="inbox-item-meta">${escapeHtml(c.account_label || c.alias)} · ${c.status === 'processed' ? '已处理' : '待处理'}${c.location === 'stranger' ? ' · 陌生人' : ''}${c.new_count ? ` · <span class="inbox-alert">${c.new_count} 条新消息</span>` : ''}${c.review_needed ? ' · <span class="inbox-alert">需要核对</span>' : ''}</span></span></button>`).join('') || '<p class="empty">没有符合条件的会话。<br>可以同步账号或更换筛选条件。</p>';
  $('#inbox-more').hidden = inboxState.conversations.length >= data.total;
}

async function showInboxThread(id) {
  inboxState.selected = id;
  $('#inbox-list').querySelectorAll('.inbox-item').forEach(el => el.classList.toggle('selected', Number(el.dataset.conversation) === id));
  $('#inbox-thread').innerHTML = '<p class="empty">正在读取消息…</p>';
  let result = await api(`/api/inbox/conversations/${id}?limit=200&offset=0`);
  if (inboxState.selected !== id) return;
  const offset = Math.max(0, result.total - 200);
  if (offset) result = await api(`/api/inbox/conversations/${id}?limit=200&offset=${offset}`);
  if (inboxState.selected !== id) return;
  inboxState.thread = result.conversation;
  inboxState.messages = result.messages;
  inboxState.reviewAlerts = result.review_alerts || [];
  inboxState.messageOffset = offset;
  renderInboxThread(true);
  if (location.hash === '#inbox' && !document.hidden && result.unseen_notifications?.length) {
    await post(`/api/inbox/conversations/${id}/acknowledge`, {alerts: result.unseen_notifications});
    await refreshInboxSummary();
    await refreshConversations();
  }
}

function renderInboxThread(scrollToEnd = false) {
  const c = inboxState.thread;
  if (!c) return;
  const messages = inboxState.messages.map(m => {
    const direction = m.direction === 'outgoing' ? 'outgoing' : '';
    const person = m.direction === 'outgoing' ? '我' : m.direction === 'incoming' ? '对方' : '系统';
    const picture = m.media_id ? `<img class="inbox-media" src="/api/inbox/media/${m.media_id}" alt="同步的图片或表情" loading="lazy">` : '';
    const body = m.media_id && m.body === '[图片或表情]' ? '' : escapeHtml(m.body);
    return `<div class="inbox-message ${direction}"><small>${person} ${escapeHtml(inboxTime(m.timestamp_ms) || m.time_label)}</small><div class="inbox-bubble">${picture}${body}</div></div>`;
  }).join('');
  $('#inbox-thread').innerHTML = `<div class="inbox-thread-head"><div><h2>${escapeHtml(c.display_name || '未命名会话')}</h2><small>${escapeHtml(c.account_label || c.alias)} · ${c.location === 'stranger' ? '陌生人消息 · ' : ''}${c.status === 'processed' ? '已处理' : '待处理'}</small></div>
    <div class="inbox-thread-actions"><button class="secondary" data-thread-action="important">${c.important ? '★ 已标重要' : '☆ 标为重要'}</button>
    <button class="secondary" data-thread-action="status">${c.status === 'processed' ? '设为待处理' : '标为已处理'}</button><button class="secondary" data-thread-action="open">打开原会话</button></div></div>
    ${c.review_needed ? '<div class="inbox-thread-warning">部分消息未完整读取或新旧顺序无法确认，请核对原会话。 <button class="secondary" data-thread-action="review">已核对</button></div>' : ''}
    ${c.history_complete ? '' : '<div class="inbox-thread-warning">历史记录尚未完整核实，可以再次同步或打开原会话查看。</div>'}
    <div class="inbox-messages">${inboxState.messageOffset ? '<button class="inbox-more" data-thread-action="older">查看更早消息</button>' : ''}${messages || '<p class="empty">暂无可显示的消息。</p>'}</div>
    <div class="inbox-thread-foot">只读同步，不会自动发送或回复。图片可能是缩略图；未读到的图片、语音和部分卡片请在原会话查看。</div>`;
  if (scrollToEnd) {
    const container = $('#inbox-thread .inbox-messages');
    container.scrollTop = container.scrollHeight;
  }
}

$('#inbox-list').addEventListener('click', event => {
  const button = event.target.closest('[data-conversation]');
  if (button) showInboxThread(Number(button.dataset.conversation)).catch(error => notify(error.message, true));
});
$('#inbox-more').addEventListener('click', () => refreshConversations(true).catch(error => notify(error.message, true)));
$('#inbox-thread').addEventListener('click', async event => {
  const button = event.target.closest('[data-thread-action]');
  const c = inboxState.thread;
  if (!button || !c) return;
  button.disabled = true;
  try {
    if (button.dataset.threadAction === 'open') {
      const job = await post(`/api/inbox/conversations/${c.id}/open`, {});
      notify('正在打开对应账号的私信窗口。关闭窗口后可继续同步或发布。');
      waitJob(job.job).then(() => notify('原会话窗口已关闭。')).catch(error => notify(error.message, true));
    } else if (button.dataset.threadAction === 'review') {
      await post(`/api/inbox/conversations/${c.id}/acknowledge`, {alerts: inboxState.reviewAlerts});
      await refreshInboxSummary();
      await refreshConversations();
      await showInboxThread(c.id);
    } else if (button.dataset.threadAction === 'older') {
      const offset = Math.max(0, inboxState.messageOffset - 200);
      const older = await api(`/api/inbox/conversations/${c.id}?limit=${inboxState.messageOffset - offset}&offset=${offset}`);
      if (inboxState.selected !== c.id) return;
      inboxState.messages = older.messages.concat(inboxState.messages);
      inboxState.messageOffset = offset;
      renderInboxThread();
    } else {
      const update = button.dataset.threadAction === 'important' ? {important: !c.important} :
        {status: c.status === 'processed' ? 'pending' : 'processed', alerts_version: c.alerts_version};
      await post(`/api/inbox/conversations/${c.id}/state`, update);
      await refreshConversations();
      await showInboxThread(c.id);
    }
  } catch (error) {
    notify(error.message, true);
    if (button.dataset.threadAction === 'status' && error.message.includes('新的提醒')) {
      await refreshConversations();
      await showInboxThread(c.id);
    }
  }
  finally { button.disabled = false; }
});

async function runInboxSync(mode) {
  if (inboxState.syncing) return;
  const selected = $('#inbox-account').value;
  const accounts = selected ? [selected] : inboxState.accounts.map(a => a.alias);
  if (!accounts.length || accounts.length > 10) return notify('请选择 1 到 10 个账号同步；账号较多时可先按账号筛选。', true);
  inboxState.syncing = true;
  updateInboxSyncButton();
  $('#inbox-progress').hidden = false;
  $('#inbox-progress').textContent = '正在准备同步，请保持工作台运行。';
  try {
    const started = await post('/api/inbox/sync', {accounts, mode});
    if (started.merged) notify('已合并到正在进行的相同同步任务。');
    const job = await waitJob(started.job, detail => { $('#inbox-progress').textContent = detail; });
    const results = job.result.accounts;
    const attention = results.filter(item => item.state !== 'complete').length;
    await refreshInboxAccounts();
    await refreshConversations();
    await refreshInboxSummary();
    if (inboxState.selected) await showInboxThread(inboxState.selected);
    notify(attention ? `同步结束，${attention} 个账号有历史待核实或需要处理。` : '私信同步完成。', !!attention);
  } catch (error) { notify(error.message, true); }
  finally { inboxState.syncing = false; $('#inbox-progress').hidden = true; updateInboxSyncButton(); }
}
$('#inbox-sync').addEventListener('click', () => runInboxSync('quick'));
$('#inbox-sync-full').addEventListener('click', () => runInboxSync('full'));

let inboxSearchTimer;
$('#inbox-search').addEventListener('input', () => {
  clearTimeout(inboxSearchTimer);
  inboxSearchTimer = setTimeout(() => refreshConversations().catch(error => notify(error.message, true)), 250);
});
for (const selector of ['#inbox-account', '#inbox-filter']) $(selector).addEventListener('change', () => {
  updateInboxSyncButton();
  refreshConversations().catch(error => notify(error.message, true));
});
async function loadInbox() {
  if (location.hash !== '#inbox') return;
  try { await refreshInboxAccounts(); await refreshConversations(); await refreshInboxSettings(); }
  catch (error) { notify(error.message, true); }
}
window.addEventListener('hashchange', loadInbox);
loadInbox();
refreshInboxSummary().catch(() => {});
setInterval(() => { refreshInboxSummary().catch(() => {}); if (location.hash === '#inbox') refreshInboxSettings().catch(() => {}); }, 30000);
