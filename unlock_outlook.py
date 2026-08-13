# -*- coding: utf-8 -*-
"""
Outlook Account Recovery Script
Unlocks accounts, then extracts and persists Graph refresh tokens in one run.

Usage:
  python unlock_outlook.py --input outlook_accounts/accounts_xxx.txt
  python unlock_outlook.py --input emails_locked.txt --concurrency 2
  python unlock_outlook.py --input outlook_accounts/accounts_xxx.txt --proxy-file proxies.txt
  python unlock_outlook.py   (auto-finds latest locked file)

Input file format (---- separated, one per line):
  email----password
  email----password----any_extra_fields...

Output (unlock_results/):
  unlocked_*.txt          successfully unlocked
  graph_tokens_*.txt      Graph refresh tokens extracted after recovery
  graph_failed_*.txt      recovered accounts whose Graph extraction failed
  needs_phone_*.txt       requires SMS — cannot auto-unlock
  failed_*.txt            failed / timeout
"""

import argparse, asyncio, os, random, re, sys, time
from datetime import datetime

# 顶部加载 .env（真实环境变量优先），保持仓库内无明文凭据
try:
    from config import EZCAPTCHA_API_KEY as _EZCAPTCHA_KEY, EZCAPTCHA_API_BASE as _EZCAPTCHA_BASE
except Exception:
    _EZCAPTCHA_KEY = os.environ.get("EZCAPTCHA_API_KEY", "")
    _EZCAPTCHA_BASE = os.environ.get("EZCAPTCHA_API_BASE", "https://api.ez-captcha.com")

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stdin):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")

import requests
from playwright.async_api import async_playwright

# 与 Outlook 注册共用完整的 PerimeterX 目标定位和拟人按压实现。
# 保证脚本被 importlib 从任意路径加载时也能找到 common 包。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import outlook_press as _outlook_press
from common.browser import open_and_connect, react_fill, teardown
from bitbrowser import selected_browser_provider

# ── Config ───────────────────────────────────────────────────────────
BITBROWSER_API  = os.environ.get("BITBROWSER_API", "http://127.0.0.1:54345")
EZCAPTCHA_KEY   = _EZCAPTCHA_KEY
EZCAPTCHA_BASE  = _EZCAPTCHA_BASE
OUTPUT_DIR      = "unlock_results"
SCREENSHOT_DIR  = "screenshots_unlock"
UNLOCK_TIMEOUT  = 300   # seconds per account


def ensure_clash_proxy_env():
    """Use .env CLASH_PROXY for direct unlock runs, while local APIs stay direct.
    对齐 register_outlook_standalone.py：给 Python 侧(EZCaptcha PX API 等)设 HTTP(S)_PROXY，
    并把 127.0.0.1/localhost 放进 NO_PROXY，避免 BitBrowser 本地 API 也走代理。"""
    existing = (
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        or ""
    ).strip()
    proxy = existing or os.environ.get("CLASH_PROXY", "").strip()
    if not proxy:
        return ""
    if not existing:
        os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = proxy
        os.environ["http_proxy"] = os.environ["https_proxy"] = proxy
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    parts = [p.strip() for p in no_proxy.split(",") if p.strip()]
    for item in ("127.0.0.1", "localhost", "::1"):
        if item not in parts:
            parts.append(item)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(parts)
    return proxy


def _load_default_proxies():
    """住宅代理账密池来自 .env 的 OUTLOOK_PROXIES(多个用换行或逗号分隔)，默认空。
    对齐 register_outlook_standalone.py —— 旧的硬编码 ipwo 池已欠费(402)作废。"""
    raw = os.environ.get("OUTLOOK_PROXIES", "")
    if not raw:
        return []
    parts = [p.strip() for p in raw.replace(",", "\n").splitlines()]
    return [p for p in parts if p and not p.startswith("#")]


DEFAULT_PROXIES = _load_default_proxies()


# ── BitBrowser ───────────────────────────────────────────────────────
_BROWSER_CLIENT = None


def _fingerprint_provider():
    return (
        os.environ.get("FINGERPRINT_BROWSER")
        or os.environ.get("BROWSER_PROVIDER")
        or "bitbrowser"
    ).strip().lower()


def _bb_post(path, data=None):
    global _BROWSER_CLIENT
    if _fingerprint_provider() not in {"bitbrowser", "bit"}:
        if _BROWSER_CLIENT is None:
            from bitbrowser import BitBrowser
            _BROWSER_CLIENT = BitBrowser()
        return _BROWSER_CLIENT._post(path, data or {})
    r = requests.post(f"{BITBROWSER_API}{path}", json=data or {}, timeout=120)
    r.raise_for_status()
    res = r.json()
    if not res.get("success"):
        raise Exception(f"BitBrowser: {res.get('msg', '?')}")
    return res

def _parse_proxy(s):
    if not s: return None
    pt = "http"
    for pfx in ["socks5://", "http://", "https://"]:
        if s.lower().startswith(pfx):
            pt = pfx.split("://")[0]; s = s[len(pfx):]
    s = s.replace(",", "@", 1) if "@" not in s and "," in s else s
    m = re.match(r'^(.+):(.+)@(.+):(\d+)$', s)
    if m:
        return {"type": pt, "username": m.group(1), "password": m.group(2),
                "host": m.group(3), "port": m.group(4)}
    m2 = re.match(r'^(.+):(\d+)$', s)
    if m2:
        return {"type": pt, "host": m2.group(1), "port": m2.group(2)}
    return None

def create_browser(name="unlock", proxy_str=None):
    data = {"name": name, "remark": "outlook unlock",
            "proxyMethod": 2, "browserFingerPrint": {"coreVersion": "130"}}
    p = _parse_proxy(proxy_str)
    if p:
        data.update({"proxyType": p.get("type", "http"),
                     "host": p["host"], "port": p["port"]})
        if p.get("username"): data["proxyUserName"] = p["username"]
        if p.get("password"): data["proxyPassword"] = p["password"]
    else:
        data["proxyType"] = "noproxy"
    return _bb_post("/browser/update", data)["data"]["id"]

def open_browser(pid):
    d = _bb_post("/browser/open", {"id": pid})["data"]
    return d.get("ws") or d.get("webdriver")

def close_browser(pid):
    try: _bb_post("/browser/close", {"id": pid})
    except Exception: pass

def delete_browser(pid):
    try: _bb_post("/browser/delete", {"id": pid})
    except Exception: pass

def cleanup_stale_browsers():
    """启动时清理所有残留的 unlock/scan profile"""
    try:
        page, cleaned = 0, 0
        while True:
            r = _bb_post("/browser/list", {"page": page, "pageSize": 100})
            items = r.get("data", {}).get("list", [])
            if not items: break
            for item in items:
                name = item.get("name", "")
                if any(name.startswith(p) for p in ["unlock_", "scan_", "quick_check"]):
                    close_browser(item["id"])
                    delete_browser(item["id"])
                    cleaned += 1
            total = r.get("data", {}).get("totalNum", 0)
            page += 1
            if page * 100 >= total: break
        if cleaned:
            print(f"[startup] cleaned {cleaned} stale browser profiles")
    except Exception as e:
        print(f"[startup] cleanup error: {e}")


# ── EZCaptcha PX ────────────────────────────────────────────────────
def solve_px(page_url, app_id="PXzC5j78di", max_wait=90):
    try:
        resp = requests.post(f"{EZCAPTCHA_BASE}/createTask", json={
            "clientKey": EZCAPTCHA_KEY,
            "task": {"type": "PerimeterX", "websiteURL": page_url, "websiteKey": app_id}
        }, timeout=30)
        d = resp.json()
        if d.get("errorId", 1) != 0:
            print(f"    [px] error: {d.get('errorDescription', d)}")
            return None
        tid = d["taskId"]
        print(f"    [px] task {tid}")
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(5)
            r2 = requests.post(f"{EZCAPTCHA_BASE}/getTaskResult",
                               json={"clientKey": EZCAPTCHA_KEY, "taskId": tid},
                               timeout=30).json()
            if r2.get("status") == "ready":
                sol = r2.get("solution", {})
                print(f"    [px] solved! keys={list(sol.keys())}")
                return sol
            if r2.get("status") == "failed":
                print("    [px] failed"); return None
        print("    [px] timeout"); return None
    except Exception as e:
        print(f"    [px] error: {e}"); return None


# ── Page state classifier ────────────────────────────────────────────
# 文本标记覆盖 en/zh/ja/fr/es/de/pt/it/ru 等常见 Outlook 出口 UI。但 Clash 节点
# 出口国不定(如日本节点 -> 日文 UI)，纯文本判定必漏 -> classify 返回 unknown 时，
# snap() 再用 DOM 结构(输入框/iframe，与语言无关)兜底，对齐 register 的按 ID 定位思路。
def classify(text, url):
    t, u = text.lower(), url.lower()
    if "account.microsoft.com" in u and "unlock" not in u: return "logged_in"
    if "account.live.com" in u and "proofs" in u:          return "logged_in"
    if "fido/create" in u or "fido/update" in u:           return "fido_setup"
    if any(x in t for x in ["setting up your passkey", "passkey", "clé d'accès",
                             "clave de acceso", "passschlüssel", "パスキー", "密钥", "통행"]):
        return "fido_setup"
    if any(x in t for x in ["your account has been locked", "we've locked",
                              "locked for your protection", "帐户已锁定", "帳戶已鎖定",
                              "アカウントがロックされ", "계정이 잠겼",
                              "compte a été verrouillé", "cuenta ha sido bloqueada",
                              "konto wurde gesperrt", "conta foi bloqueada",
                              "account è stato bloccato", "заблокирована"]):
        return "locked"
    if any(x in t for x in ["let's prove you're human", "press and hold", "按住",
                             "长按", "長按", "押し続け", "누르고"]):
        return "px_challenge"
    if any(x in t for x in ["enter the code", "we texted", "we sent", "verification code",
                              "验证码", "短信", "コード", "인증 코드", "code de vérification",
                              "código de verificación", "bestätigungscode"]):
        return "sms_verify"
    if any(x in t for x in ["verify your identity", "unusual activity", "异常活动",
                             "本人確認", "неполадки"]):
        return "verify_needed"
    if any(x in t for x in ["something went wrong", "出错了", "問題が発生"]): return "error_page"
    if "chrome-error://" in u:      return "net_error"
    if any(x in t for x in ["enter your password", "输入密码", "パスワードを入力",
                             "entrez votre mot de passe", "introduce tu contraseña",
                             "kennwort eingeben"]):
        return "login_form"
    if any(x in t for x in ["email or phone", "sign in", "enter your email",
                             "电子邮件或电话", "メールまたは電話", "サインイン",
                             "이메일 또는 전화", "e-mail ou téléphone", "correo o teléfono",
                             "e-mail oder telefon"]):
        return "email_form"
    return "unknown"


async def _dom_state(page):
    """语言无关的 DOM 结构判定(text classify=unknown 时兜底)。只看元素存在/可见，
    不看文案 —— 与 register 按 input[type=email] / #px-captcha 定位同思路。"""
    try:
        return await page.evaluate(r"""() => {
            const vis = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            };
            const q = sel => [...document.querySelectorAll(sel)].some(vis);
            // PX 按住验证：主文档 #px-captcha 或 hsprotect iframe
            if (q('#px-captcha')) return 'px_challenge';
            for (const f of document.querySelectorAll('iframe')) {
                const src = (f.getAttribute('src') || '').toLowerCase();
                if (src.includes('hsprotect.net') && vis(f)) return 'px_challenge';
            }
            // 一次性验证码输入(SMS/邮箱码)
            if (q('input[name="otc"], #otc, input[autocomplete="one-time-code"]')) return 'sms_verify';
            // 密码输入 = 登录第二步
            if (q('input[type="password"], input[name="passwd"], #passwordEntry')) return 'login_form';
            // 邮箱/账号输入 = 登录第一步
            if (q('input[type="email"], input[name="loginfmt"], #usernameEntry, #i0116')) return 'email_form';
            return 'unknown';
        }""")
    except Exception:
        return "unknown"


async def snap(page, tag, name):
    path = f"{SCREENSHOT_DIR}/{tag}_{name}.png"
    try: await page.screenshot(path=path)
    except Exception: pass
    url = page.url
    try: text = await page.evaluate("() => document.body.innerText")
    except Exception: text = ""
    state = classify(text, url)
    if state == "unknown":
        # 文本没命中(多为非 en/zh 出口 UI) -> DOM 结构兜底
        dom = await _dom_state(page)
        if dom != "unknown":
            state = dom
            print(f"    [{name}] {state}  {url[:60]}  (dom)")
            return state, text
    print(f"    [{name}] {state}  {url[:60]}")
    return state, text


# ── Skip passkey / FIDO setup ────────────────────────────────────────
async def skip_fido(page):
    for sel in ['button:has-text("Cancel")', 'button:has-text("取消")',
                'button:has-text("Skip")',   'button:has-text("Not now")',
                'button:has-text("Maybe later")', 'button:has-text("Do it later")']:
        try:
            btn = page.locator(sel).filter(
                has_not=page.locator('[aria-label="Close"],[data-testid="dismissIcon"]')
            ).first
            if await btn.count() > 0 and await btn.is_visible():
                txt = (await btn.text_content() or "").strip()
                print(f"    skip passkey: '{txt}'")
                await btn.click(timeout=5000)
                return True
        except Exception:
            pass
    try:
        await page.goto("https://account.microsoft.com/", timeout=20000,
                        wait_until="domcontentloaded")
        return True
    except Exception:
        pass
    return False


# ── Core unlock logic ─────────────────────────────────────────────────
async def unlock_account(page, context, email, password, tag):
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    # ── Step 1: Login ────────────────────────────────────────────────
    await page.goto("https://login.live.com/login.srf",
                    timeout=60000, wait_until="domcontentloaded")
    await asyncio.sleep(2)

    net_err_count = 0
    email_stuck = 0   # 连续卡在 email_form 的轮数：微软静默拒绝的死号(填了邮箱点下一步仍回邮箱页)
    for i in range(20):
        state, _ = await snap(page, tag, f"L{i:02d}")
        if state == "logged_in":  return "already_ok"
        if state == "sms_verify": return "needs_phone"
        if state == "fido_setup":
            await skip_fido(page); await asyncio.sleep(4)
            return "unlocked"
        if state in ("locked", "px_challenge"): break
        if state == "net_error":
            net_err_count += 1
            if net_err_count >= 3: return "failed_net_error"
            await page.goto("https://login.live.com/login.srf",
                            timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(3); continue
        # 死号早退：填了邮箱、点了下一步，却连续 6 轮(≈24s)仍停在 email_form 且没跳转 —
        # 说明微软静默拒绝这个号(常见于已封/Abuse 号)，不必空等 300s 超时。
        if state == "email_form":
            email_stuck += 1
            if email_stuck >= 6:
                print(f"    邮箱页卡满 {email_stuck} 轮、账号被静默拒绝，判死号跳过")
                return "dead_account"
        else:
            email_stuck = 0
        if state == "email_form":
            try:
                inp = page.locator('input[type="email"],input[name="loginfmt"]').first
                if await inp.count() > 0:
                    await inp.click(timeout=5000)  # focus input (触发 React SPA 状态)
                    await asyncio.sleep(0.3)
                    committed = await react_fill(
                        page,
                        'input[type="email"],input[name="loginfmt"]',
                        email,
                        tries=3,
                    )
                    if not committed:
                        raise RuntimeError("email input did not commit")
                    await asyncio.sleep(0.5)
                    # 点击 Next/Submit 按钮（稳定 ID 优先，文本 fallback）
                    for sel in ['#idSIButton9', '#iNext', 'button[type="submit"]',
                                'input[type="submit"]']:
                        btn = page.locator(sel).first
                        if await btn.count() > 0 and await btn.is_visible():
                            await btn.click(timeout=5000)
                            break
                    else:
                        await page.keyboard.press("Enter")  # fallback
            except Exception as e:
                print(f"    fill(email) error: {e}")
            await asyncio.sleep(3); continue
        if state == "login_form":
            try:
                pwd = page.locator('input[type="password"]').first
                if await pwd.count() > 0 and not await pwd.input_value():
                    await pwd.click(timeout=5000)
                    await asyncio.sleep(0.3)
                    committed = await react_fill(
                        page, 'input[type="password"]', password, tries=3,
                    )
                    if not committed:
                        raise RuntimeError("password input did not commit")
                    await asyncio.sleep(0.5)
                for sel in ['#idSIButton9', '#iNext', 'button[type="submit"]',
                            'input[type="submit"]']:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click(timeout=5000)
                        break
                else:
                    await page.keyboard.press("Enter")
            except Exception as e:
                print(f"    fill(pwd) error: {e}")
            await asyncio.sleep(3); continue
        for sel in ['#idSIButton9', 'button:has-text("Next")',
                    'input[type="submit"]', 'button[type="submit"]']:
            b = page.locator(sel).first
            if await b.count() > 0 and await b.is_visible():
                await b.click(timeout=8000); await asyncio.sleep(2); break
        else:
            await asyncio.sleep(2)

    state, _ = await snap(page, tag, "L_final")
    if state == "logged_in":  return "already_ok"
    if state == "sms_verify": return "needs_phone"
    if state == "fido_setup":
        await skip_fido(page); await asyncio.sleep(4)
        return "unlocked"

    # ── Step 2: PX press-and-hold + unlock flow ──────────────────────
    press_count   = 0
    max_press     = 5
    no_btn_rounds = 0
    px_api_tried  = False
    net_err_count = 0
    email_stuck   = 0
    last_state    = None
    oscillation   = 0   # locked ↔ error_page 振荡计数(Abuse 页死循环检测)

    for i in range(60):
        state, _ = await snap(page, tag, f"U{i:02d}")

        # Abuse 页 locked/error_page 振荡检测：连续 locked→error→locked→error 说明卡在不可解锁的 Abuse 页
        if state in ("locked", "error_page") and last_state in ("locked", "error_page") and state != last_state:
            oscillation += 1
            if oscillation >= 4:
                print(f"    locked/error_page 振荡 {oscillation} 次(Abuse 页不可解)，放弃")
                return "abuse_locked"
        else:
            oscillation = 0
        last_state = state

        if state == "logged_in":  return "unlocked"
        if state == "sms_verify": return "needs_phone"
        if state == "fido_setup":
            await skip_fido(page); await asyncio.sleep(4)
            return "unlocked"
        # 死号兜底：U 阶段也一直卡 email_form(登录没能推进) -> 判死号，别耗满 60 轮
        if state == "email_form":
            email_stuck += 1
            if email_stuck >= 8:
                print(f"    U 阶段邮箱页卡满 {email_stuck} 轮，判死号跳过")
                return "dead_account"
        else:
            email_stuck = 0
        if state == "error_page":
            tried = False
            for sel in ['button:has-text("Try again")', 'button:has-text("重试")',
                        'button:has-text("再试一次")', 'a:has-text("Try again")']:
                b = page.locator(sel).first
                if await b.count() > 0 and await b.is_visible():
                    try: await b.click(timeout=8000)
                    except Exception: pass
                    await asyncio.sleep(5); tried = True; break
            if not tried:
                await page.go_back(); await asyncio.sleep(3)
            continue
        if state == "net_error":
            net_err_count += 1
            if net_err_count >= 5: return "failed_net_error"
            await page.goto("https://login.live.com/login.srf",
                            timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(3); continue

        if state == "locked":
            for sel in ['button[type="submit"]', 'button:has-text("Next")',
                        'button:has-text("下一步")', 'input[type="submit"]']:
                b = page.locator(sel).first
                if await b.count() > 0 and await b.is_visible():
                    try: await b.click(timeout=8000)
                    except Exception: pass
                    await asyncio.sleep(6); break
            continue

        if state == "px_challenge":
            if press_count < max_press:
                hold_result = await _outlook_press.press_and_hold(
                    page, label="   ", press_number=press_count + 1,
                )
                if hold_result:
                    press_count += 1
                    await asyncio.sleep(random.uniform(3, 6))
                    no_btn_rounds = 0
                else:
                    no_btn_rounds += 1
                    print(f"    no hold target (round {no_btn_rounds})")
                    await asyncio.sleep(3)
            elif not px_api_tried:
                px_api_tried = True
                print("    fallback: EZCaptcha PX API...")
                sol = solve_px(page_url=page.url)
                if sol:
                    for key in ['_pxCaptcha', '_px3', '_px2', '_pxhd', '_pxvid', '_pxde']:
                        if key in sol:
                            await context.add_cookies([{
                                "name": key, "value": str(sol[key]),
                                "domain": ".live.com", "path": "/"
                            }])
                    tok = sol.get("token") or sol.get("uuid")
                    if tok:
                        await page.evaluate(f"""() => {{
                            const h = document.querySelector('input[name="_pxCaptcha"]');
                            if (h) h.value = "{tok}";
                        }}""")
                    await page.reload(timeout=15000); await asyncio.sleep(5)
                else:
                    print("    PX API failed — giving up"); break
            else:
                print("    all PX attempts exhausted"); break
            continue

        # Generic next/submit for intermediate steps
        for sel in ['button[type="submit"]', 'button:has-text("Next")',
                    'button:has-text("下一步")', '#idSIButton9', 'input[type="submit"]']:
            b = page.locator(sel).first
            if await b.count() > 0 and await b.is_visible():
                await b.click(timeout=8000); await asyncio.sleep(4); break
        else:
            await asyncio.sleep(3)

    state, _ = await snap(page, tag, "U_final")
    if state == "logged_in":  return "unlocked"
    if state == "sms_verify": return "needs_phone"
    if state == "fido_setup":
        await skip_fido(page); await asyncio.sleep(4)
        return "unlocked"
    return f"failed_{state}"


# ── Worker ────────────────────────────────────────────────────────────
async def extract_graph_after_recovery(page, context, email, password, idx=0):
    """Use the recovered browser session first, then fall back to HTTP OAuth."""
    from register_outlook_standalone import extract_graph_token

    graph = None
    try:
        graph = await extract_graph_token(page, context, email, password, idx)
    except Exception as exc:
        print(f"  [#{idx}] [graph] browser-session error: {str(exc)[:140]}")

    if not graph or not graph.get("refresh_token"):
        print(f"  [#{idx}] [graph] browser-session extraction failed; trying HTTP fallback")
        from tools.extract_graph_tokens import get_graph_token

        graph = await asyncio.to_thread(get_graph_token, email, password, idx)

    if not graph or not graph.get("refresh_token"):
        return None
    return {
        **graph,
        "email": email,
        "password": password,
        "client_id": graph.get("client_id") or "",
    }


async def worker(accounts, proxy, worker_id, results, graph_attempts, sem):
    async with sem:
        for email, password, raw_line in accounts:
            tag = f"w{worker_id}"
            bb = None
            pid = None
            print(f"\n[worker-{worker_id}] {email}")
            try:
                options = {"proxy_str": proxy} if proxy else {"proxyType": "noproxy"}

                async def run_with_session(pw=None):
                    nonlocal bb, pid
                    bb, pid, _browser, ctx, page = await open_and_connect(
                        name=f"unlock_{worker_id}",
                        p=pw,
                        browser_options=options,
                    )
                    outcome = await asyncio.wait_for(
                        unlock_account(page, ctx, email, password, tag),
                        timeout=UNLOCK_TIMEOUT
                    )
                    print(f"[worker-{worker_id}] {email} => {outcome}")
                    results.append((email, password, raw_line, outcome))
                    if outcome in ("unlocked", "already_ok"):
                        print(f"[worker-{worker_id}] {email} => extracting Graph RT")
                        try:
                            graph = await extract_graph_after_recovery(
                                page, ctx, email, password,
                                f"{worker_id + 1}-{len(graph_attempts) + 1}",
                            )
                        except Exception as exc:
                            print(f"[worker-{worker_id}] {email} => Graph error: {exc}")
                            graph = None
                        graph_attempts.append({
                            "email": email,
                            "password": password,
                            "result": graph,
                        })
                        print(
                            f"[worker-{worker_id}] {email} => Graph "
                            f"{'OK' if graph else 'FAILED'}"
                        )

                if selected_browser_provider() in {"ruyipage", "ruyi", "firefox_bidi"}:
                    await run_with_session()
                else:
                    async with async_playwright() as pw:
                        await run_with_session(pw)

            except asyncio.TimeoutError:
                print(f"[worker-{worker_id}] {email} => timeout")
                results.append((email, password, raw_line, "timeout"))
            except Exception as e:
                print(f"[worker-{worker_id}] {email} => error: {e}")
                results.append((email, password, raw_line, f"error: {str(e)[:80]}"))
            finally:
                if bb is not None and pid:
                    await teardown(bb, pid, delete=True)


# ── File I/O ──────────────────────────────────────────────────────────
def load_accounts(path):
    accounts = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"): continue
            parts = line.split("----")
            if len(parts) >= 2:
                accounts.append((parts[0].strip(), parts[1].strip(), line))
            else:
                print(f"[warn] skip: {line[:60]}")
    return accounts

def scan_all_accounts(statuses=("unlock", "expired", "unknown")):
    """Load recovery candidates from the asset pool, then merge legacy account files."""
    reg_dir = "outlook_accounts"
    unlock_dir = "unlock_results"

    # Collect already-unlocked emails
    unlocked_emails = set()
    if os.path.isdir(unlock_dir):
        for uf in os.listdir(unlock_dir):
            if uf.startswith("unlocked_clean_") and uf.endswith(".txt"):
                with open(os.path.join(unlock_dir, uf), "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.strip().split("----")
                        if parts and parts[0]:
                            unlocked_emails.add(parts[0].lower())

    # Asset scan is authoritative: emails.txt is the primary Outlook pool.
    seen = set()
    accounts = []
    try:
        from common.outlook_recovery import candidate_counts, load_scan_candidates

        candidates = load_scan_candidates(statuses)
    except Exception as exc:
        print(f"[auto] asset recovery candidates unavailable: {str(exc)[:120]}")
        candidates = []
    for item in candidates:
        identity = item["email"].lower()
        if identity in seen:
            continue
        accounts.append((item["email"], item["password"], item["line"]))
        seen.add(identity)
    if candidates:
        counts = candidate_counts(candidates)
        rendered = ", ".join(f"{name}={count}" for name, count in counts.items())
        print(f"[auto] {len(candidates)} recovery candidates from emails.txt ({rendered})")

    # Keep legacy outlook_accounts support for unscanned projects.
    legacy_added = 0
    if os.path.isdir(reg_dir):
        for af in sorted(os.listdir(reg_dir)):
            if af.startswith("accounts_") and af.endswith(".txt"):
                with open(os.path.join(reg_dir, af), "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"): continue
                        parts = line.split("----")
                        if len(parts) >= 2:
                            email_lc = parts[0].lower()
                            if email_lc not in seen and email_lc not in unlocked_emails:
                                accounts.append((parts[0].strip(), parts[1].strip(), line))
                                seen.add(email_lc)
                                legacy_added += 1

    if unlocked_emails:
        print(f"[auto] Skipping {len(unlocked_emails)} already-unlocked accounts")
    print(f"[auto] {len(accounts)} accounts queued ({legacy_added} from legacy {reg_dir}/)")
    return accounts

def _clash_browser_proxy():
    """把 CLASH_PROXY(http://127.0.0.1:7897) 转成 BitBrowser 能用的 host:port 代理串。
    这样浏览器出口走 Clash 当前节点，而不是宿主原始(常为 AWS 机房)IP —— 否则
    login.live.com 被 Cloudflare/PerimeterX 全页拦成空 body，解锁流程根本进不去登录表单。"""
    raw = (os.environ.get("CLASH_PROXY", "") or "").strip()
    if not raw:
        return None
    m = re.match(r'^(?:https?://)?(?:.*@)?([^:/@]+):(\d+)', raw)
    if not m:
        return None
    return f"http://{m.group(1)}:{m.group(2)}"


def load_proxies(path):
    # 显式指定代理文件优先
    if path:
        if not os.path.exists(path):
            return DEFAULT_PROXIES or [_clash_browser_proxy()] or [None]
        proxies = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    proxies.append(line)
        return proxies or DEFAULT_PROXIES or [_clash_browser_proxy()] or [None]
    # 未指定：住宅池(OUTLOOK_PROXIES) > Clash 出口 > 无代理
    if DEFAULT_PROXIES:
        return DEFAULT_PROXIES
    clash = _clash_browser_proxy()
    if clash:
        print(f"[proxy] no residential pool, routing browser via Clash: {clash}")
        return [clash]
    print("[proxy] WARNING: no proxy — browser exits on host IP; "
          "login.live.com may be blocked (blank page). Set CLASH_PROXY or OUTLOOK_PROXIES.")
    return [None]

def save_results(results, ts):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    unlocked   = [r for r in results if r[3] in ("unlocked", "already_ok")]
    needs_ph   = [r for r in results if r[3] == "needs_phone"]
    dead       = [r for r in results if r[3] == "dead_account"]
    abuse      = [r for r in results if r[3] == "abuse_locked"]
    failed     = [r for r in results if r[3] not in ("unlocked", "already_ok", "needs_phone", "dead_account", "abuse_locked")]

    def write(name, rows):
        if not rows:
            return
        p = os.path.join(OUTPUT_DIR, f"{name}_{ts}.txt")
        with open(p, "w", encoding="utf-8") as f:
            for email, password, raw, outcome in rows:
                f.write(f"{raw}----{outcome}\n")
        print(f"  {name:<22s} {len(rows):4d}  -> {p}")

    print(f"\n{'='*55}")
    write("unlocked", unlocked)
    write("needs_phone", needs_ph)
    write("dead_account", dead)
    write("abuse_locked", abuse)
    write("failed", failed)
    print(f"{'─'*55}")
    print(f"  Total     : {len(results)}")
    print(f"  Unlocked  : {len(unlocked)}")
    print(f"  NeedsPhone: {len(needs_ph)}")
    print(f"  DeadAcct  : {len(dead)}")
    print(f"  AbuseLock : {len(abuse)}")
    print(f"  Failed    : {len(failed)}")
    print(f"{'='*55}")

    # Convenience: write just email----password for unlocked accounts
    ok_path = os.path.join(OUTPUT_DIR, f"unlocked_clean_{ts}.txt")
    with open(ok_path, "w", encoding="utf-8") as f:
        for email, password, _, _ in unlocked:
            f.write(f"{email}----{password}\n")
    if unlocked:
        print(f"\n  Clean unlocked list: {ok_path}")

    try:
        from common import asset_scanner

        status_map = {
            "needs_phone": ("unlock", "需要手机验证解锁"),
            "dead_account": ("banned", "Outlook 账号不可用"),
            "abuse_locked": ("banned", "Outlook Abuse 锁定"),
        }
        outcomes = {}
        for email, _password, _raw, outcome in results:
            # The merged recovery task is only complete after Graph succeeds.
            # Keep the previous recoverable status when token extraction fails;
            # upsert_refresh_tokens marks successful accounts normal below.
            if outcome in ("unlocked", "already_ok"):
                continue
            status, detail = status_map.get(outcome, ("unknown", f"解锁结果：{outcome}"))
            outcomes[email.lower()] = {
                "status": status,
                "detail": detail,
                "evidence": f"recovery:unlock:{outcome}",
            }
        asset_scanner.update_cached_outlook_statuses(outcomes)
    except Exception as exc:
        print(f"  [asset] status update skipped: {str(exc)[:100]}")


def save_graph_results(graph_attempts, ts):
    """Persist the Graph stage and update the primary Outlook pool once."""
    if not graph_attempts:
        return {"succeeded": 0, "failed": 0}

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    succeeded = [
        item["result"] for item in graph_attempts
        if item.get("result") and item["result"].get("refresh_token")
    ]
    failed = [
        item for item in graph_attempts
        if not item.get("result") or not item["result"].get("refresh_token")
    ]

    if succeeded:
        path = os.path.join(OUTPUT_DIR, f"graph_tokens_{ts}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            for item in succeeded:
                handle.write("----".join([
                    item["email"],
                    item["password"],
                    item.get("refresh_token", ""),
                    item.get("client_id", ""),
                ]) + "\n")
        print(f"  Graph tokens: {len(succeeded)} -> {path}")

        try:
            from common.outlook_recovery import upsert_refresh_tokens

            update = upsert_refresh_tokens(succeeded)
            print(
                "  Main pool updated: "
                f"updated={update['updated']} appended={update['appended']} "
                f"error_entries_cleared={update['errors_cleared']}"
            )
        except Exception as exc:
            print(f"  [graph] pool update failed: {str(exc)[:120]}")

    if failed:
        path = os.path.join(OUTPUT_DIR, f"graph_failed_{ts}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            for item in failed:
                handle.write(f"{item['email']}----{item['password']}\n")
        print(f"  Graph failed: {len(failed)} -> {path}")

    print(f"  Graph RT  : {len(succeeded)}/{len(graph_attempts)}")
    return {"succeeded": len(succeeded), "failed": len(failed)}


# ── Main ──────────────────────────────────────────────────────────────
def find_latest_input():
    """Auto-find most recent accounts file to unlock."""
    for d, pat in [
        ("check_results",   "locked_for_unlock_"),
        ("outlook_accounts","accounts_"),
    ]:
        if not os.path.isdir(d): continue
        files = sorted(
            [f for f in os.listdir(d) if f.startswith(pat) and f.endswith(".txt")],
            reverse=True
        )
        if files:
            return os.path.join(d, files[0])
    return None

async def run(accounts_or_file, proxies, concurrency):
    if isinstance(accounts_or_file, str):
        accounts = load_accounts(accounts_or_file)
        label = accounts_or_file
    else:
        accounts = accounts_or_file
        label = f"(auto-scanned, {len(accounts)} accounts)"

    if not accounts:
        print("[error] no accounts found"); return

    print(f"Input     : {label}")
    print(f"Accounts  : {len(accounts)}")
    print(f"Concurrency: {concurrency}")
    print(f"Proxies   : {len(proxies)}")

    results = []
    graph_attempts = []
    sem     = asyncio.Semaphore(concurrency)
    chunks  = [[] for _ in range(concurrency)]
    for i, acc in enumerate(accounts):
        chunks[i % concurrency].append(acc)

    await asyncio.gather(*[
        worker(
            chunks[i], proxies[i % len(proxies)], i,
            results, graph_attempts, sem,
        )
        for i in range(concurrency)
        if chunks[i]
    ])

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_results(results, timestamp)
    save_graph_results(graph_attempts, timestamp)

def main():
    parser = argparse.ArgumentParser(
        description="Unlock Outlook accounts and extract Graph refresh tokens",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python unlock_outlook.py --input outlook_accounts/accounts_20260414_124527.txt
  python unlock_outlook.py --input emails_locked.txt --concurrency 2
  python unlock_outlook.py --input emails.txt --proxy-file proxies.txt --concurrency 3
  python unlock_outlook.py                          (auto-recover unlock/expired/missing-RT accounts)
""")
    parser.add_argument("--input", "-i", default=None,
        help="Input file (email----password per line). "
             "Auto-scans outlook_accounts/ and skips already-unlocked if omitted.")
    parser.add_argument("--proxy-file", "-p", default=None,
        help="Proxy list file (one per line)")
    parser.add_argument("--concurrency", "-c", type=int, default=1,
        help="Parallel workers (default: 1)")
    parser.add_argument(
        "--statuses", nargs="+", choices=("unlock", "expired", "unknown", "banned"),
        default=("unlock", "expired", "unknown"),
        help="Asset scan statuses included by auto mode (default: unlock expired unknown)",
    )
    args = parser.parse_args()

    if args.input:
        if not os.path.exists(args.input):
            print(f"[error] file not found: {args.input}")
            sys.exit(1)
        accounts_or_file = args.input
    else:
        # Auto-scan all accounts, skip already unlocked
        accounts_or_file = scan_all_accounts(args.statuses)
        if not accounts_or_file:
            print("[info] No Outlook accounts need unlock or Graph recovery.")
            sys.exit(0)

    proxy_env = ensure_clash_proxy_env()
    if proxy_env:
        print(f"  proxy env ready: {proxy_env}")

    cleanup_stale_browsers()
    proxies = load_proxies(args.proxy_file)
    asyncio.run(run(accounts_or_file, proxies, args.concurrency))

if __name__ == "__main__":
    from common import proxy_switch

    proxy_switch.apply_platform_environment("outlook")
    main()
