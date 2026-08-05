(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  if (window.location.pathname.startsWith('/chatgpt-plus')) document.body.classList.add('embedded');
  const WORKBENCH_PREFS_KEY = 'direct-bind:workbench-prefs:v1';
  const ACCOUNT_LIST_KEY = 'direct-bind:account-list:v2';
  const CARD_DECLINE_MESSAGE = '绑卡未完成：发卡行拒绝了卡片，账号没有新增支付方式。';
  const TASK_POLL_MS = 400;
  const MAX_BATCH_CONCURRENCY = 27;
  const PREFLIGHT_TIMEOUT_MS = 60000;
  const MAX_PROXY_FALLBACKS = 4;
  const ACCOUNT_SAVE_DEBOUNCE_MS = 120;
  const RANDOM_BILLING_FIXTURES = [
    { line1: '1100 Congress Ave', city: 'Austin', state: 'TX', postal_code: '78701' },
    { line1: '200 S Grand Ave', city: 'Los Angeles', state: 'CA', postal_code: '90012' },
    { line1: '1 N Dearborn St', city: 'Chicago', state: 'IL', postal_code: '60601' },
    { line1: '350 5th Ave', city: 'New York', state: 'NY', postal_code: '10018' },
    { line1: '100 Peachtree St NW', city: 'Atlanta', state: 'GA', postal_code: '30303' },
    { line1: '500 S Tryon St', city: 'Charlotte', state: 'NC', postal_code: '28202' },
  ];
  const RANDOM_BILLING_NAMES = [
    'Alex Morgan',
    'Jordan Blake',
    'Taylor Reed',
    'Casey Parker',
    'Riley Bennett',
    'Avery Collins',
  ];
  const API_PREFIX = window.location.pathname.startsWith('/chatgpt-plus')
    ? '/api/chatgpt-plus/workbench'
    : '/api';
  const apiUrl = (path) => `${API_PREFIX}${String(path || '').startsWith('/') ? path : `/${path}`}`;
  let accountSaveTimer = 0;
  let accountRenderFrame = 0;
  const state = {
    mounted: false,
    mounting: false,
    mountedAccountSignature: '',
    runtimeReady: false,
    runtimeRouteNote: '',
    running: false,
    stop: false,
    accounts: [],
    nextId: 1,
    bindProxyCursor: 0,
    promoProxyCursor: 0,
    generatedBilling: null,
    manualBatchWaiting: false,
    manualBatchResolver: null,
  };

  // Open-source cleanup migration: discard drafts created by earlier private
  // builds, which may contain access tokens, proxy credentials and task state.
  try {
    for (let index = localStorage.length - 1; index >= 0; index -= 1) {
      const key = localStorage.key(index) || '';
      if (key === 'direct-bind:proxy-draft:v5') localStorage.removeItem(key);
      else if (key.startsWith('direct-bind:') && ![WORKBENCH_PREFS_KEY, ACCOUNT_LIST_KEY].includes(key)) {
        localStorage.removeItem(key);
      }
    }
  } catch (_) {}

  function splitValues(value) {
    return [...new Set(String(value || '').split(/[\r\n,;]+/).map((item) => item.trim()).filter(Boolean))];
  }

  function parseProxyPool(value) {
    const source = String(value || '').trim();
    if (!source) return [];
    let values = [];
    if (source.startsWith('[')) {
      try {
        const parsed = JSON.parse(source);
        if (Array.isArray(parsed)) values = parsed;
      } catch (_) {}
    }
    if (!values.length) values = source.split(/[\s,;，；]+/);
    return [...new Set(values
      .map((item) => String(item || '').trim().replace(/^["']|["']$/g, ''))
      .filter(Boolean))].slice(0, 500);
  }

  function extractTokens(text) {
    const source = String(text || '');
    const isImportable = (token) => {
      const decoded = decodeAccount(token);
      return Boolean(decoded.accountId || decoded.userId || decoded.email);
    };
    const namedAccessTokens = [...source.matchAll(
      /["']access_token["']\s*:\s*["'](eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*)["']/gi,
    )].map((match) => match[1]);
    if (namedAccessTokens.length) return [...new Set(namedAccessTokens.filter(isImportable))];
    const jwtMatches = source.match(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*/g);
    const candidates = jwtMatches?.length
      ? [...new Set(jwtMatches)]
      : splitValues(source).map((item) => item.replace(/^["']|["']$/g, '')).filter(Boolean);
    return candidates.filter(isImportable);
  }

  function decodeAccount(token) {
    try {
      const part = String(token).split('.')[1];
      if (!part) return {};
      const raw = part.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(part.length / 4) * 4, '=');
      const claims = JSON.parse(decodeURIComponent(Array.from(atob(raw), (ch) => `%${ch.charCodeAt(0).toString(16).padStart(2, '0')}`).join('')));
      const profile = claims['https://api.openai.com/profile'] || {};
      const auth = claims['https://api.openai.com/auth'] || {};
      return {
        email: String(profile.email || claims.email || ''),
        accountId: String(auth.chatgpt_account_id || auth.account_id || claims.chatgpt_account_id || claims.account_id || ''),
        userId: String(auth.user_id || claims.user_id || claims.sub || ''),
      };
    } catch (_) {
      return {};
    }
  }

  function maskToken(value) {
    const text = String(value || '');
    return text.length > 24 ? `${text.slice(0, 12)}…${text.slice(-8)}` : text;
  }

  async function copyText(value) {
    const text = String(value || '').trim();
    if (!text) return false;
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      const field = document.createElement('textarea');
      field.value = text;
      field.setAttribute('readonly', '');
      field.style.position = 'fixed';
      field.style.opacity = '0';
      document.body.appendChild(field);
      field.select();
      const copied = document.execCommand('copy');
      field.remove();
      return copied;
    }
  }

  function canonicalCheckoutLink(value = {}, fallback = '') {
    const direct = String(value.checkout_link || value.checkoutLink || value.link || fallback || '').trim();
    if (/^https:\/\/chatgpt\.com\/checkout\/[A-Za-z0-9_-]+\/(?:oaics_|cs_)[A-Za-z0-9_-]+$/.test(direct)) {
      return direct;
    }
    const checkoutId = String(value.checkout_id || value.checkoutId || '').trim();
    const processor = String(value.processor_entity || value.processorEntity || '').trim();
    if (/^(?:oaics_|cs_)[A-Za-z0-9_-]+$/.test(checkoutId) && /^[A-Za-z0-9_-]+$/.test(processor)) {
      return `https://chatgpt.com/checkout/${processor}/${checkoutId}`;
    }
    return '';
  }

  function restoredAccountStage(item = {}) {
    const stage = String(item.stage || '待执行')
      .replace(/^注册指纹已就绪$/, '环境已就绪');
    if (/^(待会话确认：复制 Checkout 链接后在该账号当前会话继续|订阅尚未提交：当前任务缺少该 Checkout 的会话数据)$/.test(stage)) {
      return canonicalCheckoutLink(item, item.link)
        ? '订阅待确认：使用当前行的 Checkout 链接继续'
        : '订阅待确认：旧任务未保存 Checkout 链接，请重新执行“提链 + 支付”';
    }
    return stage;
  }

  function csvCell(value) {
    return `"${String(value ?? '').replace(/"/g, '""')}"`;
  }

  function exportAccountsCsv() {
    if (!state.accounts.length) return false;
    const rows = [
      ['账号', '账号ID', '状态', '当前步骤', 'Checkout链接', '错误'],
      ...state.accounts.map((account) => [
        account.email || '',
        account.accountId || '',
        accountStatusLabel(account),
        account.stage || '',
        canonicalCheckoutLink(account, account.link),
        account.error || '',
      ]),
    ];
    const csv = `\uFEFF${rows.map((row) => row.map(csvCell).join(',')).join('\r\n')}`;
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const download = document.createElement('a');
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    download.href = url;
    download.download = `账号任务-${stamp}.csv`;
    document.body.appendChild(download);
    download.click();
    download.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    return true;
  }

  function translateError(value) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    const lower = text.toLowerCase();
    if (!text) return '任务执行失败。';
    if (lower.includes('curl: (35)') || lower.includes('tls connect error') || lower.includes('openssl_internal:invalid library')) {
      return '代理 TLS 握手失败，请检查代理协议、地址、端口和有效期。';
    }
    if (lower.includes('impersonating') && lower.includes('is not supported')) {
      return '当前浏览器 TLS 指纹版本不受运行库支持，系统已自动切换兼容版本，请重试。';
    }
    if (lower.includes('card_declined') || lower.includes('your card was declined')) {
      return CARD_DECLINE_MESSAGE;
    }
    if (lower.includes('secret_key_required')) {
      return '支付接口权限不匹配，当前步骤没有获得所需的服务端权限。';
    }
    if (lower.includes('invalidated') || lower.includes('token expired') || lower.includes('at invalid') || lower.includes('http 401')) {
      return '账号凭证已失效，请删除后重新导入。';
    }
    if (lower.includes('at does not contain an account id') || lower.includes('at 缺少账号 id')) {
      return '该条不是可用的 AT（缺少账号 ID），请导入 access_token。';
    }
    if (lower.includes('零元校验失败') || lower.includes('expected zero amount') || lower.includes('checkout 返回非零金额')) {
      return '该账号当前未命中 Plus 免费优惠，Checkout 返回非零金额；已跳过，未进行绑卡或扣款。';
    }
    if (lower.includes('missing or invalid paymentmethod') || lower.includes('paymentmethod 创建失败')) {
      return '卡片支付方式创建失败，请重新填写卡片。';
    }
    if (lower.includes('preflight bind proxy pool exhausted')) {
      return '预检失败：绑卡/支付代理池候选均连接失败，请检查节点状态、协议和有效期。';
    }
    if (lower.includes('提链代理池已耗尽')) {
      return '预检失败：提链代理池候选均连接失败，请检查节点状态、协议和有效期。';
    }
    if (lower.includes('proxy') && (lower.includes('connect') || lower.includes('failed') || lower.includes('error'))) {
      return '代理连接失败，请检查代理格式、节点状态和网络。';
    }
    if (lower.includes('timeout') || lower.includes('timed out') || lower.includes('任务超时')) {
      return '请求超时，请检查代理速度后重试。';
    }
    if (lower.includes('checkout confirm precondition')) {
      return '订阅尚未提交：缺少当前 Checkout 会话生成的动态校验数据。';
    }
    if (lower.includes('checkout confirm') && lower.includes('server returned blocked')) {
      return '订阅请求已提交，账号风控返回 status=blocked；订阅未生效，未进入 Stripe Intent，自动重试已关闭。';
    }
    if (lower.includes('checkout confirm') && lower.includes('http 403')) {
      return '提交支付确认被拒绝（HTTP 403）：当前 Checkout 的会话校验数据无效或缺失。';
    }
    if (lower.includes('confirmation token') && lower.includes('http 403')) {
      return 'Stripe Confirmation Token 请求被拒绝（HTTP 403），请检查支付方式与商户公钥是否匹配。';
    }
    if (lower.includes('http 403')) return '请求被拒绝（HTTP 403），请检查账号状态和接口权限。';
    if (lower.includes('http 402')) return '支付请求被拒绝（HTTP 402），请检查卡片状态。';
    if (lower.includes('failed to perform, curl')) return '网络请求失败，请检查代理和本机网络。';
    if (/[\u4e00-\u9fff]/.test(text)) return text;
    return `任务失败：${text.slice(0, 220)}`;
  }

  function normalizedBilling(value = {}, email = '') {
    return {
      name: String(value.name || '').trim(),
      line1: String(value.line1 || '').trim(),
      line2: String(value.line2 || '').trim(),
      city: String(value.city || '').trim(),
      state: String(value.state || '').trim().toUpperCase(),
      postal_code: String(value.postal_code || '').trim().toUpperCase(),
      country: String(value.country || 'US').trim().toUpperCase(),
      email: String(email || value.email || '').trim(),
    };
  }

  function billingIssues(billing) {
    const issues = [];
    if (!billing.name) issues.push('持卡人姓名');
    if (!billing.line1) issues.push('账单地址');
    if (!billing.city) issues.push('城市');
    if (!/^[A-Z]{2}$/.test(billing.country)) issues.push('两位国家代码');
    if (!billing.postal_code) issues.push('邮编');
    if (billing.country === 'US') {
      if (!billing.state) issues.push('州代码');
      if (!/^\d{5}(?:-\d{4})?$/.test(billing.postal_code)) issues.push('美国 ZIP 格式');
    }
    return issues;
  }

  function cardFromForm(required = true) {
    const number = String($('cardNumberInput').value || '').replace(/\D/g, '');
    const expiry = String($('cardExpiryInput').value || '').trim().match(/^(\d{1,2})\D*(\d{2}|\d{4})$/);
    const cvc = String($('cardCvcInput').value || '').replace(/\D/g, '');
    const month = expiry ? expiry[1].padStart(2, '0') : '';
    let year = expiry ? expiry[2] : '';
    if (year.length === 2) year = `20${year}`;
    const valid = number.length >= 12 && number.length <= 19
      && Number(month) >= 1 && Number(month) <= 12
      && /^\d{4}$/.test(year)
      && /^\d{3,4}$/.test(cvc);
    if (!valid) {
      if (required) throw new Error('卡片信息不完整，请检查卡号、有效期和 CVC');
      return null;
    }
    return { number, exp_month: month, exp_year: year, cvc };
  }

  function parsePastedCard() {
    const source = String($('cardPasteInput').value || '').trim();
    const groups = source.match(/\d+/g) || [];
    let cursor = 0;
    let number = '';
    while (cursor < groups.length && number.length < 12) number += groups[cursor++];
    const rest = groups.slice(cursor);
    if (number.length < 12 || number.length > 19) throw new Error('没有识别到卡号');
    if (rest.length < 3) throw new Error('没有识别到有效期或 CVC');
    $('cardNumberInput').value = number.replace(/(.{4})/g, '$1 ').trim();
    $('cardExpiryInput').value = `${rest[0].padStart(2, '0')}/${rest[1]}`;
    $('cardCvcInput').value = rest[2];
    $('cardPasteInput').value = '';
    cardFromForm(true);
    refresh();
  }

  function isStoredCardDecline(value) {
    const text = String(value || '').toLowerCase();
    return text.includes('card_declined')
      || text.includes('your card was declined')
      || text.includes('银行卡被拒绝')
      || text.includes('发卡行拒绝');
  }

  function randomBillingAddress(email = '') {
    const fixture = RANDOM_BILLING_FIXTURES[Math.floor(Math.random() * RANDOM_BILLING_FIXTURES.length)];
    const name = RANDOM_BILLING_NAMES[Math.floor(Math.random() * RANDOM_BILLING_NAMES.length)];
    return normalizedBilling({ ...fixture, name, country: 'US' }, email);
  }

  function fillBillingForm(billing) {
    $('billingName').value = billing.name;
    $('billingLine1').value = billing.line1;
    $('billingCity').value = billing.city;
    $('billingState').value = billing.state;
    $('billingPostalCode').value = billing.postal_code;
    $('billingCountry').value = billing.country;
  }

  function applyGeneratedBilling(accounts) {
    if (!state.generatedBilling || !accounts.length) return;
    accounts.forEach((account) => {
      account.billing = normalizedBilling(state.generatedBilling, account.email);
      account.billingMode = 'random';
    });
  }

  function generateBillingAddress({ quiet = false } = {}) {
    const accounts = selectedAccounts();
    state.generatedBilling = randomBillingAddress(accounts[0]?.email || '');
    fillBillingForm(state.generatedBilling);
    applyGeneratedBilling(accounts);
    $('billingAutoStatus').className = 'billing-auto-status is-test';
    $('billingAutoStatus').textContent = accounts.length
      ? `已自动生成并应用到 ${accounts.length} 个账号；需要时可重新生成。`
      : '已自动生成随机账单地址；选择账号后会自动应用。';
    if (!quiet) {
      renderAccounts();
      refresh();
      appendLog(accounts.length
        ? `已为 ${accounts.length} 个账号生成随机账单地址。`
        : '已生成随机账单地址，等待选择账号。');
    }
    return state.generatedBilling;
  }

  function setStep(id, status) {
    const node = $(id);
    if (node) node.className = status || '';
  }

  function log(message) {
    void message;
  }

  function appendLog(message) {
    void message;
  }

  function hideBatchSummary() {
    const node = $('batchSummary');
    node.hidden = true;
    node.className = 'batch-summary';
    node.textContent = '';
  }

  function showBatchSummary(label, success, failed, stopped = false, pending = 0) {
    const node = $('batchSummary');
    node.hidden = false;
    node.className = `batch-summary ${stopped || pending ? 'is-stopped' : failed ? '' : 'is-success'}`;
    node.textContent = stopped
      ? `${label}已停止：${success} 个成功，${failed} 个失败或未执行。`
      : pending
        ? `${label}已完成 HTTP 阶段：${success} 个成功，${pending} 个尚未提交，${failed} 个失败。`
        : `${label}完成：${success} 个成功，${failed} 个失败。`;
  }

  function releaseManualBatch() {
    const resolve = state.manualBatchResolver;
    state.manualBatchResolver = null;
    state.manualBatchWaiting = false;
    if ($('nextBatchButton')) $('nextBatchButton').disabled = true;
    if (resolve) resolve();
  }

  function setBatchRotationStatus(text, stateName = '') {
    const node = $('batchRotationStatus');
    if (!node) return;
    node.textContent = String(text || '');
    node.className = `batch-rotation-status ${stateName}`.trim();
  }

  function waitForNextBatch(completedBatch, totalBatches) {
    state.manualBatchWaiting = true;
    $('nextBatchButton').disabled = false;
    const text = `第 ${completedBatch} 批完成，共 ${totalBatches} 批；点击“执行下一批”继续`;
    $('progressText').textContent = text;
    setBatchRotationStatus(text, 'is-active');
    return new Promise((resolve) => {
      state.manualBatchResolver = resolve;
    });
  }

  function saveWorkbenchPrefs() {
    try {
      localStorage.setItem(WORKBENCH_PREFS_KEY, JSON.stringify({
        concurrency: $('batchConcurrency').value,
        mode: $('batchMode').value,
      }));
    } catch (_) {}
  }

  function loadWorkbenchPrefs() {
    try {
      const prefs = JSON.parse(localStorage.getItem(WORKBENCH_PREFS_KEY) || '{}');
      const concurrency = Math.max(1, Math.min(MAX_BATCH_CONCURRENCY, Number(prefs.concurrency) || MAX_BATCH_CONCURRENCY));
      $('batchConcurrency').value = String(concurrency);
      if (prefs.mode === 'manual' || prefs.mode === 'auto') $('batchMode').value = prefs.mode;
    } catch (_) {}
  }

  function saveAccountsNow() {
    if (accountSaveTimer) {
      window.clearTimeout(accountSaveTimer);
      accountSaveTimer = 0;
    }
    try {
      localStorage.setItem(ACCOUNT_LIST_KEY, JSON.stringify({
        nextId: state.nextId,
        items: state.accounts.map((account) => ({
          id: account.id,
          token: account.token,
          email: account.email || '',
          accountId: account.accountId || '',
          userId: account.userId || '',
          selected: Boolean(account.selected),
          status: account.status || 'idle',
          stage: account.stage || '待执行',
          error: account.error || '',
          link: canonicalCheckoutLink(account, account.link),
          checkoutId: account.checkoutId || '',
          processorEntity: account.processorEntity || '',
          lastTaskId: account.lastTaskId || '',
          taskId: account.taskId || '',
          bindProxyLabel: account.bindProxyLabel || '',
          promoProxyLabel: account.promoProxyLabel || '',
          fingerprint: account.fingerprint || '',
          fingerprintId: account.fingerprintId || '',
          fingerprintTls: account.fingerprintTls || '',
          fingerprintRegion: account.fingerprintRegion || '',
          fingerprintTimezone: account.fingerprintTimezone || '',
          steps: Array.isArray(account.steps) ? account.steps : [],
          billingMode: '',
          billing: null,
        })),
      }));
    } catch (_) {}
  }

  function saveAccounts() {
    if (accountSaveTimer) window.clearTimeout(accountSaveTimer);
    accountSaveTimer = window.setTimeout(saveAccountsNow, ACCOUNT_SAVE_DEBOUNCE_MS);
  }

  function scheduleRenderAccounts() {
    if (accountRenderFrame) return;
    accountRenderFrame = window.requestAnimationFrame(() => {
      accountRenderFrame = 0;
      renderAccounts();
    });
  }

  function loadAccounts() {
    try {
      const saved = JSON.parse(localStorage.getItem(ACCOUNT_LIST_KEY) || '{}');
      const items = Array.isArray(saved.items) ? saved.items : [];
      state.accounts = items
        .filter((item) => item && typeof item.token === 'string' && item.token.trim())
        .map((item) => ({ item, decoded: decodeAccount(item.token.trim()) }))
        .filter(({ decoded }) => Boolean(decoded.accountId || decoded.userId || decoded.email))
        .map(({ item, decoded }, index) => ({
          id: String(item.id || `account-${index + 1}`),
          token: item.token.trim(),
          email: String(decoded.email || item.email || ''),
          accountId: String(decoded.accountId || item.accountId || ''),
          userId: String(decoded.userId || item.userId || ''),
          selected: item.selected !== false,
          status: ['idle', 'running', 'success', 'error', 'action_required'].includes(item.status) ? item.status : 'idle',
          stage: isStoredCardDecline(item.error) ? '绑卡失败' : restoredAccountStage(item),
          error: isStoredCardDecline(item.error)
            ? CARD_DECLINE_MESSAGE
            : item.error ? translateError(item.error) : '',
          checkoutId: String(item.checkoutId || ''),
          processorEntity: String(item.processorEntity || ''),
          link: canonicalCheckoutLink(item, item.link),
          lastTaskId: String(item.lastTaskId || ''),
          taskId: String(item.taskId || ''),
          bindProxyLabel: String(item.bindProxyLabel || ''),
          promoProxyLabel: String(item.promoProxyLabel || ''),
          fingerprint: String(item.fingerprint || ''),
          fingerprintId: String(item.fingerprintId || ''),
          fingerprintTls: String(item.fingerprintTls || ''),
          fingerprintRegion: String(item.fingerprintRegion || ''),
          fingerprintTimezone: String(item.fingerprintTimezone || ''),
          steps: Array.isArray(item.steps) ? item.steps.map((step) => ({
            key: String(step?.key || ''),
            label: String(step?.label || ''),
            status: ['pending', 'running', 'done', 'error'].includes(step?.status) ? step.status : 'pending',
            detail: step?.status === 'error' && step?.detail
              ? translateError(step.detail)
              : String(step?.detail || ''),
          })) : [],
          billingMode: '',
          billing: null,
        }));
      state.nextId = Math.max(Number(saved.nextId) || 1, state.accounts.length + 1);
      if (state.accounts.length !== items.length) saveAccountsNow();
    } catch (_) {
      state.accounts = [];
      state.nextId = 1;
    }
  }

  function selectedAccounts() {
    return state.accounts.filter((account) => account.selected);
  }

  function isBatchTerminalAccount(account) {
    return ['success', 'action_required'].includes(String(account?.status || ''));
  }

  function runnableBatchAccounts(accounts = state.accounts) {
    return accounts.filter((account) => account.selected && !isBatchTerminalAccount(account));
  }

  function accountEnvironmentSignature(accounts = selectedAccounts()) {
    return accounts
      .map((account) => `${account.id}:${account.accountId || ''}`)
      .sort()
      .join('|');
  }

  function accountEnvironmentReady(accounts = selectedAccounts()) {
    const signature = accountEnvironmentSignature(accounts);
    return Boolean(accounts.length && state.mounted && signature && state.mountedAccountSignature === signature);
  }

  function maskProxy(value) {
    const text = String(value || '').trim();
    if (!text) return '';
    try {
      const normalized = text.includes('://') ? text : `http://${text}`;
      const url = new URL(normalized);
      return `${url.protocol}//${url.username ? '***@' : ''}${url.hostname}${url.port ? `:${url.port}` : ''}`;
    } catch (_) {
      const parts = text.split(':');
      return parts.length >= 2 ? `${parts[0]}:${parts[1]}` : '代理节点';
    }
  }

  function initialAccountSteps(mode) {
    const definitions = [
      ['proxy', '分配代理'],
    ];
    if (mode !== 'link_only') definitions.push(['card_method', '生成支付方式']);
    definitions.push(['fingerprint', '加载指纹'], ['first_link', '首次提链']);
    if (mode === 'bind_only') definitions.push(['bind', '绑卡']);
    else if (mode === 'link_pay') definitions.push(
      ['payment_prepare', '验证支付方式'],
      ['second_link', '刷新支付链接'],
      ['payment', '提交支付'],
      ['verify', '结果校验'],
    );
    else if (mode !== 'link_only') definitions.push(
      ['bind', '绑卡'],
      ['second_link', '二次提链'],
      ['payment', '提交支付'],
      ['verify', '结果校验'],
    );
    return definitions.map(([key, label]) => ({
      key,
      label,
      status: key === 'proxy' ? 'done' : 'pending',
      detail: key === 'proxy' ? (mode === 'link_only' ? '提链代理已分配' : '双代理已分配') : '等待执行',
    }));
  }

  function setAccountStep(account, key, status, detail) {
    const steps = Array.isArray(account.steps) ? account.steps : [];
    let step = steps.find((item) => item.key === key);
    if (!step) {
      step = { key, label: key, status: 'pending', detail: '等待执行' };
      steps.push(step);
    }
    step.status = status;
    step.detail = detail || step.detail;
    account.steps = steps;
  }

  function failRunningAccountStep(account, detail) {
    const steps = Array.isArray(account.steps) ? account.steps : [];
    const running = [...steps].reverse().find((step) => step.status === 'running');
    if (running) {
      running.status = 'error';
      running.detail = detail;
    }
  }

  function assignProxies(accounts, bindPool, promoPool, mode) {
    const bindStart = bindPool.length ? state.bindProxyCursor % bindPool.length : 0;
    const promoStart = promoPool.length ? state.promoProxyCursor % promoPool.length : 0;
    accounts.forEach((account, index) => {
      account.bindProxy = bindPool.length ? bindPool[(bindStart + index) % bindPool.length] : '';
      account.promoProxy = promoPool.length ? promoPool[(promoStart + index) % promoPool.length] : '';
      account.bindProxyPool = bindPool.length
        ? bindPool.map((_, offset) => bindPool[(bindStart + index + offset) % bindPool.length]).slice(0, MAX_PROXY_FALLBACKS)
        : [];
      account.promoProxyPool = promoPool.length
        ? promoPool.map((_, offset) => promoPool[(promoStart + index + offset) % promoPool.length]).slice(0, MAX_PROXY_FALLBACKS)
        : [];
      account.bindProxyLabel = maskProxy(account.bindProxy);
      account.promoProxyLabel = maskProxy(account.promoProxy);
      account.status = 'running';
      account.stage = '主程序网络出口已就绪，等待并发启动';
      account.error = '';
      account.steps = initialAccountSteps(mode);
    });
    if (bindPool.length) state.bindProxyCursor = (bindStart + accounts.length) % bindPool.length;
    if (promoPool.length) state.promoProxyCursor = (promoStart + accounts.length) % promoPool.length;
    renderAccounts();
    saveAccounts();
  }

  function accountStatusLabel(account) {
    if (account.status === 'success') return '完成';
    if (account.status === 'error') return '失败';
    if (account.status === 'action_required') return '待提交';
    if (account.status === 'running') return '运行中';
    return '待执行';
  }

  function renderAccounts() {
    if (accountRenderFrame) {
      window.cancelAnimationFrame(accountRenderFrame);
      accountRenderFrame = 0;
    }
    const root = $('taskRows');
    root.innerHTML = '';
    $('accountEmpty').hidden = state.accounts.length > 0;

    state.accounts.forEach((account, index) => {
      const row = document.createElement('div');
      row.className = `task-row account-row ${account.status === 'error' ? 'is-error' : ''}`;
      row.dataset.accountId = account.id;

      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.className = 'account-select';
      checkbox.checked = account.selected;
      checkbox.disabled = state.running || state.mounting;
      checkbox.addEventListener('change', () => {
        account.selected = checkbox.checked;
        refresh();
        renderAccounts();
        void mountCard();
      });

      const meta = document.createElement('div');
      meta.className = 'task-row__meta account-row__summary';
      const accountName = document.createElement('div');
      accountName.className = 'task-row__account';
      accountName.textContent = account.email || `账号 ${String(index + 1).padStart(2, '0')}`;
      accountName.title = maskToken(account.token);
      const stage = document.createElement('div');
      stage.className = `task-row__stage account-row__current-step ${account.error ? 'is-error' : ''}`;
      const stageLabel = account.stage || '任务失败';
      const errorText = String(account.error || '');
      stage.textContent = errorText
        ? errorText.startsWith(stageLabel) ? errorText : `${stageLabel}：${errorText}`
        : account.stage || '待执行';
      const diagnostics = [
        `AT：${maskToken(account.token)}`,
        account.bindProxyLabel ? `绑卡/支付代理：${account.bindProxyLabel}` : '',
        account.promoProxyLabel ? `提链代理：${account.promoProxyLabel}` : '',
        account.fingerprint ? `指纹：${account.fingerprint}` : '',
        account.billing ? `账单：${account.billing.city || ''}, ${account.billing.state || ''} ${account.billing.postal_code || ''}` : '',
      ].filter(Boolean);
      stage.title = diagnostics.join('\n');
      meta.append(accountName, stage);
      const checkoutLink = canonicalCheckoutLink(account, account.link);
      if (checkoutLink) {
        account.link = checkoutLink;
        const link = document.createElement('button');
        link.type = 'button';
        link.className = 'task-row__link';
        link.textContent = '复制链接';
        link.title = checkoutLink;
        link.addEventListener('click', async () => {
          const copied = await copyText(checkoutLink);
          link.textContent = copied ? '已复制' : '复制失败';
          window.setTimeout(() => {
            if (link.isConnected) link.textContent = '复制链接';
          }, 1200);
        });
        meta.appendChild(link);
      }

      const actions = document.createElement('div');
      actions.className = 'account-row-actions';
      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = accountStatusLabel(account);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'at-row__remove';
      remove.textContent = '删除';
      remove.disabled = state.running || state.mounting;
      remove.addEventListener('click', () => {
        state.accounts = state.accounts.filter((item) => item.id !== account.id);
        renderAccounts();
        refresh();
        appendLog('账号已从列表删除。');
        void mountCard();
      });
      actions.append(tag, remove);
      row.append(checkbox, meta, actions);
      root.appendChild(row);
    });

    const selected = selectedAccounts().length;
    $('selectAllAccounts').checked = Boolean(state.accounts.length) && selected === state.accounts.length;
    $('selectAllAccounts').indeterminate = selected > 0 && selected < state.accounts.length;
    saveAccounts();
  }

  async function allocateAccountFingerprint(account, { quiet = false, deferRender = false } = {}) {
    const previousStage = account.stage;
    if (account.status === 'idle') account.stage = '正在分配注册指纹';
    if (!deferRender) scheduleRenderAccounts();
    try {
      const bind = parseProxyPool($('bindProxyPool').value);
      const accountIndex = Math.max(0, state.accounts.indexOf(account));
      const response = await fetch(apiUrl('/fingerprint/allocate'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          access_token: account.token,
          bind_proxy_pool: bind.length ? [bind[accountIndex % bind.length]] : [],
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error || `指纹分配失败：HTTP ${response.status}`);
      }
      account.email = payload.email || account.email;
      account.accountId = payload.account_id || account.accountId;
      account.fingerprint = String(payload.fingerprint || '');
      account.fingerprintId = String(payload.fingerprint_id || '');
      account.fingerprintTls = String(payload.tls || '');
      account.fingerprintRegion = String(payload.region || '');
      account.fingerprintTimezone = String(payload.timezone || '');
      if (account.status === 'idle') account.stage = '环境已就绪';
      account.error = '';
      if (!quiet) appendLog(`${account.email || account.id}：注册指纹 ${account.fingerprintId} 已分配。`);
    } catch (error) {
      if (account.status === 'idle') account.stage = previousStage || '待执行';
      account.error = translateError(error?.message || error);
    }
    saveAccounts();
    if (!deferRender) scheduleRenderAccounts();
  }

  async function allocateFingerprints(accounts, { quiet = false } = {}) {
    if (!accounts.length) return;
    const previousStages = new Map();
    accounts.forEach((account) => {
      previousStages.set(account.id, account.stage);
      if (account.status === 'idle') account.stage = '正在批量分配注册指纹';
    });
    scheduleRenderAccounts();
    const bind = parseProxyPool($('bindProxyPool').value);
    for (let offset = 0; offset < accounts.length; offset += 500) {
      const chunk = accounts.slice(offset, offset + 500);
      try {
        const response = await fetch(apiUrl('/fingerprint/allocate-batch'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            accounts: chunk.map((account) => {
              const accountIndex = Math.max(0, state.accounts.indexOf(account));
              return {
                client_id: account.id,
                access_token: account.token,
                bind_proxy_pool: bind.length ? [bind[accountIndex % bind.length]] : [],
              };
            }),
          }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.ok === false || !Array.isArray(payload.items)) {
          throw new Error(payload.error || `批量指纹分配失败：HTTP ${response.status}`);
        }
        const byId = new Map(payload.items.map((item) => [String(item?.client_id || ''), item]));
        chunk.forEach((account) => {
          const item = byId.get(account.id) || {};
          if (item.ok === false || !item.fingerprint_id) {
            account.stage = previousStages.get(account.id) || '待执行';
            account.error = translateError(item.error || '批量指纹分配结果缺失');
            return;
          }
          account.email = item.email || account.email;
          account.accountId = item.account_id || account.accountId;
          account.fingerprint = String(item.fingerprint || '');
          account.fingerprintId = String(item.fingerprint_id || '');
          account.fingerprintTls = String(item.tls || '');
          account.fingerprintRegion = String(item.region || '');
          account.fingerprintTimezone = String(item.timezone || '');
          if (account.status === 'idle') account.stage = '环境已就绪';
          account.error = '';
        });
      } catch (error) {
        const message = translateError(error?.message || error);
        chunk.forEach((account) => {
          account.stage = previousStages.get(account.id) || '待执行';
          account.error = message;
        });
      }
    }
    saveAccounts();
    renderAccounts();
    refresh();
    if (!quiet) appendLog(`${accounts.length} 个账号的固定指纹已批量分配。`);
  }

  async function hydrateMissingFingerprints() {
    // 已落盘且为 US 的兼容指纹直接复用；只修复缺失或旧 TR/chrome144 数据。
    const pending = state.accounts.filter((account) => (
      account.accountId
      && (!account.fingerprintId
      || !account.fingerprintTls
      || String(account.fingerprintRegion || '').toUpperCase() !== 'US'
      || String(account.fingerprintTls || '').toLowerCase() === 'chrome144'
      || /chrome\/144/i.test(String(account.fingerprint || '')))
    ));
    if (!pending.length) return;
    await allocateFingerprints(pending, { quiet: true });
    if (pending.length) appendLog(`已同步 ${pending.length} 个账号的注册指纹与 TLS 兼容版本。`);
  }

  async function resolveAccountIds(accounts, { quiet = false } = {}) {
    const pending = accounts.filter((account) => !account.accountId);
    if (!pending.length) return { resolved: 0, failed: 0 };
    const bind = parseProxyPool($('bindProxyPool').value);
    let cursor = 0;
    let resolved = 0;
    let failed = 0;
    pending.forEach((account) => {
      account.status = 'idle';
      account.stage = '正在通过 AT 获取账号 ID';
      account.error = '';
    });
    scheduleRenderAccounts();
    async function worker() {
      while (cursor < pending.length) {
        const index = cursor++;
        const account = pending[index];
        const accountIndex = Math.max(0, state.accounts.indexOf(account));
        try {
          const response = await fetch(apiUrl('/account/resolve'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              access_token: account.token,
              bind_proxy_pool: bind.length ? [bind[accountIndex % bind.length]] : [],
            }),
          });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok || payload.ok === false || !payload.account_id) {
            throw new Error(payload.error || `账号 ID 获取失败：HTTP ${response.status}`);
          }
          account.accountId = String(payload.account_id);
          account.userId = String(payload.user_id || account.userId || '');
          account.email = String(payload.email || account.email || '');
          account.status = 'idle';
          account.stage = '账号 ID 已获取，正在准备环境';
          account.error = '';
          resolved += 1;
        } catch (error) {
          account.status = 'error';
          account.stage = '账号 ID 获取失败';
          account.error = translateError(error?.message || error);
          failed += 1;
        }
        scheduleRenderAccounts();
      }
    }
    await Promise.all(Array.from({ length: Math.min(8, pending.length) }, worker));
    saveAccounts();
    renderAccounts();
    refresh();
    if (!quiet) appendLog(`AT 获取账号 ID：${resolved} 个成功，${failed} 个失败。`);
    return { resolved, failed };
  }

  async function hydrateMissingAccountIds() {
    const pending = state.accounts.filter((account) => !account.accountId);
    if (pending.length) await resolveAccountIds(pending, { quiet: true });
  }

  async function hydrateMissingBillingAddresses() {
    const pending = state.accounts.filter((account) => (
      account.billingMode !== 'random'
      || billingIssues(normalizedBilling(account.billing || {}, account.email)).length > 0
    ));
    applyGeneratedBilling(pending);
    if (pending.length) {
      saveAccounts();
      renderAccounts();
    }
  }

  async function importAccounts(text) {
    const values = extractTokens(text);
    const existing = new Set(state.accounts.map((account) => account.token));
    const imported = [];
    let added = 0;
    values.forEach((token) => {
      if (!token || existing.has(token)) return;
      const decoded = decodeAccount(token);
      const identityMissing = !decoded.accountId;
      state.accounts.push({
        id: `account-${state.nextId++}`,
        token,
        email: decoded.email || '',
        accountId: decoded.accountId || '',
        userId: decoded.userId || '',
        selected: true,
        status: 'idle',
        stage: identityMissing ? '账号已导入，缺少账号 ID' : '待执行',
        error: '',
        link: '',
        checkoutId: '',
        processorEntity: '',
        lastTaskId: '',
        taskId: '',
        bindProxyLabel: '',
        promoProxyLabel: '',
        fingerprint: '',
        fingerprintId: '',
        fingerprintTls: '',
        fingerprintRegion: '',
        fingerprintTimezone: '',
        steps: [],
        billingMode: '',
        billing: null,
      });
      imported.push(state.accounts[state.accounts.length - 1]);
      existing.add(token);
      added += 1;
    });
    $('accountImportInput').value = '';
    updateImportCount();
    renderAccounts();
    refresh();
    const importStatus = $('accountImportStatus');
    if (importStatus) {
      importStatus.className = `proxy-pool-note ${added ? '' : 'is-error'}`.trim();
      importStatus.textContent = added
        ? `已导入 ${added} 个账号，正在通过 AT 获取账号 ID。`
        : '没有发现新的账号，或文件中的账号已经导入。';
    }
    log(added ? `已导入 ${added} 个账号，导入框已清空。` : '没有发现新的账号。');
    if (imported.length) {
      const identityResult = await resolveAccountIds(imported, { quiet: true });
      const resolvedAccounts = imported.filter((account) => account.accountId);
      await allocateFingerprints(resolvedAccounts);
      if (importStatus) {
        importStatus.className = `proxy-pool-note ${identityResult.failed ? 'is-error' : ''}`.trim();
        importStatus.textContent = `已导入 ${added} 个账号；AT 获取账号 ID 成功 ${identityResult.resolved + (added - identityResult.resolved - identityResult.failed)} 个，失败 ${identityResult.failed} 个。`;
      }
      await mountCard();
    }
  }

  async function importRegistrationQueue() {
    try {
      const response = await fetch(apiUrl('/registration-queue'), { cache: 'no-store' });
      const payload = await response.json().catch(() => ({}));
      const items = Array.isArray(payload.items) ? payload.items : [];
      if (!response.ok || !items.length) return;
      await importAccounts(items.map((item) => item.access_token).filter(Boolean).join('\n'));
      await Promise.all(items.map((item) => fetch(apiUrl('/registration-queue/ack'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: item.id }),
      })));
      const status = $('accountImportStatus');
      if (status) status.textContent = `已接收 ${items.length} 个注册账号，等待人工确认订阅。`;
    } catch (_) {}
  }

  async function applyRuntimeDefaults() {
    try {
      const response = await fetch(apiUrl('/runtime-defaults'), { cache: 'no-store' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
      $('bindProxyPool').value = '';
      $('promoProxyPool').value = '';
      state.runtimeReady = true;
      state.runtimeRouteNote = String(payload.route_notice || '');
      const routeNote = $('runtimeRouteNote');
      if (routeNote) {
        routeNote.textContent = state.runtimeRouteNote;
        routeNote.classList.toggle('is-error', Boolean(state.runtimeRouteNote));
      }
      refresh();
    } catch (error) {
      state.runtimeReady = false;
      state.runtimeRouteNote = '';
      const routeNote = $('runtimeRouteNote');
      if (routeNote) routeNote.textContent = '';
      $('cardReady').className = 'inline-state error';
      $('cardReady').textContent = `主程序网络出口读取失败：${translateError(error.message)}`;
    }
  }

  function updateImportCount() {
    $('atCount').textContent = `${extractTokens($('accountImportInput').value).length} 条待导入`;
  }

  function updateAccount(account, values) {
    Object.assign(account, values);
    saveAccounts();
    scheduleRenderAccounts();
  }

  function refresh() {
    const bind = parseProxyPool($('bindProxyPool').value);
    const promo = parseProxyPool($('promoProxyPool').value);
    const selected = selectedAccounts();
    const runnable = runnableBatchAccounts();
    applyGeneratedBilling(selected);
    const missingAccountIds = selected.filter((account) => !account.accountId);
    const billingMissingAccounts = selected.filter(
      (account) => billingIssues(normalizedBilling(account.billing || {}, account.email)).length > 0,
    );
    $('bindProxyCount').textContent = `${bind.length} PROXY`;
    $('promoProxyCount').textContent = `${promo.length} PROXY`;
    const identityReady = Boolean(selected.length && missingAccountIds.length === 0);
    const basicReady = Boolean(identityReady && state.runtimeReady);
    const linkOnlyReady = basicReady;
    const environmentReady = accountEnvironmentReady(selected);
    const cardInputReady = environmentReady && Boolean(cardFromForm(false));
    const billingReady = billingMissingAccounts.length === 0;
    const ready = basicReady && cardInputReady && billingReady;
    const batchReady = ready && runnable.length > 0;
    $('loadCardButton').disabled = !basicReady || environmentReady || state.mounting || state.running;
    $('batchMode').disabled = state.running;
    $('nextBatchButton').disabled = !(state.running && state.manualBatchWaiting);
    $('batchBindLinkButton').disabled = !batchReady || state.running;
    const paymentConfirmed = $('paymentConfirm').checked;
    ['fullFlowButton', 'linkPayButton'].forEach((id) => {
      $(id).disabled = !batchReady || !paymentConfirmed || state.running;
    });
    $('linkOnlyButton').disabled = !linkOnlyReady || !runnable.length || state.running;
    $('deleteSelectedButton').disabled = state.running || state.mounting || !selected.length;
    $('exportCsvButton').disabled = !state.accounts.length;
    $('clearButton').disabled = state.running || state.mounting || !state.accounts.length;
    $('selectAllAccounts').disabled = state.running || state.mounting || !state.accounts.length;
    $('cardReady').className = `inline-state ${ready ? 'ok' : ''}`;
    if (!runnable.length && selected.length) $('cardReady').textContent = '已选账号均已完成或待提交，请选择待执行账号。';
    else if (ready) $('cardReady').textContent = `卡片输入已就绪；已选择 ${selected.length} 个账号。`;
    else if (missingAccountIds.length) $('cardReady').textContent = `${missingAccountIds.length} 个 AT 缺少账号 ID，请导入 ChatGPT access_token。`;
    else if (!basicReady) $('cardReady').textContent = state.runtimeReady ? '请先导入并选择账号。' : '正在读取主程序网络出口。';
    else if (!environmentReady) $('cardReady').textContent = state.mounting ? '正在自动预检账号环境…' : '账号环境等待自动预检。';
    else if (!cardInputReady) $('cardReady').textContent = '请填写卡号、有效期和 CVC。';
    else if (!billingReady) $('cardReady').textContent = `还有 ${billingMissingAccounts.length} 个账号的账单地址未就绪。`;
    return { accounts: selected, runnable, bind, promo, basicReady, linkOnlyReady, environmentReady, cardInputReady, billingReady, billingMissingAccounts, missingAccountIds, ready, batchReady };
  }

  async function mountCard() {
    const data = refresh();
    if (!data.basicReady || data.environmentReady) return data.environmentReady;
    if (state.mounting) return false;
    state.mounting = true;
    $('loadCardButton').disabled = true;
    $('cardReady').textContent = '正在执行账号与 Checkout 预检…';
    renderAccounts();
    try {
      const preflightPayloads = new Array(data.accounts.length);
      const preflightFailures = [];
      let preflightCursor = 0;
      let preflightDone = 0;
      let preflightStarted = 0;
      const preflightLimit = Math.max(
        1,
        Math.min(
          MAX_BATCH_CONCURRENCY,
          Number($('batchConcurrency').value) || 1,
          data.accounts.length,
        ),
      );
      async function preflightWorker() {
        while (preflightCursor < data.accounts.length) {
          const index = preflightCursor++;
          const account = data.accounts[index];
          const previewBindPool = data.bind.length
            ? data.bind.map((_, offset) => data.bind[(state.bindProxyCursor + index + offset) % data.bind.length]).slice(0, MAX_PROXY_FALLBACKS)
            : [];
          const previewPromoPool = data.promo.length
            ? data.promo.map((_, offset) => data.promo[(state.promoProxyCursor + index + offset) % data.promo.length]).slice(0, MAX_PROXY_FALLBACKS)
            : [];
          preflightStarted += 1;
          updateProgress(
            preflightDone,
            data.accounts.length,
            `自动预检账号环境（已发起 ${preflightStarted}/${data.accounts.length}）`,
          );
          const controller = new AbortController();
          const timeout = window.setTimeout(() => controller.abort(), PREFLIGHT_TIMEOUT_MS);
          try {
            const response = await fetch(apiUrl('/card-bind/session'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              signal: controller.signal,
              body: JSON.stringify({
                access_token: account.token,
                account_id: account.accountId,
                email: account.email,
                bind_proxy_pool: previewBindPool,
                promo_proxy_pool: previewPromoPool,
              }),
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok || payload.ok === false) {
              throw new Error(payload.error || `HTTP ${response.status}`);
            }
            preflightPayloads[index] = payload;
          } catch (error) {
            const message = error?.name === 'AbortError'
              ? '预检请求超时（60 秒），已跳过当前账号。'
              : translateError(error?.message || error);
            account.selected = false;
            account.status = 'error';
            account.stage = message.includes('免费优惠') ? '优惠不可用' : '预检失败';
            account.error = message;
            preflightFailures.push(account);
            scheduleRenderAccounts();
          } finally {
            window.clearTimeout(timeout);
            preflightDone += 1;
            updateProgress(preflightDone, data.accounts.length, '自动预检账号环境');
          }
        }
      }
      await Promise.all(Array.from({ length: preflightLimit }, preflightWorker));
      const readyAccounts = [];
      preflightPayloads.forEach((payload, index) => {
        if (!payload) return;
        const account = data.accounts[index];
        account.fingerprint = String(payload.fingerprint || account.fingerprint || '');
        account.fingerprintId = String(payload.fingerprint_id || account.fingerprintId || '');
        account.fingerprintTls = String(payload.fingerprint_tls || account.fingerprintTls || '');
        account.fingerprintRegion = String(payload.fingerprint_region || account.fingerprintRegion || '');
        account.fingerprintTimezone = String(payload.fingerprint_timezone || account.fingerprintTimezone || '');
        if (['优惠不可用', '预检失败'].includes(account.stage)) {
          account.status = 'idle';
          account.stage = '账号环境预检完成';
          account.error = '';
        }
        readyAccounts.push(account);
      });
      state.mounting = false;
      if (!readyAccounts.length) {
        state.mounted = false;
        state.mountedAccountSignature = '';
        saveAccounts();
        renderAccounts();
        refresh();
        $('cardReady').className = 'inline-state error';
        $('cardReady').textContent = `没有账号通过预检；已跳过 ${preflightFailures.length} 个账号，未进行绑卡或扣款。`;
        $('progressText').textContent = '预检完成：当前批次没有可用的零元优惠账号。';
        return false;
      }
      saveAccounts();
      renderAccounts();
      state.mounted = true;
      state.mountedAccountSignature = accountEnvironmentSignature(readyAccounts);
      setStep('stepCard', 'done');
      setStep('stepAt', 'active');
      $('cardReady').textContent = `账号环境预检完成；当前卡片将应用到 ${readyAccounts.length} 个账号。`;
      appendLog('账号环境预检完成，卡片信息仅保留在当前页面。');
      refresh();
      if (preflightFailures.length) {
        $('progressText').textContent = `预检完成：${readyAccounts.length} 个可用，已跳过 ${preflightFailures.length} 个失败账号。`;
      }
      if (!accountEnvironmentReady() && selectedAccounts().every((account) => account.accountId)) {
        void mountCard();
      }
      return true;
    } catch (error) {
      state.mounting = false;
      const message = translateError(error.message);
      renderAccounts();
      refresh();
      $('loadCardButton').disabled = false;
      $('cardReady').className = 'inline-state error';
      $('cardReady').textContent = message;
      appendLog(message);
      return false;
    }
  }

  async function createPaymentMethod(account) {
    if (!accountEnvironmentReady()) throw new Error('账号环境尚未完成预检');
    const billing = normalizedBilling(account?.billing || {}, account?.email || '');
    const issues = billingIssues(billing);
    if (issues.length) throw new Error(`${account?.email || account?.id || '当前账号'} 的账单地址缺少：${issues.join('、')}`);
    const card = cardFromForm(true);
    return {
      card,
      last4: card.number.slice(-4),
      billing,
    };
  }

  async function prepareAccountCard(account) {
    updateAccount(account, { status: 'running', stage: '正在准备同批同步起跑', error: '' });
    setAccountStep(account, 'card_method', 'running', '正在应用当前页面卡片');
    renderAccounts();
    const browserCard = await createPaymentMethod(account);
    setAccountStep(account, 'card_method', 'done', `卡片已应用 ····${browserCard.last4 || ''}`);
    updateAccount(account, { stage: '卡片已应用，等待同批账号同步起跑' });
    return browserCard;
  }

  function accountFlowPayload(account, mode, browserCard) {
    const payload = {
      access_token: account.token,
      account_id: account.accountId,
      email: account.email,
      flow_mode: mode,
      promo_proxy_pool: account.promoProxyPool?.length ? account.promoProxyPool : [account.promoProxy],
    };
    if (mode !== 'link_only') Object.assign(payload, {
      card: browserCard.card,
      card_last4: browserCard.last4,
      billing: browserCard.billing,
      bind_proxy_pool: account.bindProxyPool?.length ? account.bindProxyPool : [account.bindProxy],
    });
    return payload;
  }

  async function pollAccountTask(account, taskId) {
    updateAccount(account, { stage: '同批任务已放行，正在同步执行', taskId });
    for (let i = 0; i < Math.ceil(1800000 / TASK_POLL_MS); i += 1) {
      await new Promise((resolve) => setTimeout(resolve, TASK_POLL_MS));
      if (state.stop) throw new Error('任务已停止');
      const poll = await fetch(`${apiUrl(`/standalone-flow/task/${taskId}`)}?t=${Date.now()}`, { cache: 'no-store' });
      const data = await poll.json().catch(() => ({}));
      const task = data.task || {};
      const result = task.result || {};
      if (Array.isArray(task.steps)) account.steps = task.steps;
      if (task.status === 'done') {
        const checkoutLink = canonicalCheckoutLink(result);
        updateAccount(account, {
          email: result.email || account.email,
          status: 'success',
          stage: task.stage || '任务完成',
          checkoutId: String(result.checkout_id || ''),
          processorEntity: String(result.processor_entity || ''),
          link: checkoutLink,
          lastTaskId: taskId,
          error: '',
          taskId: '',
          fingerprint: result.fingerprint || account.fingerprint || '',
          steps: Array.isArray(task.steps) ? task.steps : account.steps,
        });
        return result;
      }
      if (task.status === 'action_required') {
        const checkoutLink = canonicalCheckoutLink(result, account.link);
        updateAccount(account, {
          email: result.email || account.email,
          status: 'action_required',
          stage: task.stage || (checkoutLink
            ? '订阅待确认：使用当前行的 Checkout 链接继续'
            : '订阅待确认：Checkout 链接生成失败'),
          checkoutId: String(result.checkout_id || account.checkoutId || ''),
          processorEntity: String(result.processor_entity || account.processorEntity || ''),
          link: checkoutLink,
          error: '',
          lastTaskId: taskId,
          taskId: '',
          fingerprint: result.fingerprint || account.fingerprint || '',
          steps: Array.isArray(task.steps) ? task.steps : account.steps,
        });
        return result;
      }
      if (task.status === 'error') {
        account.taskId = '';
        account.stage = task.stage || '任务执行失败';
        account.steps = Array.isArray(task.steps) ? task.steps : account.steps;
        saveAccounts();
        throw new Error(task.error || '任务执行失败');
      }
      const nextStage = task.stage || '任务处理中';
      if (nextStage !== account.stage) {
        updateAccount(account, {
          stage: nextStage,
          steps: Array.isArray(task.steps) ? task.steps : account.steps,
        });
      }
    }
    throw new Error('任务轮询超时');
  }

  async function submitAlignedWave(entries, mode) {
    entries.forEach(({ account }) => updateAccount(account, {
      stage: `本批 ${entries.length} 个账号已就绪，等待统一放行`,
    }));
    const response = await fetch(apiUrl('/standalone-flow/quick-checkout-batch'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_delay_ms: 250,
        tasks: entries.map(({ account, browserCard }) => ({
          client_id: account.id,
          payload: accountFlowPayload(account, mode, browserCard),
        })),
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false || !Array.isArray(payload.items)) {
      throw new Error(payload.error || `同步批次创建失败：HTTP ${response.status}`);
    }
    const byId = new Map(payload.items.map((item) => [String(item.client_id || ''), item.task_id]));
    entries.forEach(({ account }) => {
      const taskId = String(byId.get(account.id) || '');
      if (!taskId) throw new Error(`${account.email || account.id} 缺少同步任务 ID`);
      account.taskId = taskId;
      account.stage = `同步批次 ${String(payload.start_group || '').slice(0, 8)} 已排队`;
    });
    renderAccounts();
    return Promise.allSettled(entries.map(({ account }) => pollAccountTask(account, account.taskId)));
  }

  function setModeSteps(mode, status) {
    ['stepCard', 'stepAt', 'stepBind', 'stepLink'].forEach((id) => setStep(id, ''));
    if (mode !== 'link_only') setStep('stepCard', 'done');
    setStep('stepAt', 'done');
    if (mode === 'bind_only') setStep('stepBind', status);
    else if (mode === 'link_pay' || mode === 'link_only') setStep('stepLink', status);
    else {
      setStep('stepBind', status);
      setStep('stepLink', status);
    }
  }

  function updateProgress(done, total, label) {
    const percent = total ? Math.round((done * 100) / total) : 0;
    $('progressText').textContent = `${label} ${done}/${total}`;
    $('progressPercent').textContent = `${percent}%`;
    $('progressBar').style.width = `${percent}%`;
  }

  async function runBatch(mode, label) {
    const data = refresh();
    if ((mode === 'link_only' ? !data.linkOnlyReady : !data.ready) || !data.runnable.length || state.running) return;
    const accounts = [...data.runnable];
    const manual = $('batchMode').value === 'manual';
    state.running = true;
    state.stop = false;
    releaseManualBatch();
    hideBatchSummary();
    $('stopButton').disabled = false;
    renderAccounts();
    refresh();
    setModeSteps(mode, 'active');
    const limit = Math.max(1, Math.min(MAX_BATCH_CONCURRENCY, Number($('batchConcurrency').value) || 1));
    const totalBatches = Math.ceil(accounts.length / limit);
    setBatchRotationStatus(
      totalBatches > 1
        ? `${manual ? '手动' : '自动'}模式：共 ${totalBatches} 批，每批最多 ${limit} 个账号`
        : `当前 ${accounts.length} 个账号只有 1 批，不会出现下一批`,
      totalBatches > 1 ? 'is-active' : '',
    );
    const waitForNextWaveIfNeeded = async (offset) => {
      if (manual && !state.stop && offset + limit < accounts.length) {
        await waitForNextBatch(Math.floor(offset / limit) + 1, totalBatches);
      }
    };
    updateProgress(0, accounts.length, '正在分配代理');
    assignProxies(accounts, data.bind, data.promo, mode);
    appendLog(`已为 ${accounts.length} 个账号分配${mode === 'link_only' ? '提链代理' : '双代理'}，每批 ${Math.min(limit, accounts.length)} 个账号同步起跑。`);
    updateProgress(0, accounts.length, `${label}同步并发`);
    let done = 0;
    let success = 0;
    let pending = 0;

    function recordFailure(account, error) {
      const message = translateError(error?.message || error);
      failRunningAccountStep(account, message);
      const preciseStage = String(account.stage || '');
      const failedStep = [...(account.steps || [])].reverse().find((step) => step.status === 'error');
      updateAccount(account, {
        status: 'error',
        stage: failedStep?.label
          ? `${failedStep.label}失败`
          : preciseStage.includes('失败') ? preciseStage : `${label}失败`,
        error: message,
      });
      appendLog(`${account.email || account.id}：${message}`);
    }

    for (let offset = 0; offset < accounts.length && !state.stop; offset += limit) {
      const wave = accounts.slice(offset, offset + limit);
      const prepared = [];
      const batchNumber = Math.floor(offset / limit) + 1;
      setBatchRotationStatus(
        `${manual ? '手动' : '自动'}模式：正在执行第 ${batchNumber}/${totalBatches} 批（账号 ${offset + 1}-${offset + wave.length}）`,
        'is-active',
      );
      updateProgress(done, accounts.length, `正在准备同步批次 ${batchNumber}`);
      for (const account of wave) {
        if (state.stop) break;
        try {
          if (mode === 'link_only') {
            updateAccount(account, { status: 'running', stage: '提链代理已就绪，等待同批同步起跑', error: '' });
            prepared.push({ account, browserCard: null });
          } else {
            const browserCard = await prepareAccountCard(account);
            prepared.push({ account, browserCard });
          }
        } catch (error) {
          recordFailure(account, error);
          done += 1;
          updateProgress(done, accounts.length, label);
        }
      }
      if (!prepared.length || state.stop) {
        await waitForNextWaveIfNeeded(offset);
        continue;
      }
      appendLog(`同步批次 ${batchNumber}：${prepared.length} 个账号已就绪，统一放行。`);
      try {
        const settled = await submitAlignedWave(prepared, mode);
        settled.forEach((result, index) => {
          const account = prepared[index].account;
          if (result.status === 'fulfilled') {
            if (account.status === 'action_required') pending += 1;
            else success += 1;
          }
          else recordFailure(account, result.reason);
          done += 1;
          updateProgress(done, accounts.length, label);
        });
      } catch (error) {
        prepared.forEach(({ account }) => {
          recordFailure(account, error);
          done += 1;
          updateProgress(done, accounts.length, label);
        });
      }
      await waitForNextWaveIfNeeded(offset);
    }
    accounts.forEach((account) => {
      if (isBatchTerminalAccount(account)) account.selected = false;
    });
    releaseManualBatch();
    if (state.stop) {
      accounts.filter((account) => account.status === 'running' && !account.taskId).forEach((account) => {
        account.status = 'idle';
        account.stage = '已停止，尚未执行';
      });
    }
    state.running = false;
    $('stopButton').disabled = true;
    const remaining = runnableBatchAccounts().length;
    if (success === accounts.length && !state.stop) setModeSteps(mode, 'done');
    $('progressText').textContent = state.stop ? `${label}已停止` : `${label}完成`;
    setBatchRotationStatus(
      state.stop ? `${label}已停止，剩余 ${remaining} 个待执行账号` : `${label}完成，共 ${totalBatches} 批；剩余 ${remaining} 个待执行账号`,
      state.stop ? '' : 'is-done',
    );
    showBatchSummary(
      mode === 'bind_only' ? '批量绑卡' : mode === 'link_only' ? '批量提链' : '批量支付',
      success,
      Math.max(0, accounts.length - success - pending),
      state.stop,
      pending,
    );
    renderAccounts();
    refresh();
  }

  $('accountImportInput').addEventListener('input', updateImportCount);
  $('importAtButton').addEventListener('click', () => { void importAccounts($('accountImportInput').value); });
  $('importAtFileButton').addEventListener('click', () => $('atFileInput').click());
  $('atFileInput').addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    if (file) await importAccounts(await file.text());
    event.target.value = '';
  });
  $('selectAllAccounts').addEventListener('change', (event) => {
    state.accounts.forEach((account) => { account.selected = event.target.checked; });
    renderAccounts();
    refresh();
  });
  $('deleteSelectedButton').addEventListener('click', () => {
    const count = selectedAccounts().length;
    state.accounts = state.accounts.filter((account) => !account.selected);
    renderAccounts();
    refresh();
    appendLog(`已删除 ${count} 个账号。`);
  });
  $('clearButton').addEventListener('click', () => {
    const count = state.accounts.length;
    state.accounts = [];
    renderAccounts();
    refresh();
    $('progressBar').style.width = '0%';
    $('progressPercent').textContent = '0%';
    hideBatchSummary();
    appendLog(`账号列表已清空，共移除 ${count} 个账号。`);
  });
  $('exportCsvButton').addEventListener('click', () => {
    appendLog(exportAccountsCsv() ? 'CSV 已导出。' : '当前没有可导出的账号。');
  });
  $('applyBillingButton').addEventListener('click', () => { generateBillingAddress(); });
  $('loadCardButton').addEventListener('click', mountCard);
  $('parseCardButton').addEventListener('click', () => {
    try { parsePastedCard(); }
    catch (error) { $('cardReady').className = 'inline-state error'; $('cardReady').textContent = error.message; }
  });
  ['cardNumberInput', 'cardExpiryInput', 'cardCvcInput'].forEach((id) => $(id).addEventListener('input', refresh));
  $('batchConcurrency').addEventListener('input', saveWorkbenchPrefs);
  $('batchMode').addEventListener('change', () => { saveWorkbenchPrefs(); refresh(); });
  $('paymentConfirm').addEventListener('change', refresh);
  $('validateButton').addEventListener('click', () => {
    const data = refresh();
    const issues = [];
    if (!state.accounts.length) issues.push('尚未导入账号');
    else if (!data.accounts.length) issues.push('尚未选择账号');
    if (!state.runtimeReady) issues.push('主程序网络出口尚未就绪');
    if (!state.mounted) issues.push('账号环境尚未完成预检');
    if (!cardFromForm(false)) issues.push('卡片信息不完整');
    if (!data.billingReady) issues.push(`还有 ${data.billingMissingAccounts.length} 个账号账单地址未就绪`);
    $('progressText').textContent = issues.length
      ? `检查输入：${issues.join('；')}`
      : `检查通过：已选择 ${data.accounts.length} 个账号。`;
  });
  $('fullFlowButton').addEventListener('click', () => runBatch('full', '提链 + 绑卡 + 提链 + 支付'));
  $('batchBindLinkButton').addEventListener('click', () => runBatch('bind_only', '提链 + 绑卡'));
  $('linkPayButton').addEventListener('click', () => runBatch('link_pay', '提链 + 支付'));
  $('linkOnlyButton').addEventListener('click', () => runBatch('link_only', '提链'));
  $('stopButton').addEventListener('click', () => {
    state.stop = true;
    releaseManualBatch();
    $('stopButton').disabled = true;
    appendLog('已停止后续账号任务。');
  });
  $('nextBatchButton').addEventListener('click', () => {
    if (state.manualBatchWaiting) releaseManualBatch();
  });
  window.addEventListener('pagehide', saveAccountsNow);

  loadWorkbenchPrefs();
  loadAccounts();
  updateImportCount();
  generateBillingAddress({ quiet: true });
  renderAccounts();
  refresh();
  log(state.accounts.length ? `已恢复 ${state.accounts.length} 个账号及其状态。` : '先在上方导入账号并填写卡片。');
  void (async () => {
    await applyRuntimeDefaults();
    await importRegistrationQueue();
    await hydrateMissingAccountIds();
    await Promise.all([hydrateMissingFingerprints(), hydrateMissingBillingAddresses()]);
    const ready = refresh();
    if (ready.basicReady && !state.mounted) await mountCard();
  })();
}());
