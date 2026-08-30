# -*- coding: utf-8 -*-
"""
common/liye_sms.py — LIYE 卡密式接码平台客户端（liye.5x20.cn）。

平台模型：无账号、纯卡密（CDK）。一卡一次取号收码；15 分钟无码取消成功后退回次数，
换号(replace)不消耗卡密次数。国家由系统自动分配（可用号段黑名单过滤）。

API（cookie 会话；登录后 10 分钟不活动过期，过期重新 login 即可）：
  POST /api/card/login           {code, service}                建立卡密会话
  GET  /api/card/me              -> {card:{status,...}}          active/processing/exhausted
  GET  /api/orders               -> {orders:[...]}               当前卡密的订单列表
  POST /api/orders               {service} -> {order}            取号
  GET  /api/orders/<id>/status   -> {order}                      轮询状态/验证码
  POST /api/orders/<id>/action   {action:cancel|replace, ...}    取消退回 / 换号
  GET  /api/platform/reception?service=chatai                   各国成功率(探测用)

订单状态：queued → purchasing → waiting → received/completed；cancelled/failed 终态。
关键错误码：
  CARD_ALREADY_USED          卡密已用尽(终态，标记 exhausted)
  ACTIVE_ORDER_EXISTS        已有进行中订单(恢复它，不新取)
  CONCURRENCY_LIMIT_REACHED  平台并发满(稍后重试)
  CARD_LOGIN_REQUIRED / CARD_SESSION_INVALID   会话过期(重新 login)
  CANCEL_TOO_EARLY / REPLACE_TOO_EARLY         取消/换号冷却(order.cancelAvailableAt)
  ORDER_ACTION_IN_PROGRESS   上游正在确认动作(稍后查状态)
  reasonCode: NO_NUMBERS / PROVIDER_NETWORK_ERROR / PROVIDER_INVALID_RESPONSE

卡池管理（runtime/state/liye_cards.json，文件锁保证多任务并发安全）：
  - 卡密来源: .env LIYE_CARDS(逗号分隔) + runtime/state/liye_cards.txt(每行一张,可随时追加)
  - 状态: available(可用) / in_use(取号中) / cooldown(取消冷却) / exhausted(已用尽) / invalid(无效)
  - cooldown 到期后懒恢复: claim 时查实际订单状态，已退回则重新 available

CLI:
  python -m common.liye_sms status    # 卡池概览
  python -m common.liye_sms stats     # 平台各国成功率(chatai)
  python -m common.liye_sms reset CODE # 强制把某张卡置回 available(上游已退回时用)
"""

import json
import os
import re
import sys
import threading
import time

import requests

from config import (
    LIYE_API_BASE,
    LIYE_SERVICE,
    LIYE_CARDS,
    LIYE_ALLOC_TIMEOUT,
    LIYE_LEASE_SECONDS,
)
from common.file_lock import file_lock

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_TERMINAL_OK = ("received", "completed")
_TERMINAL_BAD = ("cancelled", "failed", "refunded")
_ACTIVE = ("queued", "purchasing", "waiting", "replacing", "cancelling",
           "admin_cancelling", "auto_cancelling")

_SESSIONS = {}          # "卡密code|service" -> requests.Session（进程内复用，省 login）
_SESSIONS_LOCK = threading.Lock()

# 卡密前缀 -> 平台服务。GPT-/CZ-=chatai(OpenAI)，GOO-=google(Gmail/Google)。
# 登录与建单的 service 必须与卡密类型一致，否则平台拒绝；无已知前缀时回退 LIYE_SERVICE。
_SERVICE_PREFIXES = (("GOO", "google"), ("GPT", "chatai"), ("CZ", "chatai"))


def _service_for_card(code):
    """按卡密前缀识别服务；未知前缀回退 LIYE_SERVICE(默认 chatai)。"""
    c = str(code or "").strip().upper()
    for prefix, svc in _SERVICE_PREFIXES:
        if c.startswith(prefix):
            return svc
    return (str(LIYE_SERVICE or "chatai").strip().lower() or "chatai")


class LiyeError(RuntimeError):
    """LIYE API 业务错误。code 为平台错误码，order 为随错误带回的订单(可能为空)。"""

    def __init__(self, message, code="", status=0, order=None, payload=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.order = order
        self.payload = payload or {}


def _browser_headers():
    """平台用 UA/Origin 校验请求来源：默认 python-requests 头会 403 INVALID_ORIGIN。"""
    base = LIYE_API_BASE.rstrip("/")
    return {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Origin": base,
        "Referer": base + "/",
    }


def _root():
    return os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or "."


def _cards_import_file():
    return os.path.join(_root(), "runtime", "state", "liye_cards.txt")


def _state_file():
    return os.path.join(_root(), "runtime", "state", "liye_cards.json")


def _empty_state():
    return {"version": 1, "cards": []}


def _load_state():
    try:
        with open(_state_file(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("cards"), list):
            return data
    except Exception:
        pass
    return _empty_state()


def _save_state(state):
    path = _state_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    # Windows 下 os.replace 可能被 AV/索引器短暂占用目标文件而 PermissionError，小退避重试
    for i in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 4:
                raise
            time.sleep(0.2 * (i + 1))


def _configured_cards():
    """收集 .env LIYE_CARDS + liye_cards.txt 里的卡密（去重，保序）。"""
    cards = []
    for raw in re.split(r"[,\s]+", str(LIYE_CARDS or "")):
        raw = raw.strip()
        if raw and raw not in cards:
            cards.append(raw)
    try:
        with open(_cards_import_file(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and line not in cards:
                    cards.append(line)
    except Exception:
        pass
    return cards


def has_cards():
    """是否配置了任何 LIYE 卡密（.env 或 txt 文件）。auto 路由据此决定是否纳入轮换。"""
    return bool(_configured_cards())


def sync_cards():
    """把 env/txt 里出现的新卡密并入状态文件（已存在的原状态不动）。返回新增数。"""
    with file_lock(_state_file()):
        state = _load_state()
        known = {c.get("code"): c for c in state["cards"] if isinstance(c, dict)}
        added = 0
        for code in _configured_cards():
            if code not in known:
                state["cards"].append({
                    "code": code,
                    "status": "available",
                    "service": _service_for_card(code),
                    "updated_at": time.time(),
                })
                added += 1
        _save_state(state)
    return added


def _api(session, method, path, body=None, timeout=30):
    """统一请求：JSON 进 JSON 出；非 2xx 抛 LiyeError(带平台 code/order)。"""
    url = LIYE_API_BASE.rstrip("/") + "/" + path.lstrip("/")
    try:
        if method == "GET":
            r = session.get(url, timeout=timeout)
        else:
            r = session.post(url, json=body if body is not None else {}, timeout=timeout)
    except requests.RequestException as e:
        raise LiyeError(f"liye network: {str(e)[:100]}", code="NETWORK_ERROR") from e
    try:
        data = r.json() if r.text else {}
    except ValueError:
        data = {}
    if not (200 <= r.status_code < 300):
        err = str((data or {}).get("error") or f"HTTP {r.status_code}")[:160]
        raise LiyeError(err, code=str((data or {}).get("code") or ""),
                        status=r.status_code, order=(data or {}).get("order"), payload=data)
    return data


def _login(card_code, service=None):
    """卡密登录，返回带会话 cookie 的 Session（进程内缓存复用）。
    service 缺省时按卡密前缀自动识别(GPT-/CZ-→chatai, GOO-→google)。"""
    svc = (service or _service_for_card(card_code)).strip().lower()
    key = f"{card_code}|{svc}"
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(key)
    if sess is not None:
        return sess
    sess = requests.Session()
    sess.headers.update(_browser_headers())
    _api(sess, "POST", "/api/card/login", {"code": card_code, "service": svc})
    with _SESSIONS_LOCK:
        _SESSIONS[key] = sess
    return sess


def _drop_session(card_code):
    with _SESSIONS_LOCK:
        for key in [k for k in _SESSIONS if k.startswith(f"{card_code}|")]:
            _SESSIONS.pop(key, None)


def _api_with_relogin(card_code, method, path, body=None, service=None):
    """带会话自愈的请求：会话过期(CARD_LOGIN_REQUIRED/CARD_SESSION_INVALID/401)时重登一次。"""
    try:
        return _api(_login(card_code, service), method, path, body)
    except LiyeError as e:
        if e.code not in ("CARD_LOGIN_REQUIRED", "CARD_SESSION_INVALID") and e.status != 401:
            raise
        _drop_session(card_code)
        return _api(_login(card_code, service), method, path, body)


def _order_of(data):
    order = (data or {}).get("order")
    return order if isinstance(order, dict) else None


def _orders_of(data):
    orders = (data or {}).get("orders")
    return orders if isinstance(orders, list) else []


def _phone_of(order):
    """订单号码 -> 完整 E.164 数字（含国家码、不带+），拨号前缀返回 ''。
    与 hero-sms/sms-man 对齐：调用方直接 '+' + phone 填号。"""
    raw = re.sub(r"[^\d]", "", str((order or {}).get("phone") or ""))
    return raw, ""


def _wait_phone(session, order, timeout):
    """取号后等号码落到订单上（queued/purchasing 也算在途）。返回带 phone 的 order 或 None。"""
    oid = order.get("id")
    start = time.time()
    cur = order
    while time.time() - start < timeout:
        if str(cur.get("phone") or "").strip():
            return cur
        if str(cur.get("status") or "") in _TERMINAL_BAD:
            return None
        try:
            data = _api(session, "GET", f"/api/orders/{oid}/status")
            nxt = _order_of(data)
            if nxt:
                cur = nxt
        except LiyeError as e:
            if e.code in ("CARD_LOGIN_REQUIRED", "CARD_SESSION_INVALID") or e.status == 401:
                raise
        time.sleep(3)
    return cur if str(cur.get("phone") or "").strip() else None


def _card_entry(state, code):
    for c in state["cards"]:
        if isinstance(c, dict) and c.get("code") == code:
            return c
    return None


def _entry_by_order(state, order_id):
    for c in state["cards"]:
        if isinstance(c, dict) and str(c.get("order_id") or "") == str(order_id):
            return c
    return None


def _recover_stale(state):
    """懒恢复（限量，避免长时间持有文件锁）：in_use/cooldown 超过租期的卡，
    查实际订单终态后回收。每次最多处理 3 张，其余留给下次 claim。"""
    now = time.time()
    checked = 0
    for c in state["cards"]:
        if checked >= 3:
            break
        if not isinstance(c, dict) or c.get("status") not in ("in_use", "cooldown"):
            continue
        cd = c.get("cooldown_until") or 0
        if c.get("status") == "cooldown" and cd and now < cd:
            continue
        if now - float(c.get("claimed_at") or 0) < LIYE_LEASE_SECONDS:
            continue
        checked += 1
        try:
            data = _api_with_relogin(c.get("code"), "GET", "/api/orders")
            orders = _orders_of(data)
            active = next((o for o in orders if str(o.get("status") or "") in _ACTIVE), None)
            got_code = any(str(o.get("smsCode") or "").strip() for o in orders)
            if active is None or got_code:
                c.update({"status": "exhausted" if got_code else "available",
                          "order_id": "", "cooldown_until": 0, "updated_at": now})
            else:
                # 上游还有活动订单：直接取消退回
                try:
                    _api_with_relogin(c.get("code"), "POST",
                                      f"/api/orders/{active.get('id')}/action",
                                      {"action": "cancel",
                                       "expectedActivationId": active.get("activationId"),
                                       "expectedGeneration": active.get("activationGeneration") or 0})
                    c.update({"status": "available", "order_id": "",
                              "cooldown_until": 0, "updated_at": now})
                except LiyeError as e:
                    # 平台不允许取消（如订单仍在排队）：30 分钟后再看，避免空轮询
                    wait = 1800 if e.code == "ORDER_NOT_CANCELLABLE" else 600
                    c.update({"status": "cooldown",
                              "cooldown_until": now + wait, "updated_at": now})
        except LiyeError:
            pass
    return state


def _pick_available(state, service=None):
    """挑下一张可用卡：跳过冷却中的 available 卡；指定 service 时只挑该服务的卡
    (避免 OpenAI 流程拿到 GOO- 的 Gmail 卡、反之亦然)。"""
    now = time.time()
    for c in state["cards"]:
        if not isinstance(c, dict) or c.get("status") != "available":
            continue
        if service and _service_for_card(c.get("code")) != str(service).strip().lower():
            continue
        cd = float(c.get("cooldown_until") or 0)
        if cd and now < cd:
            continue
        return c
    return None


def summary():
    sync_cards()
    with file_lock(_state_file()):
        state = _load_state()
    counts = {k: 0 for k in ("available", "in_use", "cooldown", "exhausted", "invalid")}
    for c in state["cards"]:
        if isinstance(c, dict):
            counts[c.get("status") or "?"] = counts.get(c.get("status") or "?", 0) + 1
    return {"total": len(state["cards"]), **counts, "cards": [
        {"code": (c.get("code") or "")[:6] + "..." + (c.get("code") or "")[-4:],
         "full_code": c.get("code"),
         "status": c.get("status"), "service": c.get("service"),
         "phone": c.get("phone"), "order_id": c.get("order_id"),
         "cooldown_until": c.get("cooldown_until"),
         "updated_at": c.get("updated_at")}
        for c in state["cards"] if isinstance(c, dict)
    ]}


def remove_card(code):
    """WebUI 删除卡密：从状态文件与 liye_cards.txt 一并移除（避免下次 sync 复活）。
    占用中的卡拒绝删除（可能有任务正在用）；.env LIYE_CARDS 配置的卡无法在此删除。
    返回 (ok, message)，ok=False 时 message 为可展示的拒绝原因。"""
    code = str(code or "").strip()
    if not code:
        return False, "缺少卡密"
    if code in re.split(r"[,\s]+", str(LIYE_CARDS or "")):
        return False, "该卡密由 .env LIYE_CARDS 配置，无法在此删除"
    sync_cards()
    with file_lock(_state_file()):
        state = _load_state()
        entry = _card_entry(state, code)
        if entry is None:
            return False, "卡密不存在"
        if entry.get("status") == "in_use":
            return False, "卡密正在被任务使用，请稍后或先「检查恢复」再删除"
        state["cards"] = [c for c in state["cards"]
                          if not (isinstance(c, dict) and c.get("code") == code)]
        _save_state(state)
    txt = _cards_import_file()
    try:
        with open(txt, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip() != code]
        with open(txt, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except FileNotFoundError:
        pass
    _drop_session(code)
    return True, ""


def claim(max_cards=3, alloc_timeout=None, service="chatai"):
    """取号入口：挑一张可用卡密 → 登录 → 取号 → 等号码分配。
    返回 (phone_digits, dial_code, pkey)；失败抛 RuntimeError。
    pkey = liye_<order_id>。service 默认 chatai(ChatGPT/OpenAI)；GOO- 前缀的
    google 卡会被跳过，避免 ChatGPT 流程错拿 Gmail 卡。"""
    timeout = alloc_timeout or LIYE_ALLOC_TIMEOUT
    sync_cards()
    last_err = ""
    for _ in range(max(1, max_cards)):
        with file_lock(_state_file()):
            state = _load_state()
            _recover_stale(state)
            entry = _pick_available(state, service=service)
            if entry is None:
                _save_state(state)
                scope = f" for service {service}" if service else ""
                raise RuntimeError(f"liye: no available cards{scope} ({last_err})".strip())
            svc = str(service or "chatai").strip().lower()
            entry.update({"status": "in_use", "claimed_at": time.time(),
                          "order_id": "", "phone": "", "cooldown_until": 0,
                          "service": svc, "updated_at": time.time()})
            code = entry["code"]
            _save_state(state)
        try:
            phone, dial, order = _claim_one(code, timeout, service=svc)
        except Exception as e:
            last_err = str(e)[:120]
            print(f"  [liye] card {code[:6]}...{code[-4:]} failed: {last_err}")
            _mark_failed(code, e)
            continue
        with file_lock(_state_file()):
            state = _load_state()
            c = _card_entry(state, code)
            if c is not None:
                c.update({"status": "in_use", "order_id": order.get("id"),
                          "activation_id": order.get("activationId"),
                          "generation": order.get("activationGeneration") or 0,
                          "phone": phone, "service": svc, "updated_at": time.time()})
                _save_state(state)
        print(f"  [liye] phone: +{phone} ({order.get('countryEnglishName') or order.get('countryName') or '?'}, card={code[:6]}...{code[-4:]})")
        return phone, dial, f"liye_{order.get('id')}"
    raise RuntimeError(f"liye: get phone failed ({last_err})".strip())


def _claim_one(code, timeout, service=None):
    """单卡取号：登录→(恢复在途订单或新建)→等号码。返回 (phone, dial, order)。"""
    svc = (service or _service_for_card(code)).strip().lower()
    session = _login(code, service=svc)
    # 已有活动订单则恢复（换进程/重试时会把上一张号续上）
    data = _api_with_relogin(code, "GET", "/api/orders", service=svc)
    active = next((o for o in _orders_of(data) if str(o.get("status") or "") in _ACTIVE), None)
    if active is None:
        data = _api_with_relogin(code, "POST", "/api/orders", {"service": svc}, service=svc)
        order = _order_of(data)
        if order is None:
            raise LiyeError("liye: create order returned no order")
    else:
        order = active
        if str(order.get("smsCode") or "").strip():
            raise LiyeError("liye: card already received code", code="CARD_ALREADY_USED")
        print(f"  [liye] resume active order {order.get('id')}")
    order = _wait_phone(session, order, timeout)
    if order is None or not str((order or {}).get("phone") or "").strip():
        raise LiyeError("liye: number assignment timeout/no number",
                        code="NO_NUMBERS")
    phone, dial = _phone_of(order)
    return phone, dial, order


def _mark_failed(code, exc):
    """取号失败后按错误语义置卡密状态。"""
    err = exc if isinstance(exc, LiyeError) else None
    status = "available"
    if err and err.code in ("CARD_ALREADY_USED",):
        status = "exhausted"
    elif err and err.status in (401, 403) and err.code in ("CARD_LOGIN_REQUIRED", "CARD_SESSION_INVALID", "INVALID_CARD", "CARD_NOT_FOUND"):
        status = "invalid"
    elif err and err.code in ("INVALID_LENGTH", "INVALID_CARD", "CARD_NOT_FOUND"):
        status = "invalid"
    with file_lock(_state_file()):
        state = _load_state()
        c = _card_entry(state, code)
        if c is not None:
            c.update({"status": status, "order_id": "", "phone": "",
                      "cooldown_until": time.time() + 60 if status == "available" else 0,
                      "updated_at": time.time()})
            _save_state(state)


def _resolve(pkey):
    """pkey(liye_<order_id>) -> (card_entry, order_id)。找不到抛 RuntimeError。"""
    order_id = str(pkey).replace("liye_", "", 1)
    with file_lock(_state_file()):
        state = _load_state()
    entry = _entry_by_order(state, order_id)
    if entry is None:
        raise RuntimeError(f"liye: no card bound to order {order_id}")
    return entry, order_id


def get_code(pkey, max_wait=180, interval=5):
    """轮询订单状态拿验证码；拿到即把卡密标记 exhausted（一卡一次）。"""
    entry, order_id = _resolve(pkey)
    code_card = entry.get("code")
    start = time.time()
    while time.time() - start < max_wait:
        try:
            data = _api_with_relogin(code_card, "GET", f"/api/orders/{order_id}/status")
            order = _order_of(data) or {}
            status = str(order.get("status") or "")
            raw = str(order.get("smsCode") or "").strip()
            if raw:
                m = re.search(r"\d{4,8}", raw)
                code = m.group(0) if m else raw
                print(f"  [liye] code: {code}")
                _transition(order_id, "exhausted")
                return code
            if status in _TERMINAL_BAD:
                print(f"  [liye] order {status}, no code")
                return None
        except LiyeError as e:
            if e.code in ("CARD_LOGIN_REQUIRED", "CARD_SESSION_INVALID") and e.status == 401:
                print(f"  [liye] session error: {e}")
                return None
        print(f"  [liye] waiting... ({int(time.time()-start)}s/{max_wait}s)")
        time.sleep(interval)
    return None


def release(pkey, wait_cooldown=90):
    """取消号码退回卡密次数。冷却中(CANCEL_TOO_EARLY)按 order.cancelAvailableAt 等待重试；
    彻底失败则标记 cooldown 由懒恢复兜底。"""
    entry, order_id = _resolve(pkey)
    code_card = entry.get("code")

    def _body():
        return {"action": "cancel",
                "expectedActivationId": entry.get("activation_id"),
                "expectedGeneration": entry.get("generation") or 0}

    try:
        _api_with_relogin(code_card, "POST", f"/api/orders/{order_id}/action", _body())
        _transition(order_id, "available")
        print("  [liye] number cancelled, card use returned")
        return True
    except LiyeError as e:
        if e.code == "ORDER_ACTION_IN_PROGRESS":
            time.sleep(5)
            try:
                _api_with_relogin(code_card, "POST", f"/api/orders/{order_id}/action", _body())
                _transition(order_id, "available")
                return True
            except LiyeError:
                pass
        # 冷却：从错误带回的 order 或当前状态读 cancelAvailableAt
        order = e.order or {}
        until = order.get("cancelAvailableAt") or (e.payload or {}).get("cancelAvailableAt")
        try:
            until_ts = float(until) / 1000.0 if until else 0
        except (TypeError, ValueError):
            until_ts = 0
        wait = max(0.0, until_ts - time.time()) if until_ts else 15.0
        if wait <= wait_cooldown:
            time.sleep(min(wait, wait_cooldown) + 1)
            try:
                _api_with_relogin(code_card, "POST", f"/api/orders/{order_id}/action", _body())
                _transition(order_id, "available")
                print("  [liye] number cancelled after cooldown")
                return True
            except LiyeError as e2:
                e = e2
        _transition(order_id, "cooldown", cooldown_until=time.time() + max(wait, 300))
        print(f"  [liye] cancel deferred ({e.code or e.status}), card in cooldown")
        return False


def replace(pkey, timeout=None):
    """换号（同一张卡密，不消耗次数）。返回新 (phone, dial, 新pkey) 或 None。"""
    entry, order_id = _resolve(pkey)
    code_card = entry.get("code")
    try:
        data = _api_with_relogin(code_card, "POST", f"/api/orders/{order_id}/action",
                                 {"action": "replace",
                                  "expectedActivationId": entry.get("activation_id"),
                                  "expectedGeneration": entry.get("generation") or 0})
    except LiyeError as e:
        print(f"  [liye] replace failed: {e.code or e} ")
        return None
    order = _order_of(data) or {}
    order = _wait_phone(_login(code_card), order, timeout or LIYE_ALLOC_TIMEOUT)
    if order is None or not str(order.get("phone") or "").strip():
        return None
    phone, dial = _phone_of(order)
    new_id = order.get("id")
    with file_lock(_state_file()):
        state = _load_state()
        c = _card_entry(state, code_card)
        if c is not None:
            c.update({"order_id": new_id, "activation_id": order.get("activationId"),
                      "generation": order.get("activationGeneration") or 0,
                      "phone": phone, "updated_at": time.time()})
            _save_state(state)
    print(f"  [liye] replaced: +{phone} ({order.get('countryEnglishName') or '?'})")
    return phone, dial, f"liye_{new_id}"


def _transition(order_id, status, **extra):
    with file_lock(_state_file()):
        state = _load_state()
        c = _entry_by_order(state, order_id)
        if c is None:
            return
        c.update({"status": status, "updated_at": time.time(),
                  "order_id": "" if status == "available" else c.get("order_id"),
                  "cooldown_until": extra.get("cooldown_until", 0)})
        _save_state(state)


def reset_card(code):
    """把卡密强制置回 available（确认上游已退回次数时用）。"""
    with file_lock(_state_file()):
        state = _load_state()
        c = _card_entry(state, code)
        if c is None:
            return False
        c.update({"status": "available", "order_id": "", "phone": "",
                  "cooldown_until": 0, "updated_at": time.time()})
        _save_state(state)
    _drop_session(code)
    return True


def recover_all(max_cards=30):
    """WebUI「检查恢复」：遍历 冷却 + 租期已过的占用 卡，逐张查平台真实订单后回退。
    不受懒恢复的租期/冷却门槛限制（冷却卡也查），但租期内的占用卡跳过
    （可能有任务正在用，避免误取消在途订单）。网络查询单张失败不中断整体。
    返回 {checked, recovered, exhausted, still_busy, uncancellable,
    skipped_active, failed, reasons}，reasons 为 {平台拒绝原因: 次数}。"""
    sync_cards()
    now = time.time()
    with file_lock(_state_file()):
        state = _load_state()
        candidates, leased = [], 0
        for c in state["cards"]:
            if not isinstance(c, dict) or c.get("status") not in ("in_use", "cooldown"):
                continue
            if (c.get("status") == "in_use"
                    and now - float(c.get("claimed_at") or 0) < LIYE_LEASE_SECONDS):
                leased += 1
                continue
            candidates.append(dict(c))
        candidates = candidates[:max(1, max_cards)]
    result = {"checked": 0, "recovered": 0, "exhausted": 0,
              "still_busy": 0, "uncancellable": 0, "skipped_active": leased,
              "failed": 0, "reasons": {}}
    for snap in candidates:
        result["checked"] += 1
        code = snap.get("code")
        outcome = "failed"
        try:
            orders = _orders_of(_api_with_relogin(code, "GET", "/api/orders"))
            active = next((o for o in orders if str(o.get("status") or "") in _ACTIVE), None)
            got_code = any(str(o.get("smsCode") or "").strip() for o in orders)
            if got_code:
                outcome = "exhausted"        # 平台已收到码：这卡实际已消耗
            elif active is None:
                outcome = "recovered"        # 无在途订单：次数已退回，放回可用
            else:
                try:                          # 上游还有活动订单：尝试取消退回
                    _api_with_relogin(code, "POST",
                                      f"/api/orders/{active.get('id')}/action",
                                      {"action": "cancel",
                                       "expectedActivationId": active.get("activationId"),
                                       "expectedGeneration": active.get("activationGeneration") or 0})
                    outcome = "recovered"
                except LiyeError as e:
                    if e.code == "ORDER_NOT_CANCELLABLE":
                        # 平台规则不允许取消（如订单仍在排队未分号）：
                        # 单列并拉长冷却，避免每 10 分钟空轮询
                        outcome = "uncancellable"
                        msg = str(e) or "平台不允许取消该订单"
                        result["reasons"][msg] = result["reasons"].get(msg, 0) + 1
                    else:
                        outcome = "still_busy"    # 平台冷却未到/确认中：继续挂 10 分钟冷却
        except LiyeError:
            outcome = "failed"                # 网络/登录失败：保持原状，下次再查
        result[outcome] += 1
        with file_lock(_state_file()):
            state = _load_state()
            c = _card_entry(state, code)
            if c is not None:
                if outcome == "recovered":
                    c.update({"status": "available", "order_id": "", "phone": "",
                              "cooldown_until": 0, "updated_at": time.time()})
                elif outcome == "exhausted":
                    c.update({"status": "exhausted", "order_id": "",
                              "cooldown_until": 0, "updated_at": time.time()})
                elif outcome == "still_busy":
                    c.update({"status": "cooldown",
                              "cooldown_until": time.time() + 600,
                              "updated_at": time.time()})
                elif outcome == "uncancellable":
                    # 平台不允许取消（订单卡在排队等平台推进）：30 分钟后再看
                    c.update({"status": "cooldown",
                              "cooldown_until": time.time() + 1800,
                              "updated_at": time.time()})
                _save_state(state)
    return result


def import_text(text):
    """WebUI 卡密导入：文本(行/逗号分隔，可多张) → 去重后追加到 liye_cards.txt 并入状态。
    返回 {added, skipped, bad, total, available, in_use, cooldown, exhausted, invalid}。"""
    codes = []
    bad = 0
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for raw in re.split(r"[,;，；\t ]+", line):
            raw = raw.strip()
            if not raw:
                continue
            if not re.fullmatch(r"[A-Za-z0-9\-]{6,64}", raw):
                bad += 1
                continue
            if raw not in codes:
                codes.append(raw)
    existing = set(_configured_cards())
    fresh = [c for c in codes if c not in existing]
    if fresh:
        path = _cards_import_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for c in fresh:
                f.write(c + "\n")
    sync_cards()
    counts = summary()
    return {
        "added": len(fresh),
        "skipped": len(codes) - len(fresh),
        "bad": bad,
        **{k: counts.get(k, 0) for k in
           ("total", "available", "in_use", "cooldown", "exhausted", "invalid")},
    }


def reception_stats(service=None):
    """平台各国成功率(/api/platform/reception)。返回原始 dict。"""
    svc = service or LIYE_SERVICE or "chatai"
    r = requests.get(f"{LIYE_API_BASE.rstrip('/')}/api/platform/reception",
                     params={"service": svc}, timeout=20, headers=_browser_headers())
    return r.json()


# ---------------- CLI ----------------
def _cli(argv):
    cmd = (argv[0] if argv else "status").lower()
    if cmd == "status":
        s = summary()
        print(f"LIYE 卡池: 总 {s['total']} 张 -> " +
              ", ".join(f"{k}={v}" for k, v in s.items() if k not in ("total", "cards")))
        for c in s["cards"]:
            extra = f" phone={c['phone']}" if c.get("phone") else ""
            cd = f" cooldown={int(c['cooldown_until'])}" if c.get("cooldown_until") else ""
            print(f"  {c['code']:<20} {c['status']:<10}{extra}{cd}")
        return 0
    if cmd == "stats":
        data = reception_stats(argv[1] if len(argv) > 1 else None)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if cmd == "reset":
        if len(argv) < 2:
            print("用法: python -m common.liye_sms reset <卡密>")
            return 1
        print("OK" if reset_card(argv[1]) else "卡密不在池中")
        return 0
    if cmd == "test":
        phone, dial, pkey = claim()
        print(f"phone=+{phone} pkey={pkey}，15 分钟内有效；验证码轮询用 get_code")
        return 0
    print("用法: python -m common.liye_sms [status|stats [service]|reset CODE|test]")
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
