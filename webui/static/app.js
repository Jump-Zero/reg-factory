// reg-factory WebUI 前端逻辑（原生 JS，无构建）
let SCRIPTS = [];
let EMBEDS = [];
let curRun = null;     // 当前运行 run_id
let curSrc = null;     // 当前选中脚本
let evtSrc = null;     // EventSource
let smsTimer = null;   // 接码助手倒计时刷新
let k12Url = 'http://127.0.0.1:8806/';
let k12Starting = false;
let plusUrl = '/chatgpt-plus/';
const PLUS_BATCH_SIZE = 27;
const PLUS_IMPORT_LIMIT = 100;
let plusImporting = false;
let proxyMode = 'clash_auto';
let assetPickMode = 'sequence';
let assetScanData = null;
let assetScanPage = 1;
let assetScanTimer = null;
let currentWebVersion = '';

// ─── 悬浮通知系统 ───
const _toastStack = () => document.getElementById('rf-toast-stack');
const _toastTimers = new Map();
function showToast(text, type='info', timeout=6000){
  const stack = _toastStack();
  if(!stack) return;
  const el = document.createElement('div');
  el.className = `rf-toast ${type}`;
  el.innerHTML = `<div class="rf-toast-body"></div><button class="rf-toast-close" type="button" aria-label="关闭">&times;</button>`;
  el.querySelector('.rf-toast-body').textContent = text;
  const close = () => {
    el.classList.add('removing');
    const tid = _toastTimers.get(el);
    if(tid){ clearTimeout(tid); _toastTimers.delete(el); }
    setTimeout(()=> el.remove(), 200);
  };
  el.querySelector('.rf-toast-close').onclick = close;
  stack.appendChild(el);
  if(timeout > 0) _toastTimers.set(el, setTimeout(close, timeout));
  return el;
}
function showPersistentToast(text, type='info'){
  return showToast(text, type, 0);
}
function updatePersistentToast(el, text, type){
  if(!el) return showToast(text, type, 0);
  el.className = `rf-toast ${type}`;
  el.querySelector('.rf-toast-body').textContent = text;
  return el;
}
let updateRequested = false;
let guideActive = false;
let guideIndex = 0;
let guideTarget = null;
let guidePositionFrame = null;
let scriptsReady = null;
let guideOriginalProxyMode = null;

const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];

async function readJsonResponse(response){
  const text = await response.text();
  if(!text.trim()){
    if(!response.ok) throw new Error(`HTTP ${response.status}`);
    return {};
  }
  try{
    return JSON.parse(text);
  }catch(_error){
    const detail = text.replace(/\s+/g, ' ').trim().slice(0, 180);
    throw new Error(`HTTP ${response.status}: ${detail || 'Invalid server response'}`);
  }
}

function setNavOpen(open){
  document.body.classList.toggle('nav-open', open);
  $('#btn-nav').setAttribute('aria-expanded', String(open));
}

$('#btn-nav').onclick = ()=>setNavOpen(!document.body.classList.contains('nav-open'));
$('#nav-backdrop').onclick = ()=>setNavOpen(false);

const telegramDialog = $('#telegram-dialog');
$('#btn-telegram').onclick = ()=>telegramDialog.showModal();
$('#btn-telegram-close').onclick = ()=>telegramDialog.close();
telegramDialog.onclick = e=>{
  if(e.target === telegramDialog) telegramDialog.close();
};

// ---------------------------------------------------------------- 状态灯轮询
function setUpdateMessage(text, state=''){
  const message = $('#update-message');
  message.textContent = text || '';
  message.classList.toggle('ok', state==='ok');
  message.classList.toggle('bad', state==='bad');
}

function renderUpdateState(update, version){
  const button = $('#btn-update');
  const label = $('#update-label');
  const available = !!(update && update.available);
  currentWebVersion = version || currentWebVersion;
  if(updateRequested && update && update.status === 'failed'){
    updateRequested = false;
    button.classList.remove('running');
    button.disabled = !available;
    label.textContent = '重试更新';
    setUpdateMessage(update.message || '更新失败', 'bad');
    return;
  }
  if(updateRequested && update && update.status === 'completed'){
    button.classList.remove('running');
    button.disabled = true;
    label.textContent = '更新完成';
    setUpdateMessage('面板正在重启…', 'ok');
    setTimeout(()=>location.reload(), 900);
    return;
  }
  button.disabled = updateRequested || !available;
  button.classList.toggle('running', updateRequested || update?.status === 'running');
  if(updateRequested || update?.status === 'running'){
    label.textContent = '更新中';
    setUpdateMessage(update?.message || '面板正在重启…');
  }else if(!available){
    label.textContent = '不可更新';
    button.title = '当前安装方式不包含自动更新程序';
  }else{
    label.textContent = '更新';
    button.title = '检查并安装最新版本';
  }
}

async function pollStatus(){
  try{
    const s = await (await fetch('/api/status')).json();
    $('#dot-bb').classList.toggle('on', s.bitbrowser);
    const browserLabels = {ruyipage:'RuyiPage Firefox', adspower:'AdsPower', bundled:'内置浏览器', custom:'自定义 Chrome', custom_api:'自定义指纹浏览器'};
    const label = browserLabels[s.browser_provider] || 'BitBrowser';
    $('#browser-label').textContent = label;
    const networkOnline = s.network ?? s.clash;
    $('#dot-clash').classList.toggle('on', !!networkOnline);
    const modeLabels = {clash_auto:'Clash 自动', clash_fixed:'Clash 固定', residential:'住宅代理', direct:'直连'};
    $('#network-label').textContent = modeLabels[s.proxy_mode] || '网络出口';
    $('#dot-k12').classList.remove('pending');
    $('#dot-k12').classList.toggle('on', !!s.k12);
    $('#k12-nav-state').textContent = s.k12 ? '在线' : '离线';
    $('#k12-nav-state').classList.toggle('on', !!s.k12);
    $('#dot-plus').classList.remove('pending');
    $('#dot-plus').classList.toggle('on', !!s.chatgpt_plus);
    $('#plus-nav-state').textContent = s.chatgpt_plus ? '在线' : '离线';
    $('#plus-nav-state').classList.toggle('on', !!s.chatgpt_plus);
    $('#version').textContent = 'v' + (s.version || '--');
    if(updateRequested && currentWebVersion && s.version && s.version !== currentWebVersion){
      setUpdateMessage('更新完成，正在刷新…', 'ok');
      setTimeout(()=>location.reload(), 500);
    }
    renderUpdateState(s.update || {}, s.version || '');
    $('#node').textContent = '出口 ' + (s.node || '--');
    const online = [s.bitbrowser, networkOnline, s.k12, s.chatgpt_plus].filter(Boolean).length;
    $('#health-dot').classList.remove('pending', 'on', 'partial');
    if(online === 4) $('#health-dot').classList.add('on');
    else if(online) $('#health-dot').classList.add('partial');
    $('#health-label').textContent = `${online}/4 服务在线`;
    $('#running').textContent = s.running ? `${s.running} 个任务运行中` : '';
  }catch(e){
    if(updateRequested) setUpdateMessage('面板正在重启，等待重新连接…');
  }
}
setInterval(pollStatus, 5000);

async function startUpdate(){
  if(updateRequested) return;
  if(!window.confirm('更新会停止当前面板并重启，运行中的任务必须先停止。继续吗？')) return;
  const button = $('#btn-update');
  currentWebVersion = currentWebVersion || $('#version').textContent.replace(/^v/, '');
  updateRequested = true;
  button.disabled = true;
  button.classList.add('running');
  $('#update-label').textContent = '更新中';
  setUpdateMessage('正在准备更新…');
  try{
    const response = await fetch('/api/update', {method:'POST'});
    const data = await response.json().catch(()=>({}));
    if(!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
    renderUpdateState(data.update || {status:'running'}, currentWebVersion);
  }catch(error){
    updateRequested = false;
    button.classList.remove('running');
    button.disabled = false;
    $('#update-label').textContent = '重试更新';
    setUpdateMessage(error.message || String(error), 'bad');
  }
}
$('#btn-update').onclick = startUpdate;

// ---------------------------------------------------------------- 视图切换
async function showView(v){
  setNavOpen(false);
  document.body.classList.toggle('k12-active', v==='k12');
  $('#view-run').style.display  = v==='run' ? '' : 'none';
  $('#view-env').style.display  = v==='env' ? 'block' : 'none';
  $('#view-assets').style.display = v==='assets' ? 'block' : 'none';
  $('#view-network').style.display = v==='network' ? 'block' : 'none';
  $('#view-embed').style.display = v==='embed' ? 'block' : 'none';
  $('#view-mailpool').style.display = v==='mailpool' ? 'block' : 'none';
  $('#view-k12').style.display = v==='k12' ? 'block' : 'none';
  $('#view-plus').style.display = v==='plus' ? 'block' : 'none';
  $$('.navbtn').forEach(b=>b.classList.toggle('active', b.dataset.view===v));
  if(v!=='run' && v!=='embed') $$('.scriptbtn').forEach(b=>b.classList.remove('active'));
  if(v==='env') return loadEnv();
  if(v==='assets') return loadAssetPanel();
  if(v==='network') return loadProxyPanel();
  if(v==='mailpool') return loadMailpool();
  if(v==='k12') return openK12Channel();
  if(v==='plus'){ loadPlusAtCount(); return openPlusChannel(); }
  return Promise.resolve();
}
$$('.navbtn').forEach(b=> b.onclick = ()=> showView(b.dataset.view));

// ---------------------------------------------------------------- 网络出口
function setProxyMode(mode){
  proxyMode = mode;
  $$('[data-proxy-mode]').forEach(button=>button.classList.toggle('active', button.dataset.proxyMode===mode));
  updateProxyFieldVisibility();
}

function updateProxyFieldVisibility(){
  const platformModes = $$('[data-platform-proxy]').map(select=>select.value);
  const needsClash = ['clash_auto','clash_fixed'].includes(proxyMode)
    || platformModes.some(mode=>['clash_auto','clash_fixed'].includes(mode));
  const needsFixed = proxyMode === 'clash_fixed' || platformModes.includes('clash_fixed');
  const needsResidential = proxyMode === 'residential' || platformModes.includes('residential');
  $('#network-clash-fields').style.display = needsClash ? 'block' : 'none';
  $('#network-fixed-field').style.display = needsFixed ? 'flex' : 'none';
  $('#network-residential-fields').style.display = needsResidential ? 'block' : 'none';
}

function setProxyMessage(text, ok=null){
  const message = $('#proxy-msg');
  message.textContent = text || '';
  message.classList.toggle('ok', ok===true);
  message.classList.toggle('bad', ok===false);
}

function renderProxyNodes(nodes, selected){
  const select = $('#proxy-fixed-node');
  select.innerHTML = '';
  const values = [...(nodes||[])];
  if(selected && !values.includes(selected)) values.unshift(selected);
  if(!values.length){
    const option = document.createElement('option');
    option.value = ''; option.textContent = '未读取到节点';
    select.appendChild(option);
    return;
  }
  values.forEach(node=>{
    const option = document.createElement('option');
    option.value = node; option.textContent = node; option.selected = node===selected;
    select.appendChild(option);
  });
}

function escapeHtml(value){
  return String(value ?? '').replace(/[&<>"']/g, char=>({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;',
  }[char]));
}

function renderPlusRouteOptions(id, nodes, selected, fallback){
  const select = $(id);
  if(!select) return;
  const values = [
    {value:'residential', label:'住宅 IP'},
    {value:'clash', label:'Clash 当前节点'},
    ...(nodes || []).map(node=>({value:`clash:${node}`, label:`Clash：${node}`})),
  ];
  const known = new Set(values.map(item=>item.value));
  if(selected && !known.has(selected) && selected.startsWith('clash:')){
    values.push({value:selected, label:`Clash：${selected.slice(6)}`});
  }
  select.innerHTML = values.map(item=>`<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join('');
  select.value = selected || fallback;
  if(!select.value) select.value = fallback;
}

async function loadProxyPanel(includeNodes=false){
  setProxyMessage('读取中…');
  try{
    const response = await fetch(includeNodes ? '/api/proxy?nodes=1' : '/api/proxy');
    const data = await readJsonResponse(response);
    if(!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    const config = data.config || {};
    setProxyMode(config.PROXY_MODE || 'clash_auto');
    $('#proxy-clash-api').value = config.CLASH_API || '';
    $('#proxy-clash-secret').value = config.CLASH_SECRET || '';
    $('#proxy-clash-proxy').value = config.CLASH_PROXY || '';
    $('#proxy-clash-group').value = config.CLASH_GROUP || '';
    const residentialConfigured = Boolean(config.REG_FACTORY_PROXY || config.REG_FACTORY_PROXY_POOL);
    renderPlusRouteOptions(
      '#proxy-plus-link-route',
      data.nodes,
      config.REG_FACTORY_PLUS_LINK_ROUTE || (residentialConfigured ? 'residential' : 'clash'),
      residentialConfigured ? 'residential' : 'clash',
    );
    renderPlusRouteOptions(
      '#proxy-plus-bind-route',
      data.nodes,
      config.REG_FACTORY_PLUS_BIND_ROUTE || 'clash',
      'clash',
    );
    $('#proxy-plus-link-proxy').value = config.REG_FACTORY_PLUS_LINK_PROXY_OVERRIDE || '';
    $('#proxy-plus-bind-proxy').value = config.REG_FACTORY_PLUS_BIND_PROXY_OVERRIDE || '';
    $('#proxy-residential-url').value = config.REG_FACTORY_PROXY || '';
    $('#proxy-residential-pool').value = config.REG_FACTORY_PROXY_POOL || '';
    $('#proxy-rotate-url').value = config.REG_FACTORY_PROXY_ROTATE_URL || '';
    $('#proxy-rotate-method').value = config.REG_FACTORY_PROXY_ROTATE_METHOD || 'GET';
    $('#proxy-chatgpt-retries').value = config.CHATGPT_RESIDENTIAL_ROTATE_RETRIES || '3';
    $('#proxy-mode-outlook').value = config.OUTLOOK_PROXY_MODE || 'inherit';
    $('#proxy-mode-claude').value = config.CLAUDE_PROXY_MODE || 'inherit';
    $('#proxy-mode-chatgpt').value = config.CHATGPT_PROXY_MODE || 'inherit';
    $('#proxy-mode-grok').value = config.GROK_PROXY_MODE || 'inherit';
    $('#proxy-mode-kiro').value = config.KIRO_PROXY_MODE || 'inherit';
    updateProxyFieldVisibility();
    renderProxyNodes(data.nodes, config.CLASH_FIXED_NODE || data.current);
    $('#network-current').textContent = `${data.current || '未连接'} · ${config.PROXY_MODE || 'clash_auto'}`;
    $('#network-current-dot').classList.remove('pending');
    $('#network-current-dot').classList.toggle('on', !!data.current);
    setProxyMessage('');
  }catch(error){
    setProxyMessage('读取网络配置失败: '+error, false);
  }
}

function collectProxyConfig(){
  return {
    PROXY_MODE: proxyMode,
    OUTLOOK_PROXY_MODE: $('#proxy-mode-outlook').value,
    CLAUDE_PROXY_MODE: $('#proxy-mode-claude').value,
    CHATGPT_PROXY_MODE: $('#proxy-mode-chatgpt').value,
    GROK_PROXY_MODE: $('#proxy-mode-grok').value,
    KIRO_PROXY_MODE: $('#proxy-mode-kiro').value,
    CLASH_API: $('#proxy-clash-api').value.trim(),
    CLASH_SECRET: $('#proxy-clash-secret').value.trim(),
    CLASH_PROXY: $('#proxy-clash-proxy').value.trim(),
    CLASH_GROUP: $('#proxy-clash-group').value.trim(),
    CLASH_FIXED_NODE: $('#proxy-fixed-node').value,
    REG_FACTORY_PLUS_LINK_ROUTE: $('#proxy-plus-link-route').value,
    REG_FACTORY_PLUS_BIND_ROUTE: $('#proxy-plus-bind-route').value,
    REG_FACTORY_PLUS_LINK_PROXY_OVERRIDE: $('#proxy-plus-link-proxy').value.trim(),
    REG_FACTORY_PLUS_BIND_PROXY_OVERRIDE: $('#proxy-plus-bind-proxy').value.trim(),
    REG_FACTORY_PROXY: $('#proxy-residential-url').value.trim(),
    REG_FACTORY_PROXY_POOL: $('#proxy-residential-pool').value.trim(),
    REG_FACTORY_PROXY_ROTATE_URL: $('#proxy-rotate-url').value.trim(),
    REG_FACTORY_PROXY_ROTATE_METHOD: $('#proxy-rotate-method').value,
    CHATGPT_RESIDENTIAL_ROTATE_RETRIES: $('#proxy-chatgpt-retries').value,
  };
}

async function applyProxyConfig(){
  const response = await fetch('/api/proxy', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({config:collectProxyConfig()}),
  });
  const data = await readJsonResponse(response);
  if(!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  if(!data.ok) throw new Error(data.error || '保存失败');
  if(data.config?.PROXY_MODE && data.config.PROXY_MODE !== proxyMode){
    throw new Error(`代理模式未生效：后端仍为 ${data.config.PROXY_MODE}`);
  }
  return data;
}

async function saveProxy(){
  const button = $('#btn-save-proxy');
  button.disabled = true; setProxyMessage('正在保存并应用…');
  try{
    const data = await applyProxyConfig();
    setProxyMessage(`已应用，当前出口：${data.current || data.applied?.node || '--'}`, true);
    await pollStatus();
  }catch(error){
    setProxyMessage(error.message || String(error), false);
  }finally{ button.disabled = false; }
}

async function rotateProxy(){
  const button = $('#btn-rotate-proxy');
  button.disabled = true; setProxyMessage('正在切换出口…');
  try{
    const target = $('#proxy-platform-target').value;
    const params = target ? `?platform=${encodeURIComponent(target)}` : '';
    const response = await fetch(`/api/proxy/rotate${params}`, {method:'POST'});
    const data = await readJsonResponse(response);
    if(!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    if(!data.ok) throw new Error(data.error || '轮换失败');
    const delay = data.latency_ms ? `，延迟 ${data.latency_ms} ms` : '';
    const applies = data.mode === 'residential' ? '，新建 BitBrowser 窗口生效' : '';
    setProxyMessage(`${data.platform || 'global'} 当前出口：${data.node || '--'}${delay}${applies}`, true);
    $('#network-current').textContent = `${data.node || '--'} · ${data.mode || proxyMode}`;
    await pollStatus();
  }catch(error){ setProxyMessage(error.message || String(error), false); }
  finally{ button.disabled = false; }
}

async function testProxy(){
  const button = $('#btn-test-proxy');
  const saveButton = $('#btn-save-proxy');
  button.disabled = true; saveButton.disabled = true;
  setProxyMessage('正在应用当前配置并检测公网出口…');
  try{
    const applied = await applyProxyConfig();
    const target = $('#proxy-platform-target').value;
    const params = target ? `?platform=${encodeURIComponent(target)}` : '';
    const response = await fetch(`/api/proxy/test${params}`, {method:'POST'});
    const data = await readJsonResponse(response);
    if(!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    if(!data.ok) throw new Error(data.error || '检测失败');
    setProxyMessage(`${data.platform || 'global'} 出口 IP：${data.ip} · ${data.node || data.mode}`, true);
    $('#network-current').textContent = `${applied.current || data.node || '--'} · ${data.mode || proxyMode}`;
    await pollStatus();
  }catch(error){ setProxyMessage(error.message || String(error), false); }
  finally{ button.disabled = false; saveButton.disabled = false; }
}

$$('[data-proxy-mode]').forEach(button=>button.onclick=()=>setProxyMode(button.dataset.proxyMode));
$$('[data-platform-proxy]').forEach(select=>select.onchange=updateProxyFieldVisibility);
$('#btn-save-proxy').onclick = saveProxy;
$('#btn-rotate-proxy').onclick = rotateProxy;
$('#btn-test-proxy').onclick = testProxy;
$('#btn-refresh-nodes').onclick = ()=>loadProxyPanel(true);

// ---------------------------------------------------------------- 本地资产 API
const ASSET_FORMATS = {
  emails: ['json', 'line'],
  chatgpt: ['cookies', 'raw', 'header', 'session', 'sub2api', 'cpa', 'chatgpt2api'],
  claude: ['cookies', 'raw', 'header'],
  grok: ['cookies', 'raw', 'header', 'session', 'sub2api'],
  kiro: ['session'],
};
const ASSET_SCAN_STATUS_LABELS = {
  normal:'正常', unlock:'待解锁', banned:'封禁', expired:'过期', restricted:'受限',
  invalid:'凭据异常', unknown:'未知', error:'检测异常',
};
const ASSET_SCAN_PLATFORM_LABELS = {
  outlook:'Outlook', chatgpt:'ChatGPT', claude:'Claude', grok:'Grok', kiro:'Kiro',
};
const ASSET_PLUS_TRIAL_LABELS = {
  eligible:'可试用', ineligible:'暂无试用', active:'已有套餐', unknown:'资格未知', disabled:'检测关闭',
};
const ASSET_SCAN_PAGE_SIZE = 25;

function assetHeaders(json=false){
  const headers = {};
  const key = $('#asset-api-key').value.trim();
  if(key) headers['X-API-Key'] = key;
  if(json) headers['Content-Type'] = 'application/json';
  return headers;
}

function setAssetMessage(target, text, ok=null){
  // 悬浮通知：只对扫描/导入/删除/分裂相关消息弹出
  if(target === '#asset-scan-msg' && text){
    const type = ok===true ? 'ok' : ok===false ? 'bad' : 'info';
    showToast(text, type, ok===true ? 5000 : 8000);
  }
}

function updateAssetFormats(){
  const resource = $('#asset-resource').value;
  const select = $('#asset-format');
  const previous = select.value;
  select.innerHTML = '';
  ASSET_FORMATS[resource].forEach(format=>{
    const option = document.createElement('option');
    option.value = format;
    option.textContent = format;
    option.selected = format === previous;
    select.appendChild(option);
  });
  updateAssetRequestPreview();
}

function buildAssetRequest(){
  const resource = $('#asset-resource').value;
  const format = $('#asset-format').value || ASSET_FORMATS[resource][0];
  const path = resource === 'emails' ? '/api/assets/emails' : `/api/assets/cookies/${resource}`;
  const params = new URLSearchParams({format});
  if(assetPickMode === 'index'){
    const index = Math.max(0, parseInt($('#asset-index').value || '0', 10));
    params.set('index', String(index));
  }
  const relative = `${path}?${params}`;
  return {resource, format, relative, absolute:`${window.location.origin}${relative}`};
}

function updateAssetRequestPreview(){
  const request = buildAssetRequest();
  $('#asset-api-base').value = window.location.origin;
  $('#asset-request-url').textContent = request.absolute;
  const auth = $('#asset-api-key').value.trim() ? " -H \"X-API-Key: your-key\"" : '';
  $('#asset-curl-example').textContent = `curl \"${request.absolute}\"${auth}`;
}

function setAssetPickMode(mode){
  assetPickMode = mode;
  $$('[data-asset-pick]').forEach(button=>button.classList.toggle('active', button.dataset.assetPick===mode));
  $('#asset-index').disabled = mode !== 'index';
  updateAssetRequestPreview();
}

async function readAssetResponse(response){
  const text = await response.text();
  let data;
  try{ data = text ? JSON.parse(text) : {}; }
  catch(error){ data = {error:text || `HTTP ${response.status}`}; }
  if(!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function renderAssetSummary(data){
  $('#asset-count-emails').textContent = data.emails ?? 0;
  ['chatgpt', 'claude', 'grok', 'kiro'].forEach(platform=>{
    const item = data.platforms?.[platform] || {};
    const total = Math.max(item.cookies || 0, item.sessions || 0);
    $(`#asset-count-${platform}`).textContent = total;
  });
}

function formatAssetScanTime(value){
  if(!value) return '--';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', {hour12:false});
}

function appendAssetScanCell(row, text, className=''){
  const cell = document.createElement('td');
  if(className) cell.className = className;
  cell.textContent = text || '--';
  row.appendChild(cell);
  return cell;
}

function filteredAssetScanItems(){
  if(!assetScanData) return [];
  const platform = $('#asset-scan-platform').value;
  const status = $('#asset-scan-status').value;
  const source = $('#asset-scan-source') ? $('#asset-scan-source').value : 'all';
  return (assetScanData.items || []).filter(item=>
    (platform === 'all' || item.platform === platform) &&
    (status === 'all' ||
     (status === 'sub_imported' && item.sub2api_uploaded) ||
     (status === 'sub_not_imported' && !item.sub2api_uploaded) ||
     (status !== 'sub_imported' && status !== 'sub_not_imported' && item.status === status)) &&
    (source === 'all' || item.mail_source === source)
  );
}

function renderAssetScanTable(){
  const items = filteredAssetScanItems();
  const pages = Math.max(1, Math.ceil(items.length / ASSET_SCAN_PAGE_SIZE));
  assetScanPage = Math.min(Math.max(1, assetScanPage), pages);
  const start = (assetScanPage - 1) * ASSET_SCAN_PAGE_SIZE;
  const visible = items.slice(start, start + ASSET_SCAN_PAGE_SIZE);
  const body = $('#asset-scan-rows');
  body.innerHTML = '';
  // 从完整 items 中构建子邮箱索引（不限于当前页），用于母邮箱展开时查找子邮箱数据
  const allChildMap = {};
  (assetScanData?.items || []).forEach(item=>{
    if(item.parent_email){
      const key = item.parent_email.toLowerCase();
      if(!allChildMap[key]) allChildMap[key] = [];
      allChildMap[key].push(item);
    }
  });
  // 辅助函数：创建一行（复用渲染逻辑）
  function createScanRow(item, isChild){
    const row = document.createElement('tr');
    const isParentWithKids = !isChild && item.is_parent && (item.child_emails || []).length > 0;
    if(isParentWithKids){
      row.className = 'asset-scan-row-parent';
      row.dataset.parentEmail = (item.email || '').toLowerCase();
      row.style.cursor = 'pointer';
    }
    if(isChild){
      row.className = 'asset-scan-row-child';
      row.dataset.parentEmail = item.parent_email.toLowerCase();
      row.style.display = 'none';
    }
    // 复选框列
    const checkCell = document.createElement('td');
    checkCell.className = 'col-check';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'asset-scan-check';
    cb.dataset.email = item.email || '';
    cb.dataset.platform = item.platform || '';
    cb.addEventListener('click', e=>e.stopPropagation());
    checkCell.appendChild(cb);
    row.appendChild(checkCell);
    // 类型
    appendAssetScanCell(row, ASSET_SCAN_PLATFORM_LABELS[item.platform] || item.platform, 'asset-scan-platform');
    // 账号
    const account = appendAssetScanCell(row, item.email || item.source, 'asset-scan-account');
    account.title = item.source || '';
    // 母/子邮箱标记
    if(item.platform === 'outlook'){
      if(isParentWithKids){
        const pBadge = document.createElement('span');
        pBadge.className = 'mail-split-badge parent';
        pBadge.textContent = `母(${item.child_count || 0})`;
        pBadge.title = `点击展开/折叠子邮箱`;
        account.appendChild(pBadge);
        const arrow = document.createElement('span');
        arrow.className = 'mail-split-arrow';
        arrow.textContent = '▶';
        account.insertBefore(arrow, account.firstChild);
        row._arrow = arrow;
      } else if(isChild || item.parent_email){
        const cBadge = document.createElement('span');
        cBadge.className = 'mail-split-badge child';
        cBadge.textContent = `子`;
        cBadge.title = `子邮箱，母邮箱: ${item.parent_email}`;
        account.appendChild(cBadge);
      }
    }
    // 分类
    const catCell = document.createElement('td');
    catCell.className = 'col-source';
    if(item.mail_source === 'imported'){
      const badge = document.createElement('span');
      badge.className = 'mail-source-badge imported';
      badge.textContent = '自主导入';
      catCell.appendChild(badge);
    } else if(item.mail_source === 'registered'){
      const badge = document.createElement('span');
      badge.className = 'mail-source-badge registered';
      badge.textContent = '自主注册';
      catCell.appendChild(badge);
    } else {
      catCell.textContent = '—';
    }
    row.appendChild(catCell);
    // 状态
    const statusCell = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = `asset-status-badge ${item.status || 'unknown'}`;
    badge.textContent = ASSET_SCAN_STATUS_LABELS[item.status] || item.status || '未知';
    statusCell.appendChild(badge);
    row.appendChild(statusCell);
    const trialCell = document.createElement('td');
    if(item.platform === 'chatgpt'){
      const trial = document.createElement('span');
      trial.className = `asset-trial-badge ${item.plus_trial || 'unknown'}`;
      trial.textContent = ASSET_PLUS_TRIAL_LABELS[item.plus_trial] || '资格未知';
      trial.title = `${item.plus_trial_detail || '尚未检测'} · ${item.plus_trial_evidence || 'none'}`;
      trialCell.appendChild(trial);
    }else{
      trialCell.textContent = '--';
    }
    row.appendChild(trialCell);
    const detail = appendAssetScanCell(row, item.detail || '尚未扫描', 'asset-scan-detail');
    detail.title = `${item.evidence || 'none'} · ${item.source || ''}`;
    // Sub2API 导入状态
    const subCell = document.createElement('td');
    subCell.className = 'asset-sub-cell';
    const subBadge = document.createElement('span');
    if(item.sub2api_uploaded){
      subBadge.className = 'asset-sub-badge imported';
      subBadge.textContent = '已导入';
      subBadge.title = '已导入 Sub2API';
    } else {
      subBadge.className = 'asset-sub-badge not-imported';
      subBadge.textContent = '未导入';
      subBadge.title = '未导入 Sub2API';
    }
    subCell.appendChild(subBadge);
    row.appendChild(subCell);
    // 检测时间
    appendAssetScanCell(row, formatAssetScanTime(item.checked_at), 'asset-scan-checked');
    return row;
  }

  visible.forEach(item=>{
    const isParentWithKids = item.is_parent && (item.child_emails || []).length > 0;
    const row = createScanRow(item, false);
    body.appendChild(row);
    // 母邮箱行：紧接着插入子邮箱行（从完整数据中查找），并绑定展开/折叠
    if(isParentWithKids){
      const parentKey = (item.email || '').toLowerCase();
      const children = allChildMap[parentKey] || [];
      children.forEach(childItem=>{
        const childRow = createScanRow(childItem, true);
        body.appendChild(childRow);
      });
      row.addEventListener('click', ()=>{
        const expanded = row.classList.toggle('expanded');
        if(row._arrow) row._arrow.textContent = expanded ? '▼' : '▶';
        body.querySelectorAll('tr.asset-scan-row-child').forEach(childRow=>{
          if(childRow.dataset.parentEmail === parentKey){
            childRow.style.display = expanded ? '' : 'none';
          }
        });
      });
    }
  });
  $('#asset-scan-empty').style.display = visible.length ? 'none' : 'block';
  $('#asset-scan-result-count').textContent = `${items.length} 条`;
  $('#asset-scan-page').textContent = `${assetScanPage} / ${pages}`;
  $('#btn-scan-prev').disabled = assetScanPage <= 1;
  $('#btn-scan-next').disabled = assetScanPage >= pages;
  updateScanSelectAllState();
}

function renderAssetScan(data){
  assetScanData = data;
  const statuses = data.summary?.statuses || {};
  Object.keys(ASSET_SCAN_STATUS_LABELS).forEach(status=>{
    $(`#asset-status-${status}`).textContent = statuses[status] || 0;
  });
  const sub2apiCounts = data.sub2api_counts || {};
  $('#asset-status-sub_imported').textContent = sub2apiCounts.imported || 0;
  $('#asset-status-sub_not_imported').textContent = sub2apiCounts.not_imported || 0;
  const scan = data.scan || {};
  const progress = scan.progress || {};
  const completed = progress.completed || 0;
  const total = progress.total || 0;
  const bar = $('#asset-scan-progress-bar');
  bar.max = Math.max(1, total);
  bar.value = Math.min(completed, Math.max(1, total));
  $('#btn-scan-all').disabled = !!scan.running;
  $('#btn-scan-current').disabled = !!scan.running;
  $('#btn-scan-selected').disabled = !!scan.running || getCheckedScanEmails().length === 0;
  $('#asset-scan-concurrency').disabled = !!scan.running;
  if(scan.running){
    const current = progress.current ? ` · ${progress.current}` : '';
    $('#asset-scan-progress-text').textContent = `扫描中 ${completed}/${total}${current}`;
    $('#btn-scan-all').textContent = '扫描中…';
  }else if(scan.error){
    $('#asset-scan-progress-text').textContent = `扫描失败：${scan.error}`;
  }else if(data.last_scan_at){
    $('#asset-scan-progress-text').textContent = `扫描完成 ${statuses.normal || 0}/${data.summary?.total || 0} 正常`;
  }else{
    $('#asset-scan-progress-text').textContent = '尚未扫描';
  }
  if(!scan.running) $('#btn-scan-all').textContent = '一键扫描全部';
  $('#asset-scan-time').textContent = data.last_scan_at ? `更新于 ${formatAssetScanTime(data.last_scan_at)}` : '';
  renderAssetScanTable();
}

async function loadAssetScan(){
  if(assetScanTimer){ clearTimeout(assetScanTimer); assetScanTimer = null; }
  try{
    const response = await fetch('/api/assets/scan', {headers:assetHeaders()});
    const data = await readAssetResponse(response);
    renderAssetScan(data);
    if(data.scan?.running) assetScanTimer = setTimeout(loadAssetScan, 1000);
  }catch(error){
    setAssetMessage('#asset-scan-msg', error.message || String(error), false);
  }
}

async function startAssetScan(all=false){
  const platform = $('#asset-scan-platform').value;
  const platforms = all || platform === 'all' ? ['outlook','chatgpt','claude','grok','kiro'] : [platform];
  setAssetMessage('#asset-scan-msg', '正在启动扫描…');
  try{
    const response = await fetch('/api/assets/scan', {method:'POST', headers:assetHeaders(true), body:JSON.stringify({
      platforms,
      concurrency:parseInt($('#asset-scan-concurrency').value || '4', 10),
      timeout:15,
    })});
    await readAssetResponse(response);
    setAssetMessage('#asset-scan-msg', `已开始扫描 ${all || platform === 'all' ? '全部号池' : ASSET_SCAN_PLATFORM_LABELS[platforms[0]]}`, true);
    await loadAssetScan();
  }catch(error){
    setAssetMessage('#asset-scan-msg', error.message || String(error), false);
  }
}

async function startAssetScanSelected(){
  const emails = getCheckedScanEmails();
  if(!emails.length){
    setAssetMessage('#asset-scan-msg', '请先勾选要扫描的邮箱', false);
    return;
  }
  const platform = $('#asset-scan-platform').value;
  const platforms = platform === 'all' ? ['outlook','chatgpt','claude','grok'] : [platform];
  setAssetMessage('#asset-scan-msg', `正在启动选中扫描（${emails.length} 个）…`);
  try{
    const response = await fetch('/api/assets/scan', {method:'POST', headers:assetHeaders(true), body:JSON.stringify({
      platforms,
      concurrency:parseInt($('#asset-scan-concurrency').value || '4', 10),
      timeout:15,
      emails,
    })});
    await readAssetResponse(response);
    setAssetMessage('#asset-scan-msg', `已开始扫描选中的 ${emails.length} 个账号`, true);
    await loadAssetScan();
  }catch(error){
    setAssetMessage('#asset-scan-msg', error.message || String(error), false);
  }
}

async function refreshAssetSummary(quiet=false){
  if(!quiet) setAssetMessage('#asset-call-msg', '正在读取资产统计…');
  try{
    const response = await fetch('/api/assets/summary', {headers:assetHeaders()});
    renderAssetSummary(await readAssetResponse(response));
    if(!quiet) setAssetMessage('#asset-call-msg', '资产统计已更新', true);
  }catch(error){
    setAssetMessage('#asset-call-msg', error.message || String(error), false);
  }
}

async function loadAssetPanel(){
  $('#asset-api-base').value = window.location.origin;
  try{
    const data = await (await fetch('/api/env')).json();
    const item = (data.groups || []).flatMap(group=>group.items || []).find(entry=>entry.key === 'REG_FACTORY_ASSET_API_KEY');
    $('#asset-api-key').value = item?.value || '';
  }catch(error){
    setAssetMessage('#asset-key-msg', '读取配置失败', false);
  }
  updateAssetFormats();
  await Promise.all([refreshAssetSummary(), loadAssetScan()]);
}

async function saveAssetKey(){
  const button = $('#btn-save-asset-key');
  button.disabled = true;
  setAssetMessage('#asset-key-msg', '保存中…');
  try{
    const response = await fetch('/api/env', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({env:{REG_FACTORY_ASSET_API_KEY:$('#asset-api-key').value.trim()}})});
    const data = await readAssetResponse(response);
    setAssetMessage('#asset-key-msg', data.ok ? '已保存并立即生效' : '保存失败', !!data.ok);
    updateAssetRequestPreview();
    await Promise.all([refreshAssetSummary(), loadAssetScan()]);
  }catch(error){ setAssetMessage('#asset-key-msg', error.message || String(error), false); }
  finally{ button.disabled = false; }
}

async function callAssetApi(){
  const button = $('#btn-call-asset');
  button.disabled = true;
  setAssetMessage('#asset-call-msg', '正在在线校验并读取资产…');
  try{
    const request = buildAssetRequest();
    const response = await fetch(request.relative, {headers:assetHeaders()});
    const data = await readAssetResponse(response);
    $('#asset-response').textContent = JSON.stringify(data, null, 2);
    const progress = data.remaining === undefined ? '' : `，剩余 ${data.remaining} 个未领取正常账号`;
    setAssetMessage('#asset-call-msg', `在线校验通过并完成一次性领取${progress}`, true);
    await refreshAssetSummary(true);
  }catch(error){
    $('#asset-response').textContent = JSON.stringify({error:error.message || String(error)}, null, 2);
    setAssetMessage('#asset-call-msg', error.message || String(error), false);
  }finally{ button.disabled = false; }
}

async function resetAssetCursor(){
  const button = $('#btn-reset-asset');
  button.disabled = true;
  const request = buildAssetRequest();
  const scope = $('#asset-reset-scope').value === 'all'
    ? 'all'
    : (request.resource === 'emails' ? 'outlook' : request.resource);
  try{
    const response = await fetch('/api/assets/cursors/reset', {method:'POST', headers:assetHeaders(true), body:JSON.stringify({scope})});
    const data = await readAssetResponse(response);
    $('#asset-response').textContent = JSON.stringify(data, null, 2);
    setAssetMessage('#asset-call-msg', scope === 'all' ? '全部领取记录已重置' : '当前资产领取记录已重置', true);
  }catch(error){ setAssetMessage('#asset-call-msg', error.message || String(error), false); }
  finally{ button.disabled = false; }
}

$('#asset-resource').onchange = updateAssetFormats;
$('#asset-format').onchange = updateAssetRequestPreview;
$('#asset-index').oninput = updateAssetRequestPreview;
$('#asset-api-key').oninput = updateAssetRequestPreview;
$$('[data-asset-pick]').forEach(button=>button.onclick=()=>setAssetPickMode(button.dataset.assetPick));
$('#btn-refresh-assets').onclick = ()=>Promise.all([refreshAssetSummary(), loadAssetScan()]);
$('#btn-save-asset-key').onclick = saveAssetKey;
$('#btn-call-asset').onclick = callAssetApi;
$('#btn-reset-asset').onclick = resetAssetCursor;
$('#btn-copy-url').onclick = ()=>copyText($('#asset-request-url').textContent, $('#btn-copy-url'));
$('#btn-copy-curl').onclick = ()=>copyText($('#asset-curl-example').textContent, $('#btn-copy-curl'));
$('#btn-scan-all').onclick = ()=>startAssetScan(true);
$('#btn-scan-current').onclick = ()=>startAssetScan(false);
$('#btn-scan-selected').onclick = ()=>startAssetScanSelected();
$('#asset-scan-platform').onchange = ()=>{ assetScanPage = 1; renderAssetScanTable(); updateScanSelectAllState(); };
$('#asset-scan-status').onchange = ()=>{ assetScanPage = 1; renderAssetScanTable(); };
$$('[data-scan-status]').forEach(button=>button.onclick=()=>{
  $('#asset-scan-status').value = button.dataset.scanStatus;
  assetScanPage = 1;
  renderAssetScanTable();
});
$('#btn-scan-prev').onclick = ()=>{ assetScanPage -= 1; renderAssetScanTable(); };
$('#btn-scan-next').onclick = ()=>{ assetScanPage += 1; renderAssetScanTable(); };
$('#asset-scan-source').onchange = ()=>{ assetScanPage = 1; renderAssetScanTable(); };

// 扫描表格复选框 + 批量删除
function getCheckedScanEmails(){
  return Array.from(document.querySelectorAll('.asset-scan-check:checked'))
    .map(cb=>cb.dataset.email).filter(e=>e);
}

function updateScanSelectAllState(){
  const checks = document.querySelectorAll('.asset-scan-check');
  const checked = document.querySelectorAll('.asset-scan-check:checked');
  const selectAll = $('#asset-scan-check-all');
  if(selectAll) selectAll.checked = checks.length > 0 && checks.length === checked.length;
  const delBtn = $('#btn-delete-scan-mails');
  if(delBtn){
    delBtn.disabled = checked.length === 0;
    delBtn.textContent = checked.length > 0 ? `删除选中(${checked.length})` : '删除选中';
  }
  const scanBtn = $('#btn-scan-selected');
  if(scanBtn){
    const scanRunning = !!(assetScanData?.scan?.running);
    scanBtn.disabled = scanRunning || checked.length === 0;
    scanBtn.textContent = checked.length > 0 ? `扫描选中(${checked.length})` : '扫描选中';
  }
  const splitBtn = $('#btn-split-selected');
  if(splitBtn){
    splitBtn.disabled = checked.length === 0;
    splitBtn.textContent = checked.length > 0 ? `分裂选中(${checked.length})` : '分裂选中';
  }
  const omniBtn = $('#btn-omni-import');
  if(omniBtn){
    const curPlatform = $('#asset-scan-platform')?.value || '';
    const isGrok = curPlatform === 'grok';
    const label = isGrok ? '导入 Sub2API' : '导入 OmniRoute';
    omniBtn.disabled = checked.length === 0;
    omniBtn.textContent = checked.length > 0 ? `${label}(${checked.length})` : label;
  }
}

$('#asset-scan-check-all').onchange = function(){
  document.querySelectorAll('.asset-scan-check').forEach(cb=>cb.checked = this.checked);
  updateScanSelectAllState();
};

document.addEventListener('change', function(e){
  if(e.target.classList && e.target.classList.contains('asset-scan-check')){
    updateScanSelectAllState();
  }
});

$('#btn-delete-scan-mails').onclick = async ()=>{
  const emails = getCheckedScanEmails();
  if(!emails.length) return;
  const platform = $('#asset-scan-platform').value;
  const platformLabels = {outlook:'Outlook',chatgpt:'ChatGPT',claude:'Claude',grok:'Grok',kiro:'Kiro'};
  const plabel = platformLabels[platform] || platform;
  let confirmMsg;
  if(platform === 'outlook'){
    confirmMsg = `确认完全删除选中的 ${emails.length} 个邮箱？\n（将从邮箱池永久删除，不可撤销）`;
  } else {
    confirmMsg = `确认删除选中的 ${emails.length} 个邮箱在 ${plabel} 的凭据记录？\n（仅删除平台 cookie/token，邮箱本身不受影响）`;
  }
  if(!confirm(confirmMsg)) return;
  const btn = $('#btn-delete-scan-mails');
  btn.disabled = true;
  btn.textContent = '删除中…';
  try{
    const r = await (await fetch('/api/mailpool/delete',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({emails, platform})})).json();
    if(r.ok){
      if(r.mode === 'full'){
        setAssetMessage('#asset-scan-msg', `已删除 ${r.deleted} 个邮箱，剩余 ${r.total}`, true);
      } else {
        setAssetMessage('#asset-scan-msg', `已清理 ${plabel} 凭据：删除 ${r.deleted_files} 个文件，清理 ${r.cleaned_accounts} 条账号记录`, true);
      }
      await Promise.all([refreshAssetSummary(), loadAssetScan(), loadMailpool()]);
    } else {
      setAssetMessage('#asset-scan-msg', '删除失败: '+(r.msg||''), false);
    }
  }catch(e){
    setAssetMessage('#asset-scan-msg', '删除请求失败: '+e, false);
  }finally{
    btn.disabled = false;
    btn.textContent = '删除选中';
  }
};

// ---------------------------------------------------------------- 邮箱分裂（选中邮箱 → 每个母邮箱生成 N 个子邮箱）
$('#btn-split-selected').onclick = async ()=>{
  const emails = getCheckedScanEmails();
  if(!emails.length) return;
  const count = parseInt($('#asset-split-count').value, 10) || 1;
  if(!confirm(`确认为选中的 ${emails.length} 个邮箱各分裂出 ${count} 个子邮箱？\n（使用 Outlook Plus Addressing，继承母邮箱密码/token）`)) return;
  const btn = $('#btn-split-selected');
  btn.disabled = true;
  btn.textContent = '分裂中…';
  let totalGenerated = 0;
  let errors = [];
  for(const email of emails){
    try{
      const r = await (await fetch('/api/mailpool/split',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({parent_email: email, count})})).json();
      if(r.ok){
        totalGenerated += r.count;
      } else {
        errors.push(`${email}: ${r.msg||'未知错误'}`);
      }
    }catch(e){
      errors.push(`${email}: ${e}`);
    }
  }
  if(totalGenerated > 0){
    let msg = `成功生成 ${totalGenerated} 个子邮箱`;
    if(errors.length) msg += `；${errors.length} 个失败: ${errors.join('; ')}`;
    setAssetMessage('#asset-scan-msg', msg, errors.length === 0);
  } else {
    setAssetMessage('#asset-scan-msg', `分裂全部失败: ${errors.join('; ')}`, false);
  }
  btn.disabled = false;
  btn.textContent = '分裂选中';
  await Promise.all([refreshAssetSummary(), loadAssetScan(), loadMailpool()]);
};

// ---------------------------------------------------------------- 导入按钮：Grok→Sub2API / 其他→OmniRoute
$('#btn-omni-import').onclick = async ()=>{
  const emails = getCheckedScanEmails();
  if(!emails.length) return;
  const platform = $('#asset-scan-platform').value;
  const isGrok = platform === 'grok';
  const btn = $('#btn-omni-import');

  if(isGrok){
    // ── Grok → Sub2API 导入 ──
    if(!confirm(`确认将选中的 ${emails.length} 个 Grok 账号导入到 Sub2API？\n（需先在环境配置中填写 SUB2API_URL/EMAIL/PASSWORD）`)) return;
    btn.disabled = true;
    const origText = btn.textContent;
    btn.textContent = '导入中…';
    const toastEl = showPersistentToast('正在导入 Sub2API…', 'info');
    try{
      const r = await (await fetch('/api/assets/sub2api-import',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({emails, platform:'grok'})})).json();
      if(r.ok){
        let msg = r.msg;
        if(r.skipped && r.skipped.length > 0){
          msg += `（跳过 ${r.skipped.length} 个: ${r.skipped.join('; ')}）`;
        }
        updatePersistentToast(toastEl, msg, 'ok');
        setTimeout(()=>{ const e=toastEl; if(e){e.classList.add('removing');setTimeout(()=>e.remove(),200);} }, 5000);
      } else {
        let msg = r.msg || '导入失败';
        if(r.skipped && r.skipped.length > 0){
          msg += `（跳过: ${r.skipped.join('; ')}）`;
        }
        updatePersistentToast(toastEl, msg, 'bad');
      }
    }catch(e){
      updatePersistentToast(toastEl, '导入请求失败: '+e, 'bad');
    }finally{
      btn.disabled = false;
      btn.textContent = origText;
    }
  } else {
    // ── OmniRoute 导入（Kiro 等）──
    if(!confirm(`确认将选中的 ${emails.length} 个账号导入到 OmniRoute？\n（需先在环境配置中填写 OMNIROUTE_URL 和密码）`)) return;
    btn.disabled = true;
    const origText = btn.textContent;
    btn.textContent = '导入中…';
    const toastEl = showPersistentToast('正在导入 OmniRoute…', 'info');
    try{
      const r = await (await fetch('/api/kiro/omni-import',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({emails})})).json();
      if(r.ok){
        let msg = r.msg;
        if(r.skipped && r.skipped.length > 0){
          msg += `（跳过 ${r.skipped.length} 个: ${r.skipped.join('; ')}）`;
        }
        updatePersistentToast(toastEl, msg, 'ok');
        setTimeout(()=>{ const e=toastEl; if(e){e.classList.add('removing');setTimeout(()=>e.remove(),200);} }, 5000);
      } else {
        let msg = r.msg || '导入失败';
        if(r.skipped && r.skipped.length > 0){
          msg += `（跳过: ${r.skipped.join('; ')}）`;
        }
        updatePersistentToast(toastEl, msg, 'bad');
      }
    }catch(e){
      updatePersistentToast(toastEl, '导入请求失败: '+e, 'bad');
    }finally{
      btn.disabled = false;
      btn.textContent = origText;
    }
  }
};

// ---------------------------------------------------------------- Codex K12 集成通道
function renderK12Status(status){
  const alive = !!status.alive;
  k12Url = status.url || k12Url;
  $('#k12-open').href = k12Url;
  $('#dot-k12').classList.toggle('on', alive);
  $('#k12-nav-state').textContent = alive ? '在线' : (status.ready ? '可启动' : '未安装');
  $('#k12-nav-state').classList.toggle('on', alive);
  $('#k12-channel-status').textContent = alive ? '服务在线' : (status.message || '服务离线');
  $('#k12-channel-status').classList.toggle('on', alive);
  $('#k12-offline').style.display = alive ? 'none' : 'flex';
  $('#k12-offline-detail').textContent = status.message || 'Codex K12 服务未启动';
  $('#btn-k12-start').disabled = k12Starting || !status.ready;
  $('#btn-k12-start').textContent = k12Starting ? '启动中…' : '启动服务';
  if(alive && $('#k12-frame').src !== k12Url){
    $('#k12-frame').src = k12Url;
  }
}

async function fetchK12Status(){
  try{
    const status = await (await fetch('/api/k12/status')).json();
    renderK12Status(status);
    return status;
  }catch(e){
    const status = {alive:false, ready:false, url:k12Url, message:'主面板无法读取 K12 服务状态'};
    renderK12Status(status);
    return status;
  }
}

async function startK12Service(){
  if(k12Starting) return;
  k12Starting = true;
  renderK12Status({alive:false, ready:true, url:k12Url, message:'正在启动 Codex K12 服务'});
  try{
    const result = await (await fetch('/api/k12/start',{method:'POST'})).json();
    renderK12Status(result);
  }catch(e){
    renderK12Status({alive:false, ready:false, url:k12Url, message:'启动请求失败: '+e});
  }finally{
    k12Starting = false;
  }
}

async function openK12Channel(){
  const status = await fetchK12Status();
  if(!status.alive && status.ready) await startK12Service();
}

$('#btn-k12-start').onclick = startK12Service;
$('#btn-k12-retry').onclick = openK12Channel;

// ---------------------------------------------------------------- ChatGPT Plus 本地工作台
function setPlusImportState(text, kind=''){
  const state = $('#plus-import-state');
  if(!state) return;
  state.textContent = text;
  state.className = kind;
}

function renderPlusStatus(status){
  const alive = !!status.alive;
  $('#dot-plus').classList.remove('pending');
  $('#dot-plus').classList.toggle('on', alive);
  $('#plus-nav-state').textContent = alive ? '在线' : '离线';
  $('#plus-nav-state').classList.toggle('on', alive);
  if(status.url){
    plusUrl = status.url;
    $('#plus-open').href = plusUrl;
  }
}

async function fetchPlusStatus(){
  try{
    const status = await (await fetch('/api/chatgpt-plus/status')).json();
    renderPlusStatus(status);
    return status;
  }catch(e){
    renderPlusStatus({alive:false});
    return {alive:false};
  }
}

async function openPlusChannel(){
  let status = await fetchPlusStatus();
  if(!status.alive && status.ready){
    status = await (await fetch('/api/chatgpt-plus/start', {method:'POST'})).json();
    renderPlusStatus(status);
  }
  if(!status.alive) throw new Error(status.message || '本地 Plus 工作台当前不可用');
  if($('#plus-frame').getAttribute('src') === 'about:blank') $('#plus-frame').src = plusUrl;
  await waitForPlusFrame();
  setPlusImportState('本地工作台已就绪', 'ok');
  return status;
}

async function waitForPlus(test, timeoutMs, timeoutMessage){
  const deadline = Date.now() + timeoutMs;
  while(Date.now() < deadline){
    const result = test();
    if(result) return result;
    await new Promise(resolve=>setTimeout(resolve, 100));
  }
  throw new Error(timeoutMessage);
}

async function waitForPlusFrame(){
  const frame = $('#plus-frame');
  return waitForPlus(()=>{
    try{
      const doc = frame.contentDocument;
      return doc && doc.getElementById('accountImportInput') ? {frame, doc, win:frame.contentWindow} : null;
    }catch(e){ return null; }
  }, 20000, '本地 Plus 工作台加载超时');
}

async function importLocalPlusAts(){
  if(plusImporting) return;
  const button = $('#btn-import-ats');
  plusImporting = true;
  button.disabled = true;
  setPlusImportState('正在读取本地 free AT...');
  try{
    await openPlusChannel();
    const response = await fetch(`/api/chatgpt-plus/export-ats?limit=${PLUS_IMPORT_LIMIT}`, {cache:'no-store'});
    const data = await response.json();
    if(!response.ok) throw new Error(data.error || `AT 读取失败 (${response.status})`);
    if(!data.count) throw new Error('没有可导入的未过期 free AT');
    const context = await waitForPlusFrame();
    const importInput = context.doc.getElementById('accountImportInput');
    const importButton = context.doc.getElementById('importAtButton');
    if(!importInput || !importButton) throw new Error('本地工作台批量导入控件不可用');
    const before = context.doc.querySelectorAll('.account-row').length;
    importInput.value = data.ats.join('\n');
    importInput.dispatchEvent(new context.win.Event('input', {bubbles:true}));
    importButton.click();
    await waitForPlus(()=> importInput.value === '' ? true : null, 30000, 'AT 批量导入未完成');
    const total = context.doc.querySelectorAll('.account-row').length;
    const imported = Math.max(0, total - before);
    const selectAll = context.doc.getElementById('selectAllAccounts');
    if(selectAll && !selectAll.checked){
      selectAll.checked = true;
      selectAll.dispatchEvent(new context.win.Event('change', {bubbles:true}));
    }
    const concurrency = context.doc.getElementById('batchConcurrency');
    if(concurrency){
      const current = Number(concurrency.value) || PLUS_BATCH_SIZE;
      concurrency.value = String(Math.min(PLUS_BATCH_SIZE, Math.max(1, current)));
      concurrency.dispatchEvent(new context.win.Event('input', {bubbles:true}));
    }
    setPlusImportState(
      imported ? `已批量导入 ${imported} 条 AT，当前共 ${total} 个账号` : `这 ${data.count} 条 AT 已在工作台中`,
      'ok'
    );
  }catch(error){
    setPlusImportState(error.message || String(error), 'bad');
  }finally{
    plusImporting = false;
    button.disabled = false;
  }
}

async function copyLocalPlusAts(){
  const btn = $('#btn-copy-ats');
  btn.disabled = true;
  btn.textContent = '加载中...';
  try{
    const data = await (await fetch(`/api/chatgpt-plus/export-ats?limit=${PLUS_IMPORT_LIMIT}`, {cache:'no-store'})).json();
    if(!data.ats || !data.ats.length){
      btn.textContent = '无可用 AT';
      setTimeout(()=>{ btn.textContent = '复制本批 AT'; btn.disabled = false; }, 2000);
      return;
    }
    await navigator.clipboard.writeText(data.ats.join('\n'));
    btn.textContent = '已复制 ' + data.count + ' 条';
    setTimeout(()=>{ btn.textContent = '复制本批 AT'; btn.disabled = false; }, 2000);
  }catch(e){
    btn.textContent = '复制失败';
    setTimeout(()=>{ btn.textContent = '复制本批 AT'; btn.disabled = false; }, 2000);
  }
}

async function loadPlusAtCount(){
  try{
    const data = await (await fetch(`/api/chatgpt-plus/export-ats?limit=${PLUS_IMPORT_LIMIT}`, {cache:'no-store'})).json();
    $('#plus-at-count').textContent = `${data.available || 0} 个可用 · 可导入 ${data.count || 0}`;
    $('#btn-import-ats').disabled = Number(data.count || 0) === 0;
  }catch(e){
    $('#plus-at-count').textContent = '无法读取';
  }
}

$('#btn-copy-ats').onclick = copyLocalPlusAts;
$('#btn-import-ats').onclick = importLocalPlusAts;

// ---------------------------------------------------------------- 脚本导航
function appendNavGroup(nav, title, items, renderItem, open=false){
  const group = document.createElement('details');
  group.className = 'nav-group';
  group.open = open;
  const summary = document.createElement('summary');
  summary.innerHTML = `<span>${title}</span><span class="nav-count">${items.length}</span>`;
  group.appendChild(summary);
  const body = document.createElement('div');
  body.className = 'nav-group-items';
  items.forEach(item=>body.appendChild(renderItem(item)));
  group.appendChild(body);
  nav.appendChild(group);
}

async function loadScripts(){
  SCRIPTS = (await (await fetch('/api/scripts')).json()).scripts;
  const nav = $('#script-nav');
  const cats = {};
  SCRIPTS.forEach(s => (cats[s.category]=cats[s.category]||[]).push(s));
  nav.innerHTML = '';
  $('#task-count').textContent = SCRIPTS.length;

  let catIndex = 0;
  for(const cat of Object.keys(cats)){
    appendNavGroup(nav, cat, cats[cat], s=>{
      const b=document.createElement('button');
      b.className='scriptbtn'; b.textContent=s.title; b.dataset.id=s.id;
      b.onclick=()=>{ showView('run'); selectScript(s.id); };
      return b;
    }, catIndex++===0);
  }

  try{
    EMBEDS = (await (await fetch('/api/embeds')).json()).embeds || [];
    if(EMBEDS.length){
      appendNavGroup(nav, '辅助功能', EMBEDS, e=>{
        const b=document.createElement('button');
        b.className='scriptbtn'; b.textContent=e.title; b.dataset.embed=e.id;
        b.onclick=()=>openEmbed(e.id);
        return b;
      });
    }
  }catch(err){}

  try{
    const links = (await (await fetch('/api/links')).json()).links || [];
    if(links.length){
      appendNavGroup(nav, '外部工具', links, l=>{
        const a=document.createElement('a');
        a.className='scriptbtn linkbtn'; a.href=l.url; a.target='_blank'; a.rel='noopener';
        a.title=l.desc||l.url; a.textContent=l.title+' ↗';
        return a;
      });
    }
  }catch(e){}

  if(!curSrc && SCRIPTS.length) selectScript(SCRIPTS[0].id);
}

function selectScript(id){
  curSrc = SCRIPTS.find(s=>s.id===id);
  if(!curSrc) return;
  $$('.scriptbtn').forEach(b=>b.classList.toggle('active', b.dataset.id===id));
  const active = $(`.scriptbtn[data-id="${id}"]`);
  if(active?.closest('details')) active.closest('details').open = true;
  if(!evtSrc) setRunState('idle', '待运行');
  renderForm(curSrc);
}

// ---------------------------------------------------------------- 渲染表单
function renderArgField(a){
  const f = document.createElement('div');
  f.className='field';
  f.dataset.argFlag = a.flag;
  const label = a.flag.replace(/^--/,'');
  if(a.type==='bool'){
    f.className='field checkbox';
    f.innerHTML = `<input type="checkbox" id="f_${label}" ${a.default?'checked':''}>
      <label for="f_${label}"><span>${label}</span>${a.help?`<small>${a.help}</small>`:''}</label>`;
  }else if(a.type==='choice'){
    f.innerHTML = `<label for="f_${label}">${label}</label>
      <select id="f_${label}">${a.choices.map(c=>`<option value="${c}" ${c==a.default?'selected':''}>${(a.labels&&a.labels[c])||c}</option>`).join('')}</select>
      ${a.help?`<div class="fhelp">${a.help}</div>`:''}`;
  }else if(a.type==='multi'){
    const def = a.default||[];
    f.classList.add('field-wide');
    f.innerHTML = `<label>${label}</label>
      <div class="multi">${a.choices.map(c=>`<label><input type="checkbox" value="${c}" ${def.includes(c)?'checked':''} data-multi="${label}">${c}</label>`).join('')}</div>
      ${a.help?`<div class="fhelp">${a.help}</div>`:''}`;
  }else{
    const t = a.type==='int' ? 'number' : 'text';
    f.innerHTML = `<label for="f_${label}">${label}</label>
      <input type="${t}" id="f_${label}" value="${a.default!==undefined&&a.default!==''?a.default:''}" placeholder="${a.help||''}">
      ${a.help?`<div class="fhelp">${a.help}</div>`:''}`;
  }
  return f;
}

function renderForm(s){
  const p = $('#form-panel');
  p.innerHTML = '';
  const h = document.createElement('div');
  h.className = 'form-head';
  h.innerHTML = `<div><span class="form-eyebrow">${s.category}</span><h1 class="form-title">${s.title}</h1></div><p class="form-desc">${s.desc||''}</p>`;
  p.appendChild(h);
  if(s.warning){
    const warning = document.createElement('div');
    warning.className = 'form-warning';
    warning.setAttribute('role', 'alert');
    warning.textContent = s.warning;
    p.appendChild(warning);
  }

  const primaryArgs = s.args.slice(0, 6);
  const advancedArgs = s.args.slice(6);
  const grid = document.createElement('div'); grid.className='form-grid';
  primaryArgs.forEach(a=>grid.appendChild(renderArgField(a)));
  p.appendChild(grid);
  if(advancedArgs.length){
    const more = document.createElement('details'); more.className='advanced-fields';
    more.dataset.guide = 'advanced-task-options';
    more.innerHTML = `<summary><span>更多设置</span><span>${advancedArgs.length} 项</span></summary>`;
    const moreGrid = document.createElement('div'); moreGrid.className='form-grid';
    advancedArgs.forEach(a=>moreGrid.appendChild(renderArgField(a)));
    more.appendChild(moreGrid);
    p.appendChild(more);
  }

  const actions = document.createElement('div'); actions.className='form-actions';
  const btn = document.createElement('button');
  btn.className='btn-run btn-run-task'; btn.textContent='▶ 运行任务';
  btn.onclick = runScript;
  const cmd = document.createElement('div'); cmd.className='cmd-line'; cmd.id='cmd-preview';
  actions.appendChild(btn);
  actions.appendChild(cmd);
  p.appendChild(actions);
}

function collectArgs(s){
  const args = {};
  s.args.forEach(a=>{
    const label = a.flag.replace(/^--/,'');
    if(a.type==='bool'){
      args[a.flag] = $(`#f_${label}`).checked;
    }else if(a.type==='multi'){
      args[a.flag] = $$(`input[data-multi="${label}"]:checked`).map(x=>x.value);
    }else{
      const v = $(`#f_${label}`).value.trim();
      if(v!=='') args[a.flag] = a.type==='int' ? parseInt(v,10) : v;
    }
  });
  return args;
}

// ---------------------------------------------------------------- 运行 + SSE 日志
function setRunState(state, label){
  const el = $('#run-state');
  el.className = `run-state ${state}`;
  el.textContent = label;
}

async function runScript(){
  if(curRun && evtSrc){ evtSrc.close(); evtSrc = null; }
  const args = collectArgs(curSrc);
  const selectedPlatforms = args['--platforms'] || [];
  const plusRequested = !!args['--plus-subscription']
    && (curSrc.id === 'register_chatgpt' || selectedPlatforms.includes('chatgpt'));
  const log = $('#log'); log.textContent='';
  $('#log-title').textContent = `运行日志 — ${curSrc.title}`;
  setRunState('running', '运行中');
  let r;
  try{
    r = await (await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({script:curSrc.id, args})})).json();
  }catch(e){
    log.textContent='启动失败: '+e;
    setRunState('failed', '启动失败');
    return;
  }
  if(r.error){
    log.textContent='错误: '+r.error;
    setRunState('failed', '启动失败');
    return;
  }
  curRun = r.run_id;
  $('#cmd-preview').textContent = '$ '+r.cmd;
  $('#btn-stop').disabled = false;
  const stream = new EventSource(`/api/logs/${curRun}`);
  evtSrc = stream;
  stream.onmessage = e=>{ log.textContent += e.data+'\n'; log.scrollTop = log.scrollHeight; };
  stream.addEventListener('done', e=>{
    let result = {};
    try{ result = JSON.parse(e.data); }catch(err){}
    if(result.stopped) setRunState('stopped', '已停止');
    else if(result.returncode===0) setRunState('success', '已成功');
    else setRunState('failed', `失败 (${result.returncode ?? '?'})`);
    stream.close();
    if(evtSrc === stream){ evtSrc = null; curRun = null; }
    $('#btn-stop').disabled = true;
    pollStatus();
    if(result.returncode===0 && plusRequested){
      showView('plus').then(importLocalPlusAts).catch(error=>setPlusImportState(error.message, 'bad'));
    }
  });
  stream.onerror = ()=>{
    stream.close();
    if(evtSrc !== stream) return;
    evtSrc = null;
    curRun = null;
    $('#btn-stop').disabled = true;
    if($('#run-state').classList.contains('running')) setRunState('failed', '日志连接中断');
  };
}

$('#btn-stop').onclick = async ()=>{
  if(!curRun) return;
  setRunState('stopped', '停止中');
  await fetch(`/api/stop/${curRun}`,{method:'POST'});
  $('#btn-stop').disabled = true;
};

$('#btn-stop-all').onclick = async ()=>{
  if(!confirm('确定停止本项目的全部注册任务吗？')) return;
  const btn = $('#btn-stop-all');
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = '停止中';
  try{
    const response = await fetch('/api/stop-all',{method:'POST'});
    const result = await response.json();
    const log = $('#log');
    if(result.ok){
      setRunState('stopped', '已全部停止');
      log.textContent += `\n[webui] 已停止 ${result.stopped || 0} 个任务进程树`;
    }else{
      setRunState('failed', '部分停止失败');
      log.textContent += `\n[webui] 停止失败的 PID: ${(result.failed || []).join(', ')}`;
    }
    log.scrollTop = log.scrollHeight;
    $('#btn-stop').disabled = true;
    pollStatus();
  }catch(error){
    setRunState('failed', '停止失败');
    $('#log').textContent += `\n[webui] 停止全部失败: ${error}`;
  }finally{
    btn.disabled = false;
    btn.innerHTML = original;
  }
};

// ---------------------------------------------------------------- 配置页
function envControlValue(control){
  return control.type === 'checkbox' ? (control.checked ? 'true' : 'false') : control.value;
}

async function loadEnv(){
  const data = await (await fetch('/api/env')).json();
  const wrap = $('#env-groups'); wrap.innerHTML='';
  data.groups.forEach(g=>{
    const box = document.createElement('div'); box.className='env-group';
    const itemKeys = new Set((g.items||[]).map(item=>item.key));
    if(itemKeys.has('FINGERPRINT_BROWSER')) box.dataset.guideGroup = 'browser';
    else if(itemKeys.has('OUTLOOK_GRAPH_RECOVERY_EMAIL')) box.dataset.guideGroup = 'outlook-recovery';
    else if(itemKeys.has('CHATGPT_EMAIL_PROVIDER')) box.dataset.guideGroup = 'chatgpt-email';
    else if(itemKeys.has('TEMP_EMAIL_PROVIDER')) box.dataset.guideGroup = 'temp-email';
    else if(itemKeys.has('SMSMAN_TOKEN')) box.dataset.guideGroup = 'sms';
    const tests = (g.tests||[]).map(t=>
      `<button class="btn-test" data-test="${t.target}">${t.label}</button>`).join('');
    const notice = g.notice
      ? `<div class="env-notice ${g.notice_level==='warning'?'warning':''}" role="alert">${g.notice}</div>`
      : '';
    box.innerHTML = `<div class="env-group-title">
        <span>${g.group}</span>
        <span class="test-area">${tests}<span class="test-result" data-result-for="${g.group}"></span></span>
      </div>${notice}`;
    g.items.forEach(it=>{
      const row = document.createElement('div'); row.className='env-item';
      const type = it.secret ? 'password':'text';
      const value = it.value || it.default || '';
      let control;
      if(it.type === 'bool'){
        const checked = ['1','true','yes','on'].includes(String(value).toLowerCase());
        control = `<label class="env-checkbox">
          <input type="checkbox" data-env="${it.key}" ${checked?'checked':''}>
          <span>启用</span>
        </label>`;
      }else if(it.type === 'choice'){
        control = `<select data-env="${it.key}">${(it.choices||[]).map(c=>`<option value="${c}" ${c===value?'selected':''}>${c}</option>`).join('')}</select>`;
      }else{
        control = `<input type="${type}" data-env="${it.key}" value="${(it.value||'').replace(/"/g,'&quot;')}"
                 placeholder="${it.default? '默认 '+it.default : ''}">`;
      }
      row.innerHTML = `
        <div class="k">${it.key}${it.required?'<span class="req">*</span>':''}</div>
        <div class="v">
          ${control}
          ${it.help?`<div class="ehelp">${it.help}</div>`:''}
        </div>`;
      box.appendChild(row);
    });
    // 绑定该组的测试按钮
    box.querySelectorAll('.btn-test').forEach(btn=>{
      btn.onclick = ()=> runTest(btn.dataset.test, btn);
    });
    wrap.appendChild(box);
  });
}

// 连通测试：把当前页面所有 .env 输入(含未保存的)一起发过去，用最新值测
async function runTest(target, btn){
  const env = {};
  $$('input[data-env],select[data-env]').forEach(i=>{
    const value = envControlValue(i);
    if(value!=='') env[i.dataset.env]=value;
  });
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '测试中…';
  const res = btn.closest('.env-group').querySelector('.test-result');
  res.textContent=''; res.className='test-result';
  try{
    const r = await (await fetch(`/api/test/${target}`,{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({env})})).json();
    res.textContent = (r.ok?'✓ ':'✗ ') + r.msg;
    res.classList.add(r.ok?'ok':'bad');
  }catch(e){
    res.textContent = '✗ 测试请求失败: '+e; res.classList.add('bad');
  }finally{
    btn.disabled=false; btn.textContent=old;
  }
}

$('#btn-save-env').onclick = async ()=>{
  const env = {};
  $$('input[data-env],select[data-env]').forEach(i=>{ env[i.dataset.env] = envControlValue(i); });
  const r = await (await fetch('/api/env',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({env})})).json();
  const msg = $('#env-msg');
  msg.textContent = r.ok
    ? ('✓ 已保存 '+r.saved+' 项，新任务立即生效，无需重启')
    : ('保存失败: '+(r.error||''));
  setTimeout(()=>msg.textContent='', 5000);
};

// ---------------------------------------------------------------- 内嵌页 + 接码助手
function openEmbed(id){
  const e = EMBEDS.find(x=>x.id===id);
  if(!e) return;
  showView('embed');
  $$('.scriptbtn').forEach(b=>b.classList.toggle('active', b.dataset.embed===id));
  $('#embed-title').textContent = e.title;
  $('#embed-open').href = e.url;
  $('#embed-frame').src = e.url;
  const helper = $('#sms-helper');
  if(e.sms_helper){
    helper.style.display='block';
    if(e.sms_service_default) $('#sms-service').placeholder = e.sms_service_default;
    refreshRents();
  }else{
    helper.style.display='none';
  }
}

async function copyText(txt, btn){
  try{ await navigator.clipboard.writeText(txt); if(btn){const o=btn.textContent;btn.textContent='已复制';setTimeout(()=>btn.textContent=o,1200);} }
  catch(e){ alert('复制失败,请手动选择: '+txt); }
}

function fmtRemain(sec){
  sec=Math.max(0,sec); const m=Math.floor(sec/60), s=sec%60;
  return `${m}:${String(s).padStart(2,'0')}`;
}

async function refreshRents(){
  let data;
  try{ data = await (await fetch('/api/sms/rents')).json(); }catch(e){ return; }
  const wrap = $('#sms-rents'); wrap.innerHTML='';
  (data.rents||[]).forEach(r=>{
    const card = document.createElement('div'); card.className='rent-card';
    const codesHtml = r.codes.length
      ? r.codes.map(c=>`<span class="code-chip">${c}<button class="mini" data-copy="${c}">复制</button></span>`).join('')
      : '<span class="dim">暂无验证码</span>';
    card.innerHTML = `
      <div class="rent-phone">
        <b>+${r.phone}</b>
        <button class="mini" data-copy="${r.phone}">复制号码</button>
        <span class="multi-badge ${r.can_multi?'ok':'no'}">${r.can_multi?'多次接码':'单次'}</span>
        <span class="remain" data-remain="${r.remain}">剩 ${fmtRemain(r.remain)}</span>
      </div>
      <div class="rent-actions">
        <button class="btn-sm getcode" data-pkey="${r.pkey}">获取验证码</button>
        <button class="btn-sm release" data-pkey="${r.pkey}">完成/释放</button>
      </div>
      <div class="codes">${codesHtml}</div>
      <div class="sms-msg" data-msg="${r.pkey}"></div>`;
    wrap.appendChild(card);
  });
  // 绑定
  wrap.querySelectorAll('[data-copy]').forEach(b=> b.onclick=()=>copyText(b.dataset.copy,b));
  wrap.querySelectorAll('.getcode').forEach(b=> b.onclick=()=>getCode(b.dataset.pkey,b));
  wrap.querySelectorAll('.release').forEach(b=> b.onclick=()=>releaseNum(b.dataset.pkey));
  // 倒计时滴答
  if(smsTimer) clearInterval(smsTimer);
  if((data.rents||[]).length){
    smsTimer = setInterval(()=>{
      $$('.remain').forEach(el=>{
        let r=parseInt(el.dataset.remain,10)-1; el.dataset.remain=r;
        el.textContent = r>0 ? '剩 '+fmtRemain(r) : '已过期';
      });
    },1000);
  }
}

$('#btn-rent').onclick = async ()=>{
  const btn=$('#btn-rent'); const o=btn.textContent; btn.disabled=true; btn.textContent='租号中…';
  const body={ service: $('#sms-service').value.trim()||undefined, country: $('#sms-country').value.trim()||undefined,
    prefer_multi: $('#sms-prefer-multi') ? $('#sms-prefer-multi').checked : true };
  try{
    const r = await (await fetch('/api/sms/rent',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(!r.ok){ alert('获取号码失败: '+r.msg); }
    await refreshRents();
  }finally{ btn.disabled=false; btn.textContent=o; }
};

async function getCode(pkey, btn){
  const o=btn.textContent; btn.disabled=true; btn.textContent='等待验证码…';
  const msg = document.querySelector(`[data-msg="${pkey}"]`);
  if(msg) msg.textContent='';
  try{
    const r = await (await fetch('/api/sms/code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pkey})})).json();
    if(r.ok){ await refreshRents(); }
    else if(msg){ msg.textContent = (r.expired?'⏰ ':'') + r.msg; }
  }finally{ btn.disabled=false; btn.textContent=o; }
}

async function releaseNum(pkey){
  await fetch('/api/sms/release',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pkey})});
  await refreshRents();
}

// ---------------------------------------------------------------- 新手指南
const GUIDE_STORAGE_KEY = 'reg-factory-guide-v2';
const GUIDE_STEPS = [
  {
    id:'welcome', section:'开始', title:'先认识工作台', target:'.brand', placement:'bottom',
    body:`<p>这套指南会带你完成一次运行前检查。教程只会切换页面和展开表单，不会自动保存配置，也不会启动注册任务。</p>
      <p>任何阶段都可以跳过。以后可从顶栏的“新手指南”重新打开。</p>`,
  },
  {
    id:'library', section:'界面', title:'任务和工具都在左侧', target:'[data-guide="task-library"]', placement:'right',
    prepare:async()=>{ if(innerWidth<=900) setNavOpen(true); },
    body:`<p>上半部分是可运行任务，下半部分是邮箱池、网络出口、环境配置和资产管理。</p>
      <p>首次使用先完成网络、浏览器和接码配置，再选择任务。</p>`,
  },
  {
    id:'network', section:'网络', title:'先打开网络出口', target:'[data-guide="network-entry"]', placement:'right',
    prepare:async()=>{ await showView('network'); if(innerWidth<=900) setNavOpen(true); },
    skipLabel:'跳过网络配置', skipTo:'env-overview',
    body:`<p>网络出口决定注册页面看到的公网 IP。新手建议先用 Clash 自动轮换，稳定住宅代理也可以单独配置。</p>
      <p>不同平台可使用不同出口，避免一个平台的设置影响全部任务。</p>`,
  },
  {
    id:'clash', section:'网络', title:'Clash API 配置方法', target:'[data-guide="clash-config"]', placement:'left',
    prepare:async()=>{ setNavOpen(false); await ensureGuideView('network'); previewGuideClashFields(); },
    skipLabel:'跳过网络配置', skipTo:'env-overview',
    body:`<ol class="tour-list">
        <li>打开 Clash Verge，进入“设置”中的 Clash 设置或内核设置，找到“外部控制器”或 External Controller。</li>
        <li>启用外部控制器，并填写监听地址 <code>127.0.0.1:9097</code>。面板中的控制器地址填写 <code>http://127.0.0.1:9097</code>。mihomo 常见端口是 <code>9090</code>。</li>
        <li>在同一页填写控制密码。不同版本名称可能是 Secret、Controller Secret 或 API Secret。将完全相同的内容填入面板的“控制器密钥”。</li>
        <li>保存 Clash 设置后应用或重载内核。若 Clash 未设置控制密码，面板中的控制器密钥也必须留空。</li>
        <li>找到 mixed-port 或混合端口，通常为 <code>7897</code>。面板中的混合代理地址填写 <code>http://127.0.0.1:7897</code>。</li>
        <li>全局模式的代理组通常填 <code>GLOBAL</code>，规则模式填写实际节点选择组名。</li>
      </ol>
      <p class="tour-note">外部控制器应只监听 <code>127.0.0.1</code>，不要配置为公网地址。教程临时切换到自动轮换用于展示，当前配置尚未保存。</p>`,
  },
  {
    id:'residential', section:'网络', title:'住宅代理填写方法', target:'#network-residential-fields', placement:'left',
    prepare:async()=>{ setNavOpen(false); await ensureGuideView('network'); previewGuideProxyMode('residential'); },
    skipLabel:'跳过网络配置', skipTo:'env-overview',
    body:`<p>选择“动态住宅 IP”后，单个代理填写完整地址，例如 <code>http://user:pass@host:port</code> 或 <code>socks5://user:pass@host:port</code>。</p>
      <p>有多个出口时使用代理池，每行一个。程序会在创建新的浏览器 profile 时从池中选择代理。</p>
      <p>供应商提供换 IP 接口时填写接口地址和请求方法。轮换后新的浏览器窗口才会使用新 IP。</p>
      <p class="tour-note">教程临时切换到住宅代理表单用于展示，当前配置尚未保存。</p>`,
  },
  {
    id:'platform-proxy', section:'网络', title:'平台出口覆盖和继承全局', target:'[data-guide="platform-proxy-overrides"]', placement:'top',
    prepare:async()=>{ setNavOpen(false); await ensureGuideView('network'); },
    skipLabel:'跳过网络配置', skipTo:'env-overview',
    body:`<p>每个平台默认选择“继承全局”，表示直接使用页面顶部选择的全局网络模式和配置。</p>
      <p>只有某个平台需要单独出口时才改为 Clash 自动、Clash 固定或动态住宅 IP。例如 Outlook 用 Clash，ChatGPT 用住宅代理。</p>
      <p>下方“轮换或测试目标”选择具体平台后，轮换和 IP 测试只作用于该平台。没有特殊需求时全部保持继承全局。</p>`,
  },
  {
    id:'network-test', section:'网络', title:'保存后先测试出口', target:'[data-guide="network-actions"]', placement:'top',
    prepare:async()=>{ setNavOpen(false); await ensureGuideView('network'); },
    skipLabel:'跳过网络配置', skipTo:'env-overview',
    body:`<p>填写完成后先点“应用并测试 IP”。成功时会显示出口 IP 和当前节点。</p>
      <p>测试失败时不要直接跑任务，先检查 Clash 是否启动、端口是否正确、Secret 是否一致，以及代理组是否能切换节点。</p>`,
  },
  {
    id:'env-overview', section:'环境配置', title:'环境配置按功能分组', target:'#view-env .env-head', placement:'bottom',
    prepare:async()=>{ setNavOpen(false); restoreGuideProxyMode(); await showView('env'); },
    skipLabel:'跳过环境配置', skipTo:'chatgpt-email',
    body:`<p>环境配置会保存到本机 <code>.env</code>，新任务立即读取，无需重启服务。</p>
      <ul class="tour-list">
        <li>指纹浏览器决定 Chrome、内置浏览器或 BitBrowser 的启动方式。</li>
        <li>Outlook 提取 Graph RT 时必须启用辅助邮箱，并配置可接收 Microsoft 验证码的 provider。</li>
        <li>邮箱和临时邮箱组用于收取网页验证码。</li>
        <li>短信接码和打码平台只在需要手机号或验证码挑战时配置。</li>
        <li>SUB2API、CPA、chatgpt2api 等组只在注册后自动导入时配置。</li>
      </ul>
      <p>只填写本次任务会用到的组。所有填写完成后点击这里的“保存配置”。</p>`,
  },
  {
    id:'browser', section:'浏览器', title:'选择浏览器方式', target:'[data-guide-group="browser"]', placement:'left',
    prepare:async()=>ensureGuideView('env'),
    skipLabel:'跳过浏览器配置', skipTo:'outlook-recovery',
    body:`<p><strong>ruyipage</strong> 是默认值，使用 Firefox WebDriver BiDi 指纹内核。首次使用先在任务库运行“安装 RuyiPage Firefox”，再点本组连通测试。</p>
      <p><strong>bundled</strong> 使用程序自带或系统可用的 Chromium；Claude、Grok 和 Outlook 等仍依赖 Chromium CDP 的旧流程会自动回退到它。</p>
      <p><strong>custom</strong> 使用普通 Chrome，填写 <code>CUSTOM_BROWSER_PATH</code>。Windows 常见路径是 <code>C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe</code>。</p>
      <p><strong>bitbrowser</strong> 需要先启动比特浏览器，并确认本地 API 通常为 <code>http://127.0.0.1:54345</code>。填写后使用本组的连通测试。</p>`,
  },
  {
    id:'outlook-recovery', section:'Outlook', title:'提取 RT 前配置辅助邮箱', target:'[data-guide-group="outlook-recovery"]', placement:'left',
    prepare:async()=>ensureGuideView('env'),
    skipLabel:'跳过 Outlook 辅助邮箱', skipTo:'env-test',
    body:`<p>需要提取 Graph refresh token 时，保持“启用”复选框勾选。Microsoft 出现 <code>proofs/Add</code> 安全信息页后，程序会自动绑定辅助邮箱并接收验证码。</p>
      <p>临时邮箱可选择 <code>yyds</code> 或 <code>custom</code>。使用自有 Outlook 辅助邮箱时选择 <code>outlook</code>，并填写 <code>email----password----refresh_token----client_id</code> 四段凭据。</p>
      <p>填写自有 Outlook 邮箱后，先点击组标题右侧的“验证 Outlook 辅助邮箱 RT”。验证通过再保存配置；取消勾选后不能保证成功提取 RT。</p>`,
  },
  {
    id:'env-test', section:'环境配置', title:'每组都可以一键测试', target:()=>$('[data-guide-group="browser"] .btn-test'), placement:'left',
    prepare:async()=>ensureGuideView('env'),
    skipLabel:'跳过环境配置', skipTo:'asset-scan',
    body:`<p>组标题右侧的测试按钮会使用当前表单中的值发起连通检查，即使还没有保存也可以先测试。</p>
      <p>浏览器组用于测试本地浏览器 API，临时邮箱和短信组用于测试接码服务。测试通过后再点击页面顶部“保存配置”。</p>
      <p>连通测试只确认本机配置和服务接口可用，不代表目标网站一定允许注册，注册前仍应测试网络出口。</p>`,
  },
  {
    id:'asset-scan', section:'资产 API', title:'先看号池状态', target:'[data-guide="asset-scan"]', placement:'bottom',
    prepare:async()=>{ setNavOpen(false); await showView('assets'); },
    skipLabel:'跳过资产 API', skipTo:'chatgpt-email',
    body:`<p>资产 API 用于向本地程序或下游服务读取邮箱、Cookie 和登录凭据。先在号池状态区选择类型，再运行“扫描当前类型”或“一键扫描全部”。</p>
      <p>扫描会将资产分为正常、待解锁、封禁、过期、受限、凭据异常和检测异常。网络错误或目标站风控会显示为受限或检测异常，不会误判为封禁。</p>
      <p>正常 ChatGPT 账号会额外标注 Plus 免费试用资格。资格接口失败只显示“资格未知”，不会改变账号健康状态。</p>
      <p>API 不会输出未验证、封禁、过期、受限或凭据异常资产。</p>`,
  },
  {
    id:'asset-call', section:'资产 API', title:'每次输出前都会在线校验', target:'[data-guide="asset-call"]', placement:'top',
    prepare:async()=>ensureGuideView('assets'),
    skipLabel:'跳过资产 API', skipTo:'chatgpt-email',
    body:`<p>选择资产、输出格式和取用方式后，点击“在线校验并调用 API”。顺序取用和指定 index 都只面向未领取的健康账号。</p>
      <p>接口会先扫描对应平台，只返回状态为“正常”的记录，并立即写入领取账本。同一平台账号即使切换输出格式也不会再次返回；只有手动重置领取记录后才可重新领取。</p>
      <p>响应中的 <code>verification</code> 是本次检测依据，<code>remaining</code> 是该平台剩余未领取正常账号数。</p>
      <p>这代表账号在本次检测时可用，不代表后续不会被目标服务限制。读取地址和 API key 仅应提供给受信任的本地服务。</p>`,
  },
  {
    id:'chatgpt-email', section:'邮箱接码', title:'ChatGPT 邮箱接码', target:'[data-guide-group="chatgpt-email"]', placement:'left',
    prepare:async()=>ensureGuideView('env'),
    skipLabel:'跳过邮箱接码', skipTo:'sms',
    body:`<p>使用 Outlook 邮箱池时选择 <code>pool</code>，并先在左侧“邮箱池”导入邮箱、密码、refresh token 和 client id。</p>
      <p>使用 iCloud API 时选择 <code>icloud</code>，填写 API 地址和 API key。推荐类型为 <code>icloud-code</code>，服务名为 <code>openai</code>。</p>
      <p>API key 只保存在本机 <code>.env</code>。</p>`,
  },
  {
    id:'temp-email', section:'邮箱接码', title:'Claude 和 Grok 临时邮箱', target:'[data-guide-group="temp-email"]', placement:'left',
    prepare:async()=>ensureGuideView('env'),
    skipLabel:'跳过邮箱接码', skipTo:'sms',
    body:`<p>按任务选择 YYDS、GPTMail、MoeMail、CFMail 或自定义 provider，并填写对应 API key。</p>
      <p>配置后使用组标题右侧的连通测试。测试通过只代表邮箱 API 可用，注册页面是否放行仍取决于网络出口。</p>`,
  },
  {
    id:'sms', section:'短信接码', title:'只有需要手机号时才配置', target:'[data-guide-group="sms"]', placement:'left',
    prepare:async()=>ensureGuideView('env'),
    skipLabel:'跳过短信接码', skipTo:'task',
    body:`<p>Codex OAuth 的 add-phone、Gmail 注册等流程需要短信接码。常用配置是 <code>SMSMAN_TOKEN</code> 和 OpenAI 服务 ID。</p>
      <p>只注册普通 ChatGPT、Claude 或 Grok 网页账号时，可以跳过本项。填写后先运行本组连通测试。</p>`,
  },
  {
    id:'task', section:'运行任务', title:'新手从端到端全流程开始', target:'.scriptbtn[data-id="run_full_flow"]', placement:'right',
    prepare:async()=>{
      setNavOpen(false);
      await showView('run');
      selectScript('run_full_flow');
      if(innerWidth<=900) setNavOpen(true);
    },
    body:`<p>端到端全流程会先准备邮箱，再注册你选中的平台。第一次建议只选一个平台并只跑一轮，确认链路正常后再批量运行。</p>
      <p>已有邮箱时也可以使用“三平台注册”或单平台任务。</p>`,
  },
  {
    id:'platforms', section:'运行任务', title:'先选择要注册的平台', target:'[data-arg-flag="--platforms"]', placement:'right',
    prepare:async()=>{
      setNavOpen(false);
      await ensureGuideView('run');
      selectScript('run_full_flow');
    },
    body:`<p>在 platforms 中勾选目标平台。首次测试只保留一个选项，rounds 保持为 1。</p>
      <p>需要直接使用现成邮箱时，展开更多设置并勾选 skip-email，再填写 email 和 password。</p>`,
  },
  {
    id:'task-options', section:'运行任务', title:'按目标勾选附加功能', target:'[data-guide="advanced-task-options"]', placement:'right',
    prepare:async()=>{
      await ensureGuideView('run');
      selectScript('run_full_flow');
      const advanced = $('.advanced-fields');
      if(advanced) advanced.open = true;
    },
    body:`<ul class="tour-list">
        <li>ChatGPT 需要导入 SUB2API 时勾选 codex。</li>
        <li>Grok 需要导入 SUB2API 时勾选 grok-sub2api。</li>
        <li>ChatGPT 需要导入 chatgpt2api 时勾选 import-c2a。</li>
        <li>首次配置建议先勾选 dry-run，只预览命令，不执行注册。</li>
      </ul>
      <p>未使用的附加功能不要勾选，对应的 API 配置也可以跳过。</p>`,
  },
  {
    id:'run', section:'运行任务', title:'确认后启动任务', target:'.form-actions', placement:'top',
    prepare:async()=>{ await ensureGuideView('run'); selectScript('run_full_flow'); },
    body:`<p>确认平台、轮数和附加功能后点击“运行任务”。按钮右侧会显示实际命令，便于检查参数。</p>
      <p>教程不会替你点击运行。正式运行前建议先完成一次 dry-run。</p>`,
  },
  {
    id:'logs', section:'完成', title:'从日志判断结果', target:'.log-panel', placement:'left',
    prepare:async()=>ensureGuideView('run'),
    body:`<p>运行日志会持续显示当前步骤。顶部状态为“已成功”才代表任务正常结束，失败时保留最后一段日志用于排查。</p>
      <p>现在已经完成基础配置路线。以后可随时从顶栏重新打开本指南。</p>`,
  },
];

function guideStorageCompleted(){
  try{ return localStorage.getItem(GUIDE_STORAGE_KEY)==='complete'; }
  catch(_error){ return false; }
}

function saveGuideCompleted(){
  try{ localStorage.setItem(GUIDE_STORAGE_KEY, 'complete'); }
  catch(_error){}
}

function guideViewVisible(view){
  const element = $(`#view-${view}`);
  return !!element && getComputedStyle(element).display!=='none';
}

function previewGuideProxyMode(mode){
  if(guideOriginalProxyMode===null) guideOriginalProxyMode = proxyMode;
  setProxyMode(mode);
}

function previewGuideClashFields(){
  previewGuideProxyMode('clash_auto');
}

function restoreGuideProxyMode(){
  if(guideOriginalProxyMode===null) return;
  setProxyMode(guideOriginalProxyMode);
  guideOriginalProxyMode = null;
}

async function ensureGuideView(view){
  if(!guideViewVisible(view)) await showView(view);
}

async function waitForGuideTarget(selector, timeout=4000){
  const deadline = Date.now()+timeout;
  while(Date.now()<deadline){
    const element = typeof selector==='function' ? selector() : $(selector);
    if(element && element.getClientRects().length) return element;
    await new Promise(resolve=>setTimeout(resolve, 80));
  }
  return $('.content') || document.body;
}

function setGuideRect(element, left, top, width, height){
  element.style.left = `${Math.max(0,left)}px`;
  element.style.top = `${Math.max(0,top)}px`;
  element.style.width = `${Math.max(0,width)}px`;
  element.style.height = `${Math.max(0,height)}px`;
}

function positionGuide(){
  if(!guideActive || !guideTarget || !guideTarget.isConnected) return;
  const viewportWidth = innerWidth;
  const viewportHeight = innerHeight;
  const padding = 7;
  const raw = guideTarget.getBoundingClientRect();
  const left = Math.max(6, raw.left-padding);
  const top = Math.max(6, raw.top-padding);
  const right = Math.min(viewportWidth-6, raw.right+padding);
  const bottom = Math.min(viewportHeight-6, raw.bottom+padding);
  const width = Math.max(0, right-left);
  const height = Math.max(0, bottom-top);
  const spotlight = $('#tour-spotlight');
  setGuideRect(spotlight, left, top, width, height);
  setGuideRect($('#tour-curtain-top'), 0, 0, viewportWidth, top);
  setGuideRect($('#tour-curtain-left'), 0, top, left, height);
  setGuideRect($('#tour-curtain-right'), right, top, viewportWidth-right, height);
  setGuideRect($('#tour-curtain-bottom'), 0, bottom, viewportWidth, viewportHeight-bottom);

  const popover = $('#tour-popover');
  const margin = 12;
  const gap = 14;
  popover.style.width = `${Math.min(390, viewportWidth-margin*2)}px`;
  const popWidth = popover.offsetWidth;
  const popHeight = popover.offsetHeight;
  const spaces = {
    right:viewportWidth-right,
    left:left,
    bottom:viewportHeight-bottom,
    top:top,
  };
  const preferred = GUIDE_STEPS[guideIndex].placement || 'right';
  let side = preferred;
  const needed = (side==='left' || side==='right' ? popWidth : popHeight)+gap+margin;
  if(spaces[side]<needed){
    side = Object.keys(spaces).sort((a,b)=>spaces[b]-spaces[a])[0];
  }
  let popLeft = margin;
  let popTop = margin;
  if(side==='right'){
    popLeft = right+gap;
    popTop = raw.top;
  }else if(side==='left'){
    popLeft = left-popWidth-gap;
    popTop = raw.top;
  }else if(side==='bottom'){
    popLeft = raw.left+(raw.width-popWidth)/2;
    popTop = bottom+gap;
  }else{
    popLeft = raw.left+(raw.width-popWidth)/2;
    popTop = top-popHeight-gap;
  }
  popLeft = Math.max(margin, Math.min(viewportWidth-popWidth-margin, popLeft));
  popTop = Math.max(margin, Math.min(viewportHeight-popHeight-margin, popTop));
  popover.style.left = `${popLeft}px`;
  popover.style.top = `${popTop}px`;
}

function scheduleGuidePosition(){
  if(!guideActive || guidePositionFrame) return;
  guidePositionFrame = requestAnimationFrame(()=>{
    guidePositionFrame = null;
    positionGuide();
  });
}

async function renderGuideStep(index){
  if(!guideActive) return;
  const renderToken = ++guideRenderToken;
  guideIndex = Math.max(0, Math.min(GUIDE_STEPS.length-1, index));
  const step = GUIDE_STEPS[guideIndex];
  if(step.prepare) await step.prepare();
  if(!guideActive || renderToken!==guideRenderToken) return;
  if(guideTarget) guideTarget.classList.remove('tour-target-active');
  guideTarget = await waitForGuideTarget(step.target);
  if(!guideActive || renderToken!==guideRenderToken) return;
  guideTarget.classList.add('tour-target-active');
  guideTarget.scrollIntoView({block:'center', inline:'nearest'});
  $('#tour-section').textContent = step.section;
  $('#tour-title').textContent = step.title;
  $('#tour-body').innerHTML = step.body;
  $('#tour-progress-label').textContent = `${guideIndex+1} / ${GUIDE_STEPS.length}`;
  $('#tour-progress-bar').style.width = `${((guideIndex+1)/GUIDE_STEPS.length)*100}%`;
  $('#btn-tour-prev').disabled = guideIndex===0;
  $('#btn-tour-next').textContent = guideIndex===GUIDE_STEPS.length-1 ? '完成指南' : '下一步';
  const skipSection = $('#btn-tour-skip-section');
  skipSection.hidden = !step.skipTo;
  skipSection.textContent = step.skipLabel || '跳过此配置';
  skipSection.dataset.skipTo = step.skipTo || '';
  await new Promise(resolve=>requestAnimationFrame(resolve));
  positionGuide();
  $('#btn-tour-next').focus({preventScroll:true});
}

async function startGuide(index=0){
  if(scriptsReady){
    try{ await scriptsReady; }catch(_error){}
  }
  guideActive = true;
  document.body.classList.add('tour-open');
  const layer = $('#tour-layer');
  layer.hidden = false;
  layer.setAttribute('aria-hidden', 'false');
  await renderGuideStep(index);
}

function finishGuide(){
  guideActive = false;
  guideRenderToken++;
  saveGuideCompleted();
  if(guideTarget) guideTarget.classList.remove('tour-target-active');
  guideTarget = null;
  restoreGuideProxyMode();
  setNavOpen(false);
  document.body.classList.remove('tour-open');
  const layer = $('#tour-layer');
  layer.hidden = true;
  layer.setAttribute('aria-hidden', 'true');
  $('#btn-guide').focus({preventScroll:true});
}

let guideRenderToken = 0;
$('#btn-guide').onclick = ()=>startGuide(0);
$('#btn-tour-close').onclick = finishGuide;
$('#btn-tour-exit').onclick = finishGuide;
$('#btn-tour-prev').onclick = ()=>renderGuideStep(guideIndex-1);
$('#btn-tour-next').onclick = ()=>{
  if(guideIndex>=GUIDE_STEPS.length-1) finishGuide();
  else renderGuideStep(guideIndex+1);
};
$('#btn-tour-skip-section').onclick = event=>{
  const targetId = event.currentTarget.dataset.skipTo;
  const targetIndex = GUIDE_STEPS.findIndex(step=>step.id===targetId);
  renderGuideStep(targetIndex>=0 ? targetIndex : guideIndex+1);
};
window.addEventListener('resize', scheduleGuidePosition);
document.addEventListener('scroll', scheduleGuidePosition, true);
document.addEventListener('keydown', event=>{
  if(guideActive && event.key==='Escape') finishGuide();
});

// ---------------------------------------------------------------- 邮箱池
async function loadMailpool(){
  try{
    const d = await (await fetch('/api/mailpool')).json();
    $('#mailpool-total').textContent = `当前池中 ${d.total} 个邮箱`;
  }catch(e){}
}

$('#btn-import-mail').onclick = async ()=>{
  const text = $('#mailpool-input').value;
  if(!text.trim()){ $('#mailpool-msg').textContent='请先粘贴邮箱'; return; }
  const btn=$('#btn-import-mail'); const o=btn.textContent; btn.disabled=true; btn.textContent='导入中…';
  const msg=$('#mailpool-msg'); msg.textContent='';
  try{
    const r = await (await fetch('/api/mailpool',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text})})).json();
    if(r.ok){
      let m = `✓ 导入 ${r.added}，跳过重复 ${r.skipped}`;
      if(r.bad) m += `，格式错误 ${r.bad}`;
      m += `，池中共 ${r.total}`;
      msg.textContent = m;
      if(r.bad && r.bad_samples.length) msg.textContent += `（错误样例：${r.bad_samples[0]}…）`;
      $('#mailpool-total').textContent = `当前池中 ${r.total} 个邮箱`;
      if(r.added){
        $('#mailpool-input').value='';
      }
    }else{ msg.textContent='导入失败: '+(r.msg||''); }
  }catch(e){ msg.textContent='导入请求失败: '+e; }
  finally{ btn.disabled=false; btn.textContent=o; }
};

// ---------------------------------------------------------------- 启动
scriptsReady = loadScripts();
scriptsReady.then(()=>{
  if(!guideStorageCompleted()) setTimeout(()=>startGuide(0), 500);
}).catch(()=>{});
pollStatus();
