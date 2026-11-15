'use strict';

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {accounts: [], images: [], batches: [], current: null, activeBatch: null, busy: false, accountJobs: Object.create(null), individual: Object.create(null)};
const states = {
  queued: '待确认', running: '提交中', verifying: '等待人工验证',
  submitted: '已提交', failed: '提交前失败', login_needed: '需要登录',
  uncertain: '待人工核对', duplicate: '已有相同记录', cancelled: '已取消（未提交）'
};
const declarations = {
  ai: '笔记含 AI 合成内容', repost: '来源转载', self_shot: '自主拍摄',
  fiction: '虚构演绎，仅供娱乐', advertising: '内容包含营销广告'
};
const platformStates = {scheduled: '显示排期', published: '显示已发布', auditing: '审核中', rejected: '未通过'};
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, ch => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
})[ch]);
const hasVerifiedNickname = account => !!(account?.identity_verified && typeof account.nickname === 'string' && account.nickname.trim());
function accountName(account) { return hasVerifiedNickname(account) ? account.nickname.trim() : '未核验'; }
function accountCaption(account) {
  return `${account?.alias || ''}${hasVerifiedNickname(account) ? '' : ' · 请先检查账号'}`;
}
const iconPaths = {
  left: '<path d="M15 6l-6 6 6 6"/>',
  right: '<path d="M9 6l6 6-6 6"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  trash: '<path d="M4 7h16M10 3h4M7 7l1 12a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2l1-12M10 11v6M14 11v6"/>',
  star: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z"/>'
};
const uiIcon = name => `<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${iconPaths[name] || ''}</svg>`;
function checkedTime(value) {
  if (!value) return '尚未核验';
  const date = new Date(value.replace(' ', 'T') + 'Z');
  return Number.isNaN(date.valueOf()) ? '核验时间未知' : `核验于 ${new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short'}).format(date)}`;
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let data;
  try { data = await response.json(); } catch { throw new Error('无法读取工作台响应。'); }
  if (!response.ok) throw new Error(data.error || data.detail || '操作失败，请重试。');
  return data;
}
function post(url, value) {
  return api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(value)});
}
function notify(message, isError = false) {
  const element = $('#notification');
  element.textContent = message;
  element.classList.toggle('error', isError);
  element.hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { element.hidden = true; }, 7000);
}
function navigate() {
  const name = location.hash.slice(1) || 'compose';
  const view = ['compose', 'inbox', 'accounts', 'plans', 'history'].includes(name) ? name : 'compose';
  for (const section of $$('.work > section')) section.hidden = section.id !== `view-${view}`;
  for (const link of $$('.rail nav a')) {
    if (link.hash === `#${view}`) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  if (view === 'plans' || view === 'history') refreshBatches();
  if (view === 'accounts') {
    refreshBackups().catch(error => notify(error.message, true));
    refreshRestoreGuard().catch(error => notify(error.message, true));
  }
}
window.addEventListener('hashchange', navigate);

let restoreReview = null;
async function refreshRestoreGuard() {
  const result = await api('/api/restore-guard');
  restoreReview = result;
  const panel = $('#restore-review');
  panel.hidden = !result.pending && !result.generation;
  if (panel.hidden) return;
  $('#restore-review-description').textContent = result.pending ?
    '恢复备份后，请先核对各账号的平台作品。确认前无法发布；恢复前的发布记录仍会参与防重复检查。' :
    result.accounts.some(a => !a.audited) ? '新增或更换的账号需要先核对平台作品，其他已核对账号仍可使用。' :
    '发布保护核对已完成。若你在平台外新增或修改作品，可以再次核对，防重复记录会继续保留。';
  if (result.broken) {
    $('#restore-review-list').innerHTML = '<p>恢复保护记录与数据库不一致，请先找回独立防重复记录，再核对平台。</p>';
    $('#restore-review-confirm').disabled = true;
    return;
  }
  $('#restore-review-list').innerHTML = result.accounts.map(account => {
    const label = `${escapeHtml(accountName(account))} <small>${escapeHtml(accountCaption(account))}</small>`;
    const detail = account.audited ? (account.note_count < 0 ? '已人工核对' : `已读取 ${account.note_count} 条平台作品`) :
      (account.platform === 'xiaohongshu' && !account.identity_verified ? '请先检查账号身份' : '尚未核对');
    const action = account.platform === 'xiaohongshu' ?
      `<button class="secondary" data-review-audit="${escapeHtml(account.alias)}" ${account.identity_verified ? '' : 'disabled'}>核对平台</button>` :
      `<button class="secondary" data-review-manual="${escapeHtml(account.alias)}">我已在平台核对</button>`;
    const notes = account.audited && account.note_count >= 0 ? `<details><summary>查看已读取的作品</summary><ul>${account.notes.map(note => `<li>${escapeHtml(note.title || '未命名作品')}</li>`).join('') || '<li>没有作品</li>'}</ul></details>` : '';
    return `<div class="restore-review-row"><div><span><strong>${label}</strong> · ${escapeHtml(detail)}</span>${action}</div>${notes}</div>`;
  }).join('') || '<p>当前没有账号，新增账号后仍需先核对平台。</p>';
  const confirm = $('#restore-review-confirm');
  confirm.hidden = !result.pending;
  confirm.disabled = result.accounts.some(account => !account.audited);
}
$('#restore-review-list').addEventListener('click', async event => {
  const button = event.target.closest('[data-review-audit], [data-review-manual]');
  if (!button || !restoreReview?.generation) return;
  button.disabled = true;
  try {
    if (button.dataset.reviewAudit) {
      const started = await post('/api/restore-guard/audit', {alias:button.dataset.reviewAudit, generation:restoreReview.generation});
      notify('正在读取平台作品列表。');
      await waitJob(started.job);
    } else {
      if (!confirm(`确认已在平台逐条核对账号 ${button.dataset.reviewManual} 的作品吗？`)) { button.disabled = false; return; }
      await post('/api/restore-guard/manual', {alias:button.dataset.reviewManual,generation:restoreReview.generation,confirm:'reviewed'});
    }
    await refreshRestoreGuard();
    notify('平台核对结果已记录。');
  } catch (error) { notify(error.message, true); button.disabled = false; }
});
$('#restore-review-confirm').addEventListener('click', async event => {
  if (!restoreReview?.generation || !confirm('确认已查看所有账号的平台作品，并要解除本次恢复的发布保护吗？')) return;
  const button = event.currentTarget;
  button.disabled = true;
  try { await post('/api/restore-guard/confirm', {generation:restoreReview.generation,confirm:'reviewed'}); await refreshRestoreGuard(); notify('平台核对已确认，防重复记录继续保留。'); }
  catch (error) { notify(error.message, true); button.disabled = false; }
});

async function refreshBackups() {
  const result = await api('/api/backups');
  const select = $('#backup-choice');
  const selected = select.value;
  select.innerHTML = result.backups.map(item => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)} · ${(item.bytes / 1048576).toFixed(1)} MB</option>`).join('') || '<option value="">暂无备份</option>';
  if (result.backups.some(item => item.name === selected)) select.value = selected;
  $('#backup-restore').disabled = !result.backups.length;
}
$('#backup-create').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try { await post('/api/backups', {}); await refreshBackups(); notify('数据库备份已保存到本机。'); }
  catch (error) { notify(error.message, true); }
  finally { button.disabled = false; }
});
$('#backup-restore').addEventListener('click', async event => {
  const button = event.currentTarget;
  const name = $('#backup-choice').value;
  if (!name || !confirm(`恢复 ${name} 将覆盖当前数据库。恢复前会自动备份，恢复后自动同步会关闭。请先退出 Codex、关闭其他发布台和命令行窗口，再在独立浏览器中恢复。确定恢复吗？`)) return;
  button.disabled = true;
  try {
    const result = await post('/api/backups/restore', {name, confirm: name});
    alert(`恢复完成，发布保护已开启。请到账号管理核对平台作品后再解除保护。恢复前的备份：${result.safety_backup}`);
    location.reload();
  } catch (error) { notify(error.message, true); }
  finally { button.disabled = false; }
});

async function refreshAccounts() {
  const selected = new Set($$('#target-list input:checked').map(el => el.value));
  const data = await api('/api/accounts');
  state.accounts = data.accounts;
  $('#account-count').textContent = `${state.accounts.length} 个账号`;
  $('#connection-text').textContent = '本机服务已连接';
  $('#connection-dot').classList.add('online');
  const empty = '<p class="empty">还没有账号，请先登记并登录。</p>';
  $('#target-list').innerHTML = state.accounts.map(account => `
    <label class="target-row"><input type="checkbox" value="${escapeHtml(account.alias)}" ${selected.has(account.alias) ? 'checked' : ''}>
    <span class="avatar">${escapeHtml(accountName(account).slice(0, 1))}</span>
    <span class="target-text"><strong>${escapeHtml(accountName(account))}</strong><small>${escapeHtml(accountCaption(account))}</small></span></label>`).join('') || empty;
  $('#account-list').innerHTML = state.accounts.map(account => `
    <div class="account-row"><span class="avatar">${escapeHtml(accountName(account).slice(0, 1))}</span>
    <span class="target-text"><strong>${escapeHtml(accountName(account))}</strong><small>${escapeHtml(accountCaption(account))} · ${escapeHtml(checkedTime(account.checked_at))}</small><small>${account.note ? `备注：${escapeHtml(account.note)}` : '暂无备注'}${account.label && account.label !== account.alias ? ` · 标签：${escapeHtml(account.label)}` : ''}</small></span>
    <div class="row-actions">${state.accountJobs[account.alias] ? `<button class="secondary" data-cancel-job="${escapeHtml(state.accountJobs[account.alias])}">取消登录</button>` : `<button class="secondary" data-account="${escapeHtml(account.alias)}" data-action="login">登录</button>
    <button class="secondary" data-account="${escapeHtml(account.alias)}" data-action="check">检查</button><button class="secondary" data-note="${escapeHtml(account.alias)}">备注</button>`}</div></div>`).join('') || empty;
  const selectedAccount = $('#history-account').value;
  $('#history-account').innerHTML = '<option value="">全部账号</option>' + state.accounts.map(account => `<option value="${escapeHtml(account.alias)}">${escapeHtml(accountName(account))}（${escapeHtml(accountCaption(account))}）</option>`).join('');
  $('#history-account').value = selectedAccount;
  updateTargetCount();
}
function updateTargetCount() {
  $('#target-count').textContent = `已选 ${$$('#target-list input:checked').length} 个`;
  renderIndividual();
}
$('#target-list').addEventListener('change', updateTargetCount);
$('#account-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await post('/api/accounts', {alias: $('#alias').value.trim(), label: $('#label').value.trim()});
    $('#account-form').reset();
    await refreshAccounts();
    notify('账号已登记。请点击登录，并在 Edge 窗口核对账号。');
  } catch (error) { notify(error.message, true); }
});
$('#account-list').addEventListener('click', async event => {
  const noteButton = event.target.closest('button[data-note]');
  if (noteButton) {
    const account = state.accounts.find(item => item.alias === noteButton.dataset.note);
    if (!account) return;
    const note = window.prompt(`编辑 ${accountName(account)}（${account.alias}）的备注（最多 120 字）`, account.note || '');
    if (note === null) return;
    try { await post(`/api/accounts/${encodeURIComponent(account.alias)}/note`, {note}); await refreshAccounts(); notify('账号备注已保存。'); }
    catch (error) { notify(error.message, true); }
    return;
  }
  const cancel = event.target.closest('button[data-cancel-job]');
  if (cancel) {
    try { await post(`/api/jobs/${encodeURIComponent(cancel.dataset.cancelJob)}/cancel`, {}); notify('正在取消登录。'); }
    catch (error) { notify(error.message, true); }
    return;
  }
  const button = event.target.closest('button[data-account]');
  if (!button) return;
  const alias = button.dataset.account;
  const action = button.dataset.action;
  let jobId = null;
  button.disabled = true;
  try {
    const result = await post(`/api/accounts/${encodeURIComponent(alias)}/${action}`, {});
    jobId = result.job;
    if (action === 'login') {
      state.accountJobs[alias] = jobId;
      await refreshAccounts();
      notify('请在 Edge 窗口登录；等待超过 10 分钟会自动结束。');
    }
    const job = await waitJob(result.job);
    if (job.result.state === 'ready') notify(`${job.result.nickname || accountName(state.accounts.find(item => item.alias === alias))}（${alias}）已进入创作者图文页。`);
    else notify(job.result.detail || '未能确认登录状态，请重试。', true);
  } catch (error) { notify(error.message, true); }
  finally {
    if (state.accountJobs[alias] === jobId) delete state.accountJobs[alias];
    button.disabled = false;
    try { await refreshAccounts(); } catch (error) { notify(error.message, true); }
  }
});

function renderIndividual() {
  const aliases = $$('#target-list input:checked').map(input => input.value);
  $('#individual-section').hidden = !aliases.length;
  $('#individual-list').innerHTML = aliases.map(alias => {
    const account = state.accounts.find(item => item.alias === alias);
    const item = state.individual[alias] ||= {textEnabled: false, initialized: false, title: '', body: '', tags: '', timing: 'inherit', schedule: ''};
    return `<details class="individual" data-individual="${escapeHtml(alias)}"><summary><strong>${escapeHtml(accountName(account))} <small>${escapeHtml(alias)}</small></strong><span>${item.textEnabled || item.timing !== 'inherit' ? '已单独设置' : '使用共用内容'}</span></summary>
      <label class="check"><input type="checkbox" data-account-field="textEnabled" ${item.textEnabled ? 'checked' : ''}>单独编辑文字</label>
      <div class="individual-text" ${item.textEnabled ? '' : 'hidden'}><label class="field"><span>标题</span><input maxlength="20" data-account-field="title" value="${escapeHtml(item.title)}"></label>
      <label class="field"><span>正文</span><textarea maxlength="1000" rows="4" data-account-field="body">${escapeHtml(item.body)}</textarea></label>
      <label class="field"><span>话题</span><input data-account-field="tags" value="${escapeHtml(item.tags)}" placeholder="用空格或逗号分隔"></label></div>
      <label class="field"><span>发布时间</span><select data-account-field="timing"><option value="inherit" ${item.timing === 'inherit' ? 'selected' : ''}>跟随共用设置</option><option value="now" ${item.timing === 'now' ? 'selected' : ''}>立即发布</option><option value="later" ${item.timing === 'later' ? 'selected' : ''}>单独定时</option></select></label>
      <label class="field individual-time" ${item.timing === 'later' ? '' : 'hidden'}><span>北京时间（UTC+8）</span><input type="datetime-local" data-account-field="schedule" value="${escapeHtml(item.schedule)}"></label></details>`;
  }).join('');
}
$('#individual-list').addEventListener('input', event => {
  const field = event.target.dataset.accountField;
  const container = event.target.closest('[data-individual]');
  if (!field || !container) return;
  const item = state.individual[container.dataset.individual];
  item[field] = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
  if (field === 'textEnabled') {
    if (item.textEnabled && !item.initialized) {
      item.initialized = true;
      item.title = $('#title').value;
      item.body = $('#body').value;
      item.tags = $('#tags').value;
      for (const name of ['title', 'body', 'tags']) container.querySelector(`[data-account-field="${name}"]`).value = item[name];
    }
    container.querySelector('.individual-text').hidden = !item.textEnabled;
  }
  if (field === 'timing') container.querySelector('.individual-time').hidden = item.timing !== 'later';
  container.querySelector('summary span').textContent = item.textEnabled || item.timing !== 'inherit' ? '已单独设置' : '使用共用内容';
});

function renderImages() {
  $('#image-count').textContent = `${state.images.length} / 18`;
  $('#images').innerHTML = state.images.map((item, index) => `
    <div class="image-tile"><button type="button" class="image-open" data-image-src="${escapeHtml(item.url)}" data-image-name="${escapeHtml(item.name)}" aria-label="查看完整图片 ${index + 1}"><img src="${escapeHtml(item.url)}" alt=""></button>
    <div class="image-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
    <div class="image-actions"><button type="button" data-index="${index}" data-move="-1" ${index === 0 ? 'disabled' : ''} aria-label="前移图片">${uiIcon('left')}</button>
    <button type="button" data-index="${index}" data-move="1" ${index === state.images.length - 1 ? 'disabled' : ''} aria-label="后移图片">${uiIcon('right')}</button>
    <button type="button" data-index="${index}" data-remove aria-label="移除图片">${uiIcon('close')}</button></div></div>`).join('');
}
async function addImages(files) {
  if (!files.length) return;
  if ($('#upload').disabled) return;
  if (state.images.length + files.length > 18) return notify('每条图文最多 18 张图片。', true);
  const invalid = files.find(file => !/\.(jpe?g|png|webp)$/i.test(file.name) || file.size === 0 || file.size > 32000000);
  if (invalid) return notify(`“${invalid.name}”请选择不超过 32 MB 的 JPG、PNG 或 WebP 图片。`, true);
  const form = new FormData();
  for (const file of files) form.append('files', file);
  $('#upload').disabled = true;
  $('#upload-message').hidden = false;
  $('#upload-message').textContent = '正在检查图片…';
  try {
    const data = await api('/api/images', {method: 'POST', body: form});
    state.images.push(...data.images);
    renderImages();
    $('#upload-message').hidden = true;
  } catch (error) { $('#upload-message').textContent = error.message; }
  finally { $('#upload').disabled = false; }
}
$('#upload').addEventListener('click', () => $('#image-input').click());
$('#image-input').addEventListener('change', event => {
  const files = [...event.target.files];
  event.target.value = '';
  addImages(files);
});
for (const eventName of ['dragenter', 'dragover']) $('#upload').addEventListener(eventName, event => { event.preventDefault(); $('#upload').classList.add('dragging'); });
for (const eventName of ['dragleave', 'drop']) $('#upload').addEventListener(eventName, event => { event.preventDefault(); $('#upload').classList.remove('dragging'); });
$('#upload').addEventListener('drop', event => addImages([...event.dataTransfer.files]));
$('#images').addEventListener('click', event => {
  const button = event.target.closest('button[data-index]');
  if (!button) return;
  const index = Number(button.dataset.index);
  if ('remove' in button.dataset) state.images.splice(index, 1);
  else {
    const next = index + Number(button.dataset.move);
    [state.images[index], state.images[next]] = [state.images[next], state.images[index]];
  }
  renderImages();
});

for (const id of ['title', 'body']) $(`#${id}`).addEventListener('input', event => {
  $(`#${id}-count`).textContent = `${event.target.value.length} / ${event.target.maxLength}`;
});
for (const radio of $$('input[name=timing]')) radio.addEventListener('change', () => {
  $('#schedule-field').hidden = $('input[name=timing]:checked').value !== 'later';
});
$('#declaration').addEventListener('change', () => {
  $('#source-field').hidden = $('#declaration').value !== 'repost';
  $('#shoot-fields').hidden = $('#declaration').value !== 'self_shot';
});
$('#original').addEventListener('change', () => {
  $('#original-consent').hidden = !$('#original').checked;
  if (!$('#original').checked) $('#original-agree').checked = false;
});

function readDraft() {
  const targets = $$('#target-list input:checked').map(input => {
    const alias = input.value, item = state.individual[alias];
    const override = {};
    if (item?.textEnabled) {
      override.title = item.title.trim(); override.body = item.body;
      override.tags = item.tags.split(/[\s,，、]+/).map(x => x.replace(/^#/, '')).filter(Boolean);
    }
    if (item?.timing === 'now') override.schedule = null;
    if (item?.timing === 'later') override.schedule = item.schedule;
    return Object.keys(override).length ? {account: alias, post: override} : alias;
  });
  if (!targets.length || targets.length > 10) throw new Error('请选择 1 至 10 个账号。');
  if (!state.images.length) throw new Error('请添加至少一张图片。');
  if ($('#original').checked && !$('#original-agree').checked) throw new Error('请阅读并同意小红书《原创声明须知》。');
  const postData = {
    title: $('#title').value.trim(), body: $('#body').value,
    tags: $('#tags').value.split(/[\s,，、]+/).map(x => x.replace(/^#/, '')).filter(Boolean),
    images: state.images.map(image => image.id)
  };
  if ($('input[name=timing]:checked').value === 'later') postData.schedule = $('#schedule').value;
  if ($('#declaration').value) postData.declaration = $('#declaration').value;
  if (postData.declaration === 'repost') postData.source_name = $('#source-name').value.trim();
  if (postData.declaration === 'self_shot') {
    postData.shoot_place = $('#shoot-place').value.trim();
    postData.shoot_address = $('#shoot-address').value.trim();
    postData.shoot_date = $('#shoot-date').value;
  }
  if ($('#original').checked) postData.original = true;
  return {post: postData, targets};
}
$('#compose-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('#preview-button').disabled = true;
  try {
    const data = await post('/api/preview', readDraft());
    openPreview(data);
    await refreshBatches();
  } catch (error) { notify(error.message, true); }
  finally { $('#preview-button').disabled = false; }
});

function canRepeat(task) {
  return !!task.duplicate_matches?.length && task.duplicate_matches.every(match => match.state === 'submitted' && !match.same_options && match.source !== 'restore_history');
}
function canAttempt(task) {
  return ['queued', 'failed', 'login_needed', 'cancelled'].includes(task.state) || (task.state === 'duplicate' && canRepeat(task));
}
function needsIdentity(batch) {
  return !!batch && batch.tasks.some(task => canAttempt(task) && !task.identity_verified);
}
function openPreview(batch) {
  state.current = batch;
  $('#batch-id').textContent = `批次 ${batch.batch}`;
  $('#confirm').checked = false;
  $('#dialog-message').hidden = true;
  $('#verification-done').hidden = true;
  $('#verification-cancel').hidden = true;
  $('#preview-content').innerHTML = batch.tasks.map(task => {
    const content = task.post || {};
    const pictures = (content.images || []).map((_, index) => {
      const src = `/api/batches/${encodeURIComponent(batch.batch)}/images/${encodeURIComponent(task.alias)}/${index}`;
      return `<button type="button" class="image-open" data-image-src="${escapeHtml(src)}" data-image-name="配图 ${index + 1}" aria-label="查看完整配图 ${index + 1}"><img src="${escapeHtml(src)}" alt=""></button>`;
    }).join('');
    const matches = task.duplicate_matches || [];
    let duplicateNote = '';
    if (matches.length) {
      const unresolved = matches.some(match => match.state !== 'submitted');
      const identical = matches.some(match => match.same_options);
      const retained = matches.some(match => match.source === 'restore_history');
      const titleOnly = matches.some(match => match.source === 'platform_title');
      const explanation = retained ? '恢复前已有相同图文的发布或待核对记录，系统会阻止再次提交。' : unresolved ? '相同图文存在执行中或待核对记录，请先到小红书核对。' :
        titleOnly ? '平台核对发现同名作品，请确认它与本次内容不同。' :
        identical ? '完全相同的发布已有记录，系统会跳过。' : '该账号已发布过相同图文，只有时间或声明不同。未勾选时将跳过该账号。';
      const approval = canRepeat(task) && canAttempt(task) ? `<label class="check"><input type="checkbox" data-duplicate-consent="${escapeHtml(task.alias)}">${titleOnly ? '我已核对平台同名作品，确认不是重复' : '我确认此账号需要再次发布相同图文'}</label>` : '';
      duplicateNote = `<div class="duplicate-warning"><p>${escapeHtml(explanation)}</p><small>相关批次：${matches.map(match => escapeHtml(match.batch)).join('、')}</small>${approval}</div>`;
    }
    return `<article class="preview-card"><div class="preview-title"><div class="preview-account"><strong>${escapeHtml(accountName(task))}</strong><small>${escapeHtml(accountCaption(task))}</small></div><span>${escapeHtml(states[task.state] || task.state)}</span></div>
      <h3>${escapeHtml(content.title)}</h3><p class="preview-body">${escapeHtml(content.body)}</p>
      <p class="muted">${(content.tags || []).map(tag => `#${escapeHtml(tag)}`).join(' ')}</p><div class="preview-images">${pictures}</div>
      <dl><dt>发布时间</dt><dd>${content.schedule ? `北京时间 ${escapeHtml(content.schedule.replace('T', ' '))}` : '立即发布'}</dd>
      <dt>内容声明</dt><dd>${escapeHtml(declarations[content.declaration] || '不添加')}${content.source_name ? ` · ${escapeHtml(content.source_name)}` : ''}${content.shoot_place ? ` · ${escapeHtml(content.shoot_place)} ${escapeHtml(content.shoot_address || '')} · ${escapeHtml(content.shoot_date)}` : ''}${content.original ? ' · 声明原创' : ''}</dd></dl>
      ${duplicateNote}${task.detail ? `<p class="field-notice">${escapeHtml(task.detail)}</p>` : ''}</article>`;
  }).join('');
  const pending = batch.tasks.some(canAttempt);
  $('#confirm-row').hidden = !pending;
  $('#publish').hidden = !pending;
  $('#publish').disabled = state.busy || needsIdentity(batch);
  if (needsIdentity(batch)) {
    $('#dialog-message').textContent = '请先到账号管理检查账号，再重新生成发布预览。';
    $('#dialog-message').hidden = false;
  }
  if (!$('#preview-dialog').open) $('#preview-dialog').showModal();
}
$('#close-preview').addEventListener('click', () => $('#preview-dialog').close());
$('#cancel-preview').addEventListener('click', () => $('#preview-dialog').close());
document.addEventListener('click', event => {
  const image = event.target.closest('button[data-image-src]');
  if (!image) return;
  $('#image-viewer-title').textContent = image.dataset.imageName || '图片预览';
  $('#image-viewer-image').src = image.dataset.imageSrc;
  $('#image-viewer-image').alt = image.dataset.imageName || '图片预览';
  $('#image-viewer').showModal();
});
$('#close-image-viewer').addEventListener('click', () => $('#image-viewer').close());

async function waitJob(id, onProgress = () => {}) {
  while (true) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    onProgress(job.detail || '正在处理，请保持工作台运行。', job);
    if (job.state === 'failed') throw new Error(job.error || '操作失败。');
    if (job.state === 'cancelled') throw new Error(job.detail || '任务已取消。');
    if (job.state === 'done') return job;
  }
}

const queueNames = {publish: '发布图文', sync: '同步私信', open: '打开原会话', login: '登录账号', check: '检查账号', platform: '核对作品'};
async function refreshQueue() {
  const result = await api('/api/browser-queue');
  const jobs = result.jobs;
  $('#queue-count').textContent = jobs.length ? `(${jobs.length})` : '';
  if (!$('#queue-dialog').open) return;
  const external = Math.max(0, result.queue.length - jobs.length);
  $('#queue-list').innerHTML = jobs.map(job => {
    const canCancel = job.state === 'waiting' || (job.state === 'running' && ['sync', 'open', 'login'].includes(job.kind));
    return `<div class="queue-row"><div><strong>${escapeHtml(queueNames[job.kind] || '浏览器任务')} · ${job.state === 'waiting' ? '等待中' : '进行中'}</strong><small>${escapeHtml(job.detail || '正在准备')}</small></div>${canCancel ? `<button class="secondary" data-cancel-job="${escapeHtml(job.id)}" data-kind="${escapeHtml(job.kind)}" data-state="${escapeHtml(job.state)}">取消</button>` : ''}</div>`;
  }).join('') + (external ? `<p class="empty">另有 ${external} 个其他窗口的浏览器任务。</p>` : '') || '<p class="empty">当前没有浏览器任务。</p>';
}
$('#queue-open').addEventListener('click', () => { $('#queue-dialog').showModal(); refreshQueue().catch(error => notify(error.message, true)); });
$('#queue-close').addEventListener('click', () => $('#queue-dialog').close());
$('#queue-list').addEventListener('click', async event => {
  const button = event.target.closest('[data-cancel-job]');
  if (!button) return;
  if (button.dataset.kind === 'open' && button.dataset.state === 'running' &&
      !confirm('关闭原会话窗口可能丢失尚未发送的文字，确定关闭吗？')) return;
  try { await post(`/api/jobs/${encodeURIComponent(button.dataset.cancelJob)}/cancel`, {}); await refreshQueue(); }
  catch (error) { notify(error.message, true); }
});
setInterval(() => refreshQueue().catch(() => {}), 2000);
refreshQueue().catch(() => {});
$('#publish').addEventListener('click', async () => {
  if (!$('#confirm').checked) return notify('请先确认内容、账号和发布时间。', true);
  if (state.busy || !state.current) return;
  state.busy = true;
  $('#publish').disabled = true;
  const batchId = state.current.batch;
  state.activeBatch = batchId;
  try {
    const allowed = $$('#preview-content input[data-duplicate-consent]:checked').map(input => input.dataset.duplicateConsent);
    if (state.current.tasks.some(task => task.state === 'duplicate' && canRepeat(task) && !allowed.includes(task.alias))) {
      throw new Error('如需再次发布，请先勾选对应账号的重复发布确认。');
    }
    const retry = state.current.tasks.some(t => ['failed', 'login_needed', 'cancelled', 'duplicate'].includes(t.state));
    const started = await post(`/api/batches/${encodeURIComponent(batchId)}/publish`, {confirm: batchId, retry, allow_duplicate_accounts: allowed});
    $('#dialog-message').hidden = false;
    const job = await waitJob(started.job, (text, status) => {
      $('#dialog-message').textContent = text;
      $('#verification-done').hidden = !status.verification || state.current?.batch !== batchId;
      $('#verification-cancel').hidden = !status.verification || state.current?.batch !== batchId;
    });
    state.current = await api(`/api/batches/${encodeURIComponent(batchId)}`);
    openPreview(state.current);
    await refreshBatches();
    notify('任务处理完成，请查看每个账号的结果。');
  } catch (error) { $('#dialog-message').hidden = false; $('#dialog-message').textContent = error.message; }
  finally { state.busy = false; state.activeBatch = null; $('#publish').disabled = needsIdentity(state.current); $('#verification-done').hidden = true; $('#verification-cancel').hidden = true; }
});
$('#verification-done').addEventListener('click', async () => {
  if (!state.activeBatch || state.current?.batch !== state.activeBatch) return;
  try {
    await post(`/api/batches/${encodeURIComponent(state.activeBatch)}/continue`, {});
    notify('已通知工作台检查验证状态。');
  } catch (error) { notify(error.message, true); }
});
async function stopVerification(batchId) {
  if (!window.confirm('取消等待后，如果已经尝试提交，请先到小红书核对结果。确认取消等待吗？')) return;
  await post(`/api/batches/${encodeURIComponent(batchId)}/cancel-verification`, {});
  notify('正在停止验证，请稍候。');
}
$('#verification-cancel').addEventListener('click', async () => {
  if (!state.activeBatch || state.current?.batch !== state.activeBatch) return;
  try { await stopVerification(state.activeBatch); } catch (error) { notify(error.message, true); }
});

function batchRow(batch) {
  const tasks = batch.tasks || [];
  const rows = tasks.map(task => `<div class="record-task"><span class="record-account"><strong>${escapeHtml(accountName(task))}</strong> <small>${escapeHtml(accountCaption(task))}</small></span>
    <span class="status status-${escapeHtml(task.state)}">${escapeHtml(states[task.state] || task.state)}</span>
    ${task.schedule ? `<span>北京时间 ${escapeHtml(task.schedule.replace('T', ' '))}</span>` : ''}
    ${task.platform_result ? `<small>平台列表：${escapeHtml(platformStates[task.platform_result.status] || '状态未确认')} · 作品 ID ${escapeHtml(task.platform_result.note_id)} · ${escapeHtml(checkedTime(task.platform_result.checked_at))}</small>` : (['submitted', 'uncertain'].includes(task.state) ? `<small>${task.identity_verified ? '平台作品状态尚未核对' : '旧记录没有账号身份快照，请手动核对平台作品'}</small>` : '')}
    ${task.detail ? `<small>${escapeHtml(task.detail)}</small>` : ''}
    ${['submitted', 'uncertain'].includes(task.state) && task.identity_verified ? `<span class="row-actions"><button class="secondary" data-sync="${escapeHtml(batch.batch)}" data-alias="${escapeHtml(task.alias)}">核对平台状态</button></span>` : ''}
    ${task.state === 'verifying' ? `<span class="row-actions"><button class="secondary" data-continue="${escapeHtml(batch.batch)}">已完成验证，继续等待</button><button class="secondary" data-cancel-verification="${escapeHtml(batch.batch)}">取消等待</button></span>` : ''}
    ${task.state === 'uncertain' ? `<span class="row-actions"><a class="text-link" href="https://creator.xiaohongshu.com/new/note-manager" target="_blank" rel="noopener noreferrer">打开笔记管理</a>
    <button class="secondary" data-reconcile="${escapeHtml(batch.batch)}" data-alias="${escapeHtml(task.alias)}" data-outcome="submitted">确认已提交</button>
    ${task.platform_result ? '' : `<button class="secondary" data-reconcile="${escapeHtml(batch.batch)}" data-alias="${escapeHtml(task.alias)}" data-outcome="failed">确认未提交</button>`}</span>` : ''}</div>`).join('');
  return `<article class="record"><div class="record-head"><div><strong>${escapeHtml(tasks[0]?.title || '图文批次')}</strong><small>批次 ${escapeHtml(batch.batch)}</small></div>
    <button class="secondary" data-preview="${escapeHtml(batch.batch)}">查看详情</button></div>${rows}</article>`;
}
async function refreshBatches() {
  try {
    const data = await api('/api/batches');
    state.batches = data.batches;
    renderHistory();
    const scheduled = state.batches.filter(batch => batch.tasks.some(task => task.schedule));
    $('#plan-list').innerHTML = scheduled.map(batchRow).join('') || '<p class="empty">还没有定时计划。</p>';
  } catch (error) { notify(error.message, true); }
}
function renderHistory() {
  const allTasks = state.batches.flatMap(batch => batch.tasks);
  const submitted = allTasks.filter(task => task.state === 'submitted').length;
  const attention = allTasks.filter(task => ['uncertain', 'failed', 'login_needed', 'verifying'].includes(task.state)).length;
  $('#history-overview').innerHTML = `<span><strong>${state.batches.length}</strong> 个批次</span><span><strong>${submitted}</strong> 个账号已提交</span><span><strong>${attention}</strong> 个账号待处理</span><small>核对平台状态会检查作品 ID 或最多 20 页笔记，匹配标题、时间和图片缩略图。</small>`;
  const query = $('#history-search').value.trim().toLowerCase();
  const alias = $('#history-account').value, selectedState = $('#history-state').value;
  const visible = state.batches.map(batch => ({...batch, tasks: batch.tasks.filter(task =>
    (!alias || task.alias === alias) && (!selectedState || task.state === selectedState) &&
    (!query || [batch.batch, task.alias, task.nickname, task.label, task.title, task.detail].some(value => String(value || '').toLowerCase().includes(query)))
  )})).filter(batch => batch.tasks.length);
  $('#history-list').innerHTML = visible.map(batchRow).join('') || '<p class="empty">没有符合条件的记录。</p>';
}
for (const selector of ['#history-search', '#history-account', '#history-state']) $(selector).addEventListener('input', renderHistory);
for (const button of $$('.refresh')) button.addEventListener('click', refreshBatches);
for (const list of [$('#history-list'), $('#plan-list')]) list.addEventListener('click', async event => {
  const button = event.target.closest('button');
  if (!button) return;
  try {
    if (button.dataset.preview) openPreview(await api(`/api/batches/${encodeURIComponent(button.dataset.preview)}`));
    if (button.dataset.sync) {
      button.disabled = true;
      const label = button.textContent;
      button.textContent = '正在核对…';
      try {
        const started = await post(`/api/batches/${encodeURIComponent(button.dataset.sync)}/sync`, {alias: button.dataset.alias});
        await waitJob(started.job);
        await refreshBatches();
        notify('平台笔记列表已核对。');
      } finally { button.disabled = false; button.textContent = label; }
    }
    if (button.dataset.continue) {
      await post(`/api/batches/${encodeURIComponent(button.dataset.continue)}/continue`, {});
      notify('已通知工作台检查验证状态。');
    }
    if (button.dataset.cancelVerification) await stopVerification(button.dataset.cancelVerification);
    if (button.dataset.reconcile) {
      const label = button.dataset.outcome === 'submitted' ? '已提交' : '未提交';
      const task = state.batches.find(batch => batch.batch === button.dataset.reconcile)?.tasks
        .find(item => item.alias === button.dataset.alias) || {alias: button.dataset.alias};
      if (!window.confirm(`请先在小红书笔记管理核对。确认账号 ${accountName(task)}（${accountCaption(task)}）的这条笔记${label}吗？`)) return;
      await post(`/api/batches/${encodeURIComponent(button.dataset.reconcile)}/reconcile`, {alias: button.dataset.alias, outcome: button.dataset.outcome});
      await refreshBatches();
      notify('已记录核对结果。');
    }
  } catch (error) { notify(error.message, true); }
});

navigate();
refreshAccounts().catch(error => { $('#connection-text').textContent = '连接失败'; notify(error.message, true); });
