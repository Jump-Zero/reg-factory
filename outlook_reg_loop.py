"""Standalone Outlook registration loop. Continuously registers fresh
outlook accounts via BitBrowser + standalone register_outlook script, and
writes each success to _data_bundle/_outlook_pool/ as one JSON file per
record (email + password + session cookies).

The Replit batch (_batch_register.py / bs_register_step1.py) consumes these
via the `pool` email source — fully decoupled, so a slow self-reg attempt
never blocks the Replit signup pipeline.

Usage:
  python outlook_reg_loop.py                       # run until the rate breaker trips
  python outlook_reg_loop.py --count 20            # 20 attempts then exit
  python outlook_reg_loop.py --min-success-rate 10 --success-rate-window 20
  python outlook_reg_loop.py --target-pool 10      # stop refilling once pool >= 10
  python outlook_reg_loop.py --max-press 5         # OUTLOOK_REG_MAX_PRESS
  python outlook_reg_loop.py --sleep 5             # gap between attempts (s)

Reads HTTP_PROXY env for Clash routing (host:port form). Set
SELF_REG_SCRIPT_PATH to override standalone script location.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
import hashlib
import json
import os
import sys
import time
import importlib.util
import urllib.request
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ARTIFACT_DIR = os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or SCRIPT_DIR
POOL_DIR = os.path.join(ARTIFACT_DIR, "_outlook_pool")
EMAILS_META_FILE = os.path.join(ARTIFACT_DIR, "emails_meta.json")


def _mark_email_source(email_lower, source):
    """标记邮箱来源分类到 emails_meta.json。"""
    meta = {}
    try:
        if os.path.isfile(EMAILS_META_FILE):
            with open(EMAILS_META_FILE, encoding="utf-8") as f:
                meta = json.load(f)
    except Exception:
        meta = {}
    meta[email_lower] = {"source": source}
    try:
        with open(EMAILS_META_FILE, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
# 账号注册侧消费的池（common/emails.next_email 读取），格式 email----password----token----clientid
EMAILS_POOL = os.path.join(ARTIFACT_DIR, "emails.txt")
# 注册成功但 Graph refresh_token 抽取失败的号：邮箱+密码单独存这里，别丢。
# 之后可用 tools/extract_graph_tokens.py 对这个文件补抽 RT，或浏览器登录直接用。
NO_GRAPH_POOL = os.path.join(ARTIFACT_DIR, "outlook_no_graph.txt")
DEFAULT_MAX_PRESS = "5"
DEFAULT_MIN_SUCCESS_RATE = 10.0
DEFAULT_SUCCESS_RATE_WINDOW = 20
_OUTLOOK_DEFAULT_EXCLUDED_REGION_HINTS = ("香港", "hong kong", "hongkong", "🇭🇰")
_GRAPH_HTTP_FALLBACK_ERRORS = frozenset({
    "password_signin_unavailable",
    "signin_rate_limited",
})

STANDALONE_PATH = os.environ.get(
    "SELF_REG_SCRIPT_PATH",
    os.path.join(SCRIPT_DIR, "register_outlook_standalone.py"),
)

# Optional Clash rotation between attempts. Without this, MS PerimeterX
# learns the egress IP after 1-2 signups and ERR_CONNECTION_CLOSEDs us out.
sys.path.insert(0, SCRIPT_DIR)
try:
    import _clash_verge  # type: ignore
except ImportError:
    _clash_verge = None


def log(msg, level="INFO"):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{level}] {msg}", flush=True)


def _graph_error_allows_http_fallback(reason):
    normalized = str(reason or "").strip().lower()
    return not normalized or normalized in _GRAPH_HTTP_FALLBACK_ERRORS


def _success_rate_breaker(outcomes, minimum_rate, window):
    """Evaluate the configured threshold against a full recent window."""
    window = max(1, int(window))
    sample = list(outcomes)[-window:]
    rate = 100.0 * sum(bool(value) for value in sample) / max(1, len(sample))
    ready = len(sample) >= window
    enabled = float(minimum_rate) > 0
    return enabled and ready and rate < float(minimum_rate), rate, len(sample)


async def _new_registration_page(context):
    """Create a clean registration tab, then remove BitBrowser startup tabs."""
    startup_pages = list(getattr(context, "pages", []) or [])
    live_startup_pages = []
    for startup_page in startup_pages:
        try:
            if startup_page.is_closed() is True:
                continue
        except Exception:
            pass
        live_startup_pages.append(startup_page)
    try:
        page = await asyncio.wait_for(context.new_page(), timeout=20)
    except asyncio.TimeoutError:
        if live_startup_pages:
            log("new registration tab timed out; reusing the startup tab", "WARN")
            return live_startup_pages[0]
        raise RuntimeError("BitBrowser did not create a registration tab within 20 seconds")
    closed = 0
    for extra in live_startup_pages:
        try:
            await asyncio.wait_for(extra.close(), timeout=5)
            closed += 1
        except Exception:
            pass
    if closed:
        log(f"created clean registration tab; closed {closed} startup tab(s)")
    return page


def _env_truthy_norotate():
    """OUTLOOK_NO_ROTATE 环境变量：1/true/yes/on 任一即视为开启不轮换。"""
    return (os.environ.get("OUTLOOK_NO_ROTATE", "") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _outlook_region_keywords():
    """Read regional node hints and decode legacy escaped values."""
    raw = (
        os.environ.get("OUTLOOK_NODE_REGION_KEYWORDS")
        or os.environ.get("NODE_REGION_KEYWORDS")
        or ""
    ).strip()
    keywords = []
    for item in raw.replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if "\\u" in item:
            try:
                item = item.encode("ascii").decode("unicode_escape")
            except Exception:
                pass
        keywords.append(item.casefold())
    return keywords


def _outlook_excluded_region_keywords():
    raw = (
        os.environ.get("OUTLOOK_NODE_EXCLUDE_REGION_KEYWORDS")
        or ",".join(_OUTLOOK_DEFAULT_EXCLUDED_REGION_HINTS)
    ).strip()
    return [
        item.strip().casefold()
        for item in raw.replace("，", ",").split(",")
        if item.strip()
    ]


def _filter_outlook_nodes_by_region(nodes):
    """Prefer configured country nodes and never return subscription pseudo-nodes."""
    concrete = [
        node for node in nodes
        if node
        and not _clash_verge.is_fake_node(node)
        and not any(
            hint in node.casefold()
            for hint in _outlook_excluded_region_keywords()
        )
    ]
    keywords = _outlook_region_keywords()
    if not keywords:
        return concrete
    preferred = [
        node for node in concrete
        if any(keyword in node.casefold() for keyword in keywords)
    ]
    if preferred:
        return preferred
    log(
        f"no Clash node matches OUTLOOK_NODE_REGION_KEYWORDS={keywords}; "
        "falling back to all concrete nodes",
        "WARN",
    )
    return concrete


def ensure_clash_proxy_env():
    """Use the selected egress for direct loop runs while local APIs stay direct."""
    from common import proxy_switch
    existing = (
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        or ""
    ).strip()
    proxy = existing or proxy_switch.effective_proxy_url()
    if not proxy:
        return ""
    if not existing:
        os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = proxy
        os.environ["http_proxy"] = os.environ["https_proxy"] = proxy
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    required = ["127.0.0.1", "localhost", "::1"]
    parts = [p.strip() for p in no_proxy.split(",") if p.strip()]
    for item in required:
        if item not in parts:
            parts.append(item)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(parts)
    return proxy


def load_standalone():
    if not os.path.isfile(STANDALONE_PATH):
        log(f"standalone not found at {STANDALONE_PATH}", "ERR")
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("_self_reg_standalone", STANDALONE_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    log(f"loaded standalone from {STANDALONE_PATH}")
    return m


def init_clash():
    """Connect to Clash controller. Returns (client, group_name) or (None, None)."""
    if _clash_verge is None:
        return None, None
    api = os.environ.get("CLASH_API", "").strip() or None
    secret = os.environ.get("CLASH_SECRET", "").strip()
    if not api:
        try:
            api = _clash_verge.auto_detect_api(secret=secret)
        except Exception as e:
            log(f"clash auto-detect failed: {e}", "WARN")
            return None, None
    if not api:
        return None, None
    try:
        client = _clash_verge.ClashClient(api=api, secret=secret)
    except Exception as e:
        log(f"clash client init failed: {e}", "WARN")
        return None, None
    group = (os.environ.get("CLASH_GROUP", "").strip() or "").strip()
    if not group or group.lower() == "auto":
        try:
            group = _clash_verge.auto_pick_group(client) or ""
        except Exception as e:
            log(f"clash auto-pick group failed: {e}", "WARN")
    if not group:
        log("clash: no usable group", "WARN")
        return None, None
    log(f"clash ready: api={api} group={group!r}")
    return client, group


# Clash 节点轮换排除名单：国内直连/大陆节点从中国 IP 出口，Outlook(MS PerimeterX)
# 对中国 IP 的按住验证基本必挂，且轮到它纯浪费一次 attempt，故从 GLOBAL 轮换里剔除。
# 子串匹配（节点名含任一即排除）。可经 CLASH_EXCLUDE_NODES 环境变量追加（逗号分隔）。
_CN_EXCLUDE_HINTS = ("国内直连", "直连", "DIRECT", "大陆", "国内", "China", "回国")

def _ordered_nodes(client, group, excluded):
    """按名称排序的可用节点列表(已排除 CN/excluded)。不做区域优先，节点平等轮换。"""
    try:
        nodes = [
            n for n in _filter_outlook_nodes_by_region(client.list_nodes(group))
            if n not in excluded
        ]
    except Exception as e:
        log(f"list nodes err: {type(e).__name__}: {e}", "WARN")
        return []
    return sorted(nodes)


# 本会话已试过的节点(跨 attempt 累积)，保证逐个用，全用完再重置一轮。
_TRIED_NODES = set()


def _rotate_excluded(client, group):
    """把 CN/直连子串提示解析成 GLOBAL 组里真实节点名集合（pick_node 用精确匹配，
    故必须先列出实际节点名再按子串挑出要排除的）。CLASH_EXCLUDE_NODES 追加精确名。"""
    ex = set()
    extra = (os.environ.get("CLASH_EXCLUDE_NODES") or "").strip()
    if extra:
        ex |= {x.strip() for x in extra.replace("，", ",").split(",") if x.strip()}
    try:
        for name in client.list_nodes(group):
            if any(h in name for h in _CN_EXCLUDE_HINTS):
                ex.add(name)
    except Exception as e:
        log(f"resolve excluded nodes err: {type(e).__name__}: {e}", "WARN")
    return ex


def maybe_rotate(client, group, strategy="round_robin", max_latency_ms=6000,
                 mixed_port=7897):
    """切到下一个节点并验证出口 IP 变了。按名称顺序平等轮换：在 _ordered_nodes 排好
    的列表里挑第一个本会话没试过的节点，全试过则重置循环。排除 CN/直连。"""
    if client is None or not group:
        return None
    try:
        excluded = _rotate_excluded(client, group)
        ordered = _ordered_nodes(client, group, excluded)
        if not ordered:
            log("no usable node after exclude", "WARN")
            return None
        # 挑【第一个本会话没试过的】节点，全试过则重置循环。
        global _TRIED_NODES
        nxt = next((n for n in ordered if n not in _TRIED_NODES), None)
        if nxt is None:           # 一轮全试过，重置再来
            _TRIED_NODES = set()
            nxt = ordered[0]
        _TRIED_NODES.add(nxt)
        ip_before = None
        try:
            ip_before = _clash_verge.public_ip(timeout=5, mixed_port=mixed_port)
        except Exception:
            pass
        client.switch(group, nxt)
        try:
            client.close_connections()
        except Exception:
            pass
        time.sleep(1.5)
        ip_after = None
        try:
            ip_after = _clash_verge.public_ip(timeout=5, mixed_port=mixed_port)
        except Exception:
            pass
        changed = bool(ip_before and ip_after and ip_before != ip_after)
        log(f"clash rotate -> {nxt} IP {ip_before}->{ip_after} "
            f"{'changed' if changed else 'UNCHANGED'}")
        return {"ok": True, "next": nxt, "ip_changed": changed,
                "ip_before": ip_before, "ip_after": ip_after}
    except Exception as e:
        log(f"clash rotate err: {type(e).__name__}: {e}", "WARN")
        return None


def _probe_delay(client, node, timeout_ms):
    """探测单节点延迟(ms)，超时/出错返回 None。用 Clash 自带 /delay(直接测该节点，
    无需先 switch)。"""
    try:
        return client.delay(node, _clash_verge.DEFAULT_TEST_URL, timeout_ms)
    except Exception:
        return None


def maybe_rotate_verified(client, group, mixed_port=7897):
    """轮换到【可用】节点：切之前先探 /delay，跳过超时的，在一批里挑延迟最低的。

    旧逻辑按名字顺序直接 switch，只在切完后验 IP —— 会把整整一次 attempt(~3min)
    浪费在死节点/超时节点上。现在改成：先探测候选节点延迟，超时(None)或超过
    CLASH_MAX_LATENCY_MS 的直接跳过并标记试过，在 CLASH_PROBE_BATCH 个可用节点里
    选延迟最低的再 switch。本会话所有节点都试过则重置一轮。
    """
    if client is None or not group:
        return None
    try:
        excluded = _rotate_excluded(client, group)
        ordered = _ordered_nodes(client, group, excluded)
        if not ordered:
            log("no usable node after exclude", "WARN")
            return None

        # 延迟上限 + 每轮探测多少个候选后就在可用的里挑最优
        try:
            max_latency_ms = int(os.environ.get("CLASH_MAX_LATENCY_MS", "2500") or "2500")
        except Exception:
            max_latency_ms = 2500
        try:
            probe_batch = max(1, int(os.environ.get("CLASH_PROBE_BATCH", "8") or "8"))
        except Exception:
            probe_batch = 8
        probe_tmo = max_latency_ms + 1500

        global _TRIED_NODES
        # 本会话把所有节点都试过了 -> 重置，开新一轮
        if all(n in _TRIED_NODES for n in ordered):
            _TRIED_NODES = set()

        ip_before = None
        try:
            ip_before = _clash_verge.public_ip(timeout=5, mixed_port=mixed_port)
        except Exception:
            pass

        # 1) 探测候选：跳过超时/过慢，在一批可用节点里挑延迟最低的
        best, best_d, probed = None, 1 << 30, 0
        for node in ordered:
            if node in _TRIED_NODES:
                continue
            if probed >= probe_batch and best is not None:
                break
            d = _probe_delay(client, node, probe_tmo)
            probed += 1
            if d is None or d > max_latency_ms:
                _TRIED_NODES.add(node)   # 超时/过慢：本轮不再考虑
                log(f"clash probe skip {node} "
                    f"({'timeout' if d is None else str(d) + 'ms >' + str(max_latency_ms)})")
                continue
            if d < best_d:
                best, best_d = node, d

        # 2) 一批里全超时？放宽 batch，继续往后找第一个能响应的(哪怕慢)，避免整轮无节点
        if best is None:
            for node in ordered:
                if node in _TRIED_NODES:
                    continue
                d = _probe_delay(client, node, probe_tmo)
                if d is not None:
                    best, best_d = node, d
                    break
                _TRIED_NODES.add(node)

        if best is None:
            log("no responsive Clash node (all timed out)", "WARN")
            return {"ok": False, "next": None, "ip_changed": False,
                    "ip_before": ip_before, "ip_after": None}

        # 3) 切到选中的可用节点并验出口 IP
        _TRIED_NODES.add(best)
        client.switch(group, best)
        try:
            client.close_connections()
        except Exception:
            pass
        time.sleep(1.5)

        ip_after = None
        try:
            ip_after = _clash_verge.public_ip(timeout=5, mixed_port=mixed_port)
        except Exception:
            pass
        changed = bool(ip_before and ip_after and ip_before != ip_after)
        # A successful /ip response is not enough: if the egress stayed the
        # same, the next signup would reuse the exact fingerprinted exit.
        ok = bool(ip_after) and (not ip_before or changed)
        log(f"clash rotate -> {best} ({best_d}ms) IP {ip_before}->{ip_after} "
            f"{'changed' if changed else 'UNCHANGED'} {'OK' if ok else 'BAD'}")
        return {"ok": ok, "next": best, "ip_changed": changed,
                "ip_before": ip_before, "ip_after": ip_after, "latency_ms": best_d}
    except Exception as e:
        log(f"clash rotate verified err: {type(e).__name__}: {e}", "WARN")
        return None


def clash_proxy_from_env():
    raw = (
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        or ""
    ).strip()
    if not raw:
        return None
    for pfx in ("http://", "https://", "socks5://"):
        if raw.lower().startswith(pfx):
            raw = raw[len(pfx):]
            break
    return raw.rstrip("/") or None


BB_API = os.environ.get("BITBROWSER_API", "http://127.0.0.1:54345")
# Match bs_register_step1 — user's BitBrowser has Chromium 146 not 130.
BB_CORE_VERSION = (os.environ.get("BB_CORE_VERSION") or "146").strip()
OUTLOOK_BROWSER_FALLBACK_CORE_VERSION = (
    os.environ.get("OUTLOOK_BROWSER_FALLBACK_CORE_VERSION") or "130"
).strip()


def _fingerprint_provider():
    return (
        os.environ.get("FINGERPRINT_BROWSER")
        or os.environ.get("BROWSER_PROVIDER")
        or "bitbrowser"
    ).strip().lower()


def _bb_call(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BB_API}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _bitbrowser_proxy_fields(proxy_str=None):
    if not proxy_str:
        return {"proxyMethod": 2, "proxyType": "noproxy"}
    from common.direct_proxy import parse_proxy

    proxy = parse_proxy(proxy_str)
    if proxy is None:
        return {"proxyMethod": 2, "proxyType": "noproxy"}
    if proxy.scheme == "socks4":
        raise ValueError("BitBrowser does not support SOCKS4 profiles")
    fields = {
        "proxyMethod": 2,
        "proxyType": "socks5" if proxy.scheme == "socks5" else "http",
        "host": proxy.host,
        "port": str(proxy.port),
    }
    if proxy.username:
        fields["proxyUserName"] = proxy.username
    if proxy.password:
        fields["proxyPassword"] = proxy.password
    return fields


def bb_create_for_outlook_reg(name, proxy_str=None):
    """Create an Outlook profile with the selected residential proxy when set."""
    proxy_fields = _bitbrowser_proxy_fields(proxy_str)
    from common.fingerprint import browser_fingerprint

    fingerprint = browser_fingerprint("outlook", BB_CORE_VERSION)
    if _fingerprint_provider() not in {"bitbrowser", "bit"}:
        from bitbrowser import BitBrowser
        return BitBrowser().create_browser(
            name=name,
            remark="outlook reg loop auto-deleted after use",
            **proxy_fields,
            browserFingerPrint=fingerprint,
        )
    from common.traffic_saver import bitbrowser_profile_defaults

    body = {
        "name": name,
        "remark": "outlook reg loop — auto-deleted after use",
        **proxy_fields,
        **bitbrowser_profile_defaults(),
        "browserFingerPrint": fingerprint,
    }
    r = _bb_call("/browser/update", body)
    if not r.get("success"):
        raise RuntimeError(f"/browser/update failed: {r}")
    data = r.get("data") or {}
    pid = data.get("id") or data.get("browserId")
    if not pid:
        raise RuntimeError(f"/browser/update returned no id: {data}")
    return pid


def bb_open_for_outlook_reg(bb, profile_id):
    """Open with the preferred core, falling back only on install failure."""
    try:
        return bb.open_browser(profile_id)
    except Exception as exc:
        message = str(exc).lower()
        core_update_failed = any(marker in message for marker in (
            "内核更新失败", "kernel update failed", "core update failed",
        ))
        fallback = OUTLOOK_BROWSER_FALLBACK_CORE_VERSION
        if not core_update_failed or not fallback:
            raise

        log(
            f"BitBrowser core {BB_CORE_VERSION} unavailable; "
            f"falling back to {fallback}",
            "WARN",
        )
        try:
            bb._post(
                "/browser/update/partial",
                {
                    "ids": [profile_id],
                    "browserFingerPrint": {"coreVersion": fallback},
                },
            )
            return bb.open_browser(profile_id)
        except Exception as fallback_error:
            raise RuntimeError(
                "BitBrowser 内核回退后仍无法打开；请修复或更新 BitBrowser 内核"
            ) from fallback_error


def count_pool():
    if not os.path.isdir(POOL_DIR):
        return 0
    try:
        return sum(1 for f in os.listdir(POOL_DIR) if f.endswith(".json"))
    except Exception:
        return 0


def _rotate_graph_retry_egress():
    """Rotate a Graph retry without violating the configured proxy mode."""
    from common import proxy_switch as ps

    mode = ps.proxy_mode()
    if mode == "residential":
        return ps.rotate_proxy()
    if mode == "clash_fixed":
        return ps.ensure_proxy_mode()

    import random
    current = ps.current_node()
    candidates = [node for node in ps.concrete_nodes() if node != current]
    if candidates:
        node = random.choice(candidates)
        ps.set_node(node)
        return {"ok": True, "node": node}
    return {"ok": False, "node": current}


def extract_graph_for_account(email, password, attempts=1):
    """Return Graph token data for a freshly registered Outlook account."""
    try:
        from tools.extract_graph_tokens import get_graph_token
        for attempt in range(attempts):
            from common import proxy_switch as ps

            res = get_graph_token(
                email,
                password,
                proxy=ps.effective_proxy_url(),
            )
            if res and res.get("refresh_token"):
                graph = {
                    "refresh_token": res["refresh_token"],
                    "client_id": res.get("client_id") or "",
                }
                log(f"graph token extracted for {email}", "OK")
                return graph
            if attempt < attempts - 1:
                log(f"graph token attempt {attempt + 1}/{attempts} failed, rotate and retry: {email}", "WARN")
                try:
                    _rotate_graph_retry_egress()
                except Exception as exc:
                    log(f"graph retry node switch failed: {str(exc)[:50]}", "WARN")
                time.sleep(3 * (attempt + 1))
        log(f"graph token missing after {attempts} attempts: {email}", "WARN")
    except Exception as exc:
        log(f"graph token extraction error: {type(exc).__name__}: {exc}", "WARN")
    return None


def append_graph_account_to_emails_pool(email, password, graph):
    """Append only Graph-ready accounts to emails.txt."""
    token = (graph or {}).get("refresh_token") or ""
    client_id = (graph or {}).get("client_id") or ""
    if not token:
        log(f"emails.txt skip {email}: no graph refresh_token", "WARN")
        return False
    try:
        from common.file_lock import file_lock

        with file_lock(EMAILS_POOL):
            existing = set()
            if os.path.isfile(EMAILS_POOL):
                with open(EMAILS_POOL, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            existing.add(line.split("----")[0].strip().lower())
            if email.lower() in existing:
                return True
            with open(EMAILS_POOL, "a", encoding="utf-8") as handle:
                handle.write(f"{email}----{password}----{token}----{client_id}\n")
        # 标记为"自主注册"
        _mark_email_source(email.lower(), "registered")
        log(f"emails.txt += {email} (token=yes)", "OK")
        return True
    except Exception as exc:
        log(f"append_to_emails_pool failed: {type(exc).__name__}: {exc}", "WARN")
        return False


def append_to_emails_pool(email, password):
    """把成功号桥接进 emails.txt 池，供账号注册侧 common/emails.next_email 消费。
    注册成功后立即用纯 HTTP OAuth 抽 Graph refresh_token（tools.extract_graph_tokens.get_graph_token），
    写真 token/client_id —— 之后 ChatGPT 取码全走 Graph API，免浏览器登录/取码。
    抽取失败（偶发风控/网络）才回退占位符 fresh，消费侧届时退化到浏览器取码。"""
    token = client_id = "fresh"
    graph = globals().pop("_CURRENT_GRAPH_ACCOUNT", None)
    if graph is not None:
        return append_graph_account_to_emails_pool(email, password, graph)
    try:
        from tools.extract_graph_tokens import get_graph_token
        # 抽取经代理偶发 TLS 抖动(SSLEOFError)，单试一次一抖就回退 fresh、白丢 token 快路；
        # 这里重试 3 次(短退避)，绝大多数抖动二/三次就过。
        res = None
        for _try in range(3):
            from common import proxy_switch as ps

            res = get_graph_token(
                email,
                password,
                proxy=ps.effective_proxy_url(),
            )
            if res and res.get("refresh_token"):
                break
            if _try < 2:
                # 抽取经代理偶发 TLS 抖动：第 2 次起先切 Clash 节点换出口再试(绕开坏节点)。
                log(f"graph token 抽取第{_try+1}次未成，切节点重试: {email}", "WARN")
                try:
                    _rotate_graph_retry_egress()
                except Exception as _e:
                    log(f"切节点失败(忽略): {str(_e)[:50]}", "WARN")
                time.sleep(3 * (_try + 1))
        if res and res.get("refresh_token"):
            token = res["refresh_token"]
            client_id = res.get("client_id") or "fresh"
            log(f"graph token extracted for {email}", "OK")
        else:
            log(f"graph token 抽取失败(3 次)，回退 fresh: {email}", "WARN")
    except Exception as e:
        log(f"graph token 抽取异常，回退 fresh: {type(e).__name__}: {e}", "WARN")
    try:
        from common.file_lock import file_lock

        with file_lock(EMAILS_POOL):
            existing = set()
            if os.path.isfile(EMAILS_POOL):
                with open(EMAILS_POOL, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            existing.add(line.split("----")[0].strip().lower())
            if email.lower() in existing:
                return
            with open(EMAILS_POOL, "a", encoding="utf-8") as handle:
                handle.write(f"{email}----{password}----{token}----{client_id}\n")
        log(f"emails.txt += {email} (token={'yes' if token != 'fresh' else 'fresh'})", "OK")
    except Exception as e:
        log(f"append_to_emails_pool failed: {type(e).__name__}: {e}", "WARN")


def append_no_graph_account(email, password):
    """注册成功但 Graph refresh_token 提取失败的号：邮箱+密码单独存到 NO_GRAPH_POOL，
    别丢弃。这些号本体有效(能登录/收码)，只是没抽到 RT，后续可用
    tools/extract_graph_tokens.py 重跑补 token 再入池。去重按邮箱。格式 email----password。"""
    try:
        from common.file_lock import file_lock

        with file_lock(NO_GRAPH_POOL):
            existing = set()
            if os.path.isfile(NO_GRAPH_POOL):
                with open(NO_GRAPH_POOL, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            existing.add(line.split("----")[0].strip().lower())
            if email.lower() in existing:
                return
            with open(NO_GRAPH_POOL, "a", encoding="utf-8") as handle:
                handle.write(f"{email}----{password}\n")
        log(f"outlook_no_graph.txt += {email} (无 RT，已存待补)", "OK")
    except Exception as e:
        log(f"append_no_graph_account failed: {type(e).__name__}: {e}", "WARN")


def write_record(record):
    os.makedirs(POOL_DIR, exist_ok=True)
    safe = record["email"].replace("@", "_at_").replace("/", "_")
    fname = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:18] + f"_{safe}.json"
    tmp = os.path.join(POOL_DIR, fname + ".tmp")
    dst = os.path.join(POOL_DIR, fname)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.rename(tmp, dst)
    except Exception as e:
        log(f"write_record FAILED: {type(e).__name__}: {e}  (tmp={tmp})", "ERR")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise
    # Verify it actually landed.
    if os.path.isfile(dst):
        sz = os.path.getsize(dst)
        log(f"write_record OK: {dst}  ({sz} bytes)", "OK")
    else:
        log(f"write_record sus: {dst} missing right after rename!", "ERR")
    return fname


async def _run_outlook_on_ctx(
    mod,
    ctx,
    idx,
    registration_timeout=None,
    graph_timeout=None,
):
    """Register, export cookies, then obtain Graph RT in the live login session."""
    from common.traffic_saver import (
        install as install_traffic_saver,
        log_summary as log_traffic_summary,
    )

    await install_traffic_saver(ctx)
    # Scrub Chromium residual state so signup.live.com doesn't see a
    # stale identity from a previous session.
    try:
        await ctx.clear_cookies()
        for _pg in ctx.pages:
            try:
                c = await ctx.new_cdp_session(_pg)
                await c.send("Network.clearBrowserCookies")
                await c.send("Network.clearBrowserCache")
                try: await c.detach()
                except Exception: pass
                break
            except Exception:
                pass
    except Exception:
        pass
    page = await _new_registration_page(ctx)
    registration = mod.register_outlook(page, ctx, idx)
    if registration_timeout:
        email, password = await asyncio.wait_for(
            registration,
            timeout=max(1, int(registration_timeout)),
        )
    else:
        email, password = await registration
    cookies = []
    graph = None
    if email:
        try:
            all_cookies = await ctx.cookies()
            keep_domains = (
                "outlook.", "live.com", "microsoftonline.",
                "microsoft.com", "office.com", ".office365.",
                "msn.com", "bing.com", "mail.live.com",
            )
            cookies = [
                c for c in all_cookies
                if any(d in (c.get("domain") or "") for d in keep_domains)
            ]
        except Exception as e:
            log(f"cookie export failed: {e}", "WARN")
        try:
            log(f"graph browser-session extraction start: {email}")
            authorization = mod.extract_graph_token(page, ctx, email, password, idx)
            if graph_timeout:
                graph = await asyncio.wait_for(
                    authorization,
                    timeout=max(1, int(graph_timeout)),
                )
            else:
                graph = await authorization
            if graph and graph.get("refresh_token"):
                graph["client_id"] = graph.get("client_id") or mod.GRAPH_CLIENT_ID
                log(f"graph token extracted from live browser session: {email}", "OK")
            elif graph and graph.get("terminal_error"):
                log(
                    f"graph sign-in stopped ({graph['terminal_error']}): {email}",
                    "WARN",
                )
            else:
                graph = None
                log(f"graph browser-session extraction returned no RT: {email}", "WARN")
        except Exception as e:
            log(f"graph browser-session extraction failed: {type(e).__name__}: {e}", "WARN")
            graph = None
    log_traffic_summary(ctx)
    return email, password, cookies, graph


async def one_attempt(
    mod,
    proxy_str,
    idx,
    registration_timeout=None,
    graph_timeout=None,
):
    """Mirrors bs_register_step1.fetch_email_from_self_register's inline
    flow, but doesn't carry the breaker state — we're a dedicated loop and
    want to keep trying."""
    profile_id = None
    bb = mod.BitBrowserClient()
    try:
        ts = datetime.now().strftime("%m%d_%H%M%S")
        for _r in range(5):
            try:
                # Use our own create that picks coreVersion=146 (matches the
                # BitBrowser install on this machine). Standalone's hardcoded
                # 130 makes BB return 502.
                from common import proxy_switch
                browser_proxy = proxy_str if proxy_switch.proxy_mode() == "residential" else None
                name = f"outlook_loop_{ts}_{idx}"
                profile_id = bb_create_for_outlook_reg(
                    name,
                    proxy_str=browser_proxy,
                )
                break
            except Exception as e:
                m = str(e)
                if "最大" in m or "超过" in m:
                    log("BitBrowser quota — cleanup_browsers(keep=2)", "WARN")
                    try: bb.cleanup_browsers(keep=2)
                    except Exception: pass
                    await asyncio.sleep(3)
                    continue
                if _r >= 4:
                    raise
                log(f"create_browser err (try {_r+1}/5): {m[:200]}", "WARN")
                await asyncio.sleep(3 + _r)
        if not profile_id:
            return None, None, [], None
        info = bb_open_for_outlook_reg(bb, profile_id)
        ws = info.get("ws", "")
        if not ws:
            return None, None, [], None
        from playwright.async_api import async_playwright as _apw
        async with _apw() as p:
            browser = await p.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            email, password, cookies, graph = await _run_outlook_on_ctx(
                mod,
                ctx,
                idx,
                registration_timeout=registration_timeout,
                graph_timeout=graph_timeout,
            )
        return email, password, cookies, graph
    finally:
        if profile_id:
            try:
                bb.close_browser(profile_id)
                await asyncio.sleep(2)
                bb.delete_browser(profile_id)
            except Exception:
                pass


def _playwright_shutdown_exception_handler(loop, context):
    """Ignore only detached Playwright futures after a timed-out profile closes."""
    from common.playwright_runtime import is_expected_shutdown_error

    if is_expected_shutdown_error(context):
        return
    loop.default_exception_handler(context)


def _graph_authorization_timeout(environ=None):
    env = os.environ if environ is None else environ
    try:
        return max(60, int(env.get("OUTLOOK_GRAPH_AUTH_TIMEOUT", "240") or "240"))
    except (TypeError, ValueError):
        return 240


async def _one_attempt_with_timeout(
    mod,
    proxy_str,
    idx,
    registration_timeout,
    graph_timeout=None,
):
    """Apply independent caps so completed Graph OAuth is never lost to a total cap."""
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_playwright_shutdown_exception_handler)
    return await one_attempt(
        mod,
        proxy_str,
        idx,
        registration_timeout=registration_timeout,
        graph_timeout=graph_timeout or _graph_authorization_timeout(),
    )


async def _run_registration_workers(
    args,
    mod,
    no_rotate,
    proxy_mode,
    clash_client,
    clash_group,
):
    from common import direct_proxy, proxy_switch
    from common.concurrency import build_worker_plan
    from common.task_context import activate_worker

    planning_count = args.count or max(1, args.concurrency)
    worker_plan = build_worker_plan("outlook", planning_count, args.concurrency)
    worker_plan.log()
    lane_workers = [
        worker_plan.worker(slot + 1)
        for slot in range(worker_plan.effective_concurrency)
    ]
    state = {
        "next": 0,
        "success": 0,
        "failed": 0,
        "in_flight": 0,
        "stop_reason": "",
        "recent": deque(maxlen=args.success_rate_window),
    }
    state_lock = asyncio.Lock()

    async def record_outcome(success, reason=""):
        tripped_now = False
        async with state_lock:
            state["success" if success else "failed"] += 1
            state["recent"].append(bool(success))
            trip, rate, sample_size = _success_rate_breaker(
                state["recent"],
                args.min_success_rate,
                args.success_rate_window,
            )
            if trip and not state["stop_reason"]:
                state["stop_reason"] = (
                    f"recent success rate {rate:.1f}% is below "
                    f"{args.min_success_rate:g}% "
                    f"({sum(state['recent'])}/{sample_size}, "
                    f"window={args.success_rate_window})"
                )
                if reason:
                    state["stop_reason"] += f"; latest={reason}"
                tripped_now = True
            snapshot = {
                "success": state["success"],
                "failed": state["failed"],
                "rate": rate,
                "sample_size": sample_size,
                "stop_reason": state["stop_reason"],
            }
        if tripped_now:
            log(f"success-rate breaker tripped: {snapshot['stop_reason']}", "WARN")
        return snapshot

    async def reserve_attempt(slot):
        while True:
            async with state_lock:
                if state["stop_reason"]:
                    return None
                if args.count > 0 and state["next"] >= args.count:
                    return None
                pool_size = count_pool()
                if args.target_pool and pool_size >= args.target_pool:
                    return None
                target_full = bool(
                    args.target_pool
                    and pool_size + state["in_flight"] >= args.target_pool
                )
                if not target_full:
                    state["next"] += 1
                    state["in_flight"] += 1
                    return state["next"], pool_size
            if slot == 0:
                log(
                    f"pool at target ({count_pool()}/{args.target_pool}) - "
                    f"sleep {args.sleep_when_full}s"
                )
            await asyncio.sleep(max(1, args.sleep_when_full))

    async def run_lane(slot):
        worker = lane_workers[slot]
        lane_attempt = 0
        while True:
            reserved = await reserve_attempt(slot)
            if reserved is None:
                return
            idx, pool_size = reserved
            worker.index = idx
            worker.fingerprint_seed = hashlib.sha256(
                f"outlook:{slot}:{idx}:{time.time_ns()}".encode("ascii")
            ).hexdigest()
            t0 = time.time()
            email = password = None
            cookies = []
            graph = None
            try:
                with activate_worker(worker):
                    rotate_info = None
                    if not no_rotate:
                        if proxy_mode == "residential":
                            if lane_attempt:
                                rotate_info = proxy_switch.rotate_proxy()
                        elif proxy_mode == "clash_auto":
                            rotate_info = await asyncio.to_thread(
                                maybe_rotate_verified,
                                clash_client,
                                clash_group,
                            )
                    if rotate_info and not rotate_info.get("ok"):
                        await record_outcome(False, "proxy egress unavailable")
                        log(
                            f"worker {worker.worker_id}: no reachable proxy egress",
                            "WARN",
                        )
                        if args.sleep > 0:
                            await asyncio.sleep(args.sleep)
                        continue

                    proxy = proxy_switch.effective_proxy_url()
                    redacted = (
                        proxy_switch.current_node()
                        if proxy_mode.startswith("clash")
                        else direct_proxy.redact_proxy(proxy)
                    )
                    log(
                        f"=== attempt #{idx} worker={worker.worker_id} "
                        f"slot={worker.slot} proxy={redacted} "
                        f"(pool={pool_size}, succ={state['success']}, fail={state['failed']}) ==="
                    )
                    email, password, cookies, graph = await _one_attempt_with_timeout(
                        mod,
                        proxy,
                        idx,
                        args.timeout,
                        _graph_authorization_timeout(),
                    )
                    elapsed = time.time() - t0
                    if email and password:
                        terminal_graph_error = (graph or {}).get("terminal_error")
                        if (
                            _graph_error_allows_http_fallback(terminal_graph_error)
                            and (not graph or not graph.get("refresh_token"))
                        ):
                            log(
                                f"live browser RT unavailable; falling back to pure HTTP: {email}",
                                "WARN",
                            )
                            fallback_graph = await asyncio.to_thread(
                                extract_graph_for_account,
                                email,
                                password,
                            )
                            if fallback_graph and fallback_graph.get("refresh_token"):
                                graph = fallback_graph
                                terminal_graph_error = ""
                        if not graph or not graph.get("refresh_token"):
                            reason = terminal_graph_error or "Graph refresh token missing"
                            await record_outcome(False, reason)
                            if terminal_graph_error in _GRAPH_HTTP_FALLBACK_ERRORS:
                                append_no_graph_account(email, password)
                                log(
                                    f"registered account has a recoverable Graph sign-in stop; saved for "
                                    f"later recovery: {email}",
                                    "WARN",
                                )
                            elif terminal_graph_error:
                                log(
                                    f"discarding unverified account after terminal Graph error "
                                    f"({terminal_graph_error}): {email}",
                                    "WARN",
                                )
                            else:
                                append_no_graph_account(email, password)
                                log(
                                    f"registered but graph RT missing; saved to "
                                    f"outlook_no_graph.txt: {email}",
                                    "WARN",
                                )
                            if args.sleep > 0:
                                await asyncio.sleep(args.sleep)
                            continue
                        fname = write_record({
                            "email": email,
                            "password": password,
                            "refresh_token": graph["refresh_token"],
                            "client_id": graph.get("client_id") or "",
                            "graph": graph,
                            "outlook_cookies": cookies,
                            "source": "self-loop",
                            "worker": worker.worker_id,
                            "network": redacted,
                            "ts": datetime.now().isoformat(),
                        })
                        append_graph_account_to_emails_pool(email, password, graph)
                        await record_outcome(True)
                        log(
                            f"OK in {elapsed:.1f}s: {email} -> {fname} "
                            f"(pool now {count_pool()})",
                            "OK",
                        )
                    else:
                        snapshot = await record_outcome(False, "registration failed")
                        completed = snapshot["success"] + snapshot["failed"]
                        rate = 100 * snapshot["success"] / max(1, completed)
                        log(
                            f"FAIL in {elapsed:.1f}s "
                            f"(success rate {snapshot['success']}/{completed} = {rate:.0f}%)",
                            "WARN",
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await record_outcome(False, type(exc).__name__)
                log(
                    f"attempt #{idx} raised {type(exc).__name__}: {str(exc)[:200]}",
                    "WARN",
                )
            finally:
                lane_attempt += 1
                async with state_lock:
                    state["in_flight"] = max(0, state["in_flight"] - 1)
            if args.sleep > 0:
                await asyncio.sleep(args.sleep)

    await asyncio.gather(*[
        run_lane(slot)
        for slot in range(worker_plan.effective_concurrency)
    ])
    if state["stop_reason"]:
        log(
            f"stopped by success-rate breaker: {state['stop_reason']} "
            f"(success={state['success']}, fail={state['failed']})",
            "WARN",
        )
    elif args.count > 0 and state["next"] >= args.count:
        log(
            f"attempt limit reached ({args.count}), exit "
            f"(success={state['success']}, fail={state['failed']})"
        )
    elif args.target_pool and count_pool() >= args.target_pool:
        log(f"target pool reached ({count_pool()}/{args.target_pool}), exit")
    if state["stop_reason"] or (args.target_pool and count_pool() >= args.target_pool):
        return 0
    return 0 if state["failed"] == 0 and state["success"] > 0 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=0,
                    help="optional hard attempt limit (0 = no hard limit)")
    ap.add_argument("--concurrency", "-c", type=int, default=1,
                    help="concurrent registrations; dedicated residential proxies recommended")
    ap.add_argument("--target-pool", type=int, default=0,
                    help="stop registering once pool dir has this many records "
                         "(0 = no cap; producer always runs)")
    ap.add_argument("--min-success-rate", type=float, default=DEFAULT_MIN_SUCCESS_RATE,
                    help="stop when recent success rate falls below this percent (0 = disabled)")
    ap.add_argument("--success-rate-window", type=int, default=DEFAULT_SUCCESS_RATE_WINDOW,
                    help="completed attempts used by the success-rate breaker")
    ap.add_argument("--max-press", default=DEFAULT_MAX_PRESS,
                    help="OUTLOOK_REG_MAX_PRESS — captcha press-and-hold cap")
    ap.add_argument("--confirm-before-register", action="store_true",
                    help="auto-click confirmation on the signup page before filling")
    ap.add_argument("--timeout", type=int, default=180,
                    help="hard cap per attempt (seconds)")
    ap.add_argument("--sleep", type=int, default=5,
                    help="seconds between attempts (after fail or success)")
    ap.add_argument("--sleep-when-full", type=int, default=60,
                    help="seconds to sleep when pool is at target")
    ap.add_argument("--no-rotate", action="store_true",
                    help="不轮换 Clash 节点：每次 attempt 都用当前节点，不切换/不探测。"
                         "也可用环境变量 OUTLOOK_NO_ROTATE=1 开启。")
    ap.add_argument("--node", default="auto",
                    help="固定 Clash 节点；auto 使用网络配置，配合并发时建议指定")
    args = ap.parse_args()

    if args.count < 0:
        ap.error("--count must be 0 or greater")
    if not 0 <= args.min_success_rate <= 100:
        ap.error("--min-success-rate must be between 0 and 100")
    if args.success_rate_window < 1:
        ap.error("--success-rate-window must be at least 1")

    # 不轮换开关：命令行 --no-rotate 或 env OUTLOOK_NO_ROTATE 任一为真即生效。
    no_rotate = args.no_rotate or _env_truthy_norotate()

    os.environ.setdefault("OUTLOOK_REG_MAX_PRESS", args.max_press)
    if args.confirm_before_register:
        os.environ["OUTLOOK_CONFIRM_BEFORE_REGISTER"] = "1"
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

    mod = load_standalone()
    injected_proxy = ensure_clash_proxy_env()
    from common import direct_proxy, proxy_switch
    proxy_mode = proxy_switch.proxy_mode()
    requested_node = str(args.node or "auto").strip()
    if (
        proxy_mode in {"clash_auto", "clash_fixed"}
        and requested_node.lower() not in {"auto", "none", "off", "direct"}
    ):
        proxy_switch.pin_fixed_node(requested_node, "outlook")
        proxy_mode = proxy_switch.proxy_mode()
        log(f"Outlook batch pinned to Clash node: {requested_node}")
    elif no_rotate and args.concurrency > 1 and proxy_mode == "clash_auto":
        current = proxy_switch.current_node()
        if current:
            proxy_switch.pin_fixed_node(current, "outlook")
            proxy_mode = proxy_switch.proxy_mode()
            log(f"Outlook --no-rotate concurrency pinned current node: {current}")
    if injected_proxy:
        log(f"{proxy_mode} proxy env ready: {direct_proxy.redact_proxy(injected_proxy)}")
    proxy = clash_proxy_from_env()
    if not proxy:
        log("HTTP_PROXY not set — running without proxy (signup will likely fail)", "WARN")
    else:
        log(f"using {proxy_mode} proxy: {direct_proxy.redact_proxy(proxy)}")

    if proxy_mode == "clash_fixed":
        proxy_switch.ensure_proxy_mode()

    # Initialize Clash controller for per-attempt node rotation. MS PerimeterX
    # learns the egress IP fast — without rotation we get ERR_CONNECTION_CLOSED
    # after 1-2 signups from the same node.
    # --no-rotate / OUTLOOK_NO_ROTATE 时不连 Clash 控制器，固定用当前节点。
    if no_rotate or proxy_mode != "clash_auto":
        clash_client, clash_group = None, None
        if no_rotate:
            log("egress rotation DISABLED (--no-rotate / OUTLOOK_NO_ROTATE)")
        elif proxy_mode == "clash_fixed":
            log(f"fixed Clash node: {proxy_switch.current_node()}")
        elif proxy_mode == "residential":
            log("residential proxy rotation enabled")
    else:
        clash_client, clash_group = init_clash()

    log(f"pool dir: {POOL_DIR}")
    os.makedirs(POOL_DIR, exist_ok=True)
    log(f"current pool size: {count_pool()}")

    try:
        return asyncio.run(_run_registration_workers(
            args,
            mod,
            no_rotate,
            proxy_mode,
            clash_client,
            clash_group,
        ))
    except KeyboardInterrupt:
        log("received Ctrl+C, stopping Outlook workers", "WARN")
        return 130


if __name__ == "__main__":
    from common import proxy_switch

    proxy_switch.apply_platform_environment("outlook")
    raise SystemExit(main())
