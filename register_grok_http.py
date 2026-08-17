# -*- coding: utf-8 -*-
"""
Grok (x.ai) 自动注册 —— 纯 HTTP 协议版（集成 HM2899/grokcli-2api 的 xconsole_client）。

为什么用纯 HTTP：
  - 早期的浏览器版会卡在 accounts.x.ai 的验证码
    XXX-XXX 掩码输入框——在浏览器里逐字敲会被掩码打乱、弹回 Retry。
  - 本版完全不开浏览器：用 curl_cffi 浏览器指纹直连 accounts.x.ai，走 gRPC-web 发码/验码
    + Next.js server action 建号。验证码是**字符串直传** gRPC，从根上绕开掩码输入框；
    Turnstile 用打码平台(CapSolver/EZCaptcha)解。

流程（对齐 grok-build-auth/run.py，去掉 OAuth，只要 sso）：
  1) 切 Clash 干净节点（过 grok CF）
  2) visit_home + load_signup_page（拿 cf_clearance cookie + 动态 next-action/sitekey）
  3) 临时邮箱建号 -> CreateEmailValidationCode -> 轮询取码 -> VerifyEmailValidationCode
  4) ValidatePassword
  5) CapSolver 解 Turnstile（用动态抓到的 sitekey）
  6) create_account（castleRequestToken="", conversionId=uuid）
  7) fetch_sso_token / obtain_session_via_password -> 落 sso token

用法:
    .venv\\Scripts\\python.exe register_grok_http.py --count 1
    .venv\\Scripts\\python.exe register_grok_http.py --count 1 --node "美国 01"
    .venv\\Scripts\\python.exe register_grok_http.py --count 1 --sub2api
"""

import argparse
import os
import sys
import time
import uuid

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

import requests

from xconsole_client import XConsoleAuthClient, config as C

from common import proxy_switch
from common.temp_email import create_mailbox, _scan_once
from common.session_export import save_grok_token
from common.token_upload_state import mark_uploaded

try:
    from config import (
        YESCAPTCHA_API_KEY,
        YESCAPTCHA_API_BASE,
        CAPSOLVER_API_KEY,
        EZCAPTCHA_API_KEY,
        EZCAPTCHA_API_BASE,
        SUB2API_URL,
        SUB2API_EMAIL,
        SUB2API_PASSWORD,
        SUB2API_GROK_GROUP,
        SUB2API_GROK_PROXY_ID,
    )
except Exception:
    YESCAPTCHA_API_KEY = ""
    YESCAPTCHA_API_BASE = "https://api.yescaptcha.com"
    CAPSOLVER_API_KEY = ""
    EZCAPTCHA_API_KEY = ""
    EZCAPTCHA_API_BASE = "https://api.ez-captcha.com"
    SUB2API_URL = ""
    SUB2API_EMAIL = ""
    SUB2API_PASSWORD = ""
    SUB2API_GROK_GROUP = "grok"
    SUB2API_GROK_PROXY_ID = 0

try:
    from config import TEMP_EMAIL_PROVIDER
except Exception:
    TEMP_EMAIL_PROVIDER = "yyds"

# 运行时生效的临时邮箱 provider：默认取 .env 的 TEMP_EMAIL_PROVIDER，可被 --provider 覆盖。
PROVIDER = TEMP_EMAIL_PROVIDER

PLATFORM = "grok"
CLASH_PROXY = proxy_switch.effective_proxy_url()
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

GROK_SENDER = ("x.ai", "grok", "noreply", "no-reply")
GROK_SUBJECT = ("code", "verify", "verification", "grok", "x.ai", "confirm",
                "確認", "認証", "コード", "验证", "驗證")
# x.ai 验证码为 XXX-XXX（字母数字含分隔符）；也兜底 6 位连写
CODE_REGEX = r"\b((?=[A-Z0-9-]*[A-Z])(?:[A-Z0-9]{2,4}-[A-Z0-9]{2,4}|[A-Z0-9]{6}))\b"


def _rand_password():
    return "Pw" + os.urandom(6).hex() + "!a#A"


def _rand_name():
    import random
    import string
    w = random.choice("BCDFGHJKLMNPQRST") + random.choice("aeiou") + \
        "".join(random.choices(string.ascii_lowercase, k=4))
    return w.capitalize()


# ============================================================ Turnstile 打码（同步）
def solve_turnstile(sitekey, page_url, action=None, cdata=None, max_wait=140):
    """按 YesCaptcha、CapSolver、EZ-Captcha 顺序解 Turnstile。"""
    if YESCAPTCHA_API_KEY:
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
            if token:
                print(f"  [yescaptcha] solved (token len={len(token)})")
                return token
        except Exception as e:
            print(f"  [yescaptcha] error: {str(e)[:80]}")
    if CAPSOLVER_API_KEY:
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
            if data.get("errorId", 1) == 0:
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
                        break
            else:
                print(f"  [capsolver] create error: {data.get('errorDescription', data)}")
        except Exception as e:
            print(f"  [capsolver] error: {str(e)[:80]}")
    if EZCAPTCHA_API_KEY:
        try:
            resp = requests.post(f"{EZCAPTCHA_API_BASE}/createTask", json={
                "clientKey": EZCAPTCHA_API_KEY,
                "task": {"type": "TurnstileTaskProxyless", "websiteURL": page_url, "websiteKey": sitekey},
            }, timeout=30)
            data = resp.json()
            if data.get("errorId", 1) == 0:
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
                        break
            else:
                print(f"  [ezcaptcha] create error: {data.get('errorDescription', data)}")
        except Exception as e:
            print(f"  [ezcaptcha] error: {str(e)[:80]}")
    return None


# ============================================================ 取码（同步轮询）
def poll_code_sync(mb, max_wait=150, poll=5):
    """同步轮询临时邮箱取 x.ai 验证码。复用 common.temp_email._scan_once。"""
    start = time.time()
    while time.time() - start < max_wait:
        code = _scan_once(mb["id"], mb["provider"], mb["email"], mb.get("token"),
                          None, None, GROK_SENDER, GROK_SUBJECT, CODE_REGEX)
        if code:
            print(f"  [temp-email] code found: {code}")
            return code
        print(f"  [temp-email] waiting for code... ({int(time.time()-start)}s/{max_wait}s)")
        time.sleep(poll)
    print("  [temp-email] timeout, no code")
    return None


def create_mailbox_retry(provider, tries=4):
    """临时邮箱建号带重试（yyds 会随机分到被限流的共享域，重试换一个好域）。"""
    last = None
    for i in range(tries):
        try:
            return create_mailbox(provider=provider)
        except Exception as e:
            last = e
            print(f"  [temp-email] 建号失败 (try {i+1}/{tries}): {str(e)[:70]}")
            time.sleep(2)
    raise RuntimeError(f"临时邮箱建号全部失败: {str(last)[:100]}")


# ============================================================ 主流程（单号）
def register_one(index, total, sub2api=False, sub2api_group="", mailbox_attempts=6,
                 code_timeout=75):
    email = ""
    print(f"\n#{index}/{total}")
    active_proxy = proxy_switch.effective_proxy_url()
    c = XConsoleAuthClient(debug=True, proxy=active_proxy, signup_url=SIGNUP_URL,
                           impersonate="chrome131", timeout=40.0)
    try:
        # 1. warm-up + 动态抓 next-action / sitekey（同时拿 cf_clearance cookie）
        st = c.visit_home()
        print(f"  [1] visit grok home HTTP {st}")
        st = c.load_signup_page()
        print(f"  [2] load signup page HTTP {st}  sitekey={c.turnstile_sitekey}")
        sitekey = c.turnstile_sitekey or C.TURNSTILE_SITEKEY

        # 2. 临时邮箱 + 发码
        mb = None
        r = None
        code = None
        password = _rand_password()
        for mailbox_try in range(1, max(1, mailbox_attempts) + 1):
            try:
                candidate = create_mailbox_retry(PROVIDER, tries=2)
            except Exception as e:
                print(f"  [temp-email] 第 {mailbox_try}/{mailbox_attempts} 个邮箱创建失败: {str(e)[:90]}")
                continue
            email = candidate["email"]
            print(f"  [3] temp mailbox {mailbox_try}/{mailbox_attempts}: {email} ({candidate['provider']})")
            r = c.create_email_validation_code(email)
            print(f"  [4] CreateEmailValidationCode ok={r.ok} http={r.http_status} grpc={r.grpc_status}")
            if r.ok:
                candidate_code = poll_code_sync(
                    candidate, max_wait=max(15, code_timeout), poll=5
                )
                if candidate_code:
                    mb = candidate
                    code = candidate_code
                    break
                print("  [temp-email] 发码成功但未收到邮件，自动换邮箱")
                continue
            print(f"  [temp-email] xAI 拒绝该邮箱域名，自动换邮箱: {r.trailers}")
            time.sleep(1)
        if not mb or not code:
            print(f"  [FAIL] 连续 {mailbox_attempts} 个临时邮箱均未取得验证码")
            return None

        # 3. 验码（字符串直传 gRPC，绕开掩码输入框）。先带分隔符原码，失败再去杠重试。
        v = c.verify_email_validation_code(email, code)
        print(f"  [5] VerifyEmailValidationCode ok={v.ok} grpc={v.grpc_status}")
        if not v.ok:
            alt = code.replace("-", "").replace(" ", "")
            if alt != code:
                v = c.verify_email_validation_code(email, alt)
                print(f"  [5] retry(去分隔符 {alt}) ok={v.ok} grpc={v.grpc_status}")
        if not v.ok:
            print(f"  [FAIL] 验证码校验失败: {v.trailers}")
            return None

        # 4. 密码强度校验（x.ai 建号前必调）
        try:
            c.validate_password(email, password)
        except Exception as e:
            print(f"  [6] validate_password 跳过: {str(e)[:50]}")

        # 5. Turnstile 打码
        print(f"  [7] solving Turnstile (sitekey={sitekey})")
        turnstile = solve_turnstile(sitekey, SIGNUP_URL)
        if not turnstile:
            print("  [FAIL] Turnstile 打码失败")
            return None

        # 6. 建号（Next.js server action）
        first, last = _rand_name(), _rand_name()
        res = c.create_account(
            email=email, given_name=first, family_name=last,
            password=password, email_validation_code=code,
            turnstile_token=turnstile, castle_request_token="",
            conversion_id=str(uuid.uuid4()),
        )
        print(f"  [8] create_account ok={res.ok} http={res.http_status}")
        if not res.ok:
            err = c.extract_signup_error(res.rsc_body)
            print(f"  [FAIL] 建号失败: {err}")
            return None

        # 7. 取 sso（RSC set-cookie 链 / grok.com 兜底 / CreateSession 密码登录兜底）
        sso = c.fetch_sso_token(email=email, password=password, save=False, retries=4)
        if not sso:
            print("  [7] RSC 链未拿到 sso，尝试 CreateSession 密码登录兜底")
            turnstile2 = solve_turnstile(sitekey, C.SIGNIN_URL) or turnstile
            sso = c.obtain_session_via_password(
                email=email, password=password, turnstile_token=turnstile2, retries=3)
        if not sso:
            print("  [FAIL] 建号成功但未取到 sso token")
            return None

        # 8. 落盘标准 grok token（{email,sso,ts}）
        save_grok_token(
            sso,
            email,
            authorization_status="pending" if sub2api else "not_requested",
        )
        print(f"  [OK] grok sso token 已保存  email={email} pw={password}")
        if sub2api:
            from common.uploaders import upload_sub2api_grok

            ok, msg = upload_sub2api_grok(
                SUB2API_URL,
                SUB2API_EMAIL,
                SUB2API_PASSWORD,
                sub2api_group or SUB2API_GROK_GROUP,
                sso,
                account_email=email,
                proxy_id=SUB2API_GROK_PROXY_ID,
                local_proxy=proxy_switch.effective_proxy_url(),
            )
            print(f"  [{'OK' if ok else 'FAIL'}] {msg}")
            save_grok_token(
                sso,
                email,
                authorization_status="authorized" if ok else "failed",
                announce=False,
            )
            if not ok:
                print("  [hint] SSO 已落盘，可修复配置后运行: python tools/upload_tokens.py grok")
                return None
            try:
                mark_uploaded("grok", "sub2api", email)
            except Exception as e:
                print(f"  [warn] SUB2API 已导入，但本地幂等标记写入失败: {e}")
        return sso

    except Exception as e:
        print(f"  ERROR: {e}")
        return None
    finally:
        c.close()


def _resolve_node(node_arg):
    """把 '美国 01' 这类短名解析成带国旗的完整节点名（子串匹配）。"""
    try:
        nodes = proxy_switch.concrete_nodes()
        toks = [t for t in node_arg.replace("|", " ").split() if t]
        matches = [n for n in nodes if all(t in n for t in toks)]
        if matches:
            return matches[0]
        if nodes:
            print(f"  [warn] 未匹配到 '{node_arg}'，可选: {nodes[:6]} ...")
    except Exception as e:
        print(f"  [warn] 节点名解析失败({str(e)[:40]})")
    return node_arg


def _find_signup_node():
    return proxy_switch.find_working_node(
        test_url=SIGNUP_URL,
        warmup_url="https://console.x.ai/home",
        required_markers=("/_next/static/chunks/", "self.__next_f.push"),
    )


def _rotate_signup_egress():
    if proxy_switch.proxy_mode() == "residential":
        result = proxy_switch.rotate_proxy()
        return result.get("node") if result.get("ok") else None
    return _find_signup_node()


def main():
    parser = argparse.ArgumentParser(description="Grok Auto Register (HTTP protocol / xconsole_client)")
    parser.add_argument("--count", "-n", type=int, default=1)
    parser.add_argument("--node", default="auto", help="Clash 出口节点(过 grok CF)")
    parser.add_argument("--provider", default="", help="临时邮箱 provider(留空用 .env 的 TEMP_EMAIL_PROVIDER；"
                                                       "支持逗号分隔故障转移，如 yyds,gptmail)")
    parser.add_argument("--sub2api", action="store_true",
                        help="注册成功后把 SSO 转成 SUB2API Grok OAuth 账号")
    parser.add_argument("--sub2api-group", default="",
                        help="SUB2API Grok 目标分组名(默认取 SUB2API_GROK_GROUP)")
    parser.add_argument("--mailbox-attempts", type=int, default=6,
                        help="单号发码被拒时更换临时邮箱的次数")
    parser.add_argument("--code-timeout", type=int, default=75,
                        help="单个临时邮箱等待验证码秒数")
    parser.add_argument("--rotate-every", type=int, default=5,
                        help="批量注册每 N 个账号重新探测节点(0=不轮换，仅 auto 模式)")
    args = parser.parse_args()

    if args.sub2api and not (SUB2API_URL and SUB2API_EMAIL and SUB2API_PASSWORD):
        parser.error("--sub2api 需要配置 SUB2API_URL/SUB2API_EMAIL/SUB2API_PASSWORD")

    global PROVIDER
    if args.provider.strip():
        PROVIDER = args.provider.strip()
    print(f"  临时邮箱 provider: {PROVIDER}")

    print("=" * 50)
    print(f"  Grok Auto Register (HTTP)  count={args.count} node={args.node}")
    print("=" * 50)

    # 选节点过 grok CF（curl_cffi 走 Clash mixed-port 出口）
    try:
        if args.node and args.node.lower() != "auto":
            target = _resolve_node(args.node)
            if target != args.node:
                print(f"  节点名解析: '{args.node}' -> '{target}'")
            proxy_switch.set_node(target)
            time.sleep(2)
            print(f"  使用指定节点 -> {proxy_switch.current_node()}")
        else:
            print("  自动探测能完整加载 xAI 注册页的节点...")
            node = _find_signup_node()
            if not node:
                print("  没找到能过 grok CF 的节点(稍后重试)")
                return 1
            print(f"  选用节点: {node}")
    except Exception as e:
        print(f"  切节点失败(确认 Clash 在跑): {e}")
        return 1

    results = []
    last_attempt_failed = False
    for i in range(1, args.count + 1):
        try:
            if (
                i > 1
                and args.node.lower() == "auto"
                and args.rotate_every > 0
                and (i - 1) % args.rotate_every == 0
                and not last_attempt_failed
            ):
                print(f"\n  批量轮换节点 ({i - 1}/{args.count})...")
                rotated = _rotate_signup_egress()
                print(f"  新节点: {rotated or proxy_switch.current_node()}")
            result = register_one(
                i,
                args.count,
                args.sub2api,
                args.sub2api_group,
                args.mailbox_attempts,
                args.code_timeout,
            )
            results.append(result)
            last_attempt_failed = not bool(result)
            if last_attempt_failed and args.node.lower() == "auto" and i < args.count:
                print(f"\n  #{i} 失败，立即更换注册节点...")
                rotated = _rotate_signup_egress()
                print(f"  新节点: {rotated or proxy_switch.current_node()}")
        except Exception as e:
            print(f"  #{i} fatal: {e}")
            results.append(None)
            last_attempt_failed = True

    ok = sum(1 for r in results if r)
    print(f"\n{'='*50}\n  success: {ok}/{len(results)}\n{'='*50}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
