"""账号健康（Codex OAuth 401 处置）核心模块。

401 token_revoked 的根因：OpenAI 作废了 SUB2API 持有的 access_token。
refresh_token 能否刷新是「账号被封禁」与「单纯掉授权」的分水岭：
- POST auth.openai.com/oauth/token (grant_type=refresh_token) 返回 200 → 掉授权，
  可免浏览器修复：新 credentials PUT 回 SUB2API 并恢复 active，同时回写本地凭据。
- 返回 400 invalid_grant → OpenAI 明确拒绝刷新 → 封禁嫌疑（须浏览器人工确认后才隔离）。
- 连接异常/超时 → 网络问题，不可定罪（防误杀）。

处置策略（已与用户确认）：
- 免浏览器优先：refresh_token 能刷新就不开浏览器；
- 封禁必须浏览器确认后才隔离（tools/confirm_codex_banned.py 子进程）；
- 隔离的同时把 SUB2API 账号禁用（disabled，失败退试 paused）；
- 手动 + 定时两种触发；定时巡检只自动修复，封禁嫌疑只标记不自动隔离。

注意：OpenAI 刷新时会轮换 refresh_token。因此凡真正发起过刷新（扫描探测或修复），
成功后都必须立刻把新凭据回写本地 oauth-*.session.json，绝不能继续使用旧 token，
否则下一次刷新必报 invalid_grant，把好账号误判成封禁。

扫描探测铁律（2026-09 与用户确认）：扫描一律走 SUB2API 服务端刷新
（POST /accounts/{id}/refresh，走账号自身 proxy 出站），与实际使用完全同路径：
- 成功 → 账号在 SUB2API 真实可用（token 由 SUB2API 自己轮换并保管，不产生不同步）
- 被拒（401/session ended）→ 真死号
扫描中严禁本地 _refresh_oauth 探测：本地刷新轮换出的新 token 只落本地文件，
SUB2API 手里的旧 token 立即作废——这正是「扫描显示正常、一到 SUB2API 使用就
401」的根因。本地刷新只允许出现在 fix_accounts（刷新→回写本地→PUT 回 SUB2API
的完整闭环）里。

401 实录优先：SUB2API 的 error_message 会记录实际调用 OpenAI 被拒的实录
（"Authentication failed (401): ... invalidated oauth token"），这比 status 字段
更权威——status 仍为 active 的账号也可能已记录 401（尚未触发 auto_pause）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time

import requests

from common.uploaders import _origin, _sub2api_login, _sub2api_request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEDULE_PATH = os.path.join(ROOT, "runtime", "state", "health_schedule.json")
SCAN_CACHE_PATH = os.path.join(ROOT, "runtime", "state", "health_scan_cache.json")
CONFIRM_SCRIPT = os.path.join(ROOT, "tools", "confirm_codex_banned.py")
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
# Codex OAuth 公开 client_id；优先用本地凭据文件里保存的值，缺失时兜底。
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CREDENTIAL_FIELDS = (
    "access_token", "refresh_token", "id_token", "expires_at", "email",
    "chatgpt_account_id", "chatgpt_user_id", "organization_id", "plan_type",
    "client_id",
)
# 刷新请求串行执行并保持间隔，避免并发打爆 OpenAI 授权端点触发风控。
PROBE_INTERVAL_SECONDS = 1.5
PAGE_SIZE = 100
MAX_PAGES = 50
ACTIVE_STATUS = "active"
# SUB2API 的 status 字段除 active 外均为嫌疑（auto_pause_on_expired 停用即 401 前兆）。
SUSPECT_EXEMPT = {"", "unknown", ACTIVE_STATUS}
# 禁用状态候选：disabled 不被接受时退试 paused（SUB2API 版本间枚举不定）。
DISABLE_STATUS_CANDIDATES = ("disabled", "paused")

CATEGORY_LABELS = {
    "ok": "正常",
    "suspect": "待探测",
    "fixable": "可修复",
    "suspicious_banned": "封禁嫌疑",
    "auth401": "已 401",
    "sub2api_active": "SUB2API 正常",
    "no_refresh": "无 refresh_token",
    "no_local": "本地无凭据",
    "network_error": "网络异常",
    "refresh_error": "刷新异常",
    "reauth": "可重新授权",
    "probe_error": "探测异常",
}

# 接入 SUB2API 的平台注册表：
# - fixable: 本地持有 refresh_token，可静默刷新修复（openai 独有；grok 的 RT 在服务端）；
# - asset_platform: 本地资产归档时的 platform 字段（openai 的资产在本地叫 chatgpt）。
SUB2API_PLATFORMS = {
    "openai": {"label": "GPT", "fixable": True, "asset_platform": "chatgpt"},
    "grok": {"label": "Grok", "fixable": False, "asset_platform": "grok"},
}


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_emails(emails):
    values = emails if isinstance(emails, (list, tuple, set)) else [emails]
    return sorted({str(item or "").strip().lower() for item in values if str(item or "").strip()})


# ============================================================ 本地凭据
def _tokens_base_dir():
    base = "tokens"
    try:
        from config import TOKEN_OUTPUT_DIR as configured

        base = configured
    except Exception:
        pass
    if not os.path.isabs(base):
        base = os.path.join(ROOT, base)
    return base


def _tokens_chatgpt_dir():
    return os.path.join(_tokens_base_dir(), "chatgpt")


def _tokens_grok_dir():
    return os.path.join(_tokens_base_dir(), "grok")


def list_local_credentials():
    """扫描 tokens/chatgpt/oauth-*.session.json，返回 {email: {path, data}}。"""
    result = {}
    directory = _tokens_chatgpt_dir()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return result
    for name in names:
        if not (name.startswith("oauth-") and name.endswith(".session.json")):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            continue
        email = str((data or {}).get("email") or "").strip().lower()
        if email and isinstance(data, dict):
            result.setdefault(email, {"path": path, "data": data})
    return result


def list_grok_sso_credentials():
    """扫描 tokens/grok/*.sso.json，返回 {email: {path, data, sso}}。

    文件结构 {"email","sso","ts"}；email 优先取文件内字段，缺失时退用文件名
    （与 tools/upload_tokens.py 的读取规则一致）。
    """
    result = {}
    directory = _tokens_grok_dir()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return result
    for name in names:
        if not name.endswith(".sso.json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        sso = str(data.get("sso") or "").strip()
        email = str(data.get("email") or "").strip().lower()
        if not email:
            email = name[: -len(".sso.json")].strip().lower()
        if email and sso:
            result.setdefault(email, {"path": path, "data": data, "sso": sso})
    return result


def _project_imported_emails():
    """本项目导入过 SUB2API 的邮箱全集：上传台账 ∪ 本地凭据。

    账号健康的覆盖范围=「通过本项目导入 SUB2API 的账号」：
    - 上传台账 tokens/<platform>/uploaded_sub2api.txt（CLI 上传、grok 注册
      自动导入、grok 重导入都会写入）；
    - 本地凭据兜底：重新授权批量导入等链路未写台账，但账号凭据文件
      （oauth-*.session.json / *.sso.json）由本项目生成，视为本项目账号。
    两者都不含的 SUB2API 账号（外部导入）不进健康页；只在本地、未导入
    SUB2API 的项目账号同样不进（扫描本就以 SUB2API 条目为基准）。
    """
    emails = set()
    try:
        from common.token_upload_state import uploaded_set

        for platform in ("chatgpt", "grok"):
            emails |= {
                str(item).strip().lower()
                for item in uploaded_set(platform, "sub2api") if str(item).strip()
            }
    except Exception:
        pass
    emails |= set(list_local_credentials())
    emails |= set(list_grok_sso_credentials())
    return emails


def _merge_credentials_file(path, updates):
    """把刷新结果原子回写进本地凭据文件（防轮换后的旧 token 残留）。"""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("凭据文件格式异常")
    data.update({k: v for k, v in (updates or {}).items() if v not in (None, "", [])})
    data["health_refreshed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return data


# ============================================================ SUB2API
def account_email_of(item):
    """SUB2API 账号条目的邮箱：email 字段 → extra.email → name。"""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    return str(item.get("email") or extra.get("email") or item.get("name") or "").strip().lower()


def fetch_sub2api_accounts(cfg, platform="openai"):
    """登录 SUB2API 并分页拉取指定平台的全部账号，返回 (origin, token, items)。"""
    origin = _origin(cfg.get("url") or "")
    token = _sub2api_login(origin, cfg.get("email"), cfg.get("password"))
    return origin, token, _fetch_sub2api_items(origin, token, platform)


def _fetch_sub2api_items(origin, token, platform="openai"):
    """复用已登录 token 分页拉取单平台账号（多平台扫描共享一次登录）。"""
    items = []
    for page in range(1, MAX_PAGES + 1):
        data = _sub2api_request(
            origin,
            f"/api/v1/admin/accounts?page={page}&page_size={PAGE_SIZE}&platform={platform}",
            token=token,
        )
        if isinstance(data, dict):
            batch = data.get("items") or data.get("list") or []
            pages = _to_int(data.get("pages") or data.get("total_pages"))
            total = _to_int(data.get("total"))
        elif isinstance(data, list):
            batch, pages, total = data, page, 0
        else:
            batch, pages, total = [], page, 0
        items.extend(entry for entry in batch if isinstance(entry, dict))
        if not batch or len(batch) < PAGE_SIZE:
            break
        if pages and page >= pages:
            break
        if total and len(items) >= total:
            break
    return items


def disable_sub2api_account(origin, token, account_id):
    """把 SUB2API 账号置为禁用；disabled 不被接受时退试 paused。返回 (ok, status/错误)。"""
    last = ""
    for status in DISABLE_STATUS_CANDIDATES:
        try:
            _sub2api_request(
                origin, f"/api/v1/admin/accounts/{int(account_id)}",
                token=token, method="PUT", body={"status": status}, retries=1,
            )
            return True, status
        except Exception as exc:
            last = str(exc)
    return False, last


def delete_sub2api_account(origin, token, account_id):
    """把 SUB2API 账号删除（不可恢复）。返回 (ok, 信息)。"""
    try:
        _sub2api_request(
            origin, f"/api/v1/admin/accounts/{int(account_id)}",
            token=token, method="DELETE", retries=1,
        )
        return True, "deleted"
    except Exception as exc:
        return False, str(exc)


def sub2api_401_evidence(item):
    """SUB2API 条目是否已记录 401 认证失败实录。

    error_message 形如 "Authentication failed (401): ... invalidated oauth token"。
    这是 SUB2API 实际调用 OpenAI 被拒的实录，比 status 字段更权威。
    """
    text = str((item or {}).get("error_message") or "")
    lowered = text.lower()
    return bool(
        "401" in text
        or "invalidated oauth token" in lowered
        or "authentication failed" in lowered
    )


# ============================================================ 刷新测试
def _refresh_oauth(refresh_token, client_id="", timeout=20, proxy=""):
    """用 refresh_token 向 OpenAI 换新 access_token，返回判别结果。

    - ok=True: payload 含新 access_token（可能轮换 refresh_token，调用方必须回写）
    - kind="rejected": OpenAI 明确拒绝(invalid_grant / HTTP 401) → 封禁嫌疑
    - kind="network":  连接异常/超时 → 不可定罪
    - kind="http":     其他 HTTP 错误 → 状态未知

    proxy: fix 闭环（唯一允许本地刷新的场景）必须挂全局代理出站；
    本机直连 auth.openai.com 会被地区风控拦截（HTTP 403），导致修复全部失败。
    """
    body = {
        "grant_type": "refresh_token",
        "refresh_token": str(refresh_token or ""),
        "client_id": str(client_id or DEFAULT_CLIENT_ID),
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.post(OPENAI_TOKEN_URL, json=body, timeout=timeout, proxies=proxies)
    except (requests.RequestException, ConnectionError, TimeoutError) as exc:
        # 覆盖 requests 库异常与内置网络异常(ConnectionResetError 等)，一律不可定罪
        return {"ok": False, "kind": "network", "error": str(exc)[:200]}
    try:
        payload = resp.json() if resp.content else {}
    except ValueError:
        payload = {}
    if resp.status_code == 200 and isinstance(payload, dict) and payload.get("access_token"):
        return {"ok": True, "payload": payload}
    if not isinstance(payload, dict):
        payload = {}
    error = str(payload.get("error") or "")
    detail = str(payload.get("error_description") or "") or error or str(resp.text or "")[:160]
    if error.lower() == "invalid_grant" or resp.status_code == 401:
        # invalid_grant 与 HTTP 401 都表示 refresh_token 被明确拒绝（失效/吊销），可定性为封禁嫌疑；
        # 其余 HTTP 错误（如 5xx、Cloudflare 拦截页）状态未知，不可定罪。
        if not detail or detail == error:
            detail = f"HTTP {resp.status_code}，refresh_token 已失效或被吊销"
        return {"ok": False, "kind": "rejected", "error": detail}
    return {"ok": False, "kind": "http", "status": resp.status_code, "error": detail}


def _build_credentials(local_data, refresh_payload, email=""):
    """本地凭据为底 + 刷新结果换新，产出对齐 SUB2API credentials 结构。"""
    credentials = {}
    for key in CREDENTIAL_FIELDS:
        value = (local_data or {}).get(key)
        if value not in (None, "", []):
            credentials[key] = value
    for key in ("access_token", "refresh_token", "id_token"):
        value = (refresh_payload or {}).get(key)
        if value not in (None, "", []):
            credentials[key] = value
    expires_in = _to_int((refresh_payload or {}).get("expires_in"))
    if expires_in > 0:
        credentials["expires_at"] = int(time.time() + expires_in)
    if email:
        credentials.setdefault("email", email)
    return credentials


def _repair_payload(item, credentials, group_ids=None):
    """修复 PUT 的 body：已知安全字段集 + 保留原账号配置 + status=active。

    对齐 Grok 修复先例(_create_sub2api_grok_oauth)：不用 item 整体回传，
    只发 SUB2API 已知接受的全量字段，避免把服务端审计字段原样写回。
    """
    payload = {
        "name": str(item.get("name") or credentials.get("email") or "codex-oauth"),
        "notes": str(item.get("notes") or ""),
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "concurrency": _to_int(item.get("concurrency"), 10) or 10,
        "priority": _to_int(item.get("priority"), 1) or 1,
        "rate_multiplier": _to_int(item.get("rate_multiplier"), 1) or 1,
        "group_ids": [int(g) for g in (group_ids or item.get("group_ids") or []) if g],
        "auto_pause_on_expired": True if item.get("auto_pause_on_expired") is None
        else bool(item.get("auto_pause_on_expired")),
        "status": ACTIVE_STATUS,
    }
    extra = dict(item.get("extra")) if isinstance(item.get("extra"), dict) else {}
    if credentials.get("email"):
        extra["email"] = credentials["email"]
    if credentials.get("plan_type"):
        extra["plan_type"] = credentials["plan_type"]
    if extra:
        payload["extra"] = extra
    for key in ("proxy_id", "expires_at"):
        if item.get(key) not in (None, ""):
            payload[key] = item.get(key)
    return payload


# ============================================================ SUB2API 远端自愈
def _sub2api_refresh_account(origin, token, item):
    """让 SUB2API 用它自己持有的凭据刷新指定账号（POST /accounts/{id}/refresh）。

    实测：SUB2API 管理 API 对 credentials 脱敏（列表/详情均不返回 token 明文），
    「拿 SUB2API 凭据本地刷新」不可行；这是唯一可用的远端自愈/验证通道：
    - 刷新成功 → SUB2API 持有有效 refresh_token，账号活着（本地判据已失真）
    - 刷新失败 → SUB2API 侧凭据也被 OpenAI 拒绝（session ended），封禁嫌疑坐实
    服务端出站到 OpenAI 偶发网络抖动（EOF 等），失败原因不明时自动重试一次。
    返回 (ok, 说明文字, kind)；kind: ""=成功 / "rejected"=OpenAI 明确拒绝 /
    "unknown"=其他失败（网络、限流等，不可定罪）。
    """
    note, kind = "", "unknown"
    for _attempt in range(2):
        try:
            _sub2api_request(
                origin, f"/api/v1/admin/accounts/{int(item['id'])}/refresh",
                token=token, method="POST", body={}, retries=1,
            )
            return True, "SUB2API 侧刷新成功", ""
        except Exception as exc:
            text = str(exc).replace("\n", " ")
            lowered = text.lower()
            rejected = any(
                marker in lowered
                for marker in ("status 401", "session has ended", "invalid_grant")
            )
            note = f"SUB2API 侧刷新同样失败: {text[:120]}"
            kind = "rejected" if rejected else "unknown"
            if rejected:
                break
    return False, note, kind


# ============================================================ Grok 探测
def _effective_proxy():
    """出站代理：复用全局代理（过 Cloudflare / OpenAI 地区风控），不可用时返回空串。"""
    try:
        from common.proxy_switch import effective_proxy_url

        return str(effective_proxy_url() or "")
    except Exception:
        return ""


def _probe_grok_sso(sso, proxy="", timeout=20):
    """用 sso cookie 访问 grok.com 判别账号状态，返回与 openai 探测同构的判别结果。

    - ok=True  & kind="ok":       页面正常返回（HTTP 200）→ 会话有效，账号可用
    - kind="rejected":            xAI 风控判定 denied(policy=deny) → 可直接隔离
    - kind="network":             连接层异常(status=0) → 不可定罪
    - kind="http":                非 200（常见为 CF 拦截 403）→ 状态未知，不可定罪
    """
    try:
        from common.grok_oauth import inspect_grok_account_state

        state = inspect_grok_account_state(sso, proxy=proxy, timeout=timeout)
    except Exception as exc:  # inspect 本身不抛，此兜底防导入失败等意外
        return {"ok": False, "kind": "network", "error": str(exc)[:200]}
    if state.get("denied"):
        detail = (f"xAI 风控判定 policy={state.get('policy')} "
                  f"risk={state.get('risk')} event={state.get('event')}")
        return {"ok": False, "kind": "rejected", "error": detail[:200]}
    status = _to_int(state.get("status_code"))
    if status == 200:
        return {"ok": True, "kind": "ok", "status": status,
                "detail": f"grok.com 会话有效（botFlag found={bool(state.get('found'))}）"}
    if state.get("error"):
        kind = "network" if status == 0 else "http"
        return {"ok": False, "kind": kind, "status": status,
                "error": str(state.get("error"))[:200]}
    return {"ok": False, "kind": "http", "status": status,
            "error": f"grok.com HTTP {status}，状态未知"}


# ============================================================ 扫描
def _scan_grok_row(row, item, local, need_probe, origin, token, proxy):
    """就地补全单个 grok 行的分类。

    真实校验走 SUB2API 服务端刷新（与实际使用同路径，实测 grok 账号同样支持
    /accounts/{id}/refresh）。被拒时回退本地 sso 探测区分：
    sso 仍活 → 重新授权导入即可恢复；sso 也死 → 封禁。
    """
    if not need_probe:
        if local is not None:
            row["local"] = True
        return
    ok, note, kind = _sub2api_refresh_account(origin, token, item)
    time.sleep(PROBE_INTERVAL_SECONDS)
    row["probe"] = {"kind": kind, "error": note[:160]}
    if ok:
        if local is not None:
            row["local"] = True
        if row["suspect"]:
            row["category"] = "sub2api_active"
            row["detail"] = "服务端刷新成功，账号在 SUB2API 已恢复可用"
        else:
            row["detail"] = "服务端刷新验证通过，账号真实可用"
        return
    if kind == "rejected":
        if local is None:
            row["category"] = "suspicious_banned"
            row["detail"] = f"服务端刷新被拒且本地无 sso 文件；{note[:80]}"
            return
        row["local"] = True
        sso_result = _probe_grok_sso(local["sso"], proxy=proxy)
        if sso_result["ok"]:
            row["category"] = "reauth"
            row["detail"] = ("SUB2API 持有的 oauth 凭据被拒，但本地 sso 仍有效，"
                             "走「重新授权导入」即可恢复")
        else:
            row["category"] = "suspicious_banned"
            row["detail"] = f"服务端刷新与本地 sso 均被拒，疑似封禁；{note[:60]}"
        return
    row["category"] = "probe_error"
    row["detail"] = f"服务端刷新网络异常，无法判定（请重扫）：{note[:80]}"


# ============================================================ 扫描结果缓存
# 缓存语义：账号健康页进入/切平台只显示上次扫描的结果，直到下一次扫描才刷新。
def _load_scan_cache():
    try:
        with open(SCAN_CACHE_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_scan_cache(cache):
    os.makedirs(os.path.dirname(SCAN_CACHE_PATH), exist_ok=True)
    tmp = f"{SCAN_CACHE_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, SCAN_CACHE_PATH)


def _merge_scan_cache(cache, platform_names, result, probe, emails=None):
    """把扫描结果按平台合并进缓存。全量扫描整片替换该平台；
    定向扫描（emails）只替换「平台|邮箱」相同的行，其余账号沿用旧缓存。"""
    saved_at = time.time()
    scans = cache.get("scans") if isinstance(cache.get("scans"), dict) else {}
    wanted = _normalize_emails(emails)
    for name in platform_names:
        accounts = [row for row in (result.get("accounts") or [])
                    if row.get("platform") == name]
        if wanted:
            base = scans.get(name) if isinstance(scans.get(name), dict) else {}
            old_rows = ((base.get("data") or {}).get("accounts")) or []
            by_key = {f"{r.get('platform')}|{r.get('email')}": r for r in old_rows}
            for row in accounts:
                by_key[f"{row.get('platform')}|{row.get('email')}"] = row
            accounts = list(by_key.values())
        summary = {}
        for row in accounts:
            key = str(row.get("category") or "unknown")
            summary[key] = summary.get(key, 0) + 1
        scans[name] = {
            "saved_at": saved_at,
            "probe": probe,
            "data": {
                "ok": True,
                "accounts": accounts,
                "summary": summary,
                "total_in_sub2api": result.get("total_in_sub2api"),
                "excluded_not_imported": result.get("excluded_not_imported"),
                "categories": CATEGORY_LABELS,
            },
        }
    cache["version"] = 1
    cache["scans"] = scans
    cache["updated_at"] = saved_at
    return cache


def update_scan_cache(platform_names, result, probe, emails=None):
    """扫描成功后落盘缓存（best-effort，失败不影响扫描结果返回）。"""
    try:
        _save_scan_cache(_merge_scan_cache(_load_scan_cache(), platform_names,
                                           result, probe, emails))
    except Exception:
        pass


def cached_scan(platform="all"):
    """读取上次扫描缓存（不发起任何网络请求），结构与 scan_accounts 返回对齐。"""
    cache = _load_scan_cache()
    scans = cache.get("scans") if isinstance(cache.get("scans"), dict) else {}
    names = [n for n in (list(SUB2API_PLATFORMS) if platform == "all" else [platform])
             if isinstance(scans.get(n), dict)]
    if not names:
        return {"ok": True, "cached": False}
    accounts, summary = [], {}
    saved_at, probes = 0.0, []
    total_in_sub2api = excluded_not_imported = 0
    for name in names:
        entry = scans[name]
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        rows = data.get("accounts") or []
        accounts.extend(rows)
        for key, count in (data.get("summary") or {}).items():
            summary[key] = summary.get(key, 0) + count
        saved_at = max(saved_at, float(entry.get("saved_at") or 0))
        if entry.get("probe"):
            probes.append(str(entry["probe"]))
        total_in_sub2api += int(data.get("total_in_sub2api") or 0)
        excluded_not_imported += int(data.get("excluded_not_imported") or 0)
    return {
        "ok": True,
        "cached": True,
        "saved_at": saved_at or None,
        "probe": "+".join(sorted(set(probes))) or None,
        "platforms": names,
        "accounts": accounts,
        "summary": summary,
        "total_in_sub2api": total_in_sub2api,
        "excluded_not_imported": excluded_not_imported,
        "categories": CATEGORY_LABELS,
    }


def scan_accounts(cfg, probe="suspects", emails=None, platform="all"):
    """扫描 SUB2API 上多平台账号健康状态并分类。

    范围限定：只覆盖「通过本项目导入 SUB2API 的账号」（上传台账 ∪ 本地凭据
    与 SUB2API 条目的交集）。SUB2API 上外部导入的账号、只在本地未导入的
    项目账号都不进结果（用户确认的口径）。

    probe: "all"(真实校验全部：对每个账号做 SUB2API 服务端刷新) /
           "suspects"(默认，只校验 status 非 active 或已记录 401 的账号) / "none"(仅列出)
    emails: 只看这些邮箱（匹配条目 email 或 name）
    platform: "all"(默认，SUB2API_PLATFORMS 全部) / "openai" / "grok"
    返回 {ok, accounts:[row], summary:{}, total_in_sub2api, excluded_not_imported}
    校验方式与实际使用同路径（SUB2API 自己刷新自己持有的凭据），结果即真实可用性。
    """
    if not (str(cfg.get("url") or "").strip() and str(cfg.get("email") or "").strip()
            and str(cfg.get("password") or "").strip()):
        return {"ok": False, "error": "缺少 SUB2API 地址/邮箱/密码（请先在网络页保存 SUB2API 配置）"}
    wanted = _normalize_emails(emails)
    imported = _project_imported_emails()
    if not imported and not wanted:
        return {"ok": False,
                "error": "本地没有本项目导入 SUB2API 的账号记录（上传台账与本地凭据均为空），"
                         "请先通过项目导入账号"}
    platforms = list(SUB2API_PLATFORMS) if platform == "all" else [platform]
    if any(name not in SUB2API_PLATFORMS for name in platforms):
        return {"ok": False,
                "error": f"不支持的平台: {platform}（可选: all/{'/'.join(SUB2API_PLATFORMS)}）"}
    origin = _origin(cfg.get("url") or "")
    try:
        token = _sub2api_login(origin, cfg.get("email"), cfg.get("password"))
        items_by_platform = {
            name: _fetch_sub2api_items(origin, token, name) for name in platforms
        }
    except Exception as exc:
        return {"ok": False, "error": f"SUB2API 拉取失败: {str(exc)[:160]}"}
    locals_index = list_local_credentials()
    grok_index = list_grok_sso_credentials()
    grok_proxy = _effective_proxy()
    rows, counts = [], {}

    def add(row):
        rows.append(row)
        counts[row["category"]] = counts.get(row["category"], 0) + 1

    total_in_sub2api = 0
    excluded_not_imported = 0
    for name in platforms:
        meta = SUB2API_PLATFORMS[name]
        items = items_by_platform[name]
        total_in_sub2api += len(items)
        for item in items:
            email = account_email_of(item)
            if wanted and email not in wanted:
                continue
            if email not in imported:
                # 范围限定：不是本项目导入的 SUB2API 账号（外部导入）不做健康。
                excluded_not_imported += 1
                continue
            status = str(item.get("status") or "").strip().lower() or "unknown"
            sub_401 = sub2api_401_evidence(item)
            suspect = status not in SUSPECT_EXEMPT or sub_401
            suspect_detail = f"SUB2API status={status}"
            if sub_401:
                suspect_detail += "，已记录 401 认证失败"
            row = {
                "id": item.get("id"),
                "name": str(item.get("name") or ""),
                "email": email,
                "platform": name,
                "platform_label": meta["label"],
                "status": status,
                "sub_401": sub_401,
                "suspect": suspect,
                "local": False,
                "category": "ok" if not suspect else "suspect",
                "detail": "" if not suspect else suspect_detail,
            }
            need_probe = probe == "all" or (probe == "suspects" and suspect)
            if name == "grok":
                _scan_grok_row(row, item, grok_index.get(email), need_probe, origin, token, grok_proxy)
                add(row)
                continue
            row["local"] = email in locals_index
            if need_probe:
                # 真实校验：SUB2API 服务端刷新，与实际使用完全同路径。
                # 严禁本地刷新探测（token 轮换只落本地，SUB2API 旧 token 立即作废）。
                ok2, note, kind2 = _sub2api_refresh_account(origin, token, item)
                time.sleep(PROBE_INTERVAL_SECONDS)
                row["probe"] = {"kind": kind2, "error": note[:160]}
                if ok2:
                    if suspect:
                        row["category"] = "sub2api_active"
                        row["detail"] = "服务端刷新成功，账号在 SUB2API 已恢复可用"
                    else:
                        row["detail"] = "服务端刷新验证通过，账号真实可用"
                elif kind2 == "rejected":
                    local = locals_index.get(email)
                    if local is not None and str((local["data"] or {}).get("refresh_token") or "").strip():
                        row["category"] = "fixable"
                        row["detail"] = ("SUB2API 持有的凭据已被 OpenAI 拒绝（session ended）；"
                                         "本地另有凭据副本，可尝试「修复」同步回 SUB2API")
                    else:
                        row["category"] = "suspicious_banned"
                        row["detail"] = f"服务端刷新被拒（session ended），疑似封禁；{note[:80]}"
                else:
                    row["category"] = "probe_error"
                    row["detail"] = f"服务端刷新网络异常，无法判定（请重扫）：{note[:80]}"
            add(row)
    result = {
        "ok": True,
        "accounts": rows,
        "summary": counts,
        "total_in_sub2api": total_in_sub2api,
        "excluded_not_imported": excluded_not_imported,
        "categories": CATEGORY_LABELS,
    }
    # 扫描结果落盘缓存：健康页进入/切平台显示这份快照，直到下一次扫描
    update_scan_cache(platforms, result, probe, emails)
    return result


# ============================================================ 修复
def fix_accounts(cfg, emails):
    """对指定邮箱免浏览器修复：刷新 → 回写本地 → PUT SUB2API 恢复 active。"""
    if not (str(cfg.get("url") or "").strip() and str(cfg.get("email") or "").strip()
            and str(cfg.get("password") or "").strip()):
        return {"ok": False, "error": "缺少 SUB2API 地址/邮箱/密码（请先在网络页保存 SUB2API 配置）"}
    wanted = _normalize_emails(emails)
    if not wanted:
        return {"ok": False, "error": "未选择要修复的账号"}
    origin, token, items = fetch_sub2api_accounts(cfg)
    by_email = {}
    for item in items:
        by_email.setdefault(account_email_of(item), item)
    locals_index = list_local_credentials()
    results = []
    for email in wanted:
        item = by_email.get(email)
        if item is None or not item.get("id"):
            results.append({"email": email, "state": "skipped", "detail": "SUB2API 上未找到该账号"})
            continue
        local = locals_index.get(email)
        if local is None:
            results.append({"email": email, "state": "skipped",
                            "detail": "本地无 oauth 凭据文件，请走「重新授权」"})
            continue
        refresh_token = str((local["data"] or {}).get("refresh_token") or "").strip()
        if not refresh_token:
            results.append({"email": email, "state": "skipped",
                            "detail": "本地凭据无 refresh_token，请走「重新授权」"})
            continue
        # fix 闭环本地刷新必须挂全局代理：本机直连 auth.openai.com 会被地区风控
        # 拦截（HTTP 403），导致修复全部失败（2026-09-08 实测）。
        refreshed = _refresh_oauth(refresh_token, (local["data"] or {}).get("client_id"),
                                   proxy=_effective_proxy())
        time.sleep(PROBE_INTERVAL_SECONDS)
        kind = str(refreshed.get("kind") or "")
        if kind == "network":
            results.append({"email": email, "state": "failed",
                            "detail": "网络异常，未修复（稍后重试，不会误判）"})
            continue
        if not refreshed["ok"]:
            if kind == "rejected":
                # 本地 RT 已死多半是 SUB2API 侧刷新过令牌：触发 SUB2API 刷新，
                # 成功则账号在 SUB2API 恢复可用（本地凭据需重新授权才能恢复）。
                ok2, note, _kind2 = _sub2api_refresh_account(origin, token, item)
                if ok2:
                    results.append({"email": email, "state": "fixed",
                                    "detail": "本地凭据已过期（SUB2API 侧刷新过令牌），已触发 SUB2API "
                                              "刷新成功，账号在 SUB2API 上正常可用；如需本地直连请重新授权"})
                    continue
                results.append({"email": email, "state": "failed",
                                "detail": "OpenAI 拒绝刷新，SUB2API 侧刷新也失败，疑似封禁（请走「封禁确认」）"})
                continue
            if sub2api_401_evidence(item):
                # 本地刷新被地区风控拦截但 SUB2API 已实录 401：改走服务端刷新终判。
                ok2, note, kind2 = _sub2api_refresh_account(origin, token, item)
                if ok2:
                    results.append({"email": email, "state": "fixed",
                                    "detail": "本地刷新被拦截，但 SUB2API 服务端刷新成功，账号已恢复可用"})
                    continue
                if kind2 == "rejected":
                    results.append({"email": email, "state": "failed",
                                    "detail": "SUB2API 已记录 401 且服务端刷新被拒（session ended），"
                                              "疑似封禁（请走「封禁确认」）"})
                    continue
                results.append({"email": email, "state": "failed",
                                "detail": "SUB2API 已记录 401，账号当前不可用；本地刷新被拦截"
                                          f"（HTTP {refreshed.get('status')}），服务端刷新网络异常无法定根因，请稍后重试"})
                continue
            results.append({"email": email, "state": "failed",
                            "detail": f"刷新失败 HTTP {refreshed.get('status')}"})
            continue
        # 先回写本地（防轮换），再更新 SUB2API。
        try:
            _merge_credentials_file(local["path"], refreshed["payload"])
        except Exception as exc:
            results.append({"email": email, "state": "failed",
                            "detail": f"本地凭据回写失败，已中止: {str(exc)[:80]}"})
            continue
        credentials = _build_credentials(local["data"], refreshed["payload"], email)
        if not credentials.get("access_token"):
            results.append({"email": email, "state": "failed", "detail": "刷新结果缺少 access_token"})
            continue
        group_ids = [int(g) for g in (item.get("group_ids") or []) if g]
        if not group_ids:
            try:
                from common.oauth_codex import find_group_id

                group_ids = [int(find_group_id(origin, token, "codex"))]
            except Exception:
                group_ids = []
        try:
            _sub2api_request(
                origin, f"/api/v1/admin/accounts/{int(item['id'])}",
                token=token, method="PUT", body=_repair_payload(item, credentials, group_ids),
                retries=1,
            )
        except Exception as exc:
            results.append({"email": email, "state": "failed",
                            "detail": f"SUB2API 更新失败: {str(exc)[:120]}"})
            continue
        results.append({"email": email, "state": "fixed",
                        "detail": "已刷新凭据并恢复 SUB2API active"})
    summary = {}
    for row in results:
        summary[row["state"]] = summary.get(row["state"], 0) + 1
    return {"ok": True, "results": results, "summary": summary}


# ============================================================ 隔离（浏览器确认）
def find_cookie_file(email):
    """按邮箱定位本地 ChatGPT CK 文件（cookies/chatgpt/full_*.json）。"""
    from common import asset_store

    normalized = str(email or "").strip().lower()
    if not normalized:
        return ""
    for directory in asset_store._cookie_directories("chatgpt"):
        if not directory.is_dir():
            continue
        accounts = asset_store._account_map(directory)  # cookie 值 -> email
        values = {
            value for value, owner in accounts.items()
            if str(owner or "").strip().lower() == normalized
        }
        if not values:
            continue
        for path in sorted(directory.glob("full_*.json")):
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            for entry in data:
                if isinstance(entry, dict) and str(entry.get("value") or "") in values:
                    return str(path)
    return ""


def _parse_confirm_output(raw):
    text = (raw or b"").decode("utf-8", "replace")
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return {"ok": False, "error": "确认脚本无有效输出"}


async def _confirm_banned_via_browser(email, cookie_file, timeout=240):
    """子进程跑 tools/confirm_codex_banned.py，返回其 stdout 最后一行 JSON。"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", CONFIRM_SCRIPT,
        "--cookie", cookie_file, "--email", str(email or ""),
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": "浏览器确认超时"}
    return _parse_confirm_output(out)


async def _quarantine_local_assets(email, asset_platform, reason):
    """把指定平台的本地资产归档隔离（archive_asset_results 的平台参数化封装）。

    best-effort：找不到资产或归档失败都只打 WARN，不抛异常影响主流程。
    """
    cleaned = str(email or "").strip().lower()
    if not cleaned:
        return {"moved_accounts": 0}
    try:
        from common import asset_store

        return await asyncio.to_thread(
            asset_store.archive_asset_results,
            [{"platform": asset_platform, "email": cleaned, "source": ""}],
            "quarantine",
            str(reason or "")[:200],
        )
    except Exception as exc:
        print(f"  [WARN] 封禁资产隔离失败({cleaned}): {str(exc)[:120]}")
        return {"moved_accounts": 0, "error": str(exc)[:160]}


async def quarantine_accounts(cfg, emails, confirm=True, platform="openai", sub2api_action="disable"):
    """封禁处置：隔离本地资产 → SUB2API 禁用/删除（sub2api_action 可配，默认禁用）。

    openai: confirm=True 时先浏览器人工确认，见封禁标记才隔离；
    grok:   扫描探测已得 xAI 风控判定(denied)，无需也无法走浏览器确认，直接隔离。
    """
    meta = SUB2API_PLATFORMS.get(platform)
    if meta is None:
        return {"ok": False,
                "error": f"不支持的平台: {platform}（可选: {'/'.join(SUB2API_PLATFORMS)}）"}
    action = str(sub2api_action or "disable").strip().lower()
    if action not in ("disable", "delete"):
        return {"ok": False, "error": f"SUB2API 处置方式必须是 disable/delete，收到: {action}"}
    wanted = _normalize_emails(emails)
    if not wanted:
        return {"ok": False, "error": "未选择要处置的账号"}
    browser_confirm = bool(confirm) and platform == "openai"
    fallback_marker = "xAI 风控判定(policy=deny)" if platform == "grok" else "跳过浏览器确认"
    results = []
    origin = token = ""
    items_by_email = {}
    if (str(cfg.get("url") or "").strip() and str(cfg.get("email") or "").strip()
            and str(cfg.get("password") or "").strip()):
        try:
            origin, token, items = fetch_sub2api_accounts(cfg, platform=platform)
            for item in items:
                items_by_email.setdefault(account_email_of(item), item)
        except Exception as exc:
            results.append({"email": "", "state": "warning",
                            "detail": f"SUB2API 不可用，将只隔离本地资产: {str(exc)[:100]}"})
            origin = token = ""
    for email in wanted:
        marker = ""
        if browser_confirm:
            cookie_file = find_cookie_file(email)
            if not cookie_file:
                results.append({"email": email, "state": "skipped",
                                "detail": "本地无 CK 文件，无法浏览器确认；请改走「重新授权」"})
                continue
            verdict = await _confirm_banned_via_browser(email, cookie_file)
            if not verdict.get("ok"):
                results.append({"email": email, "state": "failed",
                                "detail": f"浏览器确认失败: {str(verdict.get('error') or '')[:100]}"})
                continue
            if not verdict.get("banned"):
                session_note = "" if verdict.get("session_valid") else "（session 已失效）"
                results.append({"email": email, "state": "skipped",
                                "detail": f"浏览器确认未见封禁标记{session_note}，建议走「重新授权」"})
                continue
            marker = str(verdict.get("marker") or "banned")
        else:
            marker = fallback_marker
        reason = f"账号健康: 封禁确认({marker})"
        moved = await _quarantine_local_assets(email, meta["asset_platform"], reason)
        sub_note = ""
        item = items_by_email.get(email)
        if origin and item and item.get("id"):
            if action == "delete":
                ok2, status_or_err = delete_sub2api_account(origin, token, item["id"])
                sub_note = (f"，SUB2API 已删除" if ok2
                            else f"，SUB2API 删除失败: {str(status_or_err)[:60]}")
            else:
                ok2, status_or_err = disable_sub2api_account(origin, token, item["id"])
                sub_note = (f"，SUB2API 已禁用({status_or_err})" if ok2
                            else f"，SUB2API 禁用失败: {str(status_or_err)[:60]}")
        elif origin:
            sub_note = "，SUB2API 上未找到对应账号"
        results.append({
            "email": email,
            "platform": platform,
            "state": "quarantined",
            "detail": (f"已隔离本地资产({moved.get('moved_accounts', 0)} 个文件)"
                       f"{sub_note}；标记: {marker}"),
        })
    return {"ok": True, "results": results}


# ============================================================ Claude 本地清点
def claude_inventory():
    """本地清点 Claude CK 文件（Claude 未接入 SUB2API，无 401 处置，不做网络探测）。

    只读本地文件与 accounts.txt 账号映射，零风控风险。
    返回 {ok, items:[{file, email, size_kb, modified, directory}], total, note}。
    """
    from common import asset_store

    items, seen = [], set()
    for directory in asset_store._cookie_directories("claude"):
        if not directory.is_dir():
            continue
        accounts = asset_store._account_map(directory)  # cookie 值 -> 邮箱
        for path in sorted(directory.glob("full_*.json")):
            try:
                resolved = str(path.resolve()).lower()
            except OSError:
                resolved = str(path).lower()
            if resolved in seen:
                continue
            seen.add(resolved)
            size_kb = 0.0
            modified = ""
            cookie_values = []
            try:
                stat = path.stat()
                size_kb = round(stat.st_size / 1024, 1)
                modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, list):
                    cookie_values = [
                        str(entry.get("value") or "")
                        for entry in data if isinstance(entry, dict)
                    ]
            except Exception:
                pass
            email = ""
            for value in cookie_values:
                owner = accounts.get(value)
                if owner:
                    email = str(owner).strip().lower()
                    break
            items.append({
                "file": str(path.name),
                "email": email,
                "size_kb": size_kb,
                "modified": modified,
                "directory": str(directory),
            })
    items.sort(key=lambda row: (row["email"] == "", row["email"], row["file"]))
    return {
        "ok": True,
        "items": items,
        "total": len(items),
        "note": "Claude 未接入 SUB2API，仅本地清点，无 401 探测与处置",
    }


# ============================================================ 重新授权文件准备
def prepare_reauth_lines(emails):
    """为重新授权生成与批量授权导入兼容的账号行。

    优先 CK 文件（单行 JSON {"email":..,"cookies":..}，若资产库有该邮箱
    凭据则附带 password/refresh_token/client_id 供 OAuth 取码），其次邮箱凭据
    （email----password[----refresh_token----client_id]）。
    返回 (lines, skipped:[{email, reason}])。
    """
    from common import asset_store

    lines, skipped = [], []
    for email in _normalize_emails(emails):
        cookie_file = find_cookie_file(email)
        if cookie_file:
            try:
                with open(cookie_file, encoding="utf-8") as handle:
                    cookies = json.load(handle)
            except Exception:
                cookies = None
            if isinstance(cookies, list) and cookies:
                payload = {"email": email, "cookies": cookies}
                # 附带邮箱取码凭据（密码 / Microsoft Graph RT），否则 OAuth 要求
                # 邮箱验证码时导入脚本会因"没有可用取码方式"直接失败。
                mailbox = asset_store.find_mailbox_credentials(email) or {}
                for key in ("password", "refresh_token", "client_id"):
                    if mailbox.get(key):
                        payload[key] = str(mailbox[key])
                lines.append(json.dumps(payload, ensure_ascii=False))
                continue
        record = asset_store.find_mailbox_credentials(email)
        if record and record.get("password"):
            parts = [email, str(record["password"])]
            if record.get("refresh_token") and record.get("client_id"):
                parts += [str(record["refresh_token"]), str(record["client_id"])]
            elif record.get("refresh_token"):
                parts.append(str(record["refresh_token"]))
            lines.append("----".join(parts))
            continue
        skipped.append({"email": email, "reason": "本地无 CK 文件，也无邮箱凭据"})
    return lines, skipped


# ============================================================ 定时巡检
def load_schedule():
    try:
        with open(SCHEDULE_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_schedule(enabled=None, interval_minutes=None, **extra):
    current = load_schedule()
    if enabled is not None:
        current["enabled"] = bool(enabled)
    if interval_minutes is not None:
        current["interval_minutes"] = max(10, min(1440, int(interval_minutes)))
    for key, value in extra.items():
        if value is not None:
            current[key] = value
    os.makedirs(os.path.dirname(SCHEDULE_PATH), exist_ok=True)
    tmp = f"{SCHEDULE_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(current, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, SCHEDULE_PATH)
    return current


def schedule_due(sched, now=None):
    if not sched.get("enabled"):
        return False
    interval = max(10, _to_int(sched.get("interval_minutes"), 60) or 60) * 60
    last = _to_float(sched.get("last_run"))
    if last <= 0:
        return True
    return (now or time.time()) - last >= interval


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def run_scheduled_job(cfg, log=print):
    """定时巡检任务体：探测嫌疑账号 → 自动修复可修复者 → 封禁嫌疑只标记。

    覆盖 SUB2API 全平台（openai/grok）；自动修复仅 openai（grok 无本地 RT），
    封禁嫌疑与 grok 重授权均只标记不自动处置（用户确认的策略：必须人工参与）。
    """
    save_schedule(last_run=time.time(), last_state="running")
    scan = scan_accounts(cfg, probe="suspects", platform="all")
    if not scan.get("ok"):
        save_schedule(last_state="failed", last_error=str(scan.get("error") or "")[:200])
        log(f"[health] 定时巡检失败: {scan.get('error')}")
        return {"ok": False, "error": scan.get("error")}
    fixable = [row["email"] for row in scan["accounts"]
               if row.get("category") == "fixable" and row.get("platform") == "openai"]
    banned = [f"{row.get('platform_label') or ''} {row['email']}".strip()
              for row in scan["accounts"] if row.get("category") == "suspicious_banned"]
    reauth = [row["email"] for row in scan["accounts"] if row.get("category") == "reauth"]
    fixed = 0
    if fixable:
        outcome = fix_accounts(cfg, fixable)
        fixed = sum(1 for row in outcome.get("results", []) if row.get("state") == "fixed")
    summary = scan.get("summary") or {}
    brief = "；".join(
        f"{CATEGORY_LABELS.get(key, key)} {count}" for key, count in sorted(summary.items())
    ) or "无账号"
    save_schedule(
        last_state="done",
        last_result={"summary": summary, "auto_fixed": fixed,
                     "banned_marked": banned, "reauth_marked": reauth,
                     "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
    )
    log(f"[health] 定时巡检完成: {brief}；自动修复 {fixed}/{len(fixable)}"
        + (f"；封禁嫌疑 {len(banned)} 个待人工确认" if banned else "")
        + (f"；可重授权 {len(reauth)} 个待重导入" if reauth else ""))
    return {"ok": True, "brief": brief, "summary": summary,
            "auto_fixed": fixed, "banned_marked": banned, "reauth_marked": reauth}


async def scheduler_loop(cfg_provider, log=print):
    """每 30 秒检查一次 health_schedule.json，到期则执行巡检（随 WebUI 常驻）。"""
    while True:
        try:
            if schedule_due(load_schedule()):
                cfg = cfg_provider() if callable(cfg_provider) else cfg_provider
                await asyncio.to_thread(run_scheduled_job, cfg, log)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 常驻循环绝不能因单次异常退出
            log(f"[health] 定时巡检异常: {str(exc)[:160]}")
        await asyncio.sleep(30)
