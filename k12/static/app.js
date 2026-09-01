const $ = (selector) => document.querySelector(selector);
const state = {emails: [], tasks: []};

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function showMessage(text, ok = false) {
  $('#config-message').textContent = text || '';
  $('#config-message').className = `message ${ok ? 'ok' : 'bad'}`;
}

function renderEmails() {
  $('#email-count').textContent = state.emails.length;
  $('#emails').innerHTML = state.emails.map(item => `<div class="list-row"><label class="email-select"><input type="checkbox" data-email-id="${item.id}"><span>${item.email}</span></label><b class="${item.status}">${item.status}</b></div>`).join('') || '<p class="muted">暂无邮箱</p>';
}

function renderTasks() {
  $('#tasks').innerHTML = state.tasks.map(task => {
    const logs = (task.logs || []).slice(-3).map(line => `<div>${line.replaceAll('&', '&amp;').replaceAll('<', '&lt;')}</div>`).join('');
    const actions = task.status === 'running' || task.status === 'queued'
      ? `<button data-cancel="${task.id}" class="link">取消</button>`
      : `<button data-retry="${task.id}" class="link">重试</button><button data-delete-task="${task.id}" class="link">删除</button>`;
    const otp = task.status === 'running' && task.waiting_otp ? `<div class="otp-row"><input data-otp-input="${task.id}" placeholder="6 位 OTP" maxlength="6" inputmode="numeric"><button data-submit-otp="${task.id}" class="link">提交 OTP</button></div>` : '';
    return `<article class="task"><div class="task-head"><strong>${task.email}</strong><span class="${task.status}">${task.status}</span>${actions}</div>${otp}<div class="logs">${logs || '暂无日志'}</div></article>`;
  }).join('') || '<p class="muted">暂无任务</p>';
}

async function loadConfig() {
  const data = await api('/api/config');
  const config = data.config || {};
  $('#workspace-ids').value = (config.workspace_ids || []).join('\n');
  $('#workspace-route').value = config.workspace_route || 'request';
  $('#run-workspace').checked = config.run_workspace_join !== false;
  $('#concurrency').value = config.concurrency || 1;
  $('#sms-provider').value = config.sms_provider || 'auto';
  $('#otp-mode').value = config.otp_mode || 'auto';
  $('#node').value = config.node || 'auto';
  $('#group').value = config.group || 'k12';
  $('#sub2api-url').value = config.sub2api_url || '';
  $('#sub2api-email').value = config.sub2api_email || '';
  $('#network-mode').value = config.network_mode || 'inherit';
  await loadProxy();
}

async function loadProxy() {
  try {
    const data = await api('/api/proxy');
    const select = $('#network-node');
    const current = data.configured_node || data.current || '';
    select.innerHTML = '<option value="">自动选择</option>' + (data.nodes || []).map(node => `<option value="${node.replaceAll('"', '&quot;')}">${node}</option>`).join('');
    if (current && [...select.options].some(option => option.value === current)) select.value = current;
    $('#network-mode').value = data.configured_mode || data.mode || 'inherit';
    $('#proxy-message').textContent = `当前：${data.current || '未检测'}${data.nodes?.length ? `，可选节点 ${data.nodes.length} 个` : ''}`;
    $('#proxy-message').className = 'message ok';
  } catch (error) {
    $('#proxy-message').textContent = error.message || String(error);
    $('#proxy-message').className = 'message bad';
  }
}

async function saveProxy() {
  try {
    await api('/api/proxy/select', {method: 'POST', body: JSON.stringify({mode: $('#network-mode').value, node: $('#network-node').value})});
    await loadProxy();
  } catch (error) {
    $('#proxy-message').textContent = error.message || String(error);
    $('#proxy-message').className = 'message bad';
  }
}

async function saveConfig() {
  try {
    await api('/api/config', {method: 'PATCH', body: JSON.stringify({
      workspace_ids: $('#workspace-ids').value,
      workspace_route: $('#workspace-route').value,
      run_workspace_join: $('#run-workspace').checked,
      concurrency: Number($('#concurrency').value || 1),
      sms_provider: $('#sms-provider').value,
      otp_mode: $('#otp-mode').value,
      node: $('#node').value.trim() || 'auto',
      group: $('#group').value.trim() || 'k12',
      sub2api_url: $('#sub2api-url').value.trim(),
      sub2api_email: $('#sub2api-email').value.trim(),
      sub2api_password: $('#sub2api-password').value.trim(),
    })});
    showMessage('配置已保存', true);
  } catch (error) { showMessage(error.message); }
}

async function loadEmails() { state.emails = (await api('/api/emails')).items || []; renderEmails(); }
async function loadSummary() { const data = await api('/api/summary'); const emails = data.emails || {}; const tasks = data.tasks || {}; $('#stat-emails').textContent = emails.total || 0; $('#stat-free').textContent = emails.free || 0; $('#stat-running').textContent = tasks.running || 0; $('#stat-success').textContent = tasks.success || 0; $('#stat-failed').textContent = tasks.failed || 0; $('#stat-workspaces').textContent = data.workspace_ids || 0; }
async function importEmails() {
  const text = $('#email-input').value.trim();
  if (!text) return showMessage('请填写邮箱记录');
  try { const data = await api('/api/emails/import', {method: 'POST', body: JSON.stringify({text})}); $('#email-input').value = ''; showMessage(`导入完成：新增 ${data.added}`, true); await loadEmails(); } catch (error) { showMessage(error.message); }
}
async function loadTasks() { state.tasks = (await api('/api/tasks')).items || []; renderTasks(); }
async function startTasks() {
  try { await saveConfig(); await api('/api/tasks', {method: 'POST', body: JSON.stringify({count: Number($('#task-count').value || 1)})}); await loadTasks(); } catch (error) { showMessage(error.message); }
}
async function syncEmails() { try { const data = await api('/api/factory/import-emails', {method:'POST', body:'{}'}); showMessage(`已同步主邮箱池：新增 ${data.added}`, true); await Promise.all([loadEmails(), loadSummary()]); } catch(error) { showMessage(error.message); } }
function selectedEmailIds() { return [...document.querySelectorAll('[data-email-id]:checked')].map(input => input.dataset.emailId); }
async function deleteEmails() { const ids = selectedEmailIds(); if (!ids.length) return showMessage('请先选择邮箱'); await api('/api/emails/delete', {method:'POST', body:JSON.stringify({ids})}); await Promise.all([loadEmails(), loadSummary()]); }
async function splitEmails() { const ids = selectedEmailIds(); if (!ids.length) return showMessage('请先选择母邮箱'); await api('/api/emails/split', {method:'POST', body:JSON.stringify({ids, count:Number($('#split-count').value || 4)})}); await Promise.all([loadEmails(), loadSummary()]); }
async function exportData() { const response = await fetch('/api/data/export'); const blob = await response.blob(); const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = 'k12-data.json'; link.click(); URL.revokeObjectURL(link.href); }
async function importData(event) { const file = event.target.files?.[0]; if (!file) return; await api('/api/data/import', {method:'POST', body:await file.text()}); event.target.value=''; await refresh(); }
async function checkAt() { const data = await api('/api/tasks/check-at', {method:'POST', body:JSON.stringify({})}); showMessage(`AT 测活完成：正常 ${data.ok}，失活 ${data.inactive}`, data.inactive === 0); await Promise.all([loadTasks(), loadSummary()]); }
async function repairAt() { const data = await api('/api/tasks/repair-at', {method:'POST', body:'{}'}); showMessage(`已创建 ${data.count || 0} 个 AT 修复任务`, true); await Promise.all([loadTasks(), loadSummary()]); }
async function clearFailed() { await api('/api/tasks/clear-failed', {method:'POST', body:'{}'}); await Promise.all([loadTasks(), loadSummary()]); }
async function refresh() { try { await Promise.all([loadConfig(), loadEmails(), loadTasks(), loadSummary()]); $('#health-dot').className = 'online'; $('#health').textContent = '服务在线'; } catch (error) { $('#health').textContent = error.message; } }

$('#save-config').onclick = saveConfig;
$('#refresh-proxy').onclick = loadProxy;
$('#network-mode').onchange = saveProxy;
$('#network-node').onchange = saveProxy;
$('#import-emails').onclick = importEmails;
$('#sync-emails').onclick = syncEmails;
$('#refresh-emails').onclick = loadEmails;
$('#delete-emails').onclick = deleteEmails;
$('#split-emails').onclick = splitEmails;
$('#export-data').onclick = exportData;
$('#import-data').onchange = importData;
$('#refresh-tasks').onclick = loadTasks;
$('#start-tasks').onclick = startTasks;
$('#check-at').onclick = checkAt;
$('#repair-at').onclick = repairAt;
$('#clear-failed').onclick = clearFailed;
$('#tasks').onclick = async (event) => {
  const target = event.target;
  const cancel = target.dataset.cancel;
  const retry = target.dataset.retry;
  const remove = target.dataset.deleteTask;
  const otpTask = target.dataset.submitOtp;
  if (otpTask) {
    const code = document.querySelector(`[data-otp-input="${otpTask}"]`)?.value || '';
    await api(`/api/tasks/${otpTask}/otp`, {method: 'POST', body: JSON.stringify({code})});
    await loadTasks();
    return;
  }
  if (cancel) await api(`/api/tasks/${cancel}/cancel`, {method: 'POST', body: '{}'});
  else if (retry) await api(`/api/tasks/${retry}/retry`, {method: 'POST', body: '{}'});
  else if (remove) await api(`/api/tasks/${remove}`, {method: 'DELETE'});
  else return;
  await Promise.all([loadTasks(), loadSummary()]);
};
$('#refresh-all').onclick = refresh;
setInterval(()=>Promise.all([loadTasks(), loadSummary()]), 2500);
refresh();
