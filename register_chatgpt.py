# -*- coding: utf-8 -*-
"""
ChatGPT (OpenAI) 自动注册
复用 common/ 基建: BitBrowser + stealth + Outlook 取验证码 + cookie 保存

流程: chatgpt.com/auth/login -> 填邮箱 -> Continue -> 验证码/密码 -> Arkose -> onboarding -> 保存 cookie

用法:
    python register_chatgpt.py --count 1
    python register_chatgpt.py --count 10 --concurrency 2
"""

import argparse
import asyncio
import contextvars
import functools
import json
import os as _os
import random
import re
import string
import sys
import time
from urllib.parse import urlsplit

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from playwright.async_api import async_playwright

from common.browser import open_and_connect, teardown, human_type, react_fill
from common.mailbox import get_code_by_token, get_code_outlook_pw, prelogin_outlook
from common.cookies import save_platform_cookies
from common import emails as email_pool
from common.temp_email import create_mailbox, poll_verification_code
from common import proxy_switch
from common import node_quarantine

try:
    from config import (
        CHATGPT2API_URL, CHATGPT2API_KEY, CHATGPT_EMAIL_PROVIDER,
        CHATGPT_ENABLE_2FA,
    )
except Exception:
    CHATGPT2API_URL, CHATGPT2API_KEY = "", ""
    CHATGPT_EMAIL_PROVIDER = "pool"
    CHATGPT_ENABLE_2FA = True

try:
    from config import (
        SUB2API_URL, SUB2API_EMAIL, SUB2API_PASSWORD, SUB2API_GROUP,
        CPA_URL, CPA_MGMT_KEY, CODEX_AUTH_URL_SOURCE,
    )
except Exception:
    SUB2API_URL = SUB2API_EMAIL = SUB2API_PASSWORD = ""
    SUB2API_GROUP = "codex"
    CPA_URL = CPA_MGMT_KEY = ""
    CODEX_AUTH_URL_SOURCE = "sub2"

PLATFORM = "chatgpt"
SIGNUP_URL = "https://chatgpt.com/auth/login"
KEY_COOKIES = ["__Secure-next-auth.session-token", "__Secure-next-auth.session-token.0"]
REGISTER_TIMEOUT = 480
KEEP_ON_FAIL = False  # 调试：失败时保留窗口便于排查
FIXED_EMAIL = None
FIXED_PASSWORD = None
FIXED_REFRESH_TOKEN = None
FIXED_CLIENT_ID = None
EMAIL_PROVIDER = "pool"
IMPORT_C2A = False  # 注册成功后即时把 token 导入 chatgpt2api（--import-c2a 开启）
PLUS_SUBSCRIPTION = False  # 注册成功后加入本地 Plus 订阅工作台
C2A_URL = None  # chatgpt2api host（默认取 config.CHATGPT2API_URL）
C2A_KEY = None  # chatgpt2api admin key（默认取 config.CHATGPT2API_KEY）
EXTRACT_CODEX = False  # 注册成功后顺手走 Codex OAuth 提取 rt 导入 SUB2API（--codex 开启）
ENABLE_2FA = CHATGPT_ENABLE_2FA
CODEX_GROUP = None  # SUB2API 目标分组（默认取 config.SUB2API_GROUP）
CODEX_MANUAL_PHONE = False  # add-phone 手动模式（不接码，自己在浏览器填号收码）
CODEX_SMS_PROVIDER = "auto"  # auto / custom / smsman / firefox / hero
CODEX_TIMEOUT = 120  # Codex 授权捕获超时秒
CHATGPT_NODE = "auto"
CHATGPT_COUNTRY = "auto"
ACTIVE_CHATGPT_NODE = None
ACTIVE_CHATGPT_COUNTRY = None
_ACTIVE_CHATGPT_NODE_TASK = contextvars.ContextVar(
    "chatgpt_active_node", default=None
)
_ACTIVE_CHATGPT_COUNTRY_TASK = contextvars.ContextVar(
    "chatgpt_active_country", default=None
)


def _set_active_chatgpt_node(value):
    global ACTIVE_CHATGPT_NODE
    from common.task_context import active_worker

    if active_worker():
        _ACTIVE_CHATGPT_NODE_TASK.set(value)
    else:
        ACTIVE_CHATGPT_NODE = value


def _get_active_chatgpt_node():
    from common.task_context import active_worker

    return _ACTIVE_CHATGPT_NODE_TASK.get() if active_worker() else ACTIVE_CHATGPT_NODE


def _normalize_chatgpt_country(value):
    country = str(value or "auto").strip().upper()
    if country in {"", "AUTO", "ANY"}:
        return "auto"
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("ChatGPT country must be auto or a two-letter ISO code")
    return country


def _set_active_chatgpt_country(value):
    global ACTIVE_CHATGPT_COUNTRY
    from common.task_context import active_worker

    country = _normalize_chatgpt_country(value) if value else None
    if active_worker():
        _ACTIVE_CHATGPT_COUNTRY_TASK.set(country)
    else:
        ACTIVE_CHATGPT_COUNTRY = country


def _get_active_chatgpt_country():
    from common.task_context import active_worker

    return (
        _ACTIVE_CHATGPT_COUNTRY_TASK.get()
        if active_worker()
        else ACTIVE_CHATGPT_COUNTRY
    )


def _chatgpt_country_matches(actual, requested):
    target = _normalize_chatgpt_country(requested)
    actual_country = str(actual or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", actual_country):
        return False
    return target == "auto" or actual_country == target


def _chatgpt_probe_can_defer_to_browser(ok, loc, status, requested):
    """Let the real browser handle a CF 403 when no country is pinned."""
    return (
        not ok
        and status == 403
        and _normalize_chatgpt_country(requested) == "auto"
        and _chatgpt_country_matches(loc, "auto")
    )


def _env_int(name, default):
    raw = _os.environ.get(name, "")
    try:
        return int(raw or default)
    except (TypeError, ValueError):
        print(f"  [config] {name}={raw!r} 无效，使用默认值 {default}")
        return int(default)


# CF 友好节点池。显式配置时按给定顺序使用；auto 默认从 Clash 当前代理组
# 动态读取，避免订阅更新/节点改名后继续尝试已经不存在的历史名称。
CF_NODES = [
    node.strip()
    for node in (_os.environ.get("CHATGPT_CF_NODES") or "").split(",")
    if node.strip()
]
_active_cf_nodes = []
_cf_node_idx = [0]  # 轮换游标


def _order_chatgpt_nodes(candidates):
    """Interleave preferred regions so a small probe budget covers varied exits."""
    region_markers = (
        ("🇯🇵", "日本", "Japan"),
        ("🇸🇬", "新加坡", "Singapore"),
        ("🇰🇷", "韩国", "Korea"),
        ("🇫🇷", "法国", "France"),
        ("🇺🇸", "美国", "United States", "USA"),
        ("🇬🇧", "英国", "United Kingdom"),
        ("🇩🇪", "德国", "Germany"),
        ("🇨🇦", "加拿大", "Canada"),
        ("🇦🇺", "澳大利亚", "Australia"),
        ("🇹🇼", "台湾", "Taiwan"),
    )
    buckets = [[] for _ in region_markers]
    remaining = []
    for node in candidates:
        bucket = next(
            (
                index
                for index, markers in enumerate(region_markers)
                if any(marker.lower() in node.lower() for marker in markers)
            ),
            None,
        )
        if bucket is None:
            remaining.append(node)
        else:
            buckets[bucket].append(node)

    ordered = []
    while any(buckets):
        for bucket in buckets:
            if bucket:
                ordered.append(bucket.pop(0))
    return ordered + remaining


def _discover_chatgpt_nodes():
    """Return real leaf proxies from the configured Clash selector group."""
    from common import proxy_switch
    if proxy_switch.proxy_mode() == "clash_fixed":
        node = proxy_switch.fixed_node()
        if not node:
            raise RuntimeError("固定节点模式需要配置 CLASH_FIXED_NODE")
        return [node]
    import _clash_verge as cv

    api = _os.environ.get("CLASH_API", "http://127.0.0.1:9097")
    secret = _os.environ.get("CLASH_SECRET", "")
    group = _os.environ.get("CLASH_GROUP", "GLOBAL") or "GLOBAL"
    client = cv.ClashClient(api, secret)
    catalog = (client.proxies().get("proxies") or {})
    group_info = catalog.get(group)
    if not group_info:
        group_info = client.group(group)

    group_types = {"selector", "urltest", "fallback", "loadbalance"}
    candidates = []
    for name in group_info.get("all") or []:
        info = catalog.get(name) or {}
        if name in cv.SPECIAL_NAMES or cv.is_fake_node(name):
            continue
        if (info.get("type") or "").lower() in group_types:
            continue
        candidates.append(name)

    if not candidates:
        raise RuntimeError(f"Clash 代理组 {group!r} 中没有可用的叶子节点")
    return _order_chatgpt_nodes(candidates)


def _chatgpt_node_candidates():
    """Resolve and cache the candidate pool shared by preflight and CF rotation."""
    global _active_cf_nodes
    if _active_cf_nodes:
        _active_cf_nodes = node_quarantine.filter_nodes(_active_cf_nodes)
        if not _active_cf_nodes:
            raise RuntimeError("no usable ChatGPT nodes: all candidates are temporarily tainted")
        return list(_active_cf_nodes)

    from common import proxy_switch
    if proxy_switch.proxy_mode() == "residential":
        retries = max(1, _env_int("CHATGPT_RESIDENTIAL_ROTATE_RETRIES", 3))
        _active_cf_nodes = [f"residential-{index + 1}" for index in range(retries)]
        return list(_active_cf_nodes)

    candidates = list(CF_NODES) if CF_NODES else _discover_chatgpt_nodes()
    limit = max(1, _env_int("CHATGPT_NODE_PROBE_LIMIT", 12))
    _active_cf_nodes = node_quarantine.filter_nodes(candidates[:limit])
    _cf_node_idx[0] = 0
    if not _active_cf_nodes:
        raise RuntimeError("no usable ChatGPT nodes: all candidates are temporarily tainted")
    return list(_active_cf_nodes)


async def _is_cf_blocked(page):
    """CF 全页拦截判定：无 email 输入框 且 (页面只有 cf-turnstile 隐藏域 / body 基本空)。"""
    try:
        if await page.locator('input[type="email"], input[name="email"]').count() > 0:
            return False
        body = (await page.locator("body").inner_text()).strip()
        has_ts = await page.locator('input[name="cf-turnstile-response"], .cf-turnstile, iframe[src*=challenges.cloudflare]').count() > 0
        return has_ts or len(body) < 5
    except Exception:
        # reload 中 locator 抛 DOMException：当作仍被拦(还在挑战页)
        return True


async def _click_turnstile(page):
    """尝试点 Turnstile 勾选框（临界 IP 上会降级成可点的 'Verify you are human'）。
    iframe 内 checkbox 优先；不行就按容器坐标点。点到返回 True（不保证过，过没过由调用方轮询判定）。"""
    # 1) challenges.cloudflare iframe 内的 checkbox/label
    for sel in ('iframe[src*=challenges.cloudflare]', 'iframe[src*=turnstile]'):
        try:
            if await page.locator(sel).count() > 0:
                fr = page.frame_locator(sel).first
                for inner in ('input[type=checkbox]', 'label', 'body'):
                    loc = fr.locator(inner)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=3000)
                        return True
        except Exception:
            pass
    # 2) .cf-turnstile 容器左侧勾选框位置（容器内偏左中部）
    try:
        if await page.locator('.cf-turnstile').count() > 0:
            box = await page.locator('.cf-turnstile').first.bounding_box()
            if box:
                await page.mouse.click(box["x"] + 28, box["y"] + box["height"] / 2)
                return True
    except Exception:
        pass
    return False


def _activate_cf_node(node):
    """切换 Clash 节点并断开旧连接，避免新注册会话沿用旧出口。"""
    try:
        if node_quarantine.is_tainted(node):
            print(f"  [node] skip temporarily tainted ChatGPT node: {node}")
            return None
        from common import proxy_switch
        import _clash_verge as cv
        api = _os.environ.get("CLASH_API", "http://127.0.0.1:9097")
        secret = _os.environ.get("CLASH_SECRET", "")
        group = _os.environ.get("CLASH_GROUP", "GLOBAL") or "GLOBAL"
        active = proxy_switch.fixed_node() if proxy_switch.proxy_mode() == "clash_fixed" else node
        proxy_switch.set_node(active, group)
        client = cv.ClashClient(api, secret)
        client.close_connections()
        _set_active_chatgpt_node(active)
        return active
    except Exception as e:
        print(f"  [cf] 切节点失败: {str(e)[:80]}")
        return None


def _taint_active_chatgpt_node(reason):
    """Persist a short cooldown for the route that triggered a browser risk page."""
    node = _get_active_chatgpt_node()
    if not node:
        try:
            node = proxy_switch.current_node()
        except Exception:
            node = None
    if not node or str(node).lower() in {"direct", "residential"}:
        return None
    record = node_quarantine.taint(node, reason=reason)
    if record:
        print(
            f"  [node] tainted for {int(record['expires_at'] - time.time())}s: "
            f"{record['node']} ({record['reason']})"
        )
    return record


def _switch_cf_node():
    """把 Clash 代理组切到候选池中的下一个节点。"""
    from common import proxy_switch
    if proxy_switch.proxy_mode() == "residential":
        result = proxy_switch.rotate_proxy()
        if result.get("ok"):
            if result.get("requires_new_session"):
                print("  [cf] 任务代理已轮换；当前浏览器仍绑定旧端点，需要新建 Profile")
                return None
            active = proxy_switch.current_node()
            _set_active_chatgpt_node(active)
            return active
        return None
    candidates = _chatgpt_node_candidates()
    requested_country = CHATGPT_COUNTRY
    for _ in range(len(candidates)):
        node = candidates[_cf_node_idx[0] % len(candidates)]
        _cf_node_idx[0] += 1
        if not _activate_cf_node(node):
            continue
        time.sleep(2)
        try:
            ok, loc, status = _probe_chatgpt_node()
            matched = ok and _chatgpt_country_matches(loc, requested_country)
            print(
                f"  [node] ChatGPT rotate {node}: HTTP {status} loc={loc} "
                f"{'PASS' if matched else 'SKIP'}"
            )
            if matched:
                _set_active_chatgpt_country(loc)
                return node
        except Exception as exc:
            print(f"  [node] ChatGPT rotate {node}: {str(exc)[:80]}")
    return None


def _switch_cf_node_with_state():
    """Return task-local routing state across an asyncio.to_thread boundary."""
    node = _switch_cf_node()
    return node, _get_active_chatgpt_country()


async def _rotate_chatgpt_auto_node():
    """Rotate an auto Clash route and restore task-local state in this task."""
    requested = str(CHATGPT_NODE or "auto").strip().lower()
    if proxy_switch.proxy_mode() != "clash_auto" or requested != "auto":
        return None
    node, country = await asyncio.to_thread(_switch_cf_node_with_state)
    if node:
        _set_active_chatgpt_node(node)
        if country:
            _set_active_chatgpt_country(country)
    return node


def clash_browser_proxy_fields():
    """把 CLASH_PROXY 转成 BitBrowser/AdsPower profile 代理字段。"""
    from common import proxy_switch
    return proxy_switch.browser_proxy_fields()


def chatgpt_browser_proxy_fields():
    if (CHATGPT_NODE or "auto").strip().lower() in {"none", "off", "direct"}:
        return {"proxyMethod": 1, "proxyType": "noproxy"}
    return clash_browser_proxy_fields()


def chatgpt_network_label():
    if (CHATGPT_NODE or "auto").strip().lower() in {"none", "off", "direct"}:
        return "direct"
    active = _get_active_chatgpt_node()
    if active:
        return str(active)
    try:
        return str(proxy_switch.current_node() or "")
    except Exception:
        return ""


def _probe_chatgpt_node(direct=False):
    """验证当前 Clash 出口能访问 ChatGPT，并返回 Cloudflare 识别地区。"""
    from curl_cffi import requests as creq

    from common import proxy_switch
    proxy = "" if direct else proxy_switch.effective_proxy_url()
    session = creq.Session(impersonate="chrome131", http_version="v2")
    session.trust_env = False
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    try:
        trace = session.get("https://auth.openai.com/cdn-cgi/trace", timeout=15)
        loc = next(
            (line.split("=", 1)[1] for line in trace.text.splitlines() if line.startswith("loc=")),
            "?",
        )
        response = session.get(SIGNUP_URL, allow_redirects=True, timeout=25)
        body = response.text[:100000].lower()
        blocked = (
            response.status_code != 200
            or "unsupported_country_region_territory" in body
            or "just a moment" in body
            or "performing security verification" in body
        )
        return not blocked, loc, response.status_code
    finally:
        session.close()


def select_chatgpt_node(requested, allow_blocked=False, country="auto"):
    """注册开始前选定一个节点；账号会话建立后不再静默换出口。"""
    global ACTIVE_CHATGPT_NODE
    value = (requested or "auto").strip()
    requested_country = _normalize_chatgpt_country(country)
    _set_active_chatgpt_country(None)
    from common import proxy_switch
    mode = proxy_switch.proxy_mode()
    explicit_direct = value.lower() in {"none", "off", "direct"}
    if mode == "direct" or explicit_direct:
        ok, loc, status = _probe_chatgpt_node(direct=True)
        if not ok or not _chatgpt_country_matches(loc, requested_country):
            raise RuntimeError(
                f"direct exit country mismatch: requested={requested_country}, "
                f"actual={loc}, HTTP {status}"
            )
        _set_active_chatgpt_country(loc)
        _set_active_chatgpt_node(None)
        print(f"  [node] ChatGPT direct exit verified: {loc}")
        return None
    if mode == "residential" and value.lower() == "auto":
        retries = max(1, _env_int("CHATGPT_RESIDENTIAL_ROTATE_RETRIES", 3))
        last = ""
        for attempt in range(retries):
            try:
                ok, loc, status = _probe_chatgpt_node()
                last = f"loc={loc} HTTP {status}"
                if ok and _chatgpt_country_matches(loc, requested_country):
                    _set_active_chatgpt_country(loc)
                    _set_active_chatgpt_node(None)
                    print(f"  [node] ChatGPT residential exit verified: {loc}")
                    return None
                if _chatgpt_probe_can_defer_to_browser(
                    ok, loc, status, requested_country
                ):
                    _set_active_chatgpt_country(loc)
                    _set_active_chatgpt_node(None)
                    print(
                        "  [node] ChatGPT residential preflight HTTP 403 "
                        f"(loc={loc}); auto country defers challenge to browser"
                    )
                    return None
            except Exception as exc:
                last = str(exc)
            if attempt + 1 < retries:
                result = proxy_switch.rotate_proxy()
                print(
                    f"  [node] residential country mismatch; rotate "
                    f"({attempt + 1}/{retries}): {result.get('node') or 'failed'}"
                )
        raise RuntimeError(
            f"no residential exit matched country {requested_country}: {last}"
        )
    if mode == "clash_fixed":
        fixed = proxy_switch.fixed_node()
        if not fixed:
            raise RuntimeError("固定节点模式需要配置 CLASH_FIXED_NODE")
        candidates = [fixed]
        print(f"  [node] ChatGPT 使用固定 Clash 节点: {fixed}")
    elif value.lower() == "auto":
        candidates = _chatgpt_node_candidates()
        print(f"  [node] ChatGPT auto 从 Clash 读取 {len(candidates)} 个候选节点")
    else:
        candidates = [value]
    last_error = ""
    activated = []
    for index, node in enumerate(candidates):
        if not _activate_cf_node(node):
            continue
        activated.append(node)
        time.sleep(2)
        try:
            ok, loc, status = _probe_chatgpt_node()
            country_ok = _chatgpt_country_matches(loc, requested_country)
            outcome = "PASS" if ok and country_ok else "COUNTRY" if ok else "BLOCK"
            print(f"  [node] ChatGPT probe {node}: HTTP {status} loc={loc} {outcome}")
            if ok and country_ok:
                _cf_node_idx[0] = index + 1
                _set_active_chatgpt_country(loc)
                return node
        except Exception as e:
            last_error = str(e)
            print(f"  [node] ChatGPT probe {node}: {last_error[:80]}")
    if allow_blocked and activated and requested_country == "auto":
        fallback = activated[0]
        _activate_cf_node(fallback)
        print(f"  [node] 无 Cookie 预检均被拦，OAuth 使用已有登录态继续验证: {fallback}")
        return fallback
    country_note = "" if requested_country == "auto" else f" country={requested_country}"
    raise RuntimeError(f"no usable ChatGPT node{country_note}: {last_error or value}")


def ensure_chatgpt_worker_country():
    """Verify a worker's final proxy before its browser profile is created."""
    requested = _normalize_chatgpt_country(CHATGPT_COUNTRY)
    from common import proxy_switch

    mode = proxy_switch.proxy_mode()
    explicit_direct = (CHATGPT_NODE or "auto").strip().lower() in {
        "none",
        "off",
        "direct",
    }
    attempts = (
        max(1, _env_int("CHATGPT_RESIDENTIAL_ROTATE_RETRIES", 3))
        if mode == "residential" and not explicit_direct
        else 1
    )
    last = ""
    for attempt in range(attempts):
        ok, loc, status = _probe_chatgpt_node(
            direct=explicit_direct or mode == "direct"
        )
        last = f"loc={loc} HTTP {status}"
        if ok and _chatgpt_country_matches(loc, requested):
            _set_active_chatgpt_country(loc)
            return loc
        if _chatgpt_probe_can_defer_to_browser(ok, loc, status, requested):
            _set_active_chatgpt_country(loc)
            print(
                "  [node] ChatGPT worker preflight HTTP 403 "
                f"(loc={loc}); auto country defers challenge to browser"
            )
            return loc
        if explicit_direct or mode != "residential" or attempt + 1 >= attempts:
            break
        result = proxy_switch.rotate_proxy()
        if not result.get("ok") or not result.get("changed"):
            break
    raise RuntimeError(f"worker exit did not match country {requested}: {last}")


def assert_chatgpt_node(stage):
    """检测其他任务是否在注册中途改了 GLOBAL 出口。"""
    expected = _get_active_chatgpt_node()
    if not expected:
        return
    from common import proxy_switch

    current = proxy_switch.current_node()
    if current != expected:
        raise RuntimeError(
            f"chatgpt_node_changed:{stage}: expected={expected}, current={current}"
        )


class OnboardingRejected(RuntimeError):
    pass


class EmailVerificationRetryNeeded(RuntimeError):
    pass


def _classify_chatgpt_password_step(
    url: str = "",
    body: str = "",
    autocomplete: str = "",
    input_name: str = "",
) -> str:
    """Classify a visible OpenAI password form without relying on its locale.

    The signup flow currently has both ``create-account/password`` and
    ``log-in/password`` variants.  A password input by itself is ambiguous,
    especially while the SPA keeps the previous form mounted, so explicit
    route/autocomplete/login copy wins over the signup fallback.
    """
    haystack = " ".join(
        str(value or "").strip().lower()
        for value in (url, body, autocomplete, input_name)
    )
    login_markers = (
        "/log-in/password",
        "/login/password",
        "current-password",
        "sign in to your account",
        "sign in to chatgpt",
        "登录你的账户",
        "登入你的帳戶",
        "se connecter",
    )
    if any(marker in haystack for marker in login_markers):
        return "login"
    return "create"


async def detect_chatgpt_password_step(page) -> str:
    """Return ``create``, ``login`` or ``none`` for the visible password step."""
    try:
        locator = page.locator(
            'input[type="password"], input[name="password"], '
            'input[name="new-password"]'
        )
        count = await locator.count()
        visible = None
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible():
                    visible = candidate
                    break
            except Exception:
                continue
        if visible is None:
            return "none"
        attrs = []
        for name in ("autocomplete", "name", "placeholder", "aria-label"):
            try:
                attrs.append(await visible.get_attribute(name) or "")
            except Exception:
                attrs.append("")
        try:
            body = await page.locator("body").inner_text(timeout=2000)
        except Exception:
            body = ""
        return _classify_chatgpt_password_step(
            getattr(page, "url", ""), body, attrs[0], " ".join(attrs[1:])
        )
    except Exception:
        return "none"


def _openai_error_from_text(text, status=0, url=""):
    raw = (text or "").strip()
    lower = raw.lower()
    region_markers = (
        "unsupported_country_region_territory",
        "country, region, or territory not supported",
        "not available in your country",
        "你的国家和地区不提供服务",
        "您的國家或地區不受支援",
    )
    if any(marker in lower for marker in region_markers):
        return {
            "code": "unsupported_country_region_territory",
            "message": "Country, region, or territory not supported",
            "status": status,
            "url": url,
        }
    if status < 400:
        return None
    try:
        payload = json.loads(raw)
        error = payload.get("error", payload) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            return {
                "code": str(error.get("code") or error.get("type") or f"http_{status}"),
                "message": str(error.get("message") or raw[:180]),
                "status": status,
                "url": url,
            }
    except Exception:
        pass
    return {"code": f"http_{status}", "message": raw[:180], "status": status, "url": url}


def _is_chatgpt_auth_script(url):
    try:
        parsed = urlsplit(str(url or ""))
        return (
            parsed.hostname == "chatgpt.com"
            and parsed.path.startswith("/cdn/assets/")
            and parsed.path.endswith(".js")
        )
    except Exception:
        return False


class ChatGPTAuthAssetMonitor:
    """Track whether the server-rendered login shell actually loaded its JS."""

    def __init__(self):
        self.loaded = set()
        self.failed = []

    def observe_response(self, response):
        if not _is_chatgpt_auth_script(response.url):
            return
        if response.status < 400:
            self.loaded.add(urlsplit(response.url).path)
        else:
            self.failed.append(f"HTTP {response.status}")

    def observe_failure(self, request):
        if _is_chatgpt_auth_script(request.url):
            self.failed.append(str(request.failure or "request failed")[:80])

    def attach(self, page):
        page.on("response", self.observe_response)
        page.on("requestfailed", self.observe_failure)


async def _chatgpt_email_form_hydrated(page):
    """Check that React attached handlers to the server-rendered login form."""
    try:
        button = page.locator('button[type="submit"]').first
        if await button.count() == 0:
            button = page.get_by_role("button", name="Continue", exact=True).first
        if await button.count() == 0:
            return False
        return bool(
            await button.evaluate(
                """element => {
                    const nodes = [
                        element,
                        element.form,
                        element.closest('[data-reactroot], #root, #__next'),
                        document.body,
                        document.documentElement,
                    ].filter(Boolean);
                    return nodes.some(node => Object.getOwnPropertyNames(node).some(key =>
                        key.startsWith('__reactProps$') ||
                        key.startsWith('__reactFiber$') ||
                        key.startsWith('__reactContainer$') ||
                        key.startsWith('_reactListening')
                    ));
                }"""
            )
        )
    except Exception:
        return False


async def wait_for_chatgpt_auth_assets(page, monitor, timeout=12, fallback_after=2):
    """Require loaded auth bundles and a hydrated form before interacting."""
    deadline = time.monotonic() + max(1, timeout)
    rendered_ready_since = None
    while time.monotonic() < deadline:
        if len(monitor.failed) >= 3:
            return False
        if monitor.loaded:
            try:
                email = page.locator('input[type="email"], input[name="email"]').first
                email_visible = await email.count() > 0 and await email.is_visible()
                if email_visible:
                    if await _chatgpt_email_form_hydrated(page):
                        return True
                    if rendered_ready_since is None:
                        rendered_ready_since = time.monotonic()
                    elif (
                        not monitor.failed
                        and time.monotonic() - rendered_ready_since >= max(0, fallback_after)
                    ):
                        print(
                            "  [1] React marker unavailable; "
                            "loaded auth JS and visible email form accepted"
                        )
                        return True
                else:
                    rendered_ready_since = None
            except Exception:
                pass
        await asyncio.sleep(0.4)
    return False


async def _try_pass_chatgpt_turnstile(page, rounds=4, wait=4):
    for _ in range(rounds):
        if not await _is_cf_blocked(page):
            return True
        await _click_turnstile(page)
        await asyncio.sleep(wait)
    return not await _is_cf_blocked(page)


class AuthResponseMonitor:
    """Collect auth failures without printing tokens or query strings.

    ``CHATGPT_DEBUG_AUTH=1`` additionally records redacted request events. This
    is opt-in because a stalled SPA hand-off can fail during a GET/navigation.
    """

    def __init__(self):
        self.errors = []
        self._tasks = []
        self.debug = str(_os.environ.get("CHATGPT_DEBUG_AUTH", "")).strip().lower() in {
            "1", "true", "yes", "on"
        }
        self.events = []
        self.failures = []

    def observe(self, response):
        try:
            parsed = urlsplit(response.url)
            hostname = (parsed.hostname or "").lower()
            if hostname not in {"auth.openai.com", "chatgpt.com"}:
                return
            method = response.request.method.upper()
            if self.debug and len(self.events) < 160:
                path = parsed.path or "/"
                if hostname == "auth.openai.com" or path.startswith(("/auth", "/api")):
                    self.events.append(f"{method} {hostname}{path} -> {response.status}")
            if method != "POST" or hostname != "auth.openai.com":
                return
            if not any(
                marker in parsed.path.lower()
                for marker in ("account", "onboarding", "about-you", "email-verification")
            ):
                return
            self._tasks.append(asyncio.create_task(self._record(response, parsed.path)))
        except Exception:
            pass

    def observe_failure(self, request):
        if not self.debug:
            return
        try:
            parsed = urlsplit(request.url)
            hostname = (parsed.hostname or "").lower()
            if hostname not in {"auth.openai.com", "chatgpt.com"}:
                return
            path = parsed.path or "/"
            if hostname == "auth.openai.com" or path.startswith(("/auth", "/api")):
                failure = str(request.failure or "request failed")[:120]
                if len(self.failures) < 80:
                    self.failures.append(
                        f"{request.method.upper()} {hostname}{path} !! {failure}"
                    )
        except Exception:
            pass

    async def _record(self, response, path):
        try:
            text = await response.text()
        except Exception:
            text = ""
        error = _openai_error_from_text(text, response.status, path)
        if error:
            self.errors.append(error)

    async def _drain(self):
        tasks, self._tasks = self._tasks, []
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def clear(self):
        await self._drain()
        self.errors.clear()

    async def latest(self):
        await self._drain()
        return self.errors[-1] if self.errors else None


    async def debug_summary(self):
        await self._drain()
        if not self.debug:
            return ""
        parts = []
        if self.events:
            parts.append("responses=" + " | ".join(self.events[-24:]))
        if self.failures:
            parts.append("failed=" + " | ".join(self.failures[-12:]))
        if self.errors:
            parts.append("auth_errors=" + " | ".join(
                f"{e.get('code')}:{e.get('status')}:{e.get('url')}"
                for e in self.errors[-4:]
            ))
        return " ; ".join(parts) or "no auth requests observed"


async def is_email_verification_route_error(page):
    """Detect the auth UI error returned when a JSON route responds with HTML."""
    try:
        body = (await page.locator("body").inner_text(timeout=2500)).strip().lower()
    except Exception:
        return False
    return "route error" in body and (
        "invalid content type" in body or "text/html" in body
    )


async def email_verification_succeeded(page):
    """Recognize accepted-code screens that remain on the verification URL."""
    current_url = (page.url or "").lower()
    if any(marker in current_url for marker in ("about-you", "onboarding")):
        return True
    if "chatgpt.com" in current_url and "/auth/" not in current_url:
        return True
    try:
        body = (await page.locator("body").inner_text(timeout=2500)).strip().lower()
    except Exception:
        return False
    return any(marker in body for marker in (
        "email verified",
        "email has already been verified",
        "email has been verified",
        "email berhasil diverifikasi",
        "email telah diverifikasi",
        "邮箱已验证",
        "郵箱已驗證",
        "メールアドレスは確認済み",
    ))


_VERIFICATION_SUBMIT_LABELS = [
    "Continue", "続行", "Verify", "確認", "确认", "继续", "Submit", "次へ",
    "Teruskan", "Sahkan", "Lanjutkan",
]
_VERIFICATION_RETRY_LABELS = [
    "Retry", "重试", "重試", "再试一次", "再試行", "Réessayer", "Erneut versuchen",
]


async def _fill_and_submit_email_code(
    page,
    code_sel,
    code,
    *,
    tries=3,
    verbose=True,
    submit_method="click",
):
    code_input = page.locator(code_sel).first
    if await code_input.count() == 0:
        return False
    if not await react_fill(page, code_sel, code, tries=tries, verbose=verbose):
        print("  [4] code fill not committed after retries")
        return False
    try:
        if (await code_input.input_value()).strip() != str(code).strip():
            print("  [4] code value changed before submit")
            return False
    except Exception:
        return False

    if submit_method == "request_submit":
        try:
            submitted = await code_input.evaluate(
                """node => {
                    const form = node.form || node.closest('form');
                    if (!form) return false;
                    const submitter = [...form.querySelectorAll(
                        'button[type="submit"], input[type="submit"]'
                    )].find(element => !element.disabled);
                    if (typeof form.requestSubmit === 'function') {
                        form.requestSubmit(submitter || undefined);
                    } else {
                        form.submit();
                    }
                    return true;
                }"""
            )
            if submitted:
                print("  [4] submitted verification through form.requestSubmit()")
                return True
        except Exception as error:
            if verbose:
                print(f"  [4] requestSubmit failed: {str(error)[:80]}")
        return False

    if submit_method == "enter":
        try:
            await code_input.press("Enter", timeout=5000)
            print("  [4] submitted verification with Enter")
            return True
        except Exception as error:
            if verbose:
                print(f"  [4] keyboard submit failed: {str(error)[:80]}")
            return False

    if await click_any_exact(page, _VERIFICATION_SUBMIT_LABELS):
        print("  [4] clicked verification Continue")
        return True
    submit = page.locator('button[type="submit"]:not([disabled])').first
    if await submit.count() == 0:
        return False
    try:
        await submit.click(timeout=8000)
        print("  [4] clicked verification submit button")
        return True
    except Exception:
        return False


async def _raise_email_verification_error(auth_monitor):
    if not auth_monitor:
        return
    error = await auth_monitor.latest()
    if not error:
        return
    print(
        f"  [4] verification service rejected: code={error['code']} "
        f"status={error['status']} path={error['url']} "
        f"message={error['message'][:120]}"
    )
    rejection = f"{error['code']} {error['message']}".lower()
    if any(marker in rejection for marker in ("invalid", "expired", "incorrect", "code")):
        raise EmailVerificationRetryNeeded(
            f"email_verification_rejected:{error['code']}:{error['message'][:80]}"
        )
    raise RuntimeError(
        f"email_verification_rejected:{error['code']}:{error['message'][:80]}"
    )


async def submit_email_verification_code(
    page, code_sel, code, route_retries=2, auth_monitor=None
):
    """Submit an email code and recover the transient auth HTML route error."""
    if auth_monitor:
        await auth_monitor.clear()
    if not await _fill_and_submit_email_code(page, code_sel, code):
        raise RuntimeError("email_verification_form_unavailable")
    await asyncio.sleep(5)
    await dump_state(page, "after-code")
    if await email_verification_succeeded(page):
        print("  [4] email verification accepted")
        return

    for attempt in range(route_retries):
        if not await is_email_verification_route_error(page):
            break
        print(
            "  [4] verification route returned HTML; "
            f"clicking Retry ({attempt + 1}/{route_retries})..."
        )
        if not await click_any_exact(page, _VERIFICATION_RETRY_LABELS):
            break
        await asyncio.sleep(5)
        if not any(
            marker in page.url.lower()
            for marker in ("verification", "verify", "email-verification")
        ):
            break
        if await page.locator(code_sel).first.count() > 0:
            await _fill_and_submit_email_code(
                page, code_sel, code, tries=2, verbose=False
            )
            await asyncio.sleep(5)
        await dump_state(page, f"after-code-route-retry-{attempt + 1}")
        if await email_verification_succeeded(page):
            print("  [4] email verification accepted")
            return

    if await is_email_verification_route_error(page):
        raise RuntimeError(
            "email_verification_route_error: auth route kept returning HTML after Retry"
        )
    await _raise_email_verification_error(auth_monitor)

    if any(
        marker in page.url.lower()
        for marker in ("verification", "verify", "email-verification")
    ):
        code_input = page.locator(code_sel).first
        if await code_input.count() == 0:
            if await email_verification_succeeded(page):
                print("  [4] email verification accepted")
                return
            raise EmailVerificationRetryNeeded(
                "email_verification_form_disappeared_without_success"
            )
        try:
            if not (await code_input.input_value()).strip():
                raise EmailVerificationRetryNeeded(
                    "email_verification_code_cleared_after_submit"
                )
        except EmailVerificationRetryNeeded:
            raise
        except Exception:
            pass
        print("  [4] verification click did not advance; using native form submit...")
        if auth_monitor:
            await auth_monitor.clear()
        if not await _fill_and_submit_email_code(
            page,
            code_sel,
            code,
            tries=2,
            verbose=False,
            submit_method="request_submit",
        ):
            raise RuntimeError("email_verification_submit_unavailable")
        await asyncio.sleep(5)
        await dump_state(page, "after-code-retry")
        if await email_verification_succeeded(page):
            print("  [4] email verification accepted")
            return
        await _raise_email_verification_error(auth_monitor)
        if any(
            marker in page.url.lower()
            for marker in ("verification", "verify", "email-verification")
        ):
            raise EmailVerificationRetryNeeded(
                "email_verification_stuck_after_submit"
            )


# OpenAI 发件人 / 验证码邮件特征
OAI_SENDER = ("openai.com", "noreply@", "no-reply@")
OAI_SUBJECT = ("code", "verify", "verification", "openai", "chatgpt", "confirm")


def rand_password():
    return "Aa1!" + "".join(random.choices(string.ascii_letters + string.digits, k=12))


# 常见英文名/姓，短且自然（比随机字母串更像真人，键入也快）
_FIRST_NAMES = ["James", "Mary", "John", "Anna", "David", "Laura", "Mike", "Emma",
                "Chris", "Sara", "Paul", "Lucy", "Mark", "Nina", "Tom", "Kate",
                "Alex", "Ella", "Sam", "Lily", "Ben", "Zoe", "Leo", "Ruby"]
_LAST_NAMES = ["Smith", "Jones", "Brown", "Davis", "Evans", "Clark", "Hall", "Lee",
               "Walker", "Young", "King", "Wright", "Green", "Baker", "Adams", "Carter",
               "Reed", "Cook", "Bell", "Ward", "Gray", "Hughes", "Price", "Wood"]


def rand_name():
    first = random.choice(_FIRST_NAMES)
    last = random.choice(_LAST_NAMES)
    return first, last


async def dump_state(page, tag=""):
    """打印当前页面状态，便于首跑适配"""
    try:
        print(f"  --- state {tag} ---")
        print(f"  url: {page.url}")
        n = await page.locator("input").count()
        for i in range(min(n, 6)):
            el = page.locator("input").nth(i)
            try:
                print(f"    input[{i}] type={await el.get_attribute('type')} "
                      f"name={await el.get_attribute('name')} "
                      f"placeholder={await el.get_attribute('placeholder')}")
            except Exception:
                pass
        nb = await page.locator("button").count()
        btxt = []
        for i in range(min(nb, 10)):
            try:
                t = (await page.locator("button").nth(i).inner_text()).strip()[:30]
                if t:
                    btxt.append(t)
            except Exception:
                pass
        print(f"    buttons: {btxt}")
        body = (await page.locator("body").inner_text())[:300].replace("\n", " | ")
        print(f"    body: {body}")
    except Exception as e:
        print(f"  dump_state error: {e}")


async def _chatgpt_entry_document_ready(page):
    """Accept a timed-out navigation only when a usable auth document committed."""
    try:
        host = (urlsplit(str(page.url or "")).hostname or "").lower()
        if host not in {"chatgpt.com", "auth.openai.com"}:
            return False

        async def inspect():
            if await page.locator('input[type="email"], input[name="email"]').count() > 0:
                return True
            challenge = page.locator(
                'input[name="cf-turnstile-response"], .cf-turnstile, '
                'iframe[src*="challenges.cloudflare"]'
            )
            if await challenge.count() > 0:
                return True
            body = await page.locator("body").inner_text(timeout=1500)
            return len(body.strip()) >= 5

        return bool(await asyncio.wait_for(inspect(), timeout=2.5))
    except Exception:
        return False


async def navigate_chatgpt_signup(
    context,
    page,
    response_observer=None,
    *,
    max_attempts=None,
    rotate_on_stall=True,
):
    """Open ChatGPT auth without reusing a tab whose navigation is wedged."""
    timeout_seconds = max(15, min(45, _env_int("CHATGPT_GOTO_TIMEOUT_SECONDS", 30)))
    configured_attempts = (
        _env_int("CHATGPT_GOTO_ATTEMPTS", 3)
        if max_attempts is None
        else max_attempts
    )
    attempts = max(1, min(4, int(configured_attempts)))
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(
                SIGNUP_URL,
                timeout=timeout_seconds * 1000,
                wait_until="domcontentloaded",
            )
            return page
        except Exception as exc:
            if await _chatgpt_entry_document_ready(page):
                print("  [1] goto timed out after document commit; continuing")
                return page
            print(f"  goto retry {attempt}/{attempts}: {str(exc)[:70]}")
            if attempt >= attempts:
                break

            requested = str(CHATGPT_NODE or "auto").strip().lower()
            if (
                rotate_on_stall
                and proxy_switch.proxy_mode() == "clash_auto"
                and requested == "auto"
            ):
                node = await _rotate_chatgpt_auto_node()
                print(f"  [node] browser goto stalled; switched to {node or 'no usable node'}")
            else:
                await asyncio.sleep(2)

            old_page = page
            try:
                fresh_page = await asyncio.wait_for(context.new_page(), timeout=12)
                if response_observer:
                    fresh_page.on("response", response_observer)
                page = fresh_page
            except Exception as reset_exc:
                print(f"  [1] clean-tab recovery failed: {str(reset_exc)[:80]}")
                continue
            try:
                await asyncio.wait_for(old_page.close(), timeout=4)
            except Exception:
                pass
            print("  [1] opened a clean tab after stalled navigation")
    return None


async def _click_locator(locator, timeout=5000):
    """Click a ready locator, with a DOM fallback for transient overlays."""
    try:
        await locator.click(timeout=timeout, no_wait_after=True)
        return True
    except Exception:
        pass
    try:
        await locator.evaluate(
            """el => {
                if (el.disabled || el.getAttribute('aria-disabled') === 'true') {
                    throw new Error('button disabled');
                }
                el.click();
            }"""
        )
        return True
    except Exception:
        return False


async def click_exact(page, label, timeout=5000):
    """精确点击文本完全等于 label 的按钮（避免 has-text 子串误匹配，
    如 'Continue' 误点 'Continue with Google'）。返回是否点击成功。"""
    try:
        btn = page.get_by_role("button", name=label, exact=True)
        if await btn.count() > 0:
            if await _click_locator(btn.first, timeout=timeout):
                return True
    except Exception:
        pass
    # 退化：用 CSS 但排除 "with" 字样
    try:
        cand = page.locator(f'button:has-text("{label}")')
        n = await cand.count()
        for i in range(n):
            t = (await cand.nth(i).inner_text()).strip()
            if t == label:
                if await _click_locator(cand.nth(i), timeout=timeout):
                    return True
    except Exception:
        pass
    return False


async def _submit_chatgpt_email_form_once(page):
    """Submit the current email form without reusing a detached locator."""
    # The consent banner can mount after the email field is filled and
    # intercept the first Continue click. Clear it immediately before every
    # attempt, then ensure React still has a non-empty value.
    await wait_for_cookie_banner_settle(page, timeout=2500)
    try:
        current_input = page.locator('input[type="email"], input[name="email"]').first
        if await current_input.count() > 0 and await current_input.is_visible():
            if not (await current_input.input_value()).strip():
                print("  [2] email Continue skipped: React email value is empty")
                return False
    except Exception:
        # A navigation may detach the email field between the check and click.
        pass
    labels = ["Continue", "缍氳", "缁х画", "绻肩簩", "Next", "涓嬩竴姝?", "Teruskan", "Weiter"]
    if await click_any_exact(page, labels):
        await asyncio.sleep(0.35)
        await dismiss_cookie_banner(page)
        return True
    submit = page.locator('button[type="submit"]:not([disabled]):not([aria-disabled="true"])').first
    try:
        if await submit.count() > 0 and await submit.is_visible():
            if await _click_locator(submit, timeout=8000):
                await asyncio.sleep(0.35)
                await dismiss_cookie_banner(page)
                return True
    except Exception:
        pass
    fresh_input = page.locator('input[type="email"], input[name="email"]').first
    try:
        if (
            await fresh_input.count() > 0
            and await fresh_input.is_visible()
            and (await fresh_input.input_value()).strip()
        ):
            await fresh_input.press("Enter", timeout=8000)
            await asyncio.sleep(0.35)
            await dismiss_cookie_banner(page)
            return True
    except Exception:
        pass
    print(f"  [2] email Continue unavailable (url={page.url[:100]})")
    return False


async def submit_chatgpt_email_form(page, timeout=12000):
    """Wait for the auth form to hydrate before submitting it."""
    deadline = time.monotonic() + max(1, timeout) / 1000
    while time.monotonic() < deadline:
        await dismiss_cookie_banner(page)
        if await _submit_chatgpt_email_form_once(page):
            return True
        await asyncio.sleep(0.4)
    print(f"  [2] email Continue unavailable (url={page.url[:100]})")
    return False


async def click_any_exact(page, labels):
    """依次尝试精确点击一组候选标签，命中任一即返回 True。"""
    for label in labels:
        if await click_exact(page, label):
            return True
    return False


# cookie 同意横幅按钮（中/英/日/德），弹出时不关会挡住邮箱输入
_COOKIE_BTNS = [
    "すべて受け入れる", "必須項目以外を拒否する",          # 日
    "Accept all", "Reject all", "Reject non-essential", "Accept", "Got it",  # 英
    "全部接受", "接受所有", "拒绝所有", "拒绝非必要", "同意", "知道了",          # 中
    "Alle akzeptieren", "Annehmen",                       # 德
]


async def _click_resend_code(page):
    """验证码页找「重新发送」入口点一下（多语言）。点到返回 True。"""
    labels = ["Resend code", "Resend email", "Resend", "Send again", "Send a new code",
              "再送信", "再送", "コードを再送", "重新发送", "重新發送", "重发",
              "重新傳送電郵", "重新传送电邮", "重新傳送", "重新传送", "重新傳送驗證碼", "重新獲取",
              "Kirim semula", "Hantar semula", "Kirim ulang", "Kirim ulang email"]
    for lbl in labels:
        for getter in (
            lambda l=lbl: page.get_by_role("button", name=l, exact=False),
            lambda l=lbl: page.get_by_role("link", name=l, exact=False),
            lambda l=lbl: page.locator(f'button:has-text("{l}")'),
            lambda l=lbl: page.locator(f'a:has-text("{l}")'),
        ):
            try:
                loc = getter()
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=3000)
                    print(f"  [4] resend clicked（匹配 '{lbl}'）")
                    await asyncio.sleep(2)
                    return True
            except Exception:
                pass
    return False


async def dismiss_cookie_banner(page):
    """关闭 cookie 同意横幅（命中一个即可）。"""
    for label in _COOKIE_BTNS:
        try:
            b = page.get_by_role("button", name=label, exact=True)
            if await b.count() > 0 and await b.first.is_visible():
                if not await _click_locator(b.first, timeout=2000):
                    continue
                print(f"  [cookie] dismissed: {label}")
                await asyncio.sleep(0.25)
                return True
        except Exception:
            pass
    return False


async def seed_chatgpt_cookie_consent(context):
    """Preseed the same consent state as the existing Accept all action."""
    try:
        expires = int(time.time()) + 365 * 24 * 60 * 60
        await context.add_cookies([
            {
                "name": "oai_consent_analytics",
                "value": "true",
                "domain": ".chatgpt.com",
                "path": "/",
                "expires": expires,
                "sameSite": "Lax",
            },
            {
                "name": "oai_consent_marketing",
                "value": "true",
                "domain": ".chatgpt.com",
                "path": "/",
                "expires": expires,
                "sameSite": "Lax",
            },
        ])
        return True
    except Exception as error:
        print(f"  [cookie] consent preseed failed: {str(error)[:100]}")
        return False


async def fill_email_verified(page, email_input, email, tries=4):
    """填邮箱（React 受控输入：键盘逐字+JS setter 兜底，见 common.browser.react_fill）。
    fill() 只改 DOM .value 不触发 React onChange -> 提交空邮箱 ?email=。

    坑：cookie 同意横幅常在打开页面后、填邮箱当下才异步弹出，盖住输入框抢焦点：
    键盘输入落空（React onChange 收不到值），但 JS setter 兜底把 DOM .value 写进去了
    -> react_fill 回读 input_value() 匹配 -> 误报成功 -> 不重试不关横幅 -> 空提交。
    所以这里每轮**先关横幅再填**，填完若横幅仍在则再关一次并重填。"""
    sel = 'input[type="email"], input[name="email"]'
    for i in range(tries):
        # 1) 先关横幅（可能这轮才弹出来）
        await dismiss_cookie_banner(page)
        # 2) 等横幅真正消失/页面稳定再填——横幅抢焦点会让键盘输入落空，必须等它落定
        await asyncio.sleep(0.8)
        await dismiss_cookie_banner(page)
        # Do not clear a value that is already correct. react_fill resets the
        # field on each attempt, which can create an empty React submission.
        try:
            current = page.locator(sel).first
            if (
                await current.count() > 0
                and await current.is_visible()
                and (await current.input_value()).strip() == email
            ):
                await asyncio.sleep(0.15)
                fresh = page.locator(sel).first
                if (
                    await fresh.count() > 0
                    and await fresh.is_visible()
                    and (await fresh.input_value()).strip() == email
                ):
                    return True
        except Exception:
            pass
        # 3) 填邮箱（React 受控输入）
        if await react_fill(page, sel, email, tries=2, verbose=False):
            # 4) 填完立即确认：横幅若此刻才冒出来盖住，关掉它并回读校验，防 setter 误报
            await dismiss_cookie_banner(page)
            await asyncio.sleep(0.3)
            try:
                if (await page.locator(sel).first.input_value()).strip() == email:
                    return True
            except Exception:
                pass
        print(f"  [2] email not committed, retry {i+1}/{tries}")
        await asyncio.sleep(1)
    return False


async def chatgpt_email_submission_advanced(page):
    """Return false while the visible email form still owns the auth flow."""
    try:
        email_input = page.locator(
            'input[type="email"], input[name="email"]'
        ).first
        if await email_input.count() > 0 and await email_input.is_visible():
            return False
    except Exception:
        pass
    return True


async def wait_for_chatgpt_auth_step(page, timeout=20):
    """Wait through the blank redirect between email submit and the next form."""
    deadline = time.monotonic() + max(0, timeout)
    email_visible_since = None
    while time.monotonic() < deadline:
        try:
            email_input = page.locator(
                'input[type="email"], input[name="email"]'
            ).first
            if await email_input.count() and await email_input.is_visible():
                # The old form can remain visible briefly while auth.openai.com
                # replaces the document. Requiring a stable form avoids retrying
                # against a locator that is detached by a late CF transition.
                if email_visible_since is None:
                    email_visible_since = time.monotonic()
                # Auth.openai.com can keep the old SPA form mounted while the
                # hand-off is in flight. Three seconds causes duplicate
                # submits on slower Clash routes.
                stable_window = min(8, max(3, timeout * 0.4))
                if time.monotonic() - email_visible_since >= stable_window:
                    return "email"
            else:
                email_visible_since = None
            code_input = page.locator(
                'input[name="code"], input[autocomplete="one-time-code"], '
                'input[inputmode="numeric"]'
            ).first
            if await code_input.count() and await code_input.is_visible():
                return "code"
            password_input = page.locator('input[type="password"]').first
            if await password_input.count() and await password_input.is_visible():
                return "password"
            if await detect_challenge(page):
                return "challenge"
            current_url = (page.url or "").lower()
            body = (await page.locator("body").inner_text(timeout=2000)).strip().lower()
            if any(marker in current_url for marker in (
                "email-verification", "about-you", "onboarding",
            )):
                return "advanced"
            if body and any(marker in body for marker in (
                "check your inbox", "確認コード", "検証コード",
                "verification code", "verify your email",
            )):
                return "code"
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return "unknown"


async def ensure_chatgpt_email_entry(page, retries=2):
    """Recover an expired auth hand-off before looking for the email field."""
    selector = 'input[type="email"], input[name="email"]'
    for attempt in range(max(1, retries + 1)):
        try:
            field = page.locator(selector).first
            if await field.count() > 0 and await field.is_visible():
                return True
            body = (await page.locator("body").inner_text(timeout=2500)).lower()
        except Exception:
            body = ""
        expired = any(marker in body for marker in (
            "your session has ended",
            "session has expired",
            "会话已结束",
            "会話の有効期限",
        ))
        if not expired or attempt >= retries:
            return False
        print(f"  [1] auth hand-off expired; reopening ChatGPT login ({attempt + 1}/{retries})")
        await page.goto(SIGNUP_URL, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(4)
    return False


async def wait_for_cookie_banner_settle(page, timeout=5000):
    """Wait for a late consent banner, accept it, and finish its rerender."""
    # The marker lives in the document, so a real navigation automatically
    # resets it while repeated submit helpers on the same page stay fast.
    try:
        already_settled = bool(await page.evaluate(
            "() => window.__regFactoryCookieConsentSettled === true"
        ))
    except Exception:
        already_settled = False
    if already_settled:
        if await dismiss_cookie_banner(page):
            await asyncio.sleep(0.8)
        return True

    async def mark_settled():
        try:
            await page.evaluate(
                "() => { window.__regFactoryCookieConsentSettled = true; }"
            )
        except Exception:
            pass

    deadline = time.monotonic() + max(0, timeout) / 1000
    clicked = False
    while time.monotonic() < deadline:
        if await dismiss_cookie_banner(page):
            clicked = True
            # Accepting consent updates storage and remounts part of the SPA.
            await asyncio.sleep(0.8)
            continue
        if clicked:
            # Catch a second banner mount before declaring the page stable.
            await asyncio.sleep(0.25)
            if not await dismiss_cookie_banner(page):
                await mark_settled()
                return True
        else:
            await asyncio.sleep(0.15)
    await mark_settled()
    return clicked


async def recover_stuck_chatgpt_email_submit(page, email):
    """Reopen a clean auth document when the SPA leaves ``?email=`` in place."""
    for attempt in range(1, 3):
        try:
            print(f"  [2] reopening clean ChatGPT login after stuck email ({attempt}/2)")
            await page.goto(SIGNUP_URL, timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            await dismiss_cookie_banner(page)
            email_input = page.locator(
                'input[type="email"], input[name="email"]'
            ).first
            if await email_input.count() == 0:
                continue
            if not await fill_email_verified(page, email_input, email, tries=3):
                continue
            if not await submit_chatgpt_email_form(page):
                continue
            await asyncio.sleep(4)
            step = await wait_for_chatgpt_auth_step(page, timeout=15)
            if step not in {"email", "unknown"}:
                print(f"  [2] clean email submit advanced to {step}")
                return step
        except Exception as error:
            print(f"  [2] clean email submit retry failed: {str(error)[:90]}")
    return "email"


def create_chatgpt_icloud_mailbox():
    """Allocate a low-cost iCloud submail for registration and code polling."""
    return create_mailbox(provider="icloud", mail_type="icloud")


def create_chatgpt_remail_mailbox():
    """Allocate the configured Remail project/suffix for registration and polling."""
    return create_mailbox(provider="remail")


def _icloud_registration_root(email):
    """Return the canonical iCloud mother address used by plus submail."""
    local, separator, domain = str(email or "").strip().lower().partition("@")
    if not separator:
        return ""
    return f"{local.split('+', 1)[0]}@{domain}"


def _known_icloud_registration_roots():
    """Load mother addresses OpenAI has already rejected as existing users."""
    data_root = _os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or "."
    path = _os.path.join(data_root, "emails_error_chatgpt.txt")
    roots = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "user_already_exists" not in line.lower():
                    continue
                email = line.split("----", 1)[0].strip()
                root = _icloud_registration_root(email)
                if root:
                    roots.add(root)
    except OSError:
        pass
    return roots


def _allocate_untainted_chatgpt_icloud_mailbox(max_attempts=12):
    tainted_roots = _known_icloud_registration_roots()
    for _ in range(max(1, int(max_attempts))):
        mailbox = create_chatgpt_icloud_mailbox()
        root = _icloud_registration_root(mailbox.get("email"))
        if root and root in tainted_roots:
            print(f"  [email] skipping known ChatGPT iCloud mother mailbox: {root}")
            continue
        return mailbox
    raise RuntimeError("iCloud submail pool repeatedly returned known ChatGPT mother mailboxes")


def allocate_chatgpt_registration_mailbox():
    """Allocate the configured mailbox, falling back without reusing claimed mail."""
    if FIXED_EMAIL:
        return {
            "email": FIXED_EMAIL,
            "password": FIXED_PASSWORD or "",
            "refresh_token": FIXED_REFRESH_TOKEN or "",
            "client_id": FIXED_CLIENT_ID or "",
            "mailbox": None,
        }

    if EMAIL_PROVIDER in {"icloud", "remail"}:
        try:
            if EMAIL_PROVIDER == "icloud":
                mailbox = _allocate_untainted_chatgpt_icloud_mailbox()
                label = "iCloud submail"
            else:
                mailbox = create_chatgpt_remail_mailbox()
                label = f"Remail ({mailbox.get('email', '').split('@')[-1]})"
            print(f"  [email] {label} allocated: {mailbox['email']}")
            return {
                "email": mailbox["email"],
                "password": "",
                "refresh_token": "",
                "client_id": "",
                "mailbox": mailbox,
            }
        except Exception as exc:
            print(f"  [email] {EMAIL_PROVIDER} mailbox unavailable: {str(exc)[:160]}")
            return None

    # Prefer a mailbox whose Graph token and inbox were verified just now.
    # Password login remains a fallback for pools without usable RT assets.
    selected = email_pool.retryable_email(
        PLATFORM, require_token=True, validate_token=True
    )
    if not selected:
        selected = email_pool.latest_email(
        PLATFORM, require_token=True, validate_token=True
        )
    if not selected:
        print("  [email] no verified Graph mailbox; using password fallback")
        selected = email_pool.next_email(PLATFORM)
    if selected:
        email, password, refresh_token, client_id = selected
        return {
            "email": email,
            "password": password,
            "refresh_token": refresh_token,
            "client_id": client_id,
            "mailbox": None,
        }

    fallback = _os.environ.get("CHATGPT_POOL_ICLOUD_FALLBACK", "true").strip().lower()
    if fallback in {"0", "false", "no", "off"}:
        return None
    print("  [email] Outlook pool exhausted; switching to iCloud mailbox")
    try:
        mailbox = _allocate_untainted_chatgpt_icloud_mailbox()
        print(f"  [email] iCloud submail allocated: {mailbox['email']}")
        return {
            "email": mailbox["email"],
            "password": "",
            "refresh_token": "",
            "client_id": "",
            "mailbox": mailbox,
        }
    except Exception as exc:
        print(f"  [email] iCloud fallback unavailable: {str(exc)[:160]}")
        return None


async def check_chatgpt_plus_trial_after_registration(session, email):
    """Run the single-account Plus label check without making it fatal."""
    if not session:
        return None
    try:
        from common.asset_scanner import check_chatgpt_plus_trial_for_session

        result = await asyncio.to_thread(
            check_chatgpt_plus_trial_for_session, session, email, 15
        )
        print(
            f"  [plus-check] {email}: "
            f"{result.get('plus_trial', 'unknown')} - "
            f"{result.get('plus_trial_detail', '')}"
        )
        return result
    except Exception as exc:
        print(f"  [plus-check][WARN] {email}: {str(exc)[:120]}")
        return None


def should_use_browser_mail_fallback(has_graph_token, code_try, total_tries=3):
    """Use the slower Outlook UI only after the final Graph attempt fails."""
    return bool(has_graph_token and code_try >= total_tries - 1)


async def detect_challenge(page):
    """检测 Arkose / Turnstile / hCaptcha 是否出现"""
    sel = ("iframe[src*=arkose], #arkose, [data-pkey], #FunCaptcha, "
           ".cf-turnstile, input[name=\"cf-turnstile-response\"], "
           "iframe[src*=turnstile], iframe[src*=challenges.cloudflare], "
           "iframe[src*=hcaptcha]")
    try:
        return await page.locator(sel).count() > 0
    except Exception:
        return False


def import_chatgpt2api(session, email):
    """注册成功后把单个号的 token 导入 chatgpt2api（--import-c2a）。
    用注册时已抓到的 session 直接构造导入对象并 POST，避免再抓一次。
    失败只打印告警，不影响注册成功判定。"""
    if not session:
        print("  [c2a] 无 session，跳过导入")
        return
    host = C2A_URL or CHATGPT2API_URL
    key = C2A_KEY or CHATGPT2API_KEY
    if not (host and key):
        print("  [c2a] 未配置 CHATGPT2API_URL/KEY（--c2a-url/--c2a-key 或 .env），跳过导入")
        return
    try:
        from common.session_export import build_chatgpt2api_account
        from tools.export_chatgpt2api import import_accounts
        account = build_chatgpt2api_account(session, email=email)
        ok, msg = import_accounts(host, key, [account])
        print(f"  [c2a] import {email}: {'OK' if ok else 'FAIL'} - {msg}")
    except Exception as e:
        print(f"  [c2a] 导入失败: {str(e)[:120]}")


async def extract_codex(
    page, email, p=None, ctx=None, release_current=None, totp_secret=""
):
    """注册成功后顺手走 Codex OAuth（--codex）。
    复用刚注册完已登录的 page（无需像 oauth_codex.py 那样重载 cookie 再登），
    默认打开 SUB2API 生成的授权链接并换码建号；CPA 模式则由 CPA 生成授权链接并接收 callback。
    SUB2API 模式会保存带真 refresh_token 的 OAuth 凭据，网页 session 本身没有可续期的 rt。
    失败只打印告警，不影响注册成功判定。返回是否成功。"""
    auth_source = str(_os.environ.get("CODEX_AUTH_URL_SOURCE", CODEX_AUTH_URL_SOURCE) or "sub2").strip().lower()
    if auth_source not in {"sub2", "cpa"}:
        print(f"  [codex] 未知 CODEX_AUTH_URL_SOURCE={auth_source}，跳过")
        return False
    if auth_source == "sub2" and not (SUB2API_URL and SUB2API_EMAIL and SUB2API_PASSWORD):
        print("  [codex] 未配置 SUB2API_URL/EMAIL/PASSWORD（.env），跳过")
        return False
    if auth_source == "cpa" and not (CPA_URL and CPA_MGMT_KEY):
        print("  [codex] CPA 模式未配置 CPA_URL/CPA_MGMT_KEY（.env），跳过")
        return False
    try:
        from common.uploaders import _origin
        from common import oauth_codex as ox
    except Exception as e:
        print(f"  [codex] 模块加载失败: {str(e)[:120]}")
        return False

    group = CODEX_GROUP or SUB2API_GROUP
    origin = _origin(SUB2API_URL) if auth_source == "sub2" else ""
    # 注册完页面可能停在 "You're all set / Continue" 欢迎拦层（或各种 onboarding 弹层），
    # 不清掉就直接开授权，auth_url 会被拦/重定向，drive_authorize 在循环里卡死。
    # 先尽力点掉 Continue、并导航到干净首页，给 OAuth 一个干净起点。
    try:
        for lbl in ["Continue", "続行", "继续", "繼續", "Okay, let's go", "Get started", "Done", "完成"]:
            try:
                b = page.get_by_role("button", name=lbl, exact=False)
                if await b.count() > 0 and await b.first.is_visible():
                    await b.first.click(timeout=2500)
                    print(f"  [codex] 关注册后拦层: {lbl}")
                    await asyncio.sleep(1.5)
                    break
            except Exception:
                pass
        await page.goto("https://chatgpt.com/", timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(2)
    except Exception as e:
        print(f"  [codex] 清拦层/导航首页异常(忽略): {str(e)[:60]}")
    # 手动填号给足人操作时间(≥300)；自动接码换号多次(CODEX_ADDPHONE_ATTEMPTS×CODEX_SMS_TIMEOUT)
    # 可能数分钟，超时按换号预算抬足（过 add-phone 后 drive_authorize 还会再续期捕获窗口）。
    _ph_budget = _env_int("CODEX_ADDPHONE_ATTEMPTS", 2) * _env_int("CODEX_SMS_TIMEOUT", 150)
    timeout = max(CODEX_TIMEOUT, 300, _ph_budget + 120)
    # 免手机直连尝试次数：>0 时每次重开窗口+cookie重登+重新生成 auth_url(全新会话=重摇风控)，
    # 弹手机就跳过本次换下一次赌，N 次都弹才在最后一次真接码。默认 0=直接一次性接码(不赌免手机)。
    # 实测部分新号 OAuth 必弹手机(手机要求偏向绑账号)，赌免手机多半白跑，故默认直接接码；想赌设 N>0。
    skip_n = _env_int("CODEX_PHONE_SKIP_ATTEMPTS", 0)
    try:
        token = None
        group_id = None
        if auth_source == "sub2":
            # SUB2API: 登录 + 找 openai 分组（PKCE/换码由 SUB2API 包办）
            token = ox.sub2api_login(origin, SUB2API_EMAIL, SUB2API_PASSWORD)
            group_id = ox.find_group_id(origin, token, group)
        else:
            print("  [codex] CPA: 使用管理接口生成授权地址并接收 callback")
        _skipmsg = f"免手机直连先试 {skip_n} 次，弹手机才接码" if skip_n > 0 else "直接一次性接码(不赌免手机)"
        if auth_source == "sub2":
            print(f"  [codex] SUB2API: group={group}(#{group_id})，{_skipmsg}")
        else:
            print(f"  [codex] CPA 授权，{_skipmsg}")

        # 每次尝试关窗口重开+重登(cookie)，确保是 OpenAI 眼里全新会话(同窗口重发 auth_url
        # 不改变其"要不要手机"的风控决定)，且避开刚注册窗口的促销弹层(Claim offer 等会挡住
        # 授权页 goto/consent，实测 in-register 复用脏窗口卡死、standalone 重开干净窗口就过)。
        # 故只要有 p+ctx 就总是重开窗口授权(不再限 skip_n>0)，让首次/唯一一次尝试也走干净窗口。
        reset_fn = None
        if p is not None and ctx is not None:
            try:
                cookies = await ctx.cookies()
                reset_fn = ox.make_reset_page(
                    p,
                    cookies,
                    account_email=email,
                    before_open=release_current,
                    browser_options=chatgpt_browser_proxy_fields(),
                )
            except Exception as e:
                print(f"  [codex] 构造窗口重置器失败(退化为复用窗口): {str(e)[:60]}")
                reset_fn = None

        # 驱动授权（先免手机 N 次，最后一次放开手机），捕获 localhost:1455 回调
        codex_metadata = {}
        auth_url = ""
        def _generate_auth():
            nonlocal auth_url
            if auth_source == "cpa":
                auth_url, state = ox.generate_cpa_auth_url(CPA_URL, CPA_MGMT_KEY)
                return auth_url, "", state
            auth_url, session_id, state = ox.generate_auth_url(origin, token)
            return auth_url, session_id, state

        code, session_id, cb_state, msg = await ox.authorize_with_retry(
            page, _generate_auth,
            account_email=email, phone_skip_attempts=skip_n,
            skip_timeout=120, phone_timeout=timeout, manual_phone=CODEX_MANUAL_PHONE,
            reset_page=reset_fn, sms_provider=CODEX_SMS_PROVIDER,
            result_metadata=codex_metadata, totp_secret=totp_secret)
        if reset_fn is not None:
            try:
                await reset_fn.cleanup()
            except Exception:
                pass
        if not code:
            print(f"  [codex] 授权未完成: {msg}")
            return False

        if auth_source == "cpa":
            callback = ox.callback_url(code, cb_state, auth_url)
            cpa_result = ox.submit_cpa_callback(
                CPA_URL,
                CPA_MGMT_KEY,
                callback,
                retries=_env_int("CODEX_CPA_CALLBACK_RETRIES", 5),
                retry_delay=float(_os.environ.get("CODEX_CPA_CALLBACK_RETRY_DELAY", "3") or "3"),
            )
            print(f"  [codex][CPA] callback 已提交（{str(cpa_result.get('message') or 'accepted')[:120]}）✅")
            return True

        # 换码 + 建 oauth 账号（带 refresh_token）
        exch = ox.exchange_code(origin, token, session_id, code, cb_state)
        cred = ox.build_oauth_credentials(exch)
        cred["codex_phone_status"] = codex_metadata.get("codex_phone_status", "unknown")
        from common.session_export import save_codex_oauth_credentials
        persisted_cred = dict(cred)
        if totp_secret:
            persisted_cred["two_factor"] = totp_secret
            persisted_cred["totp_secret"] = totp_secret
        save_codex_oauth_credentials(
            persisted_cred, email=cred.get("email") or email
        )
        print(f"  [codex] exchange-code OK: refresh_token={'YES' if cred.get('refresh_token') else 'NO'} "
              f"plan={cred.get('plan_type')}")
        acct = ox.create_oauth_account(origin, token, cred, [group_id],
                                       name=cred.get("email") or email)
        acct_id = (acct or {}).get("id")
        print(f"  [codex] [OK] SUB2API 账号已创建 #{acct_id}（type=oauth，带 refresh_token）✅")

        # 同一份带真 rt 的凭据顺手推到 CPA（best-effort）
        if CPA_URL and CPA_MGMT_KEY:
            try:
                from common.session_export import build_cpa_codex_json_from_oauth
                from common.uploaders import upload_cpa
                cpa = build_cpa_codex_json_from_oauth(cred, email=cred.get("email") or email)
                cok, cmsg = upload_cpa(CPA_URL, CPA_MGMT_KEY, cpa["auth_json"], cpa["file_name"])
                print(f"  [codex][CPA] {'OK' if cok else 'FAIL'} {cpa['file_name']} - {cmsg}")
            except Exception as e:
                print(f"  [codex][CPA] 推送异常: {str(e)[:80]}")
        return True
    except Exception as e:
        print(f"  [codex] 提取失败: {str(e)[:120]}")
        return False


async def register_one(index, total, p):
    start = time.time()

    def check_timeout():
        if time.time() - start > REGISTER_TIMEOUT:
            raise TimeoutError(f"timeout {REGISTER_TIMEOUT}s")

    allocation = allocate_chatgpt_registration_mailbox()
    if not allocation:
        print("  no email available")
        return None
    email = allocation["email"]
    email_pw = allocation["password"]
    refresh_token = allocation["refresh_token"]
    client_id = allocation["client_id"]
    mailbox = allocation["mailbox"]
    password = rand_password()
    print(f"\n#{index}/{total} email={email}")
    # Mailbox health scanning can quarantine a large stale pool. It is setup
    # work and must not consume the browser registration deadline.
    start = time.time()

    name = f"chatgpt_{time.strftime('%m%d_%H%M%S')}_{index}"
    bb = pid = None
    success = False
    try:
        # A Clash node change cannot repair a BitBrowser profile whose tunnel
        # was established on the previous node. Validate the hydrated auth SPA
        # and rebuild the entire profile before rotating to another route.
        profile_attempts = max(
            1, min(4, _env_int("CHATGPT_GOTO_ATTEMPTS", 3))
        )
        profile_ready = False
        profile_error = "goto_failed"
        auth_monitor = None
        consent_preseeded = False

        for profile_attempt in range(1, profile_attempts + 1):
            profile_name = (
                name
                if profile_attempt == 1
                else f"{name}_retry{profile_attempt}"
            )
            try:
                bb, pid, browser, ctx, page = await open_and_connect(
                    name=profile_name,
                    p=p,
                    browser_options=chatgpt_browser_proxy_fields(),
                )
                await ctx.clear_cookies()
                consent_preseeded = await seed_chatgpt_cookie_consent(ctx)
                auth_monitor = AuthResponseMonitor()
                asset_monitor = ChatGPTAuthAssetMonitor()
                page.on("response", auth_monitor.observe)
                page.on("requestfailed", auth_monitor.observe_failure)
                asset_monitor.attach(page)

                print(
                    f"  [1] goto signup (profile {profile_attempt}/{profile_attempts})"
                )
                page = await navigate_chatgpt_signup(
                    ctx,
                    page,
                    auth_monitor.observe,
                    max_attempts=1,
                    rotate_on_stall=False,
                )
                if page is None:
                    profile_error = "goto_failed"
                else:
                    await asyncio.sleep(3)
                    if await _is_cf_blocked(page):
                        print("  [cf] 检测到 Cloudflare 拦截，尝试 Turnstile...")
                        if await _try_pass_chatgpt_turnstile(page):
                            print("  [cf] Turnstile 点击后放行")
                        else:
                            profile_error = "cf_blocked"
                            _taint_active_chatgpt_node("cloudflare_turnstile")

                    if not await _is_cf_blocked(page):
                        email_entry_ready = await ensure_chatgpt_email_entry(page)
                        assets_ready = (
                            email_entry_ready
                            and await wait_for_chatgpt_auth_assets(
                                page, asset_monitor
                            )
                        )
                        if not assets_ready:
                            profile_error = "auth_assets_failed"
                            print(
                                "  [1] auth JS unavailable: "
                                f"loaded={len(asset_monitor.loaded)} "
                                f"failed={len(asset_monitor.failed)}; "
                                "rebuilding browser profile"
                            )
                        elif email_entry_ready:
                            profile_ready = True
                            break
                        else:
                            profile_error = "no_email_input"
            except Exception as entry_error:
                profile_error = f"entry_{type(entry_error).__name__}"
                print(f"  [1] profile failed: {str(entry_error)[:100]}")

            if bb and pid:
                await teardown(bb, pid, delete=True)
            bb = pid = None

            if profile_attempt >= profile_attempts:
                break

            requested = str(CHATGPT_NODE or "auto").strip().lower()
            if (
                proxy_switch.proxy_mode() == "clash_auto"
                and requested == "auto"
            ):
                node = await _rotate_chatgpt_auto_node()
                if not node:
                    print("  [node] no additional usable ChatGPT node")
                    break
                print(f"  [node] rebuilding ChatGPT profile on {node}")
            else:
                print("  [1] rebuilding ChatGPT profile on the current route")
                await asyncio.sleep(2)

        if not profile_ready:
            print(f"  [1][FAIL] ChatGPT entry unavailable: {profile_error}")
            email_pool.mark_error(PLATFORM, email, email_pw, profile_error)
            return None

        await dump_state(page, "after-load")

        assert_chatgpt_node("before_email")

        # Step 1.5: 先快速处理已经出现的横幅。延迟挂载的横幅在邮箱填好后
        # 统一等待一次，避免启动阶段和每次提交都重复空等。
        await dismiss_cookie_banner(page)
        # Preseeded consent normally settles in under a second; on routes that
        # reject the cookie write, keep the longer late-mount fallback here so
        # the email field is never written during a consent remount.
        await wait_for_cookie_banner_settle(
            page, timeout=750 if consent_preseeded else 6000
        )

        # Step 2: 填邮箱 -> Continue
        print("  [2] fill email")
        email_input = page.locator('input[type="email"], input[name="email"]').first
        if await email_input.count() == 0:
            print("  email input not found")
            await page.screenshot(path=f"screenshots/chatgpt_noemail_{index}.png")
            email_pool.mark_error(PLATFORM, email, email_pw, "no_email_input")
            return None
        # 填邮箱（内部：每轮先关横幅再填，填完回读确认；见 fill_email_verified）
        if not await fill_email_verified(page, email_input, email):
            print("  [2][FAIL] email was not committed; refusing blank submit")
            await page.screenshot(path=f"screenshots/chatgpt_email_not_committed_{index}.png")
            email_pool.mark_error(PLATFORM, email, email_pw, "email_not_committed")
            return None
        # 提交前等待一次延迟挂载的 consent manager。确认后页面可能重渲染，
        # 所以必须重新读取 React 输入值，必要时补填后才能 Continue。
        await wait_for_cookie_banner_settle(
            page, timeout=750 if consent_preseeded else 6000
        )
        current_email = page.locator(
            'input[type="email"], input[name="email"]'
        ).first
        try:
            current_value = (await current_email.input_value()).strip()
        except Exception:
            current_value = ""
        if current_value != email:
            print("  [2] email empty before submit, refilling once...")
            if not await fill_email_verified(page, current_email, email, tries=2):
                print("  [2][FAIL] email remained empty; refusing blank submit")
                await page.screenshot(path=f"screenshots/chatgpt_email_not_committed_{index}.png")
                email_pool.mark_error(PLATFORM, email, email_pw, "email_not_committed")
                return None
        # 关键优化：在提交邮箱（触发 OpenAI 发码）【之前】先把 Outlook 登录好、过隐私协议、
        # 停在收件箱。这样提交后码一到立刻能扫到，避免"发码后才登录、登录+过协议耗时错过码"。
        # 注意：必须用【独立 BitBrowser 窗口】预登录，绝不能在注册 ctx 里 new_page —— 同 context
        # 开 Outlook + bring_to_front 会干扰注册标签的 auth.openai.com 会话，导致点 Continue 后
        # ERR_CONNECTION_CLOSED。故另开窗口隔离（与 grok 的 noproxy 取码窗口同理）。
        mail_bb = mail_pid = mail_page = None
        prelogged = False
        mail_logged_in = False  # 取码窗口是否已登录(prelogin 成功 或 取码时登过)；跨 resend 复用，别反复关窗重登
        # 有可用 Graph token 时跳过浏览器预登录：取码首选 Graph API(get_code_by_token)直收，
        # 不必开浏览器登 Outlook。只有没 token(fresh/空)才预登录走浏览器取码兜底。
        has_token = bool(refresh_token) and refresh_token.strip().lower() != "fresh"
        if has_token:
            print(f"  [2.5] 有 Graph token，跳过浏览器预登录，取码走 Graph API")
        if email_pw and not has_token:
            try:
                print("  [2.5] pre-login Outlook (独立窗口) before sending code...")
                mail_bb, mail_pid, _mb, _mctx, mail_page = await open_and_connect(
                    name=f"mail_{time.strftime('%m%d_%H%M%S')}_{index}", p=p)
                prelogged = await prelogin_outlook(mail_page, email, email_pw)
                mail_logged_in = prelogged
                print(f"  [2.5] outlook prelogin: {'ready' if prelogged else 'failed'}")
                # 登录后稍等 10s 再发码：刚登录 Outlook 收件箱/同步还没就绪，立刻发码易"码到了却没同步进来"。
                if prelogged:
                    print("  [2.5] prelogin ready, 等 10s 让收件箱就绪再发码...")
                    await asyncio.sleep(10)
            except Exception as e:
                print(f"  [2.5] prelogin error: {str(e)[:60]}")
        # 提交：按钮文本中/英/日多语言精确匹配，避免点到 Continue with Google/Apple
        verification_requested_at = time.time()
        if not await submit_chatgpt_email_form(page):
            print("  [2][FAIL] email Continue did not become actionable")
        await asyncio.sleep(5)
        check_timeout()
        auth_step = await wait_for_chatgpt_auth_step(page)
        print(f"  [2] auth step after email: {auth_step}")
        await dump_state(page, "after-email")
        # 若仍停在登录页报"邮箱必填/required"，补填再交一次
        try:
            body_l = (await page.locator("body").inner_text()).lower()
        except Exception:
            body_l = ""
        if any(k in body_l for k in ["必須", "必填", "required", "is required"]):
            print("  [2] still on login (email required), refilling once...")
            await dismiss_cookie_banner(page)
            if not await fill_email_verified(page, email_input, email):
                print("  [2][FAIL] required retry could not commit email")
                email_pool.mark_error(PLATFORM, email, email_pw, "email_not_committed")
                return None
            verification_requested_at = time.time()
            if not await submit_chatgpt_email_form(page):
                print("  [2][FAIL] email Continue retry did not become actionable")
            await asyncio.sleep(5)
            await dump_state(page, "after-email-retry")

        # 部分登录页无错误提示，只把 ?email= 写进 URL 并留在原邮箱表单。
        # 这种状态不能进入 onboarding，否则会反复点击同一个 Continue 后误到游客首页。
        for submit_retry in range(2):
            auth_step = await wait_for_chatgpt_auth_step(page)
            if auth_step not in {"email", "unknown"}:
                break
            print(f"  [2] email form did not advance, retrying submit {submit_retry + 1}/2...")
            try:
                await dismiss_cookie_banner(page)
                email_input = page.locator(
                    'input[type="email"], input[name="email"]'
                ).first
                if not await fill_email_verified(page, email_input, email, tries=2):
                    print("  [2] retry email was not committed; skipping submit")
                    continue
                verification_requested_at = time.time()
                if not await submit_chatgpt_email_form(page):
                    print("  [2] email Continue retry did not become actionable")
                await asyncio.sleep(5)
                await dump_state(page, f"after-email-stuck-retry-{submit_retry + 1}")
            except Exception as exc:
                # A delayed navigation can detach the old email form between
                # locating and submitting it. Re-classify the new page instead
                # of aborting the whole registration on the stale locator.
                print(f"  [2] retry form changed during submit: {str(exc)[:80]}")
                auth_step = await wait_for_chatgpt_auth_step(page, timeout=5)
                if auth_step not in {"email", "unknown"}:
                    break
        auth_step = await wait_for_chatgpt_auth_step(page, timeout=5)
        if auth_step in {"email", "unknown"}:
            verification_requested_at = time.time()
            auth_step = await recover_stuck_chatgpt_email_submit(page, email)
            if auth_step in {"email", "unknown"}:
                if auth_monitor and auth_monitor.debug:
                    print(f"  [auth-debug] {await auth_monitor.debug_summary()}")
                print("  [2][FAIL] email form remained on login after clean-page recovery")
                email_pool.mark_error(PLATFORM, email, email_pw, "email_submit_stuck")
                return None

        # Step 3: 可能出现密码页 / 验证码页 / challenge
        # 先检测 challenge
        if await detect_challenge(page):
            print("  [!] challenge detected after email (Arkose/Turnstile)")
            await page.screenshot(path=f"screenshots/chatgpt_challenge_{index}.png")
            # 等待自动过（真实指纹有时能过），最多 30s
            challenge_cleared = False
            for _ in range(6):
                if await _is_cf_blocked(page):
                    await _click_turnstile(page)
                await asyncio.sleep(5)
                if not await detect_challenge(page):
                    print("  challenge cleared")
                    challenge_cleared = True
                    break
            if not challenge_cleared and await detect_challenge(page):
                print("  [!][FAIL] challenge remained after 30s")
                _taint_active_chatgpt_node("post_email_challenge")
                email_pool.mark_error(PLATFORM, email, email_pw, "challenge_after_email")
                return None

        # 密码输入。登录密码页说明邮箱已经是已有账号，不能把随机注册密码
        # 填进去；只有明确的 signup password 页面才继续。
        password_step = await detect_chatgpt_password_step(page)
        if password_step == "login":
            reason = "existing_account_login_password_required"
            print("  [3][FAIL] mailbox is already registered; login password page reached")
            email_pool.mark_error(PLATFORM, email, email_pw, reason)
            return None
        if password_step == "create":
            print("  [3] fill signup password")
            await human_type(
                page,
                'input[type="password"]:visible, input[name="password"]:visible, '
                'input[name="new-password"]:visible',
                password,
            )
            await asyncio.sleep(1)
            if not await click_exact(page, "Continue"):
                sub = page.locator('button[type="submit"]')
                if await sub.count() > 0:
                    await sub.first.click()
            await asyncio.sleep(5)
            await dump_state(page, "after-password")
        check_timeout()

        # Step 4: 邮件验证码
        # ChatGPT 通常发 6 位验证码或确认链接
        verification_code_failed = False
        fetch_email_code_for_reauth = None
        code_input = page.locator('input[inputmode="numeric"], input[name="code"], input[autocomplete="one-time-code"], input[type="text"]')
        if await code_input.count() > 0 or "verify" in page.url.lower() or "check" in (await page.locator("body").inner_text()).lower():
            code_sel = 'input[inputmode="numeric"], input[name="code"], input[autocomplete="one-time-code"], input[type="text"]'

            verification_seen_codes = set()

            async def _fetch_email_code(received_after=None, allow_browser_fallback=False):
                """取一次码：先 Graph token，失败再浏览器登录 Outlook 取信。
                取码窗口**跨 resend 复用**：已登录就只刷新收件箱轮询(skip_login)，不关窗不重登。
                窗口统一在 Step 4 结束后的兜底处一次性 teardown。
                有可用 Graph token(has_token)时**只走 Graph API、不开浏览器**：API 取码已直连可靠，
                浏览器兜底只是去查同一个收件箱、纯浪费；取不到码该靠上层 resend 重发，而非开窗口。
                received_after: resend 后传重发时刻，只收该时刻后到的邮件(旧码已被 OpenAI 作废)。"""
                nonlocal mail_bb, mail_pid, mail_page, mail_logged_in
                if mailbox and mailbox.get("provider") in {"icloud", "remail"}:
                    mailbox_provider = mailbox.get("provider")
                    c = await poll_verification_code(
                        mailbox["id"], mailbox_provider, email=mailbox["email"],
                        token=mailbox.get("token"), api_key=mailbox.get("api_key"),
                        base_url=mailbox.get("mail_api_url") or mailbox.get("base_url") or None,
                        max_wait=120, poll_interval=4,
                        sender_hint=(), subject_hint=(), code_regex=r"\b(\d{6})\b",
                        exclude_codes=tuple(verification_seen_codes),
                    )
                    if c:
                        verification_seen_codes.add(str(c))
                    return c
                c = await asyncio.get_event_loop().run_in_executor(
                    None, functools.partial(
                        get_code_by_token, email, refresh_token, client_id or None,
                        OAI_SENDER, OAI_SUBJECT, r"\b(\d{6})\b", 40, 5,
                        received_after=received_after,
                        exclude_codes=tuple(verification_seen_codes))
                )
                if c:
                    verification_seen_codes.add(str(c))
                if c or (has_token and not allow_browser_fallback):
                    return c
                if not c and email_pw:
                    # 窗口没了才开新窗(首次没 prelogin、或窗口意外掉线)；否则复用同一窗口
                    if mail_page is None:
                        print("  [4] token failed, opening Outlook window to get code...")
                        try:
                            mail_bb, mail_pid, _mb, _mctx, mail_page = await open_and_connect(
                                name=f"mail_{time.strftime('%m%d_%H%M%S')}_{index}", p=p)
                            mail_logged_in = False
                        except Exception as e:
                            print(f"  [4] open mail window failed: {str(e)[:60]}")
                            mail_page = None
                    elif mail_logged_in:
                        print("  [4] token failed, 复用已登录 Outlook 窗口轮询收件箱...")
                    else:
                        print("  [4] token failed, polling Outlook inbox...")
                    if mail_page is not None:
                        try:
                            c = await get_code_outlook_pw(
                                mail_page, email, email_pw,
                                sender_hint=("openai", "noreply", "no-reply"),
                                subject_hint=("code", "verify", "openai", "chatgpt", "验证"),
                                code_regex=r"\b(\d{6})\b", max_wait=150, poll=8,
                                skip_login=mail_logged_in,
                            )
                            # 跑过一次 get_code_outlook_pw 即已登录(其内部会登)，后续 resend 复用免重登
                            mail_logged_in = True
                        except Exception as e:
                            print(f"  [4] 取码窗口异常: {str(e)[:60]}")
                    # 主注册页可能在 150s 取码等待期间被关/掉线，bring_to_front 会抛；
                    # 这里必须吞掉，否则异常会冲出 for code_try 重试循环、直奔外层 except，
                    # 让 resend 兜底永远没机会跑(实测一次 timeout 就整号失败的根因)。
                    try:
                        await page.bring_to_front()
                    except Exception as e:
                        print(f"  [4] 主页 bring_to_front 失败(忽略): {str(e)[:60]}")
                return c

            fetch_email_code_for_reauth = _fetch_email_code

            async def _renavigate_resend():
                """收不到码且页面无 Resend：回退 signup 重输邮箱重新发码（用户建议的兜底）。"""
                print("  [4] 回退 ChatGPT signup，重输邮箱重新发码...")
                try:
                    await page.goto(SIGNUP_URL, timeout=60000, wait_until="domcontentloaded")
                    await asyncio.sleep(4)
                    await dismiss_cookie_banner(page)
                    ei = page.locator('input[type="email"], input[name="email"]').first
                    if await ei.count() == 0:
                        print("  [4] 回退后无邮箱框，放弃重发")
                        return
                    if not await fill_email_verified(page, ei, email):
                        print("  [4] resend email was not committed; skipping resend")
                        return
                    await dismiss_cookie_banner(page)
                    if not await click_any_exact(page, ["Continue", "続行", "继续", "繼續", "Next", "下一步", "Teruskan"]):
                        sub = page.locator('button[type="submit"]')
                        if await sub.count() > 0:
                            await sub.first.click()
                    await asyncio.sleep(5)
                    # 可能落到密码页（已注册一半），填密码推进回验证码页
                    pw = page.locator('input[type="password"]')
                    if await pw.count() > 0:
                        await human_type(page, 'input[type="password"]', password)
                        await asyncio.sleep(1)
                        if not await click_exact(page, "Continue"):
                            sub = page.locator('button[type="submit"]')
                            if await sub.count() > 0:
                                await sub.first.click()
                        await asyncio.sleep(4)
                except Exception as e:
                    print(f"  [4] 回退重发异常: {str(e)[:80]}")

            code = None
            # The first poll must also exclude codes from earlier registration
            # attempts in the same inbox, not only codes superseded by Resend.
            resend_at = verification_requested_at
            for code_try in range(3):
                # 主页已关就别再空转：resend/_renavigate 都要在活页上操作，死页只会再耗 2×150s。
                try:
                    if page.is_closed():
                        print("  [4] 主注册页已关闭，无法 resend，提前结束取码")
                        break
                except Exception:
                    pass
                if code_try == 0:
                    print("  [4] waiting for email verification code...")
                else:
                    print(f"  [4] 收不到码，重试 {code_try}/2：先点 Resend，没有则回退重输邮箱...")
                    if not await _click_resend_code(page):
                        await _renavigate_resend()
                    resend_at = time.time()  # 重发后只认此刻之后的新码
                    await asyncio.sleep(2)
                code = await _fetch_email_code(
                    received_after=resend_at,
                    allow_browser_fallback=should_use_browser_mail_fallback(
                        has_token, code_try
                    ),
                )
                if code:
                    break

            if code:
                print(f"  got code: {code}")
                await dismiss_cookie_banner(page)
                # React 受控输入需要真实键盘事件；HTML Route Error 则点击页面上的 Retry 恢复。
                for verification_attempt in range(2):
                    try:
                        await submit_email_verification_code(
                            page, code_sel, code, auth_monitor=auth_monitor
                        )
                        break
                    except EmailVerificationRetryNeeded as error:
                        if verification_attempt >= 1:
                            raise RuntimeError(str(error)) from error
                        print(
                            "  [4] verification did not commit; "
                            "requesting a fresh code..."
                        )
                        resend_at = time.time()
                        if not await _click_resend_code(page):
                            raise RuntimeError(
                                f"{error}: resend control unavailable"
                            ) from error
                        code = await _fetch_email_code(
                            received_after=resend_at,
                            allow_browser_fallback=not has_token,
                        )
                        if not code:
                            raise RuntimeError(
                                f"{error}: fresh verification code unavailable"
                            ) from error
                        print(f"  [4] got fresh code: {code}")
            else:
                print("  no code received")
                # 收不到码：只从 chatgpt 平台拉黑（记 emails_error_chatgpt.txt），其它平台仍可取
                email_pool.mark_error(PLATFORM, email, email_pw, "no_code")
                verification_code_failed = True
        # 兜底：关掉可能残留的预登录邮箱独立窗口（如 token 路径直接拿到码、或没进验证码分支）
        if mail_bb and mail_pid:
            try:
                await teardown(mail_bb, mail_pid, delete=True)
            except Exception:
                pass
            mail_bb = mail_pid = mail_page = None
        if verification_code_failed:
            print("  [4][FAIL] verification code unavailable; stopping before onboarding")
            return None
        check_timeout()

        # Step 5: onboarding（名字/生日）。账号 auth session 建立后禁止切换出口。
        assert_chatgpt_node("before_onboarding")
        await handle_onboarding(page, index, auth_monitor=auth_monitor)
        if "about-you" in page.url.lower():
            raise RuntimeError("onboarding_not_completed")
        check_timeout()

        # Step 6: 跳到 chatgpt.com 确保 cookie 落到主域
        try:
            await page.goto("https://chatgpt.com/", timeout=45000, wait_until="domcontentloaded")
            await asyncio.sleep(5)
        except Exception:
            pass
        await dump_state(page, "final")

        # 先抓 session，再用同一登录态开启验证器 2FA。该流程会刷新
        # session cookie，因此 cookie 必须在 2FA 完成后保存。
        totp_secret = ""
        try:
            from common.session_export import fetch_chatgpt_session, save_chatgpt_tokens
            sess = await fetch_chatgpt_session(page)
            if sess and ENABLE_2FA:
                if fetch_email_code_for_reauth is None:
                    print("  [2fa][WARN] no mailbox callback is available; skipping 2FA")
                else:
                    try:
                        from common.chatgpt_2fa import enable_chatgpt_totp

                        totp_secret, sess = await enable_chatgpt_totp(
                            page,
                            ctx,
                            email,
                            fetch_email_code_for_reauth,
                        )
                    except Exception as error:
                        print(f"  [2fa][WARN] enable failed: {str(error)[:160]}")
                        try:
                            await page.goto(
                                "https://chatgpt.com/",
                                timeout=45000,
                                wait_until="domcontentloaded",
                            )
                            await asyncio.sleep(2)
                            refreshed = await fetch_chatgpt_session(page)
                            if refreshed:
                                sess = refreshed
                        except Exception:
                            pass
            if sess:
                sess["registration_country"] = _get_active_chatgpt_country() or ""
                sess["network_node"] = chatgpt_network_label()
                if totp_secret:
                    sess["two_factor"] = totp_secret
                    sess["totp_secret"] = totp_secret
                if isinstance(mailbox, dict):
                    sess["email_provider"] = mailbox.get("provider") or "icloud"
                    share_url = str(
                        mailbox.get("mail_api_url") or mailbox.get("share_url") or ""
                    ).strip()
                    if share_url:
                        # Keep the URL in the session consumed by account
                        # importers so a later manual login can read OTP mail.
                        sess["mail_api_url"] = share_url
            if sess and save_chatgpt_tokens(sess, email):
                print("  [OK] chatgpt 标准 token 已保存")
                await check_chatgpt_plus_trial_after_registration(sess, email)
            else:
                print("  [WARN] 未取到 chatgpt session（可能未完全登录）")
        except Exception as e:
            print(f"  [WARN] 保存标准 token 失败: {e}")
            sess = None

        if mail_bb and mail_pid:
            try:
                await teardown(mail_bb, mail_pid, delete=True)
            except Exception:
                pass
            mail_bb = mail_pid = mail_page = None

        key_val, _ = await save_platform_cookies(
            ctx, PLATFORM, pid, email=email, password=password, key_cookie_names=KEY_COOKIES
        )

        if PLUS_SUBSCRIPTION:
            if sess:
                try:
                    from common.chatgpt_plus import queue_registered_account

                    queued = queue_registered_account(email)
                    print(f"  [plus] 已加入本地批处理队列: {queued['email']}")
                    print("  [plus] 工作台: 主 WebUI -> Plus 订阅")
                except Exception as e:
                    print(f"  [plus][WARN] 加入订阅队列失败: {e}")
            else:
                print("  [plus][WARN] 未抓到 accessToken，无法加入订阅队列")

        # 即时导入 chatgpt2api（--import-c2a；用刚抓到的 session 直接 POST，单号失败不影响注册成功）
        if IMPORT_C2A:
            import_chatgpt2api(sess, email)

        # 顺手走 Codex OAuth 提取 rt 导入 SUB2API（--codex；复用已登录窗口，失败不影响注册成功）
        if EXTRACT_CODEX and key_val:
            try:
                async def _release_registration_profile():
                    nonlocal bb, pid
                    if bb and pid:
                        print("  [codex] 释放注册窗口，为 OAuth 重试窗口腾出 profile 配额")
                        await teardown(bb, pid, delete=True)
                        bb = pid = None

                await extract_codex(
                    page,
                    email,
                    p=p,
                    ctx=ctx,
                    release_current=_release_registration_profile,
                    totp_secret=totp_secret,
                )
            except Exception as e:
                print(f"  [codex] 异常: {str(e)[:120]}")
        elif EXTRACT_CODEX:
            print("  [codex] 无 ChatGPT 登录态，跳过 OAuth")

        if key_val:
            email_pool.mark_used(PLATFORM, email, email_pw)
            success = True
            print(f"  [OK] session cookie saved")
            return key_val
        else:
            print("  [FAIL] no session cookie")
            email_pool.mark_error(PLATFORM, email, email_pw, "no_session_cookie")
            return None

    except Exception as e:
        print(f"  ERROR: {e}")
        if email:
            email_pool.mark_error(PLATFORM, email, email_pw, str(e)[:50])
        return None
    finally:
        if bb and pid:
            keep = KEEP_ON_FAIL and not success
            await teardown(bb, pid, delete=not keep)
            if keep:
                print(f"  [debug] window kept for inspection: {name} (id={pid})")


async def register_one_with_mailbox_retries(index, total, p):
    """Retry iCloud allocation failures without changing the requested count."""
    attempts = 1
    if EMAIL_PROVIDER in {"icloud", "remail"} and not FIXED_EMAIL:
        attempts = max(
            1,
            min(20, _env_int("CHATGPT_ICLOUD_MAILBOX_ATTEMPTS", 8)),
        )
    result = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            print(
                f"  [email] retrying registration with a new iCloud mailbox "
                f"({attempt}/{attempts})"
            )
        result = await register_one(index, total, p)
        if result:
            return result
    return result


async def blur_field(page, selector):
    """让输入框失焦：触发 React 的 onBlur 校验。
    坑：about-you 页 age 是最后填的字段，keyboard.type/JS setter 只发 input/change，
    从不失焦 -> onBlur 校验不跑 -> 'Finish creating account' 按钮一直 disabled，
    既点不动也匹配不到唯一按钮，于是 handle_onboarding 空转卡死。"""
    try:
        el = page.locator(selector).first
        if await el.count() == 0:
            return
        await el.evaluate(
            """(node) => {
                node.dispatchEvent(new Event('blur', {bubbles: true}));
                node.dispatchEvent(new Event('focusout', {bubbles: true}));
                if (typeof node.blur === 'function') node.blur();
            }"""
        )
    except Exception:
        pass


async def _raise_onboarding_error(page, index, auth_monitor=None):
    error = await auth_monitor.latest() if auth_monitor else None
    if not error:
        try:
            body = await page.locator("body").inner_text()
        except Exception:
            body = ""
        error = _openai_error_from_text(body, 400, "/about-you")
        if error and error["code"] == "http_400":
            error = None
    if not error:
        return
    print(
        f"  [onboarding] service rejected: code={error['code']} "
        f"status={error['status']} path={error['url']} message={error['message'][:120]}"
    )
    try:
        await page.screenshot(path=f"screenshots/chatgpt_onboarding_rejected_{index}.png")
    except Exception:
        pass
    raise OnboardingRejected(f"{error['code']}: {error['message']}")


async def recover_stuck_onboarding_session(page):
    """Verify a completed account in a separate tab when about-you fails to navigate."""
    probe = None
    try:
        from common.session_export import fetch_chatgpt_session

        probe = await page.context.new_page()
        await probe.goto(
            "https://chatgpt.com/", timeout=45000, wait_until="domcontentloaded"
        )
        await asyncio.sleep(3)
        session = await fetch_chatgpt_session(probe)
        if not session:
            return False
        main_ui = probe.locator(
            '[data-testid="composer-speech-button"], textarea, #prompt-textarea'
        )
        if await main_ui.count() == 0:
            return False
        print("  [onboarding] account session is valid despite stuck about-you URL")
        await page.goto(
            "https://chatgpt.com/", timeout=45000, wait_until="domcontentloaded"
        )
        return True
    except Exception as error:
        print(f"  [onboarding] session recovery check failed: {str(error)[:80]}")
        return False
    finally:
        if probe is not None:
            try:
                await probe.close()
            except Exception:
                pass


_REQUIRED_ONBOARDING_CONSENT_SELECTOR = ", ".join((
    'input[type="checkbox"][name="personalInfoConsent"]',
    'input[type="checkbox"][name="thirdPartyConsent"]',
    'input[type="checkbox"][name="overseasTransferConsent"]',
    'input[type="checkbox"][required]',
    'input[type="checkbox"][aria-required="true"]',
    '[role="checkbox"][aria-required="true"]',
))


async def ensure_required_onboarding_consents(page):
    """Check required about-you consents without opting into optional choices."""
    boxes = page.locator(_REQUIRED_ONBOARDING_CONSENT_SELECTOR)
    total = await boxes.count()
    checked = 0
    changed = 0

    async def is_checked(box):
        try:
            return bool(await box.is_checked())
        except Exception:
            return (await box.get_attribute("aria-checked")) == "true"

    for i in range(total):
        box = boxes.nth(i)
        if await is_checked(box):
            checked += 1
            continue
        try:
            await box.check(force=True, timeout=4000)
        except Exception:
            try:
                await box.click(force=True, timeout=4000)
            except Exception:
                try:
                    await box.evaluate("""el => {
                        if (el instanceof HTMLInputElement) {
                            const setter = Object.getOwnPropertyDescriptor(
                                HTMLInputElement.prototype, 'checked'
                            )?.set;
                            if (setter) setter.call(el, true);
                            else el.checked = true;
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        } else {
                            el.click();
                        }
                    }""")
                except Exception:
                    continue
        await asyncio.sleep(0.15)
        if await is_checked(box):
            checked += 1
            changed += 1

    if changed:
        print(f"  [onboarding] accepted required consents: {checked}/{total}")
    return total, checked


async def click_finish_button(page, index, age_sel, auth_monitor=None, max_wait=12):
    """about-you 页专用：等 'Finish creating account' 按钮从 disabled 变可用后点击。
    返回是否点击成功。先尝试文案精确匹配，再退化为唯一非第三方登录按钮；
    若超时仍 disabled，dump 诊断（按钮 outerHTML + 各字段值 + 截图）便于排查。"""
    finish_labels = [
        "Finish creating account", "アカウントの作成を完了する",
        "\uacc4\uc815 \uc0dd\uc131 \ub05d\ub0b4\uae30",
        "完成建立帳戶", "完成建立帳號", "完成創建帳戶", "完成創建帳號",
        "完成创建账户", "完成创建账号", "完成建立账户",
        "Selesaikan penciptaan akaun", "Selesaikan penciptaan",
    ]

    async def find_btn():
        # 1) 文案精确匹配
        for label in finish_labels:
            try:
                b = page.get_by_role("button", name=label, exact=True)
                if await b.count() > 0:
                    return b.first
            except Exception:
                pass
        # 2) 退化：唯一的非第三方登录/返回按钮
        try:
            cand = page.locator("button").filter(
                has_not_text="Google").filter(has_not_text="Apple").filter(has_not_text="Back")
            if await cand.count() == 1:
                return cand.first
        except Exception:
            pass
        return None

    # 轮询等待按钮可用（onBlur 校验通过后 disabled 才解除）
    deadline = time.time() + max_wait
    while time.time() < deadline:
        btn = await find_btn()
        if btn is not None:
            try:
                disabled = await btn.get_attribute("disabled")
                aria_dis = await btn.get_attribute("aria-disabled")
            except Exception:
                disabled = aria_dis = None
            if disabled is None and aria_dis != "true":
                try:
                    if auth_monitor:
                        await auth_monitor.clear()
                    await btn.click(timeout=6000)
                    print("  [onboarding] clicked Finish button")
                    # 关键：点了不等于提交成功。about-you 表单常出现"按钮可点但 submit 被
                    # 服务端拒/未导航"——必须验证是否真离开 about-you 页；没走就升级提交手段
                    # (age 框回车 + form.requestSubmit)，否则上层会误判成功、再 re-fill 把按钮搞回 disabled。
                    for _ in range(4):
                        await asyncio.sleep(1.5)
                        await _raise_onboarding_error(page, index, auth_monitor)
                        if "about-you" not in page.url.lower():
                            return True
                    print("  [onboarding] 点了 Finish 但仍在 about-you，升级提交(Enter + requestSubmit)...")
                    try:
                        ae = page.locator(age_sel).first
                        if await ae.count() > 0:
                            await ae.press("Enter", timeout=2000)
                    except Exception:
                        pass
                    try:
                        await btn.evaluate("(b) => { const f = b.closest('form'); if (f) f.requestSubmit ? f.requestSubmit(b) : f.submit(); }")
                    except Exception:
                        pass
                    for _ in range(4):
                        await asyncio.sleep(1.5)
                        await _raise_onboarding_error(page, index, auth_monitor)
                        if "about-you" not in page.url.lower():
                            return True
                    if await recover_stuck_onboarding_session(page):
                        return True
                    # 仍没走：返回 False，让上层别再 re-fill(会重置 React 态、按钮重新 disabled)，
                    # 而是下一轮检测到还在 about-you 时只重试点击。
                    print("  [onboarding] 升级提交后仍在 about-you")
                    return False
                except OnboardingRejected:
                    raise
                except Exception as e:
                    print(f"  [onboarding] Finish click failed: {str(e)[:60]}")
        await asyncio.sleep(1)

    # 仍未点动：dump 诊断
    print("  [onboarding] Finish button still disabled after wait, dumping diagnostics:")
    try:
        btn = await find_btn()
        if btn is not None:
            html = await btn.evaluate("(n) => n.outerHTML")
            print(f"    button: {html[:200]}")
    except Exception:
        pass
    try:
        for s in [age_sel, 'input[name="name"]']:
            el = page.locator(s).first
            if await el.count() > 0:
                print(f"    {s} value = '{await el.input_value()}'")
    except Exception:
        pass
    try:
        await page.screenshot(path=f"screenshots/chatgpt_onboarding_stuck_{index}.png")
    except Exception:
        pass
    return False


async def dump_onboarding_fields(page, tag=""):
    """dump onboarding 页的所有 input/select 结构，便于适配未知布局（age 页 / birthday 页）。"""
    try:
        print(f"  [onboarding-dump {tag}] url={page.url}")
        n = await page.locator("input").count()
        for i in range(min(n, 10)):
            el = page.locator("input").nth(i)
            try:
                print(f"    input[{i}] type={await el.get_attribute('type')} "
                      f"name={await el.get_attribute('name')} "
                      f"id={await el.get_attribute('id')} "
                      f"placeholder={await el.get_attribute('placeholder')} "
                      f"inputmode={await el.get_attribute('inputmode')} "
                      f"aria-label={await el.get_attribute('aria-label')}")
            except Exception:
                pass
        ns = await page.locator("select").count()
        for i in range(min(ns, 6)):
            el = page.locator("select").nth(i)
            try:
                print(f"    select[{i}] name={await el.get_attribute('name')} "
                      f"id={await el.get_attribute('id')} "
                      f"aria-label={await el.get_attribute('aria-label')}")
            except Exception:
                pass
        # combobox/listbox（自定义下拉，非原生 select）
        nc = await page.get_by_role("combobox").count()
        if nc:
            print(f"    comboboxes: {nc}")
        segments = page.locator(
            '[role="spinbutton"]:visible, [contenteditable="true"]:visible'
        )
        segment_count = await segments.count()
        for i in range(min(segment_count, 8)):
            el = segments.nth(i)
            try:
                print(
                    f"    date-segment[{i}] role={await el.get_attribute('role')} "
                    f"data-type={await el.get_attribute('data-type')} "
                    f"aria-label={await el.get_attribute('aria-label')} "
                    f"aria-valuenow={await el.get_attribute('aria-valuenow')}"
                )
            except Exception:
                pass
    except Exception as e:
        print(f"  [onboarding-dump] error: {e}")


_AGE_SELECTOR = (
    'input[name="age" i]:visible, input[id="age" i]:visible, '
    'input[placeholder*="age" i]:visible, input[aria-label*="age" i]:visible, '
    'input[placeholder*="年齢"]:visible, input[aria-label*="年齢"]:visible, '
    'input[placeholder*="年龄"]:visible, input[aria-label*="年龄"]:visible, '
    'input[placeholder*="年齡"]:visible, input[aria-label*="年齡"]:visible, '
    'input[placeholder*="umur" i]:visible, input[aria-label*="umur" i]:visible'
)

_BIRTHDAY_INPUT_SELECTOR = (
    'input[type="date"]:visible, '
    'input[name*="birthday" i]:not([type="hidden"]):visible, '
    'input[name*="birthdate" i]:not([type="hidden"]):visible, '
    'input[name*="dob" i]:not([type="hidden"]):visible, '
    'input[id*="birthday" i]:visible, input[id*="birthdate" i]:visible, '
    'input[id*="dob" i]:visible, '
    'input[name*="month" i]:visible, input[name*="day" i]:visible, '
    'input[name*="year" i]:visible, input[id*="month" i]:visible, '
    'input[id*="day" i]:visible, input[id*="year" i]:visible, '
    'input[placeholder*="birth" i]:visible, input[aria-label*="birth" i]:visible, '
    'input[placeholder*="生日"]:visible, input[aria-label*="生日"]:visible, '
    'input[placeholder*="出生"]:visible, input[aria-label*="出生"]:visible, '
    'input[placeholder*="DD" i]:visible, input[placeholder*="MM" i]:visible, '
    'input[placeholder*="YYYY" i]:visible, input[aria-label="day" i]:visible, '
    'input[aria-label="month" i]:visible, input[aria-label="year" i]:visible'
)

_BIRTHDAY_SELECT_SELECTOR = (
    'select[name*="birth" i]:visible, select[name*="dob" i]:visible, '
    'select[id*="birth" i]:visible, select[id*="dob" i]:visible, '
    'select[name*="month" i]:visible, select[name*="day" i]:visible, '
    'select[name*="year" i]:visible, select[id*="month" i]:visible, '
    'select[id*="day" i]:visible, select[id*="year" i]:visible, '
    'select[aria-label*="month" i]:visible, select[aria-label*="day" i]:visible, '
    'select[aria-label*="year" i]:visible'
)

_BIRTHDAY_SEGMENT_SELECTOR = (
    '[role="spinbutton"]:visible, '
    '[contenteditable="true"][data-type="month" i]:visible, '
    '[contenteditable="true"][data-type="day" i]:visible, '
    '[contenteditable="true"][data-type="year" i]:visible, '
    '[contenteditable="true"][aria-label*="month" i]:visible, '
    '[contenteditable="true"][aria-label*="day" i]:visible, '
    '[contenteditable="true"][aria-label*="year" i]:visible, '
    '[contenteditable="true"]:visible'
)

_BIRTHDAY_COMBOBOX_SELECTOR = (
    '[role="combobox"][name*="birth" i]:visible, '
    '[role="combobox"][name*="dob" i]:visible, '
    '[role="combobox"][id*="birth" i]:visible, '
    '[role="combobox"][id*="dob" i]:visible, '
    '[role="combobox"][data-type="month" i]:visible, '
    '[role="combobox"][data-type="day" i]:visible, '
    '[role="combobox"][data-type="year" i]:visible, '
    '[role="combobox"][aria-label*="month" i]:visible, '
    '[role="combobox"][aria-label*="day" i]:visible, '
    '[role="combobox"][aria-label*="year" i]:visible'
)


async def _birthday_metadata(control):
    values = []
    for name in (
        "name", "id", "placeholder", "aria-label", "data-testid", "data-type"
    ):
        try:
            value = await control.get_attribute(name)
        except Exception:
            value = None
        if value:
            values.append(str(value))
    return " ".join(values)


def _birthday_part(metadata):
    """Classify a date segment without treating the word birthday as day."""
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(metadata or "")).lower()
    tokens = set(re.findall(r"[a-z]+|\d+|[年月日]", value))
    birth_prefix = r"(?:birthday|birthdate|birth|dob)[-_ ]+"
    if "year" in tokens or "yyyy" in value or "年" in tokens or re.search(birth_prefix + "year", value):
        return "year"
    if "month" in tokens or "mm" in tokens or "月" in tokens or re.search(birth_prefix + "month", value):
        return "month"
    if "day" in tokens or "dd" in tokens or "日" in tokens or re.search(birth_prefix + "day", value):
        return "day"
    return None


async def _keyboard_fill_control(page, control, value):
    """Replace an input or editable date segment using real keyboard events."""
    value = str(value)
    try:
        await control.click(timeout=4000)
        await control.evaluate(
            """node => {
                node.focus();
                if (typeof node.select === 'function') {
                    node.select();
                    return;
                }
                const selection = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(node);
                selection.removeAllRanges();
                selection.addRange(range);
            }"""
        )
        await page.keyboard.type(value, delay=20)
        await asyncio.sleep(0.2)
    except Exception:
        return False

    observed = []
    try:
        observed.append(await control.input_value())
    except Exception:
        pass
    try:
        observed.append(await control.text_content())
    except Exception:
        pass
    for attr in ("aria-valuenow", "data-value"):
        try:
            observed.append(await control.get_attribute(attr))
        except Exception:
            pass
    expected = value.lstrip("0") or "0"
    return any(
        str(item or "").strip() == value
        or (str(item or "").strip().lstrip("0") or "0") == expected
        for item in observed
    )


async def _blur_control(control):
    try:
        await control.evaluate(
            """node => {
                node.dispatchEvent(new Event('blur', {bubbles: true}));
                node.dispatchEvent(new Event('focusout', {bubbles: true}));
                if (typeof node.blur === 'function') node.blur();
            }"""
        )
    except Exception:
        pass


async def _map_birthday_controls(controls):
    mapped = {}
    unknown = []
    count = await controls.count()
    for i in range(count):
        control = controls.nth(i)
        part = _birthday_part(await _birthday_metadata(control))
        if part and part not in mapped:
            mapped[part] = control
        else:
            unknown.append(control)
    for part, control in zip(
        [item for item in ("month", "day", "year") if item not in mapped], unknown
    ):
        mapped[part] = control
    return mapped


async def _fill_native_birthday_select(control, candidates):
    try:
        return bool(await control.evaluate(
            """(node, candidates) => {
                const options = Array.from(node.options || []);
                const normalize = value => String(value || '').trim().toLowerCase();
                let option = null;
                for (const candidate of candidates) {
                    const wanted = normalize(candidate);
                    option = options.find(item =>
                        normalize(item.value) === wanted || normalize(item.textContent) === wanted
                    );
                    if (option) break;
                }
                if (!option) {
                    const wantedNumber = Number(candidates[candidates.length - 1]);
                    option = options.find(item =>
                        Number(item.value) === wantedNumber || Number(item.textContent) === wantedNumber
                    );
                }
                if (!option) return false;
                node.value = option.value;
                node.dispatchEvent(new Event('input', {bubbles: true}));
                node.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }""",
            candidates,
        ))
    except Exception:
        return False


async def _birthday_context_present(page, body=""):
    text = str(body or "").lower()
    if any(marker in text for marker in (
        "birthday", "date of birth", "birth date", "出生", "生日", "生年月日",
        "tanggal lahir", "tarikh lahir",
    )):
        return True
    for selector in (_BIRTHDAY_INPUT_SELECTOR, _BIRTHDAY_SELECT_SELECTOR):
        try:
            if await page.locator(selector).count() > 0:
                return True
        except Exception:
            pass
    return False


async def fill_birthday_fields(page, body="", year=1995, month=6, day=15):
    """Fill all known ChatGPT birthday layouts. Returns (present, filled)."""
    if not await _birthday_context_present(page, body):
        return False, False

    iso = f"{year:04d}-{month:02d}-{day:02d}"
    month_text = f"{month:02d}"
    day_text = f"{day:02d}"

    inputs = page.locator(_BIRTHDAY_INPUT_SELECTOR)
    input_count = await inputs.count()
    if input_count == 1:
        field = inputs.first
        metadata = await _birthday_metadata(field)
        metadata_lower = metadata.lower()
        field_type = (await field.get_attribute("type") or "").lower()
        if field_type == "date":
            try:
                await field.fill(iso)
                await field.evaluate(
                    """node => {
                        node.dispatchEvent(new Event('input', {bubbles: true}));
                        node.dispatchEvent(new Event('change', {bubbles: true}));
                    }"""
                )
                if (await field.input_value()).strip() == iso:
                    await _blur_control(field)
                    return True, True
            except Exception:
                pass

        dd_pos = metadata_lower.find("dd")
        mm_pos = metadata_lower.find("mm")
        yyyy_pos = metadata_lower.find("yyyy")
        if dd_pos >= 0 and mm_pos >= 0 and dd_pos < mm_pos:
            values = [f"{day:02d}/{month:02d}/{year:04d}", iso]
        elif yyyy_pos >= 0 and mm_pos >= 0 and yyyy_pos < mm_pos:
            separator = "/" if "/" in metadata_lower else "-"
            values = [f"{year:04d}{separator}{month:02d}{separator}{day:02d}"]
        else:
            values = [f"{month:02d}/{day:02d}/{year:04d}", iso]
        for value in values:
            if await _keyboard_fill_control(page, field, value):
                await _blur_control(field)
                await asyncio.sleep(0.2)
                if (await field.get_attribute("aria-invalid")) != "true":
                    return True, True

    if input_count >= 3:
        mapped = await _map_birthday_controls(inputs)
        if all(part in mapped for part in ("month", "day", "year")):
            results = [
                await _keyboard_fill_control(page, mapped["month"], month_text),
                await _keyboard_fill_control(page, mapped["day"], day_text),
                await _keyboard_fill_control(page, mapped["year"], str(year)),
            ]
            await _blur_control(mapped["year"])
            if all(results):
                return True, True

    selects = page.locator(_BIRTHDAY_SELECT_SELECTOR)
    if await selects.count() >= 3:
        mapped = await _map_birthday_controls(selects)
        if all(part in mapped for part in ("month", "day", "year")):
            results = [
                await _fill_native_birthday_select(
                    mapped["month"], ["June", "Jun", month_text, str(month)]
                ),
                await _fill_native_birthday_select(mapped["day"], [day_text, str(day)]),
                await _fill_native_birthday_select(mapped["year"], [str(year)]),
            ]
            if all(results):
                await _blur_control(mapped["year"])
                return True, True

    segments = page.locator(_BIRTHDAY_SEGMENT_SELECTOR)
    if await segments.count() >= 3:
        mapped = await _map_birthday_controls(segments)
        if all(part in mapped for part in ("month", "day", "year")):
            results = [
                await _keyboard_fill_control(page, mapped["month"], str(month)),
                await _keyboard_fill_control(page, mapped["day"], str(day)),
                await _keyboard_fill_control(page, mapped["year"], str(year)),
            ]
            await _blur_control(mapped["year"])
            if all(results):
                return True, True

    combos = page.locator(_BIRTHDAY_COMBOBOX_SELECTOR)
    if await combos.count() >= 3:
        mapped = await _map_birthday_controls(combos)
        labels = {
            "month": ["June", "Jun", str(month), month_text, f"{month}月"],
            "day": [str(day), day_text, f"{day}日"],
            "year": [str(year), f"{year}年"],
        }
        completed = True
        for part in ("month", "day", "year"):
            control = mapped.get(part)
            if control is None:
                completed = False
                break
            try:
                await control.click(timeout=4000)
                selected = False
                for label in labels[part]:
                    option = page.get_by_role("option", name=label, exact=True)
                    if await option.count() > 0 and await option.first.is_visible():
                        await option.first.click(timeout=4000)
                        selected = True
                        break
                if not selected:
                    await page.keyboard.type(labels[part][0], delay=20)
                    await page.keyboard.press("Enter")
            except Exception:
                completed = False
                break
        if completed:
            return True, True

    return True, False


async def handle_onboarding(page, index, max_rounds=6, auth_monitor=None):
    """处理注册后的引导页：名字、生日/年龄、各种 Continue/Agree"""
    name_done = False  # about-you 名字只填一次，避免每轮重置成新随机名
    age_done = False   # 年龄同理只填一次：re-fill 会重置 React 态、把已解禁的 Finish 按钮搞回 disabled
    bday_done = False
    for r in range(max_rounds):
        await asyncio.sleep(2)
        body = (await page.locator("body").inner_text()).lower()
        url = page.url.lower()
        if r == 0:
            await dump_onboarding_fields(page, tag=f"round{r}")  # 首轮 dump 结构，便于排查未知布局

        name_sel = 'input[name="name"], input[placeholder*="name" i], input[placeholder*="全名"], input[placeholder*="姓名"], input[autocomplete="name"]'
        age_sel = _AGE_SELECTOR
        on_about_you = await page.locator(age_sel).count() > 0

        # about-you 页（名字+年龄）：填一次 -> 失焦触发校验 -> 等按钮可用后点 Finish。
        # 这里独立处理，不走下面的泛化 Continue 匹配（会被 disabled 按钮卡住空转）。
        # 名字/年龄都只填一次(name_done/age_done)：re-fill 会重置 React 态、把按钮搞回 disabled，
        # 导致 round0 点了没走、round1 re-fill 后反而点不动的死循环(实测根因)。
        if on_about_you:
            if not name_done and await page.locator(name_sel).count() > 0:
                first, last = rand_name()
                # delay/settle 调低：名字/年龄是 onboarding 的本地字段，不像邮箱要防风控，快点键入即可
                if await react_fill(page, name_sel, f"{first} {last}", tries=2, delay=12, settle=0.15, verbose=False):
                    print(f"  [onboarding] name: {first} {last}")
                    name_done = True
                    await blur_field(page, name_sel)
                    await asyncio.sleep(0.2)
            if not age_done:
                if await react_fill(page, age_sel, str(random.randint(18, 40)), tries=2, delay=12, settle=0.15, verbose=False):
                    print("  [onboarding] age filled")
                    age_done = True
                    # 关键：失焦让 onBlur 校验跑起来，Finish 按钮才会解除 disabled
                    await blur_field(page, age_sel)
                    await asyncio.sleep(0.3)
            consent_total, consent_checked = await ensure_required_onboarding_consents(page)
            if consent_total and consent_checked < consent_total:
                print(
                    "  [onboarding] required consents are not ready: "
                    f"{consent_checked}/{consent_total}"
                )
                await asyncio.sleep(1)
                continue
            if await click_finish_button(
                page, index, age_sel, auth_monitor=auth_monitor
            ):
                await asyncio.sleep(3)
                continue  # 进入下一轮看是否还有后续引导页
            # 没点动则继续往下走泛化兜底（极少数布局）

        # 名字（其它引导页：input name=name placeholder=全名/Full name，多语言界面）
        if (
            not on_about_you
            and not name_done
            and await page.locator(name_sel).count() > 0
        ):
            first, last = rand_name()
            if await react_fill(page, name_sel, f"{first} {last}", tries=2, verbose=False):
                print(f"  [onboarding] name: {first} {last}")
                name_done = True
                await blur_field(page, name_sel)
                await asyncio.sleep(1)

        # 生日页可能是单框、月日年三框、原生下拉或 React Aria 可编辑日期段。
        # 隐藏 birthday 状态字段始终排除，避免直接改 DOM 后 React 状态为空。
        birthday_present = False
        if not on_about_you:
            if bday_done:
                birthday_present = await _birthday_context_present(page, body)
            else:
                birthday_present, filled = await fill_birthday_fields(page, body)
                if filled:
                    print("  [onboarding] birthday filled")
                    bday_done = True
                    await asyncio.sleep(0.3)
                elif birthday_present:
                    print("  [onboarding] birthday controls found but values were not committed")

        # 点完成/续行（多语言：中/繁/英/日）。具体"完成创建账号"按钮优先于泛化 Continue，
        # 否则 about-you 页只有 'Finish creating account' 这一个按钮会被泛化匹配漏掉。
        clicked = False
        for label in [
                # 具体完成按钮(优先)：英 / 日 / 繁(港台) / 简 / 马来(代理走马来节点时 OpenAI 返回 Bahasa Melayu)
                "Finish creating account", "アカウントの作成を完了する",
                "\uacc4\uc815 \uc0dd\uc131 \ub05d\ub0b4\uae30",
                "完成建立帳戶", "完成建立帳號", "完成創建帳戶", "完成創建帳號",
                "完成创建账户", "完成创建账号", "完成建立账户",
                "Selesaikan penciptaan akaun", "Selesaikan penciptaan",
                # 泛化续行/同意：英/中/繁/日/马来
                "Continue", "继续", "繼續", "Agree", "同意", "I agree", "Next", "下一步",
                "Get started", "开始", "Confirm", "确认", "確認", "Submit", "提交", "保存", "完成",
                "続行", "完了", "次へ", "同意する", "はい", "始める",
                "Teruskan", "Setuju", "Mula"]:
            if await click_exact(page, label):
                print(f"  [onboarding] clicked {label}")
                clicked = True
                await asyncio.sleep(3)
                break

        # 结构化兜底：标签没命中（如代理切到马来/法语/日语等界面，文本对不上）时，
        # about-you 页通常只有一个主按钮 —— 直接回车提交 + 点唯一可用按钮，不依赖文案。
        if not clicked and await page.locator(age_sel).count() > 0:
            try:
                await page.locator(age_sel).first.press("Enter")
                await asyncio.sleep(2)
            except Exception:
                pass
            try:
                # 选页面上唯一“可点”的非返回按钮（排除 Google/Apple/手机第三方登录、返回）
                btn = page.locator(
                    'button:not([disabled]):not([aria-disabled="true"])'
                ).filter(has_not_text="Google").filter(has_not_text="Apple").filter(has_not_text="Back")
                n = await btn.count()
                if n == 1:
                    await btn.first.click(timeout=8000)
                    print("  [onboarding] clicked sole submit button (structural fallback)")
                    clicked = True
                    await asyncio.sleep(3)
                else:
                    # 多按钮时点最后一个可用按钮（主操作通常在最后）
                    sub = page.locator('button[type="submit"]:not([disabled])')
                    if await sub.count() > 0:
                        await sub.last.click(timeout=8000)
                        print("  [onboarding] clicked submit[type] (structural fallback)")
                        clicked = True
                        await asyncio.sleep(3)
            except Exception as e:
                print(f"  [onboarding] structural fallback failed: {str(e)[:60]}")

        # 已进入主界面
        if "chatgpt.com" in url and "auth" not in url and "onboarding" not in url:
            if await page.locator('[data-testid="composer-speech-button"], textarea, #prompt-textarea').count() > 0:
                print("  [onboarding] reached main UI")
                return
        if not clicked and await page.locator(name_sel).count() == 0 and not birthday_present:
            # 没有可操作元素，可能已完成
            break


async def main():
    parser = argparse.ArgumentParser(description="ChatGPT Auto Register")
    parser.add_argument("--count", "-n", type=int, default=1)
    parser.add_argument("--concurrency", "-c", type=int, default=1)
    parser.add_argument("--timeout", "-t", type=int, default=480)
    parser.add_argument("--node", default="auto",
                        help="固定 ChatGPT Clash 节点；auto 自动探测，none 直连")
    parser.add_argument(
        "--country", default="auto",
        help="注册出口国家：auto 或 JP/SG/US 等两位 ISO 国家码",
    )
    parser.add_argument("--keep-on-fail", action="store_true", help="失败时保留窗口便于排查")
    parser.add_argument("--email", default=None, help="指定邮箱(绕过邮箱池)")
    parser.add_argument("--password", default=None, help="指定邮箱密码")
    parser.add_argument("--refresh-token", default=None, help="指定 Outlook refresh_token")
    parser.add_argument("--client-id", default=None, help="指定 Outlook OAuth client_id")
    parser.add_argument("--email-provider", choices=["pool", "icloud", "remail"], default=None,
                        help="邮箱来源：pool=emails.txt，icloud=API 自动申请并取码")
    two_factor = parser.add_mutually_exclusive_group()
    two_factor.add_argument("--enable-2fa", dest="enable_2fa", action="store_true",
                            help="注册成功后启用验证器 TOTP 并保存密钥")
    two_factor.add_argument("--no-2fa", dest="enable_2fa", action="store_false",
                            help="跳过注册后的验证器 TOTP")
    parser.set_defaults(enable_2fa=None)
    parser.add_argument("--import-c2a", action="store_true",
                        help="注册成功后即时把 token 导入 chatgpt2api (POST <host>/api/accounts)")
    parser.add_argument("--plus-subscription", action="store_true",
                        help="注册成功后加入本地 Plus 批处理工作台")
    parser.add_argument("--c2a-url", default=None, help="chatgpt2api host (默认取 config.CHATGPT2API_URL)")
    parser.add_argument("--c2a-key", default=None, help="chatgpt2api admin key (默认取 config.CHATGPT2API_KEY)")
    parser.add_argument("--codex", action="store_true",
                        help="注册成功后顺手走 Codex OAuth 提取 refresh_token 导入 SUB2API (oauth 账号可续期)")
    parser.add_argument("--codex-group", default=None,
                        help="SUB2API 目标分组名 (默认取 config.SUB2API_GROUP)")
    parser.add_argument("--codex-manual-phone", action="store_true",
                        help="Codex add-phone 手动模式: 不接码, 自己在浏览器填号收码")
    parser.add_argument("--codex-sms-provider", choices=["auto", "custom", "smsman", "firefox", "hero"], default="auto",
                        help="Codex 自动接码平台；auto 按默认顺序")
    parser.add_argument("--codex-timeout", type=int, default=120,
                        help="Codex 授权捕获超时秒 (手动填号会自动抬到至少 300)")
    args = parser.parse_args()

    global REGISTER_TIMEOUT, KEEP_ON_FAIL, FIXED_EMAIL, FIXED_PASSWORD, FIXED_REFRESH_TOKEN, FIXED_CLIENT_ID, EMAIL_PROVIDER
    global IMPORT_C2A, PLUS_SUBSCRIPTION, C2A_URL, C2A_KEY
    global EXTRACT_CODEX, ENABLE_2FA, CODEX_GROUP, CODEX_MANUAL_PHONE, CODEX_SMS_PROVIDER, CODEX_TIMEOUT, CHATGPT_NODE, CHATGPT_COUNTRY
    REGISTER_TIMEOUT = args.timeout
    KEEP_ON_FAIL = args.keep_on_fail
    FIXED_EMAIL = args.email
    FIXED_PASSWORD = args.password
    FIXED_REFRESH_TOKEN = args.refresh_token
    FIXED_CLIENT_ID = args.client_id
    EMAIL_PROVIDER = args.email_provider or CHATGPT_EMAIL_PROVIDER or "pool"
    if EMAIL_PROVIDER not in {"pool", "icloud", "remail"}:
        print(f"  [config] CHATGPT_EMAIL_PROVIDER={EMAIL_PROVIDER!r} 无效，回退 pool")
        EMAIL_PROVIDER = "pool"
    IMPORT_C2A = args.import_c2a
    PLUS_SUBSCRIPTION = args.plus_subscription
    C2A_URL = args.c2a_url
    C2A_KEY = args.c2a_key
    EXTRACT_CODEX = args.codex
    ENABLE_2FA = CHATGPT_ENABLE_2FA if args.enable_2fa is None else args.enable_2fa
    CODEX_GROUP = args.codex_group
    CODEX_MANUAL_PHONE = args.codex_manual_phone
    CODEX_SMS_PROVIDER = args.codex_sms_provider
    CODEX_TIMEOUT = args.codex_timeout
    CHATGPT_NODE = args.node
    CHATGPT_COUNTRY = _normalize_chatgpt_country(args.country)

    if IMPORT_C2A and not ((C2A_URL or CHATGPT2API_URL) and (C2A_KEY or CHATGPT2API_KEY)):
        print("  [c2a][WARN] 已开 --import-c2a 但未配置 CHATGPT2API_URL/KEY（--c2a-url/--c2a-key 或 .env），导入会被跳过")

    if EXTRACT_CODEX and not (SUB2API_URL and SUB2API_EMAIL and SUB2API_PASSWORD):
        print("  [codex][WARN] 已开 --codex 但未配置 SUB2API_URL/EMAIL/PASSWORD（.env），Codex 提取会被跳过")

    try:
        selected_node = select_chatgpt_node(CHATGPT_NODE, country=CHATGPT_COUNTRY)
        requested_node = str(CHATGPT_NODE or "auto").strip().lower()
        if (
            selected_node
            and requested_node not in {"auto", "none", "off", "direct"}
            and proxy_switch.proxy_mode() == "clash_auto"
        ):
            proxy_switch.pin_fixed_node(selected_node, "chatgpt")
            print(f"  [node] 当前批次固定节点并发: {selected_node}")
    except Exception as e:
        print(f"  [node][FAIL] {e}")
        return 2

    from common.concurrency import build_worker_plan
    from common.task_context import activate_worker

    worker_plan = build_worker_plan("chatgpt", args.count, args.concurrency)
    worker_plan.log()

    print("=" * 50)
    print(
        f"  ChatGPT Auto Register  count={args.count} "
        f"concurrency={worker_plan.effective_concurrency}"
    )
    print("=" * 50)

    slot_locks = [asyncio.Lock() for _ in range(worker_plan.effective_concurrency)]
    results = []

    async def run_one(i):
        stagger_slot = (i - 1) % worker_plan.effective_concurrency
        if stagger_slot:
            await asyncio.sleep(random.uniform(1.5, 3.5) * stagger_slot)
        worker_context = worker_plan.worker(i)
        async with slot_locks[worker_context.slot - 1]:
            with activate_worker(worker_context) as worker:
                _set_active_chatgpt_node(ACTIVE_CHATGPT_NODE)
                if ACTIVE_CHATGPT_COUNTRY:
                    _set_active_chatgpt_country(ACTIVE_CHATGPT_COUNTRY)
                exit_country = await asyncio.to_thread(
                    ensure_chatgpt_worker_country
                )
                _set_active_chatgpt_country(exit_country)
                print(
                    f"  [worker] {worker.worker_id} slot={worker.slot} "
                    f"proxy={proxy_switch.current_node()} country={exit_country}"
                )
                async with async_playwright() as p:
                    try:
                        sk = await register_one_with_mailbox_retries(
                            i, args.count, p
                        )
                        results.append(sk)
                    except Exception as e:
                        print(f"  #{i} fatal: {e}")
                        results.append(None)

    await asyncio.gather(*[run_one(i) for i in range(1, args.count + 1)])

    ok = sum(1 for r in results if r)
    print(f"\n{'='*50}\n  success: {ok}/{len(results)}\n{'='*50}")
    return 0 if results and ok == len(results) else 1


if __name__ == "__main__":
    proxy_switch.apply_platform_environment("chatgpt")
    sys.exit(asyncio.run(main()))
