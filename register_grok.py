# -*- coding: utf-8 -*-
"""
Grok (x.ai) 自动注册
关键: grok.com 有 Cloudflare 全页拦截，必须走 Clash 干净节点(换节点绕过)。

流程: 切Clash节点 -> BitBrowser走代理 -> grok.com -> 新規登録 -> accounts.x.ai
       -> メールで登録 -> 填邮箱 -> 邮件验证码(浏览器登录Outlook) -> 保存 cookie

界面是日文(节点地区导致)，按钮文本用 日文+英文 双匹配。

用法:
    python register_grok.py --count 1
    python register_grok.py --count 5 --node "美国 02"
"""

import argparse
import asyncio
import os
import random
import string
import sys
import time
import uuid

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from playwright.async_api import async_playwright

import requests

from bitbrowser import BitBrowser
from common.browser import inject_stealth, create_browser_with_retry, human_type, react_fill
from common.mailbox import get_code_by_token, get_code_outlook_pw, prelogin_outlook
from common.cookies import save_platform_cookies
from common import emails as email_pool
from common import proxy_switch

# 打码平台 key（解 Cloudflare Turnstile）。config 顶部会加载 .env，真实环境变量优先。
try:
    from config import (
        CAPSOLVER_API_KEY,
        EZCAPTCHA_API_KEY,
        EZCAPTCHA_API_BASE,
        YESCAPTCHA_API_KEY,
        YESCAPTCHA_API_BASE,
    )
except Exception:
    CAPSOLVER_API_KEY = ""
    EZCAPTCHA_API_KEY = ""
    EZCAPTCHA_API_BASE = "https://api.ez-captcha.com"
    YESCAPTCHA_API_KEY = ""
    YESCAPTCHA_API_BASE = "https://api.yescaptcha.com"

# 临时邮箱开关/默认 provider（GROK_USE_TEMP_EMAIL=true 时走 HTTP API 取码，免 Outlook 浏览器）
try:
    from config import (
        GROK_USE_TEMP_EMAIL,
        TEMP_EMAIL_PROVIDER,
        SUB2API_URL,
        SUB2API_EMAIL,
        SUB2API_PASSWORD,
        SUB2API_GROK_GROUP,
        SUB2API_GROK_PROXY_ID,
    )
except Exception:
    GROK_USE_TEMP_EMAIL = False
    TEMP_EMAIL_PROVIDER = "gptmail"
    SUB2API_URL = ""
    SUB2API_EMAIL = ""
    SUB2API_PASSWORD = ""
    SUB2API_GROK_GROUP = "grok"
    SUB2API_GROK_PROXY_ID = 0
from common.temp_email import create_mailbox, poll_verification_code

PLATFORM = "grok"
GROK_URL = "https://grok.com/"
GROK_SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com&return_to=%2F"
CLASH_PROXY_HOST = "127.0.0.1"
CLASH_PROXY_PORT = "7897"
GROK_BROWSER_CORE_VERSION = (
    os.environ.get("GROK_BROWSER_CORE_VERSION")
    or os.environ.get("BB_CORE_VERSION")
    or "146"
).strip()
# 登录态关键 cookie（运行时确认，先放候选）
KEY_COOKIES = ["sso", "sso-rw", "__Secure-next-auth.session-token", "auth_token"]
REGISTER_TIMEOUT = 600
KEEP_ON_FAIL = False
FIXED_EMAIL = None
FIXED_PASSWORD = None
FIXED_REFRESH_TOKEN = None
FIXED_CLIENT_ID = None
USE_LATEST_RT = False
IMPORT_SUB2API = False
IMPORT_SUB2API_GROUP = ""
BROWSER_MAILBOX_ATTEMPTS = 6

# 注册方式按钮（中文+日文+英文，不同节点地区界面语言不同）
SIGNUP_BTN = ["新規登録", "注册", "註冊", "Sign up", "サインアップ", "注册账号"]
EMAIL_SIGNUP_BTN = [
    "メールで登録", "用邮箱注册", "使用邮箱注册", "邮箱注册", "用電子郵件註冊",
    "Sign up with email", "Continue with email", "メールアドレスで登録", "使用电子邮件",
    "Zarejestruj się za pomocą e-maila",
    "Regístrate con correo electrónico", "Registrarse con correo electrónico",
]
CONTINUE_BTN = ["続行", "继续", "繼續", "Continue", "次へ", "下一步", "Next", "Sign up", "登録", "注册", "Verify", "確認", "确认", "验证"]
COOKIE_DISMISS = [
    "すべて拒否する", "全部拒絕", "全部拒绝", "拒绝所有", "Reject all",
    "Odrzucenie wszystkich", "Odrzuć wszystkie", "接受所有 Cookie", "Accept all",
    "Akceptuj wszystkie pliki cookie", "すべて許可する", "全部允許", "拒否", "同意",
    "Rechazarlas todas", "Rechazar todas", "Aceptar todas las cookies",
]
# 提交验证码按钮
VERIFY_BTN = ["メールを確認", "確認", "确认", "验证邮件", "验证", "驗證", "Verify", "Verify email",
              "Confirm email", "Confirm Email", "続行", "继续", "Continue", "Submit"]
# 完成注册按钮（x.ai 验证码后的 givenName/familyName/password/Turnstile 页）
COMPLETE_BTN = ["登録を完了", "アカウントを作成", "Complete registration", "Complete sign up",
                "Create account", "Sign up", "完成注册", "完成註冊", "完成", "完了", "Done",
                "登録", "サインアップ", "Continue", "続行", "继续", "Next", "次へ"]

GROK_SENDER = ("x.ai", "grok", "noreply", "no-reply")
GROK_SUBJECT = ("code", "verify", "verification", "grok", "x.ai", "confirm", "確認", "認証", "コード", "验证", "驗證")
GROK_CODE_REGEX = (
    r"\b((?=[A-Z0-9-]*[A-Z])(?:[A-Z0-9]{2,4}-[A-Z0-9]{2,4}|[A-Z0-9]{6}))\b"
)


# 在 turnstile 脚本加载前 hook window.turnstile.render，截获 React 传入的 callback + sitekey。
# 这是给 React 表单灌打码 token 的关键:x.ai 用 callback(token) 更新组件 state 来解禁"完成注册",
# 只改隐藏的 cf-turnstile-response 值 React 收不到。用属性 setter 拦截 window.turnstile 赋值,
# 一旦 CF 脚本设置它就立刻包裹 render,把 opts.callback/sitekey/action/cData 存到 window.__cf*。
TURNSTILE_HOOK_JS = r"""
(() => {
  if (window.__cfHookInstalled) return;
  window.__cfHookInstalled = true;
  window.__cfCb = [];
  const wrap = (v) => {
    if (v && typeof v.render === 'function' && !v.__hooked) {
      const orig = v.render.bind(v);
      v.render = (el, opts) => {
        try {
          if (opts) {
            if (opts.callback) window.__cfCb.push(opts.callback);
            window.__cfSitekey = opts.sitekey || window.__cfSitekey;
            window.__cfAction  = opts.action  || window.__cfAction;
            window.__cfCdata   = opts.cData || opts.cdata || window.__cfCdata;
          }
        } catch (e) {}
        return orig(el, opts);
      };
      v.__hooked = true;
    }
    return v;
  };
  let _ts = window.turnstile ? wrap(window.turnstile) : undefined;
  try {
    Object.defineProperty(window, 'turnstile', {
      configurable: true,
      get() { return _ts; },
      set(v) { _ts = wrap(v); },
    });
  } catch (e) {}
})();
"""


# BitBrowser already supplies the main fingerprint. Keep the xAI supplement narrow:
# the previous shared stealth script replaced global Object.defineProperty,
# Error.prepareStackTrace and iframe getters, which broke the post-OTP React transition.
GROK_STEALTH_JS = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
  try { delete navigator.__proto__.webdriver; } catch (e) {}
  if (!window.chrome) {
    window.chrome = {runtime: {}, loadTimes() {}, csi() {}, app: {}};
  }
  try {
    const query = navigator.permissions && navigator.permissions.query;
    if (query) {
      navigator.permissions.query = params => params.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : query.call(navigator.permissions, params);
    }
  } catch (e) {}
  try { Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]}); } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']}); } catch (e) {}
  try {
    if (navigator.connection) {
      Object.defineProperty(navigator.connection, 'rtt', {get: () => 50});
    }
  } catch (e) {}
  try { Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8}); } catch (e) {}
  try { Object.defineProperty(navigator, 'deviceMemory', {get: () => 8}); } catch (e) {}
  for (const prop of Object.getOwnPropertyNames(window)) {
    if (/^cdc_|^__cdc|^_cdp|^__cdp|^chrome_devtools/i.test(prop)) {
      try { delete window[prop]; } catch (e) {}
    }
  }
  try {
    if (window.outerWidth === 0) {
      Object.defineProperty(window, 'outerWidth', {get: () => innerWidth + 16});
    }
    if (window.outerHeight === 0) {
      Object.defineProperty(window, 'outerHeight', {get: () => innerHeight + 88});
    }
  } catch (e) {}
})();
"""


async def inject_grok_stealth(context, page):
    """Inject the minimum xAI-compatible supplement without mutating JS globals."""
    await context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
    await context.add_init_script(GROK_STEALTH_JS)
    try:
        await page.evaluate(GROK_STEALTH_JS)
    except Exception:
        pass
    print("  xAI-compatible stealth injected")


def grok_browser_fingerprint():
    """Use the installed modern BitBrowser core and its native fingerprint."""
    return {
        "ostype": "PC",
        "os": "Win32",
        "coreVersion": GROK_BROWSER_CORE_VERSION,
        "isIpCreateTimeZone": True,
        "isIpCreateLanguage": True,
        "isIpCreateDisplayLanguage": True,
        "isIpCreatePosition": True,
        "isIpCountry": True,
    }


async def arm_turnstile_hook(context, page):
    """Arm Turnstile capture only after email RPCs have completed."""
    try:
        await context.add_init_script(TURNSTILE_HOOK_JS)
        await page.evaluate(TURNSTILE_HOOK_JS)
        print("  Turnstile hook armed for account-details step")
    except Exception as e:
        print(f"  turnstile hook inject failed: {str(e)[:60]}")


async def _turnstile_token(page):
    """读当前 cf-turnstile-response 的值(有值=已过)。"""
    try:
        return await page.evaluate(
            "() => { const e=document.querySelector('input[name=\"cf-turnstile-response\"],textarea[name=\"cf-turnstile-response\"]'); return e ? e.value : null; }"
        )
    except Exception:
        return None


async def _has_turnstile_widget(page):
    """页面上是否存在 Turnstile widget（cf-turnstile 容器 / data-sitekey / 隐藏响应框 /
    challenges.cloudflare.com iframe）。用于决定要不要走 ensure_turnstile，避免无墙时空等。"""
    try:
        return await page.evaluate(r"""() => !!(
            document.querySelector('.cf-turnstile,[data-sitekey],input[name="cf-turnstile-response"],textarea[name="cf-turnstile-response"]')
            || [...document.querySelectorAll('iframe')].some(f => (f.src || '').includes('challenges.cloudflare.com'))
        )""")
    except Exception:
        return False


async def _extract_sitekey(page):
    """提取 Turnstile sitekey:优先 hook 截获的 window.__cfSitekey,
    再退化到 [data-sitekey] 属性,最后从 challenges.cloudflare.com iframe 的 url 里抠 0x... 串。"""
    try:
        return await page.evaluate(r"""() => {
            if (window.__cfSitekey) return window.__cfSitekey;
            const el = document.querySelector('[data-sitekey]');
            if (el && el.getAttribute('data-sitekey')) return el.getAttribute('data-sitekey');
            for (const f of document.querySelectorAll('iframe')) {
                const src = f.src || '';
                if (src.includes('challenges.cloudflare.com')) {
                    const m = src.match(/(0x[0-9A-Za-z_-]{10,})/);
                    if (m) return m[1];
                }
            }
            return null;
        }""")
    except Exception:
        return None


def _solve_turnstile_capsolver(sitekey, page_url, action=None, cdata=None, max_wait=130):
    """CapSolver 解 Cloudflare Turnstile,返回 token 或 None。"""
    if not CAPSOLVER_API_KEY:
        return None
    try:
        task = {"type": "AntiTurnstileTaskProxyLess", "websiteURL": page_url, "websiteKey": sitekey}
        meta = {}
        if action:
            meta["action"] = action
        if cdata:
            meta["cdata"] = cdata
        if meta:
            task["metadata"] = meta
        resp = requests.post("https://api.capsolver.com/createTask",
                             json={"clientKey": CAPSOLVER_API_KEY, "task": task}, timeout=30)
        data = resp.json()
        if data.get("errorId", 1) != 0:
            print(f"  [capsolver] create error: {data.get('errorDescription', data)}")
            return None
        task_id = data["taskId"]
        print(f"  [capsolver] turnstile task: {task_id}")
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(5)
            r = requests.post("https://api.capsolver.com/getTaskResult",
                              json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}, timeout=30).json()
            st = r.get("status")
            if st == "ready":
                tok = r.get("solution", {}).get("token")
                print(f"  [capsolver] solved (token len={len(tok or '')})")
                return tok
            if st == "failed" or r.get("errorId"):
                print(f"  [capsolver] failed: {r.get('errorDescription', '')}")
                return None
        print("  [capsolver] timeout")
        return None
    except Exception as e:
        print(f"  [capsolver] error: {str(e)[:80]}")
        return None


def _solve_turnstile_yescaptcha(sitekey, page_url, max_wait=150):
    """Solve xAI Turnstile through the shared YesCaptcha client."""
    if not YESCAPTCHA_API_KEY:
        return None
    try:
        from xconsole_client.solver import YesCaptchaSolver

        solver = YesCaptchaSolver(
            YESCAPTCHA_API_KEY,
            endpoint=YESCAPTCHA_API_BASE,
            timeout=max_wait,
            poll_interval=3,
            debug=False,
        )
        token = solver.solve_turnstile(
            website_url=page_url,
            website_key=sitekey,
            premium=True,
            fallback_non_premium=True,
        )
        print(f"  [yescaptcha] solved (token len={len(token or '')})")
        return token
    except Exception as e:
        print(f"  [yescaptcha] error: {str(e)[:120]}")
        return None


def _solve_turnstile_ezcaptcha(sitekey, page_url, max_wait=130):
    """EZ-Captcha 解 Turnstile(备用),返回 token 或 None。"""
    if not EZCAPTCHA_API_KEY:
        return None
    try:
        resp = requests.post(f"{EZCAPTCHA_API_BASE}/createTask", json={
            "clientKey": EZCAPTCHA_API_KEY,
            "task": {"type": "TurnstileTaskProxyless", "websiteURL": page_url, "websiteKey": sitekey},
        }, timeout=30)
        data = resp.json()
        if data.get("errorId", 1) != 0:
            print(f"  [ezcaptcha] create error: {data.get('errorDescription', data)}")
            return None
        task_id = data["taskId"]
        print(f"  [ezcaptcha] turnstile task: {task_id}")
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(5)
            r = requests.post(f"{EZCAPTCHA_API_BASE}/getTaskResult",
                              json={"clientKey": EZCAPTCHA_API_KEY, "taskId": task_id}, timeout=30).json()
            st = r.get("status")
            if st == "ready":
                tok = r.get("solution", {}).get("token")
                print(f"  [ezcaptcha] solved (token len={len(tok or '')})")
                return tok
            if st == "failed" or r.get("errorId"):
                print(f"  [ezcaptcha] failed: {r.get('errorDescription', '')}")
                return None
        print("  [ezcaptcha] timeout")
        return None
    except Exception as e:
        print(f"  [ezcaptcha] error: {str(e)[:80]}")
        return None


async def _inject_turnstile_token(page, token):
    """把打码拿到的 token 灌回页面:调 hook 截获的 callback(让 React 更新 state)+ 写隐藏字段。"""
    try:
        n = await page.evaluate(r"""(token) => {
            let n = 0;
            (window.__cfCb || []).forEach(cb => { try { cb(token); n++; } catch(e){} });
            document.querySelectorAll('input[name="cf-turnstile-response"],textarea[name="cf-turnstile-response"]').forEach(e => {
                try {
                    const proto = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    const set = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    set.call(e, token);
                    e.dispatchEvent(new Event('input', {bubbles: true}));
                    e.dispatchEvent(new Event('change', {bubbles: true}));
                } catch (err) {}
            });
            return n;
        }""", token)
        print(f"  [turnstile] token injected (callbacks fired={n})")
        return True
    except Exception as e:
        print(f"  [turnstile] inject error: {str(e)[:80]}")
        return False


async def ensure_turnstile(page, page_url, passive_s=18):
    """确保拿到 Turnstile token。先被动等 managed/交互式自动过;过不了再上打码平台解+回灌。
    返回是否最终拿到 token。"""
    # 1) 被动等:managed 模式自动过,或交互式复选框点一次
    if await _wait_turnstile(page, max_s=passive_s):
        return True
    # 2) 打码兜底
    if not (YESCAPTCHA_API_KEY or CAPSOLVER_API_KEY or EZCAPTCHA_API_KEY):
        print("  [turnstile] 无打码 key(YESCAPTCHA/CAPSOLVER/EZCAPTCHA),跳过自动解码")
        return False
    sitekey = await _extract_sitekey(page)
    if not sitekey:
        print("  [turnstile] 取不到 sitekey,无法打码")
        return False
    action = await page.evaluate("() => window.__cfAction || null")
    cdata = await page.evaluate("() => window.__cfCdata || null")
    print(f"  [turnstile] solving via captcha service (sitekey={sitekey[:18]}...)")
    loop = asyncio.get_event_loop()
    token = await loop.run_in_executor(
        None, _solve_turnstile_yescaptcha, sitekey, page_url
    )
    if not token:
        token = await loop.run_in_executor(
            None, _solve_turnstile_capsolver, sitekey, page_url, action, cdata
        )
    if not token:
        token = await loop.run_in_executor(None, _solve_turnstile_ezcaptcha, sitekey, page_url)
    if not token:
        print("  [turnstile] 打码失败")
        return False
    await _inject_turnstile_token(page, token)
    # 回灌后等表单 state 更新
    for _ in range(6):
        await asyncio.sleep(1)
        if await _turnstile_token(page):
            return True
    return True  # 已调 callback,即使隐藏字段读不到也认为已灌入,交由提交校验


def rand_password():
    return "Aa1!" + "".join(random.choices(string.ascii_letters + string.digits, k=12))


def register_via_protocol_rt(email, refresh_token, client_id, password, attempts=3):
    """Complete xAI signup with the selected Outlook RT when the browser UI stalls."""
    from register_grok_http import SIGNUP_URL, _find_signup_node, solve_turnstile
    from xconsole_client import XConsoleAuthClient

    for attempt in range(1, attempts + 1):
        client = None
        try:
            print(f"  [protocol-fallback] attempt {attempt}/{attempts}: select complete xAI node")
            if not _find_signup_node():
                continue
            client = XConsoleAuthClient(
                debug=False,
                proxy=proxy_switch.effective_proxy_url(),
                signup_url=SIGNUP_URL,
                impersonate="chrome131",
                timeout=40,
            )
            client.visit_home()
            client.load_signup_page()
            sent_at = time.time()
            sent = client.create_email_validation_code(email)
            if not sent.ok:
                print(f"  [protocol-fallback] xAI send rejected: {sent.trailers}")
                continue
            code = get_code_by_token(
                email,
                refresh_token,
                client_id,
                GROK_SENDER,
                GROK_SUBJECT,
                GROK_CODE_REGEX,
                90,
                5,
                sent_at,
            )
            if not code:
                continue
            verified_code = code
            verified = client.verify_email_validation_code(email, verified_code)
            if not verified.ok:
                verified_code = code.replace("-", "").replace(" ", "")
                verified = client.verify_email_validation_code(email, verified_code)
            if not verified.ok:
                print(f"  [protocol-fallback] verification rejected: {verified.trailers}")
                continue
            try:
                client.validate_password(email, password)
            except Exception:
                pass
            sitekey = client.turnstile_sitekey
            token = solve_turnstile(sitekey, SIGNUP_URL) if sitekey else None
            if not token:
                print("  [protocol-fallback] Turnstile solve failed")
                continue
            first = random.choice(("Alex", "Chris", "Jamie", "Taylor", "Jordan"))
            last = random.choice(("Miller", "Davis", "Wilson", "Moore", "Anderson"))
            result = client.create_account(
                email=email,
                given_name=first,
                family_name=last,
                password=password,
                email_validation_code=verified_code,
                turnstile_token=token,
                castle_request_token="",
                conversion_id=str(uuid.uuid4()),
            )
            if not result.ok:
                print(f"  [protocol-fallback] create failed: {client.extract_signup_error(result.rsc_body)}")
                continue
            sso = client.fetch_sso_token(email=email, password=password, save=False, retries=4)
            if sso:
                print("  [protocol-fallback] account + SSO completed")
                return sso
        except Exception as exc:
            print(f"  [protocol-fallback] error: {str(exc)[:140]}")
        finally:
            if client:
                client.close()
    return None


def save_and_import_grok(sso, email, password, mark_pool=True, oauth_credentials=None):
    from common.session_export import save_grok_token

    save_grok_token(sso, email)
    print("  [OK] grok sso token 已保存")
    if IMPORT_SUB2API:
        from common.token_upload_state import mark_uploaded
        from common.uploaders import upload_sub2api_grok
        ok, msg = upload_sub2api_grok(
            SUB2API_URL,
            SUB2API_EMAIL,
            SUB2API_PASSWORD,
            IMPORT_SUB2API_GROUP or SUB2API_GROK_GROUP,
            sso,
            account_email=email,
            proxy_id=SUB2API_GROK_PROXY_ID,
            local_proxy=proxy_switch.effective_proxy_url(),
            oauth_credentials=oauth_credentials,
        )
        print(f"  [{'OK' if ok else 'FAIL'}] {msg}")
        if not ok:
            print("  [hint] SSO 已保存，可修复配置后运行: python tools/upload_tokens.py grok")
            return False
        mark_uploaded("grok", "sub2api", email)
    if mark_pool:
        email_pool.mark_used(PLATFORM, email, password)
    return True


async def wait_render(page, max_s=70):
    """grok 走代理渲染慢(可达30-40s)，轮询到出现交互元素"""
    for i in range(max_s // 3):
        await asyncio.sleep(3)
        try:
            cnt = await page.evaluate("() => document.querySelectorAll('button,input,textarea,a').length")
        except Exception:
            cnt = 0
        if cnt > 3:
            print(f"  SPA rendered ~{i*3}s (interactive={cnt})")
            return True
    print("  SPA render timeout")
    return False


async def click_any(page, labels, timeout=5000):
    """点任一匹配文本的按钮/链接(日文+英文)"""
    for label in labels:
        try:
            b = page.locator(f'button:has-text("{label}"), a:has-text("{label}"), [role=button]:has-text("{label}")').first
            if await b.count() > 0:
                await b.click(timeout=timeout)
                return label
        except Exception:
            pass
    return None


async def click_email_signup(page, timeout=5000):
    """Click the email registration method across localized xAI pages."""
    clicked = await click_any(page, EMAIL_SIGNUP_BTN, timeout=timeout)
    if clicked:
        return clicked
    choices = page.locator("button, a, [role=button]")
    for index in range(await choices.count()):
        choice = choices.nth(index)
        try:
            if not await choice.is_visible():
                continue
            label = str(await choice.inner_text() or "").strip()
            normalized = label.lower().replace("-", "")
            if "email" not in normalized and "correo electrónico" not in normalized:
                continue
            await choice.click(timeout=timeout)
            return label
        except Exception:
            continue
    return None


async def acquire_browser_grok_oauth(page, sso, email):
    """Approve Device Flow in the authenticated registration browser."""
    if not IMPORT_SUB2API:
        return None
    if os.environ.get("REG_FACTORY_WEBUI_TASK", "").strip() == "1":
        print("  [oauth] WebUI 任务跳过浏览器 Device Flow，直接使用本机 OAuth 导入")
        return None

    from common.grok_oauth import (
        finish_grok_device_flow,
        inspect_grok_account_state,
        start_grok_device_flow,
    )

    proxy = proxy_switch.effective_proxy_url()
    state = await asyncio.to_thread(inspect_grok_account_state, sso, proxy)
    if state.get("denied"):
        details = state.get("bot_flag_details") or "policy=deny,event=$registration"
        print(f"  [oauth] xAI 注册风控拒绝，跳过 Device Flow: {details}")
        return None

    try:
        device = await asyncio.to_thread(start_grok_device_flow, proxy)
        await page.goto(
            device["verification_url"], timeout=45000, wait_until="domcontentloaded"
        )
        allow_labels = [
            "Allow", "Authorize", "Approve", "Accept", "允许", "授权", "同意",
            "許可", "承認",
        ]
        continue_labels = [
            "Continue", "Next", "Confirm", "继续", "下一步", "确认",
            "続ける", "次へ", "確認",
        ]
        approved = False
        for _ in range(15):
            if "/oauth2/device/done" in str(page.url or "").lower():
                approved = True
                break
            clicked = await click_any(page, allow_labels, timeout=1500)
            if clicked:
                approved = True
                await asyncio.sleep(1)
                continue
            clicked = await click_any(page, continue_labels, timeout=1500)
            if clicked:
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(1)
        if not approved:
            raise RuntimeError(f"Device Flow 页面未出现授权按钮: {page.url}")

        credentials, oauth_email = await asyncio.to_thread(
            finish_grok_device_flow,
            proxy,
            device["device_code"],
            device.get("interval") or 2,
            90,
            email,
        )
        if oauth_email and not credentials.get("email"):
            credentials["email"] = oauth_email
        print("  [oauth] 浏览器 Device Flow 已取得 refresh token")
        return credentials
    except Exception as exc:
        print(f"  [oauth] 浏览器 Device Flow 失败，将回退现有导入路径: {str(exc)[:180]}")
        return None


async def _human_click_turnstile(page):
    """用真实鼠标轨迹点 Turnstile 复选框（模拟手动点击）。
    复选框在跨域 iframe 内，但 page.mouse 按视口坐标点击，所以拿 iframe/容器的 bounding box，
    移动到左侧复选框位置（约 left+28、垂直居中）再点，比合成 .click() 更像真人、更易过交互式挑战。"""
    try:
        loc = None
        for sel in ['.cf-turnstile', '[data-sitekey]', 'iframe[src*="challenges.cloudflare.com"]']:
            cand = page.locator(sel).first
            try:
                if await cand.count() > 0 and await cand.is_visible():
                    loc = cand
                    break
            except Exception:
                continue
        if loc is None:
            return False
        box = await loc.bounding_box()
        if not box or box["width"] < 10:
            return False
        # 复选框一般在左侧；垂直居中。带点随机抖动
        tx = box["x"] + min(30, box["width"] * 0.12) + random.uniform(-3, 3)
        ty = box["y"] + box["height"] / 2 + random.uniform(-3, 3)
        # 人类轨迹：先到附近 → 分步靠近 → 停顿 → 按下/抬起
        await page.mouse.move(box["x"] - 40 + random.uniform(0, 20),
                              ty - 25 + random.uniform(0, 15), steps=8)
        await asyncio.sleep(random.uniform(0.2, 0.5))
        await page.mouse.move(tx, ty, steps=random.randint(12, 25))
        await asyncio.sleep(random.uniform(0.15, 0.4))
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.13))
        await page.mouse.up()
        print(f"  [turnstile] human-click @ ({int(tx)},{int(ty)})")
        return True
    except Exception as e:
        print(f"  [turnstile] human-click err: {str(e)[:60]}")
        return False


async def _on_page_challenge(page):
    """grok.com 是否卡在 Cloudflare 页面级挑战（Just a moment / __cf_chl 重定向）。"""
    try:
        if "__cf_chl" in (page.url or ""):
            return True
        has_cf = await page.evaluate(
            "() => !!document.querySelector('iframe[src*=\"challenges.cloudflare.com\"]')")
        n = await page.evaluate("() => document.querySelectorAll('button,a,textarea').length")
        body = ""
        try:
            body = (await page.locator("body").inner_text())[:200].lower()
        except Exception:
            pass
        markers = ("just a moment", "verifying", "checking your browser", "请稍候", "正在验证")
        if (has_cf or any(m in body for m in markers)) and n < 3:
            return True
    except Exception:
        pass
    return False


async def pass_page_challenge(page, tries=3):
    """过 grok.com 页面级 CF 挑战：用真实鼠标点交互式 Turnstile（浏览器自身=节点IP 出 clearance），
    等页面重定向回正常 SPA。页面级挑战的 cf_clearance 必须由真实浏览器在节点 IP 上拿，打码代解
    的 proxyless token 对它无效，所以这里只靠模拟手动点击 + 等待 + 刷新。"""
    for t in range(tries):
        if not await _on_page_challenge(page):
            return True
        print(f"  [page-cf] challenge detected, human-click try {t+1}/{tries}")
        await _human_click_turnstile(page)
        for _ in range(12):  # 等离开挑战页 ~24s
            await asyncio.sleep(2)
            if not await _on_page_challenge(page):
                print("  [page-cf] cleared")
                await asyncio.sleep(2)
                return True
        try:
            await page.reload(timeout=40000, wait_until="domcontentloaded")
        except Exception:
            pass
        await asyncio.sleep(4)
    return not await _on_page_challenge(page)


async def _wait_turnstile(page, max_s=90, human_click=True):
    """等 Cloudflare Turnstile token：hidden input[name=cf-turnstile-response] 有值即过。
    返回是否拿到 token。

    x.ai 多为**托管(managed)模式**：能自动过就让它过；过不了/出现交互式挑战时，**用真实鼠标
    轨迹点一次复选框**（模拟手动点击，见 `_human_click_turnstile`）。token 能否拿到强依赖出口
    IP 信誉——数据中心节点常被判定需挑战；点击过不了的，再由上层 `ensure_turnstile` 走打码兜底。"""
    clicked = False
    deadline = time.time() + max_s
    while time.time() < deadline:
        try:
            val = await page.evaluate(
                "() => { const e=document.querySelector('input[name=\"cf-turnstile-response\"],textarea[name=\"cf-turnstile-response\"]'); return e ? e.value : null; }"
            )
        except Exception:
            val = None
        if val:
            print(f"  [turnstile] passed (token len={len(val)})")
            return True
        # 先给 managed 模式一点自动过的时间(~4s)，仍没过再模拟手动点一次
        if human_click and not clicked and time.time() - (deadline - max_s) > 4:
            if await _human_click_turnstile(page):
                clicked = True
        await asyncio.sleep(2)
    print("  [turnstile] token NOT obtained (IP 可能被 CF 判定需挑战；走打码或换节点)")
    return False


async def dump_state(page, tag=""):
    try:
        info = await page.evaluate("""() => ({
            btns:[...document.querySelectorAll('button')].map(b=>b.innerText.trim()).filter(t=>t).slice(0,15),
            inputs:[...document.querySelectorAll('input,textarea')].map(i=>i.type+'/'+(i.placeholder||i.name||'')),
            url:location.href
        })""")
        print(f"  --- state {tag} ---")
        print(f"    url: {info['url']}")
        print(f"    btns: {info['btns']}")
        print(f"    inputs: {info['inputs']}")
    except Exception as e:
        print(f"  dump_state err: {e}")


async def signup_error_page(page):
    """Return the xAI global error text, or an empty string on a normal signup step."""
    try:
        state = await page.evaluate(r"""() => {
            const text = (document.body?.innerText || '').trim();
            const hasForm = !!document.querySelector(
                'input[name="code"],input[name="givenName"],input[name="familyName"],input[type="password"]'
            );
            const retry = [...document.querySelectorAll('button,a')].some(el =>
                /^(retry|再試行|重试|重試)$/i.test((el.innerText || '').trim())
            );
            const marker = /there was an error loading this page|error loading this page|このページの読み込み中にエラー|页面加载.*错误|頁面載入.*錯誤/i.test(text);
            return {isError: !hasForm && retry && marker, text};
        }""")
        return state["text"][:500] if state.get("isError") else ""
    except Exception:
        return ""


def clash_browser_proxy_fields():
    return proxy_switch.browser_proxy_fields()


async def prelogin_via_direct_browser(email, email_pw, p):
    """在【提交邮箱发码之前】预登录 Outlook：单开 noproxy 窗口登录+过隐私协议+进收件箱。
    返回 (bb, pid, page) 句柄给后续 skip_login 轮询复用；失败返回 (None, None, None)。
    broker 模式不预登录（broker 自管），返回 (None, None, None)。"""
    import os
    if os.environ.get("MAILBOX_BROKER"):
        return None, None, None
    bb = BitBrowser()
    pid = None
    try:
        pid = create_browser_with_retry(
            bb,
            f"mail_{time.strftime('%H%M%S')}",
            browserFingerPrint=grok_browser_fingerprint(),
        )
        if not pid:
            return None, None, None
        bb._post("/browser/update", {
            "id": pid, "proxyMethod": 2, "proxyType": "noproxy",
            "browserFingerPrint": grok_browser_fingerprint(),
        })
        data = None
        for _ in range(8):
            try:
                data = bb.open_browser(pid)
                break
            except Exception:
                await asyncio.sleep(4)
        if not data:
            return None, None, None
        browser = await p.chromium.connect_over_cdp(data["ws"])
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await inject_stealth(ctx, page)
        ok = await prelogin_outlook(page, email, email_pw)
        if ok:
            return bb, pid, page
    except Exception as e:
        print(f"  [mail] prelogin error: {e}")
    # 失败：清理窗口
    if pid:
        try:
            bb.close_browser(pid)
        except Exception:
            pass
        await asyncio.sleep(1)
        try:
            bb.delete_browser(pid)
        except Exception:
            pass
    return None, None, None


async def get_code_via_direct_browser(email, email_pw, p, pre=None):
    """单开一个 noproxy BitBrowser 窗口(本机直连)登录 Outlook 取验证码。
    注册浏览器走代理过 Grok CF，但 Outlook 界面走代理刷不出，故取信用直连。
    pre=(bb,pid,page)：复用 prelogin_via_direct_browser 预登录好的窗口，skip_login 直接轮询。"""
    import os
    if os.environ.get("MAILBOX_BROKER"):
        # broker 模式：委托共享取码服务，不另开浏览器（Grok 用 outlook 注定超时，timeout 调短减少拖累）
        from common.mailbox import fetch_from_broker
        return await fetch_from_broker(
            email, email_pw, GROK_SENDER, GROK_SUBJECT,
            GROK_CODE_REGEX, "code",
            int(os.environ.get("GROK_BROKER_TIMEOUT", "40")),
        )
    # 复用预登录窗口：已在收件箱，skip_login 直接轮询
    if pre and pre[2] is not None:
        bb, pid, page = pre
        try:
            return await get_code_outlook_pw(
                page, email, email_pw,
                sender_hint=GROK_SENDER, subject_hint=GROK_SUBJECT,
                code_regex=GROK_CODE_REGEX,
                max_wait=160, poll=8, skip_login=True,
            )
        except Exception as e:
            print(f"  [mail] reuse prelogin error: {e}")
            return None
        finally:
            if pid:
                try:
                    bb.close_browser(pid)
                except Exception:
                    pass
                await asyncio.sleep(2)
                try:
                    bb.delete_browser(pid)
                except Exception:
                    pass
    bb = BitBrowser()
    pid = None
    try:
        pid = create_browser_with_retry(
            bb,
            f"mail_{time.strftime('%H%M%S')}",
            browserFingerPrint=grok_browser_fingerprint(),
        )
        if not pid:
            return None
        bb._post("/browser/update", {
            "id": pid, "proxyMethod": 2, "proxyType": "noproxy",
            "browserFingerPrint": grok_browser_fingerprint(),
        })
        data = None
        for _ in range(8):
            try:
                data = bb.open_browser(pid)
                break
            except Exception:
                await asyncio.sleep(4)
        if not data:
            return None
        browser = await p.chromium.connect_over_cdp(data["ws"])
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await inject_stealth(ctx, page)
        return await get_code_outlook_pw(
            page, email, email_pw,
            sender_hint=GROK_SENDER, subject_hint=GROK_SUBJECT,
            code_regex=GROK_CODE_REGEX, max_wait=160, poll=8,
        )
    except Exception as e:
        print(f"  [mail] direct browser error: {e}")
        return None
    finally:
        if pid:
            try:
                bb.close_browser(pid)
            except Exception:
                pass
            await asyncio.sleep(2)
            try:
                bb.delete_browser(pid)
            except Exception:
                pass


async def register_one(index, total, p, node):
    start = time.time()

    def check_timeout():
        if time.time() - start > REGISTER_TIMEOUT:
            raise TimeoutError(f"timeout {REGISTER_TIMEOUT}s")

    # 临时邮箱不入 emails.txt 池，mark_* 对它 no-op（避免污染 used/error 记录文件）。
    def _mark_error(reason):
        if temp_mb is None:
            email_pool.mark_error(PLATFORM, email, email_pw, reason)

    def _mark_used():
        if temp_mb is None:
            email_pool.mark_used(PLATFORM, email, email_pw)

    # 邮箱来源三选一：
    #   1) --email 指定固定邮箱（走 Outlook 浏览器取码）
    #   2) GROK_USE_TEMP_EMAIL=true：临时邮箱 HTTP API 取码（免 Outlook 浏览器，快）
    #   3) 默认：emails.txt Outlook 邮箱池（浏览器取码）
    # temp_mb 非 None 表示本号走临时邮箱路径；创建失败会自动回退到邮箱池。
    temp_mb = None
    if USE_LATEST_RT:
        em = email_pool.latest_email(PLATFORM, require_token=True, validate_token=True)
        if not em:
            print("  no unused mailbox with refresh token available")
            return None
        email, email_pw, refresh_token, client_id = em
    elif FIXED_EMAIL:
        email, email_pw, refresh_token, client_id = (
            FIXED_EMAIL, FIXED_PASSWORD, FIXED_REFRESH_TOKEN or "", FIXED_CLIENT_ID or ""
        )
        if refresh_token.strip().lower() == "fresh":
            refresh_token = ""
    elif GROK_USE_TEMP_EMAIL:
        try:
            temp_mb = create_mailbox(provider=TEMP_EMAIL_PROVIDER)
            email = temp_mb["email"]
            email_pw, refresh_token, client_id = "", "", ""
            print(f"  [temp-email] created {temp_mb['provider']} mailbox: {email}")
        except Exception as e:
            print(f"  [temp-email] 创建失败({str(e)[:80]})，回退 emails.txt Outlook")
            temp_mb = None
    if temp_mb is None and not FIXED_EMAIL and not USE_LATEST_RT:
        em = email_pool.next_email(PLATFORM)
        if not em:
            print("  no email available")
            return None
        email, email_pw, refresh_token, client_id = em
    password = rand_password()
    print(f"\n#{index}/{total} email={email}")

    name = f"grok_{time.strftime('%m%d_%H%M%S')}_{index}"
    bb = BitBrowser()
    pid = None
    success = False

    async def _protocol_fallback(reason):
        nonlocal success
        if not refresh_token:
            return None
        print(f"  [protocol-fallback] browser state stalled: {reason}")
        sso = await asyncio.to_thread(
            register_via_protocol_rt,
            email,
            refresh_token,
            client_id,
            password,
        )
        if sso and save_and_import_grok(sso, email, password):
            success = True
            return sso
        return None

    try:
        # BitBrowser 走 Clash 代理。
        pid = create_browser_with_retry(
            bb, name, browserFingerPrint=grok_browser_fingerprint()
        )
        if not pid:
            print("  create browser failed")
            return None
        # 重新用代理配置更新窗口
        bb._post("/browser/update", {
            "id": pid, "name": name,
            **clash_browser_proxy_fields(),
            "browserFingerPrint": grok_browser_fingerprint(),
        })
        data = None
        for _ in range(8):
            try:
                data = bb.open_browser(pid)
                break
            except Exception:
                await asyncio.sleep(4)
        if not data:
            print("  open browser failed")
            return None

        browser = await p.chromium.connect_over_cdp(data["ws"])
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        print(f"  BitBrowser native fingerprint (core={GROK_BROWSER_CORE_VERSION})")
        email_rpc_statuses = []

        def _capture_email_rpc(response):
            if "AuthManagement/CreateEmailValidationCode" in response.url:
                email_rpc_statuses.append(response.status)
                print(f"  [email-rpc] CreateEmailValidationCode HTTP {response.status}")

        page.on("response", _capture_email_rpc)

        # Step 1: 直接打开 xAI 注册页。经 grok.com 的 RSC 跨域跳转会产生 CORS/CF 噪音，
        # 且不提供注册所需状态；账号完成后再回 grok.com 落主域 cookie。
        print("  [1] goto accounts.x.ai signup (via proxy node)")
        for attempt in range(3):
            try:
                await page.goto(GROK_SIGNUP_URL, timeout=60000, wait_until="domcontentloaded")
                break
            except Exception as e:
                print(f"  goto retry {attempt+1}: {str(e)[:50]}")
                await asyncio.sleep(4)
        await wait_render(page)
        # 页面级 CF 挑战（Just a moment/__cf_chl）：模拟手动点击过墙后再等渲染
        if await _on_page_challenge(page):
            await pass_page_challenge(page)
            await wait_render(page, max_s=40)
        check_timeout()

        # 关 cookie 弹窗
        await asyncio.sleep(2)
        dismissed = await click_any(page, COOKIE_DISMISS, timeout=3000)
        if dismissed:
            print(f"  cookie banner dismissed: {dismissed}")
            await asyncio.sleep(2)

        # Step 2: 已在注册方式页，无需再经过 grok.com 的跨域 signup 跳转。
        print("  [2] signup page ready")
        await dump_state(page, "after-signup")
        check_timeout()

        # Step 3: 选 メールで登録 (email signup)
        print("  [3] choose email signup")
        clicked = await click_email_signup(page, timeout=6000)
        if clicked:
            print(f"  clicked: {clicked}")

        # 等邮箱输入框出现：grok 经代理 SPA 渲染慢(可达30-40s)，点完不能立即判定。
        # 坑1：OneTrust Cookie 横幅会遮挡并拦截 '用邮箱注册' 点击，导致页面不跳转——每轮先关横幅。
        # 坑2：横幅里有个隐藏搜索框 input#vendor-search-handler(placeholder=搜索...)，会污染
        #      input[type=text] 兜底选择器、点它直接 30s 超时——故排除它并只取可见的输入框。
        email_sel = ('input[type="email"], input[name="email"], input[autocomplete="email"], '
                     'input[type="text"]:not([name="vendor-search-handler"])'
                     ':not([placeholder*="搜索"]):not([placeholder*="検索"]):not([placeholder*="search" i])'
                     ':not([aria-label*="Cookie"]):not([aria-label*="搜索"])')

        async def _visible_email():
            loc = page.locator(email_sel)
            for j in range(await loc.count()):
                el = loc.nth(j)
                try:
                    if await el.is_visible():
                        return el
                except Exception:
                    pass
            return None

        email_input = None
        for i in range(16):  # ~50s
            await click_any(page, COOKIE_DISMISS, timeout=2000)  # 关 Cookie 横幅（拦截点击）
            email_input = await _visible_email()
            if email_input:
                break
            await asyncio.sleep(3)
            if i in (4, 9):  # 横幅关掉后补点邮箱注册（首次点击可能被横幅吃掉）
                again = await click_email_signup(page, timeout=4000)
                if again:
                    print(f"  re-clicked: {again}")
        await dump_state(page, "email-method")

        # Step 4: 填邮箱
        print("  [4] fill email")
        pre_mail = None   # 预登录的 Outlook 窗口句柄 (bb,pid,page)
        if email_input:
            if not await react_fill(page, email_sel, email):
                print("  [FAIL] React 邮箱输入失败")
                _mark_error("email_react_fill_failed")
                return None
            # 临时邮箱走 HTTP API 取码，无需预登录浏览器；只有 Outlook 路径才预登录。
            # 关键：在提交邮箱（触发 x.ai 发码）【之前】先预登录 Outlook、过隐私协议、进收件箱，
            # 这样发码后立刻能扫到，避免"发码后才登录、登录耗时错过码"（grok 收不到码的根因）。
            if not temp_mb and not refresh_token:
                try:
                    print("  [4] pre-login Outlook (noproxy) before sending code...")
                    pre_mail = await prelogin_via_direct_browser(email, email_pw, p)
                    print(f"  [4] outlook prelogin: {'ready' if pre_mail and pre_mail[2] else 'failed'}")
                except Exception as e:
                    print(f"  [4] prelogin error: {str(e)[:60]}")
            # accounts.x.ai 邮箱提交这一步常带 Turnstile，**不过墙 x.ai 就不发码**
            # （表现为"邮件根本没到"）。检测到 widget 就先过墙，再点 Continue。
            if await _has_turnstile_widget(page):
                print("  [4] 邮箱步检测到 Turnstile，先过墙再提交")
                await ensure_turnstile(page, page.url, passive_s=14)
            code_requested_at = time.time()
            code_ready = False
            submit_limit = BROWSER_MAILBOX_ATTEMPTS if temp_mb else 3
            for submit_try in range(submit_limit):
                await click_any(page, COOKIE_DISMISS, timeout=1500)
                submit = page.locator('form button[type="submit"]').first
                try:
                    if await submit.count() > 0 and await submit.is_visible():
                        print(
                            f"  [4] submit email attempt {submit_try+1}/{submit_limit} "
                            f"disabled={await submit.is_disabled()}"
                        )
                        await submit.click(timeout=6000)
                    else:
                        await click_any(page, CONTINUE_BTN, timeout=5000)
                except Exception as e:
                    print(f"  [4] email submit click failed: {str(e)[:60]}")
                try:
                    await page.locator('input[name="code"]').wait_for(state="visible", timeout=10000)
                    code_ready = True
                    break
                except Exception:
                    pass
                if email_rpc_statuses and email_rpc_statuses[-1] == 403:
                    print("  [4] xAI 发码 RPC 被 Cloudflare 403，停止重复提交")
                    break
                if temp_mb and submit_try + 1 < submit_limit:
                    try:
                        print("  [4] xAI 未接受该临时邮箱域名，自动更换邮箱")
                        temp_mb = create_mailbox(provider=TEMP_EMAIL_PROVIDER)
                        email = temp_mb["email"]
                        print(f"  [temp-email] replacement: {email}")
                        if not await react_fill(page, email_sel, email):
                            print("  [4] replacement mailbox fill failed")
                            break
                        code_requested_at = time.time()
                        continue
                    except Exception as e:
                        print(f"  [4] replacement mailbox create failed: {str(e)[:80]}")
                if await _has_turnstile_widget(page):
                    print("  [4] 仍在邮箱页，重试过墙 + 提交")
                    await ensure_turnstile(page, page.url, passive_s=10)
                email_input = await _visible_email()
                if email_input:
                    await react_fill(page, email_sel, email)
                code_requested_at = time.time()
            if not code_ready:
                try:
                    debug = await page.evaluate(r"""() => ({
                        email: document.querySelector('input[name="email"],input[type="email"]')?.value || '',
                        buttons: [...document.querySelectorAll('button')]
                          .filter(b => b.offsetParent !== null)
                          .map(b => ({text:(b.innerText||'').trim(), type:b.type, disabled:b.disabled}))
                          .slice(0, 10),
                        alerts: [...document.querySelectorAll('[role="alert"],[aria-live]')]
                          .map(e => (e.innerText||'').trim()).filter(Boolean).slice(0, 5)
                    })""")
                    print(f"  [diag] email-submit state: {debug}")
                except Exception as e:
                    print(f"  [diag] email-submit read failed: {str(e)[:80]}")
                print("  [FAIL] 邮箱提交后未进入验证码页，xAI 未发信")
                fallback_sso = await _protocol_fallback("email_submit_stalled")
                if fallback_sso:
                    return fallback_sso
                _mark_error("email_submit_stalled")
                return None
        else:
            print("  email input not found")
            await dump_state(page, "no-email-input")
            _mark_error("no_email_input")
            return None
        await dump_state(page, "after-email")
        check_timeout()

        # Step 5: 邮件验证码
        if temp_mb:
            # 临时邮箱：纯 HTTP API 轮询取码，无需另开浏览器（快 & 稳）。
            print(f"  [5] get code via temp-email API ({temp_mb['provider']}: {email})")
            code = await poll_verification_code(
                temp_mb["id"], temp_mb["provider"], email=email, token=temp_mb.get("token"),
                max_wait=150, poll_interval=5,
                sender_hint=GROK_SENDER, subject_hint=GROK_SUBJECT,
                code_regex=GROK_CODE_REGEX,
            )
        elif refresh_token:
            print("  [5] get verification code via Outlook Graph refresh token")
            code = await asyncio.to_thread(
                get_code_by_token,
                email,
                refresh_token,
                client_id,
                GROK_SENDER,
                GROK_SUBJECT,
                GROK_CODE_REGEX,
                160,
                5,
                code_requested_at,
            )
        else:
            # 只有没有 RT 时才回退到 Outlook 网页取码。
            print("  [5] get verification code via separate noproxy Outlook window")
            code = await get_code_via_direct_browser(email, email_pw, p, pre=pre_mail)

        if not code:
            print("  no code received")
            fallback_sso = await _protocol_fallback("no_code")
            if fallback_sso:
                return fallback_sso
            _mark_error("no_code")
            return None

        if code:
            print(f"  got code: {code}")
            await arm_turnstile_hook(ctx, page)
            # 精确定位验证码框(name=code)，避免误填到搜索框(text/検索)
            ci = page.locator('input[name="code"]').first
            if await ci.count() == 0:
                ci = page.locator('input[inputmode="numeric"], input[autocomplete="one-time-code"]').first
            if await ci.count() == 0:
                # 兜底：排除搜索框的 text input
                ci = page.locator('input[type="text"]:not([placeholder*="検索"]):not([name="vendor-search-handler"])').first
            if await ci.count() > 0:
                # 根因修复：x.ai 验证码框自带格式掩码，会自动补分隔符。若把带 '-' 的
                # 92A-XVR 逐字符敲进去，掩码会再插一个杠→变成 92A--XVR 之类的非法串→
                # 码被拒、会话打回注册方式选择页。故【去掉分隔符】只敲字母数字，掩码自己补。
                code_raw = code.replace("-", "").replace(" ", "")

                async def _fill_code(val):
                    await ci.click()
                    await ci.fill("")
                    await asyncio.sleep(0.3)
                    # 逐字符输入触发 React onChange（fill 直接 setValue 不触发，x.ai 识别不到）
                    await ci.type(val, delay=120)
                    await asyncio.sleep(1.0)
                    # 短超时读回实际值（默认 30s 太长；框可能已因自动提交脱离 DOM）
                    try:
                        return await ci.input_value(timeout=3000)
                    except Exception:
                        return None

                filled = await _fill_code(code_raw)
                print(f"  [code] sent={code_raw!r} box={filled!r}")
                # 读回值去掉分隔符后应与去杠码一致；不一致（掩码没吃/敲串了）→用原始带杠码重试一次
                if filled is not None:
                    norm = filled.replace("-", "").replace(" ", "").upper()
                    if norm != code_raw.upper():
                        print(f"  [code] 掩码不匹配({norm} != {code_raw})，改用带分隔符原码重试")
                        filled = await _fill_code(code)
                        print(f"  [code] retry box={filled!r}")
                # 有的布局敲满自动提交（框已脱离/页面已跳）→ 先看是否已离开验证码框，
                # 没自动跳再点确认按钮；避免 Enter+按钮双提交把有效码打成重复提交。
                await asyncio.sleep(1.5)
                still_code = await page.locator('input[name="code"]').count() > 0
                if still_code:
                    submitted = await click_any(page, VERIFY_BTN, timeout=5000)
                    if not submitted:
                        try:
                            await ci.press("Enter")
                            submitted = "Enter"
                        except Exception:
                            pass
                else:
                    submitted = "auto"  # 敲满自动提交，无需再点
                print(f"  提交验证码按钮: {submitted}")
                await asyncio.sleep(6)
                # [diag] 提交后抓页面报错文本 / 是否被打回注册方式选择页，定位码是否被拒。
                try:
                    err = await page.evaluate(r"""() => {
                        const nodes = [...document.querySelectorAll('[role=alert],[aria-live],[class*=error i],[class*=Error]')];
                        const txt = nodes.map(e => (e.innerText||'').trim()).filter(t => t);
                        const body = (document.body.innerText||'');
                        const bad = ['再試行','もう一度','無効','正しくありません','invalid','incorrect','wrong','expired','失敗','エラー']
                            .filter(k => body.toLowerCase().includes(k.toLowerCase()));
                        const reset = /sign up with (email|x|apple|google)/i.test(body);
                        return {alerts: txt.slice(0,4), markers: bad, backToSignup: reset};
                    }""")
                    if err:
                        print(f"  [diag] after-submit alerts={err.get('alerts')} markers={err.get('markers')} 打回注册页={err.get('backToSignup')}")
                except Exception as e:
                    print(f"  [diag] read error text err: {str(e)[:60]}")
            await dump_state(page, "after-code")
            error_text = await signup_error_page(page)
            if error_text:
                print("  [FAIL] xAI 验码后进入全局错误页，不是 Turnstile："
                      + " | ".join(error_text.splitlines())[:300])
                fallback_sso = await _protocol_fallback("xai_error_after_code")
                if fallback_sso:
                    return fallback_sso
                _mark_error("xai_error_after_code")
                return None
            code_input = page.locator('input[name="code"]').first
            if await code_input.count() > 0 and await code_input.is_visible():
                print("  [FAIL] 验证码提交后仍停在确认页，未进入账号资料表单")
                fallback_sso = await _protocol_fallback("code_submit_stalled")
                if fallback_sso:
                    return fallback_sso
                _mark_error("code_submit_stalled")
                return None
        # Step 6: 完成注册页（x.ai 新流程：givenName/familyName + password + Cloudflare Turnstile + 登録を完了）
        def _rand_word():
            return random.choice("BCDFGHJKLMNPQRST") + "".join(random.choices("aeiou", k=1)) \
                   + "".join(random.choices(string.ascii_lowercase, k=random.randint(3, 6)))

        gname = page.locator('input[name="givenName"]').first
        fname = page.locator('input[name="familyName"]').first
        if await gname.count() > 0:
            first, last = _rand_word().capitalize(), _rand_word().capitalize()
            try:
                await gname.click(); await gname.type(first, delay=60)
                if await fname.count() > 0:
                    await fname.click(); await fname.type(last, delay=60)
                print(f"  [6] name: {first} {last}")
            except Exception as e:
                print(f"  [6] name fill err: {str(e)[:50]}")

        pw_input = page.locator('input[type="password"]').first
        if await pw_input.count() > 0:
            print("  [6] set password")
            try:
                await pw_input.click(); await pw_input.type(password, delay=50)
            except Exception:
                await pw_input.fill(password)
            await asyncio.sleep(1)

        # 等 Turnstile token + 点完成注册：拿到 token 才点（空 token 提交必被拦在原页）。
        # ensure_turnstile = 被动等(managed自动过) → 模拟手动点复选框 → 打码平台解+回灌。
        # 最多 3 轮：仍停在 accounts.x.ai/sign-up 说明没过，重等再点。
        page_url = page.url
        completed = False
        for attempt in range(3):
            has_token = await ensure_turnstile(page, page_url, passive_s=18)
            done = await click_any(page, COMPLETE_BTN, timeout=8000)
            print(f"  [6] complete: btn={done} turnstile={has_token} (attempt {attempt+1}/3)")
            try:
                await page.wait_for_url(
                    lambda url: "/sign-up" not in url, timeout=15000
                )
            except Exception:
                await asyncio.sleep(2)
            cur = page.url
            # 离开 sign-up 页 = 注册推进成功
            if "/sign-up" not in cur:
                completed = True
                break
            await dump_state(page, f"after-complete-{attempt+1}")
            check_timeout()
        if not completed:
            print("  [6] 仍停在 sign-up 页（Turnstile 未过 / 提交被拦）")
        check_timeout()

        # 回到 grok.com 确保 cookie 落到主域
        if "grok.com" not in page.url:
            try:
                await page.goto("https://grok.com/", timeout=45000, wait_until="domcontentloaded")
            except Exception:
                pass
        else:
            await asyncio.sleep(2)
        await dump_state(page, "final")

        key_val, _ = await save_platform_cookies(
            ctx, PLATFORM, pid, email=email, password=password, key_cookie_names=KEY_COOKIES
        )
        if key_val:
            try:
                oauth_credentials = await acquire_browser_grok_oauth(
                    page, key_val, email
                )
                if not save_and_import_grok(
                    key_val,
                    email,
                    password,
                    mark_pool=temp_mb is None,
                    oauth_credentials=oauth_credentials,
                ):
                    return None
            except Exception as e:
                print(f"  [FAIL] 保存/导入 grok token 失败: {e}")
                return None
            success = True
            print("  [OK] session cookie saved")
            return key_val
        else:
            print("  [FAIL] no session cookie")
            _mark_error("no_session_cookie")
            return None

    except Exception as e:
        print(f"  ERROR: {e}")
        if email:
            _mark_error(str(e)[:50])
        return None
    finally:
        if pid:
            keep = KEEP_ON_FAIL and not success
            try:
                bb.close_browser(pid)
            except Exception:
                pass
            await asyncio.sleep(2)
            if not keep:
                try:
                    bb.delete_browser(pid)
                except Exception:
                    pass
            else:
                print(f"  [debug] window kept: {name} (id={pid})")


async def main():
    parser = argparse.ArgumentParser(description="Grok Auto Register")
    parser.add_argument("--count", "-n", type=int, default=1)
    parser.add_argument("--concurrency", "-c", type=int, default=1)
    parser.add_argument("--timeout", "-t", type=int, default=600)
    parser.add_argument("--node", default="auto", help="Clash 出口节点(过grok CF)")
    parser.add_argument("--keep-on-fail", action="store_true")
    parser.add_argument("--email", default=None, help="指定邮箱(绕过邮箱池)")
    parser.add_argument("--password", default=None, help="指定邮箱密码")
    parser.add_argument("--refresh-token", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--client-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--latest-rt", action="store_true",
                        help="从 emails.txt 末尾选择最新未占用且带 RT/client_id 的 Outlook")
    parser.add_argument("--sub2api", action="store_true",
                        help="注册后直接导入 SUB2API Grok 渠道")
    parser.add_argument("--sub2api-group", default="",
                        help="SUB2API Grok 分组名(默认取 SUB2API_GROK_GROUP)")
    parser.add_argument("--mailbox-attempts", type=int, default=6,
                        help="浏览器发码未进入验证码页时更换临时邮箱的次数")
    args = parser.parse_args()

    global REGISTER_TIMEOUT, KEEP_ON_FAIL, FIXED_EMAIL, FIXED_PASSWORD
    global FIXED_REFRESH_TOKEN, FIXED_CLIENT_ID, USE_LATEST_RT
    global IMPORT_SUB2API, IMPORT_SUB2API_GROUP, BROWSER_MAILBOX_ATTEMPTS
    REGISTER_TIMEOUT = args.timeout
    KEEP_ON_FAIL = args.keep_on_fail
    FIXED_EMAIL = args.email
    FIXED_PASSWORD = args.password
    FIXED_REFRESH_TOKEN = args.refresh_token
    FIXED_CLIENT_ID = args.client_id
    USE_LATEST_RT = args.latest_rt
    IMPORT_SUB2API = args.sub2api
    IMPORT_SUB2API_GROUP = args.sub2api_group
    BROWSER_MAILBOX_ATTEMPTS = max(1, args.mailbox_attempts)

    print("=" * 50)
    print(f"  Grok Auto Register  count={args.count} node={args.node}")
    print("=" * 50)

    # 选节点过 grok CF：--node 指定则用它，否则自动探测能过的节点
    try:
        if args.node and args.node.lower() != "auto":
            proxy_switch.set_node(args.node)
            time.sleep(2)
            print(f"  使用指定节点 -> {proxy_switch.current_node()}")
        else:
            print("  自动探测能过 grok CF 的节点...")
            node = proxy_switch.find_working_node(
                test_url=GROK_SIGNUP_URL,
                required_markers=("/_next/static/chunks/", "self.__next_f.push"),
                warmup_url=GROK_URL,
            )
            if not node:
                print("  没找到能过 grok CF 的节点(可能 CF 高防护时段，稍后重试)")
                return
            print(f"  选用节点: {node}")
    except Exception as e:
        print(f"  切节点失败(确认 Clash 在跑): {e}")
        return False

    sem = asyncio.Semaphore(args.concurrency)
    results = []

    async def run_one(i):
        async with sem:
            if i > 1:
                await asyncio.sleep(random.uniform(3, 8) * (i - 1))
            async with async_playwright() as p:
                try:
                    sk = await register_one(i, args.count, p, args.node)
                    results.append(sk)
                except Exception as e:
                    print(f"  #{i} fatal: {e}")
                    results.append(None)

    await asyncio.gather(*[run_one(i) for i in range(1, args.count + 1)])

    ok = sum(1 for r in results if r)
    print(f"\n{'='*50}\n  success: {ok}/{len(results)}\n{'='*50}")
    return ok == args.count


if __name__ == "__main__":
    proxy_switch.apply_platform_environment("grok")
    raise SystemExit(0 if asyncio.run(main()) else 1)
