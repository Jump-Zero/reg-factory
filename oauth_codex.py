# -*- coding: utf-8 -*-
"""
oauth_codex.py — 用已存 cookie 重登 ChatGPT 账号，走 Codex OAuth 给 SUB2API 建带 refresh_token 的账号。

解决:网页 session token 无 refresh_token → SUB2API oauth 账号 401。

用法:
    python oauth_codex.py --cookie cookies/chatgpt/full_xxx.json     # 指定 cookie 文件
    python oauth_codex.py                                            # 默认用最新的 full_*.json
    python oauth_codex.py --keep                                     # 失败保留窗口排查

前置:account 已是 Plus(否则 OAuth 能成但无 codex 额度);.env 配好 SUB2API_*。
"""

import argparse
import asyncio
import glob
import json
import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from playwright.async_api import async_playwright

from config import (SUB2API_URL, SUB2API_EMAIL, SUB2API_PASSWORD, SUB2API_GROUP,
                    CPA_URL, CPA_MGMT_KEY)
from common.browser import open_and_connect, teardown
from common.uploaders import _origin, upload_cpa
from common.session_export import build_cpa_codex_json_from_oauth
from common import oauth_codex as ox
from common import proxy_switch


def _sanitize(cookies):
    out = []
    for c in cookies:
        nc = {k: c[k] for k in ("name", "value", "domain", "path", "httpOnly", "secure") if k in c}
        if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0:
            nc["expires"] = c["expires"]
        ss = c.get("sameSite")
        nc["sameSite"] = ss if ss in ("Strict", "Lax", "None") else "Lax"
        out.append(nc)
    return out


def _pick_cookie_file(arg):
    if arg:
        return arg
    fs = sorted(glob.glob("cookies/chatgpt/full_*.json"), key=os.path.getmtime, reverse=True)
    return fs[0] if fs else None


async def main():
    parser = argparse.ArgumentParser(description="Codex OAuth -> SUB2API(带 refresh_token)")
    parser.add_argument("--cookie", help="cookie 文件路径(默认最新 full_*.json)")
    parser.add_argument("--group", default=SUB2API_GROUP, help="SUB2API 目标分组(默认 config)")
    parser.add_argument("--timeout", type=int, default=120, help="授权捕获超时秒")
    parser.add_argument("--node", default="auto",
                        help="固定 ChatGPT Clash 节点；auto 自动探测，none 直连")
    parser.add_argument("--manual-phone", action="store_true",
                        help="add-phone 手动模式:不接码,自己在浏览器填号+输码(如 WhatsApp 码)")
    parser.add_argument("--phone", default="",
                        help="add-phone 半自动:脚本填该号(E.164,如 +8618001623966)+选 WhatsApp+发送,你只手输码")
    parser.add_argument("--phone-skip", type=int, default=0,
                        help="先赌免手机直连的次数(默认0=直接一次性接码,不赌免手机)：>0 时每次关窗重开+重登重摇风控，弹手机就跳过，用尽才接码")
    parser.add_argument("--sms-provider", choices=["auto", "custom", "smsman", "firefox", "hero"], default="auto",
                        help="自动接码平台；auto 按 sms-man、firefox.fun、hero-sms 顺序")
    parser.add_argument("--skip-cpa", action="store_true",
                        help="不把 OAuth 凭据推到 CPA(默认 CPA 配好就推,带真 refresh_token)")
    parser.add_argument("--auth-url-source", choices=["sub2", "cpa"],
                        default=os.environ.get("CODEX_AUTH_URL_SOURCE", "sub2").strip().lower() or "sub2",
                        help="授权地址来源：sub2（默认）或 cpa；cpa 模式由 CPA 直接接收 callback")
    parser.add_argument("--keep", action="store_true", help="失败保留窗口")
    args = parser.parse_args()

    # 手动/半自动填号收码需要人操作时间；自动接码换号多次(CODEX_ADDPHONE_ATTEMPTS×CODEX_SMS_TIMEOUT)
    # 也可能耗时数分钟，超时给足，避免 add-phone 还在换号就被授权捕获超时打断。
    import os as _os
    _ph_budget = int(_os.environ.get("CODEX_ADDPHONE_ATTEMPTS", "2") or "2") * int(
        _os.environ.get("CODEX_SMS_TIMEOUT", "150") or "150"
    )
    timeout = max(args.timeout, 300, _ph_budget + 120)

    auth_source = args.auth_url_source
    if auth_source == "sub2" and not (SUB2API_URL and SUB2API_EMAIL and SUB2API_PASSWORD):
        print("  [FAIL] SUB2API 未配置(.env: SUB2API_URL/EMAIL/PASSWORD)")
        return 1
    if auth_source == "cpa" and not (CPA_URL and CPA_MGMT_KEY):
        print("  [FAIL] CPA 未配置(.env: CPA_URL/CPA_MGMT_KEY)")
        return 1

    cookie_file = _pick_cookie_file(args.cookie)
    if not cookie_file or not os.path.isfile(cookie_file):
        print("  [FAIL] 找不到 cookie 文件")
        return 1
    print(f"  cookie: {cookie_file}")
    cookies = _sanitize(json.load(open(cookie_file, encoding="utf-8")))

    origin = _origin(SUB2API_URL) if auth_source == "sub2" else ""
    ok = False
    async with async_playwright() as p:
        bb = pid = None
        try:
            from register_chatgpt import clash_browser_proxy_fields, select_chatgpt_node

            select_chatgpt_node(args.node, allow_blocked=True)
            use_clash = (args.node or "auto").lower() not in {"none", "off", "direct"}
            bb, pid, browser, ctx, page = await open_and_connect(
                name="codex_oauth",
                p=p,
                browser_options=clash_browser_proxy_fields() if use_clash else None,
            )
            await ctx.clear_cookies()
            await ctx.add_cookies(cookies)
            await page.goto("https://chatgpt.com/", timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(4)

            # 确认登录态。chatgpt.com 带 cookie 进来常有客户端重定向(onboarding/auth)，
            # evaluate 撞上导航会 "Execution context was destroyed" —— 重试几次等页面稳定。
            sess = None
            for attempt in range(6):
                try:
                    sess = await page.evaluate(
                        "() => fetch('/api/auth/session',{credentials:'include'}).then(r=>r.ok?r.json():null).catch(()=>null)")
                except Exception as e:
                    print(f"  session 抓取重试 {attempt+1}/6: {str(e)[:60]}")
                    await asyncio.sleep(3)
                    continue
                if sess and sess.get("accessToken"):
                    break
                await asyncio.sleep(3)
            if not sess or not sess.get("accessToken"):
                print("  [FAIL] cookie 未生效/已登出，拿不到 session")
                return 2
            email = sess.get("user", {}).get("email", "")
            plan = sess.get("account", {}).get("planType")
            print(f"  登录态 OK: {email}  planType={plan}")
            if plan != "plus":
                print(f"  [WARN] 当前 planType={plan}，非 plus —— OAuth 能成但可能无 codex 额度")

            token = None
            group_id = None
            if auth_source == "sub2":
                # SUB2API: 登录 + 找分组
                token = ox.sub2api_login(origin, SUB2API_EMAIL, SUB2API_PASSWORD)
                group_id = ox.find_group_id(origin, token, args.group)
                print(f"  SUB2API: group={args.group}(#{group_id})")
            else:
                print("  CPA: 使用管理接口生成授权地址并接收 callback")

            # 浏览器驱动授权：phone_skip>0 时先免手机直连 N 次(关窗重登重摇风控)，弹手机才接码；=0 直接一次性接码
            _mode = "，add-phone 半自动(填号+选WhatsApp+发送)" if args.phone else ("，add-phone 手动模式" if args.manual_phone else "")
            if args.phone_skip > 0:
                print(f"  打开授权页，免手机直连先试 {args.phone_skip}次{_mode}...")
            else:
                print(f"  打开授权页，直接一次性接码(不赌免手机){_mode}...")
            reset_fn = ox.make_reset_page(p, cookies, account_email=email) if args.phone_skip > 0 else None
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
                account_email=email, phone_skip_attempts=args.phone_skip,
                skip_timeout=120, phone_timeout=timeout,
                debug_dump="oauth_authorize_dump.html",
                manual_phone=args.manual_phone, semi_phone=args.phone,
                reset_page=reset_fn, sms_provider=args.sms_provider,
                result_metadata=codex_metadata)
            if reset_fn is not None:
                try:
                    await reset_fn.cleanup()
                except Exception:
                    pass
            if not code:
                print(f"  [FAIL] 授权未完成: {msg}")
                return 2
            print(f"  捕获回调: code={code[:10]}...")

            if auth_source == "cpa":
                callback = ox.callback_url(code, cb_state, auth_url)
                cpa_result = ox.submit_cpa_callback(
                    CPA_URL,
                    CPA_MGMT_KEY,
                    callback,
                    retries=int(os.environ.get("CODEX_CPA_CALLBACK_RETRIES", "5") or "5"),
                    retry_delay=float(os.environ.get("CODEX_CPA_CALLBACK_RETRY_DELAY", "3") or "3"),
                )
                print(f"  [OK] CPA callback 已提交（{str(cpa_result.get('message') or 'accepted')[:120]}）✅")
                ok = True
                cred = None
            else:
                # 换码 + 建号
                exch = ox.exchange_code(origin, token, session_id, code, cb_state)
                cred = ox.build_oauth_credentials(exch)
                cred["codex_phone_status"] = codex_metadata.get("codex_phone_status", "unknown")
                from common.session_export import save_codex_oauth_credentials
                save_codex_oauth_credentials(cred, email=cred.get("email") or email)
                print(f"  exchange-code OK: refresh_token={'YES' if cred.get('refresh_token') else 'NO'} "
                      f"plan={cred.get('plan_type')} email={cred.get('email')}")
                acct = ox.create_oauth_account(origin, token, cred, [group_id], name=cred.get("email") or email)
                acct_id = (acct or {}).get("id")
                print(f"  [OK] SUB2API 账号已创建 #{acct_id}（type=oauth，带 refresh_token）✅")
                ok = True

            # 同一份带真 refresh_token 的 OAuth 凭据，也推到 CPA（best-effort，不影响成功判定）
            if auth_source == "cpa":
                print("  [CPA] callback 已由 CPA 处理，跳过凭据二次上传")
            elif args.skip_cpa:
                print("  [CPA] --skip-cpa，跳过 CPA 推送")
            elif not (CPA_URL and CPA_MGMT_KEY):
                print("  [CPA] 未配置(CPA_URL/CPA_MGMT_KEY)，跳过 CPA 推送")
            else:
                try:
                    cpa = build_cpa_codex_json_from_oauth(cred, email=cred.get("email") or email)
                    cok, cmsg = upload_cpa(CPA_URL, CPA_MGMT_KEY, cpa["auth_json"], cpa["file_name"])
                    print(f"  [CPA] {'OK' if cok else 'FAIL'}（refresh_token={'YES' if cpa['has_refresh_token'] else 'NO'}）"
                          f" {cpa['file_name']} - {cmsg}")
                except Exception as e:
                    print(f"  [CPA] 推送异常: {e}")
        except Exception as e:
            print(f"  ERROR: {e}")
        finally:
            if bb and pid:
                await teardown(bb, pid, delete=not (args.keep and not ok))

    return 0 if ok else 2


if __name__ == "__main__":
    proxy_switch.apply_platform_environment("chatgpt")
    sys.exit(asyncio.run(main()))
