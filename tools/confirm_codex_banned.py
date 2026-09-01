"""浏览器确认 ChatGPT 账号是否被封禁/停用（账号健康页专用子进程）。

用法:
    python tools/confirm_codex_banned.py --cookie cookies/chatgpt/full_xxx.json [--email a@b.c]

流程: 复用项目的指纹浏览器链路(含 Clash 代理) -> 注入 CK -> 打开 chatgpt.com
      -> 复用 oauth_codex.detect_account_banned 三重探测(URL/页面文案/session 响应)
      -> 顺带查询 /api/auth/session 判断登录态是否存活。

约定: stdout 最后一行输出 JSON，供父进程解析——
    {"ok": true,  "banned": true,  "marker": "...", "session_valid": false}
    {"ok": true,  "banned": false, "marker": "",     "session_valid": true}
    {"ok": false, "error": "..."}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common import oauth_codex as ox  # noqa: E402
from common import proxy_switch  # noqa: E402
from common.browser import open_and_connect, teardown  # noqa: E402


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False))


def _normalize_cookies(raw):
    cookies = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if not name or not value:
            continue
        entry = dict(item)
        entry.update({"name": name, "value": value})
        entry.setdefault("domain", ".chatgpt.com")
        entry.setdefault("path", "/")
        cookies.append(entry)
    return cookies


async def _check_session(page):
    """返回 /api/auth/session 是否给出可用登录态（拿不到 accessToken 视为失效）。"""
    try:
        session = await page.evaluate(
            "() => fetch('/api/auth/session',{credentials:'include'})"
            ".then(r=>r.ok?r.json():null).catch(()=>null)"
        )
    except Exception:
        return False
    return bool(isinstance(session, dict) and session.get("accessToken"))


async def run(cookie_file, email=""):
    try:
        with open(cookie_file, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception as exc:
        _emit({"ok": False, "error": f"CK 文件读取失败: {str(exc)[:120]}"})
        return 2
    cookies = _normalize_cookies(raw if isinstance(raw, list) else [])
    if not cookies:
        _emit({"ok": False, "error": "CK 文件中没有可用 cookie"})
        return 2

    proxy_switch.apply_platform_environment("chatgpt")
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        bb = profile_id = None
        try:
            from register_chatgpt import clash_browser_proxy_fields

            bb, profile_id, _browser, context, page = await open_and_connect(
                name=f"health_confirm_{int(time.time())}",
                p=p,
                browser_options=clash_browser_proxy_fields(),
            )
            await context.add_cookies(cookies)
            try:
                await page.goto("https://chatgpt.com/", timeout=60000,
                                wait_until="domcontentloaded")
            except Exception as exc:
                print(f"[WARN] 打开 chatgpt.com 异常(继续探测): {str(exc)[:120]}")
            # 给页面文案/重定向留出渲染时间，detect_account_banned 三重探测兜底。
            for _ in range(6):
                marker = await ox.detect_account_banned(page)
                if marker:
                    break
                await asyncio.sleep(2)
            session_valid = await _check_session(page)
            _emit({
                "ok": True,
                "email": str(email or ""),
                "banned": bool(marker),
                "marker": str(marker or ""),
                "session_valid": session_valid,
            })
            return 0
        except Exception as exc:
            _emit({"ok": False, "error": str(exc)[:240]})
            return 2
        finally:
            if bb and profile_id:
                try:
                    await teardown(bb, profile_id)
                except Exception:
                    pass


def main():
    parser = argparse.ArgumentParser(description="浏览器确认 ChatGPT 账号封禁状态")
    parser.add_argument("--cookie", required=True, help="full_*.json CK 文件路径")
    parser.add_argument("--email", default="", help="账号邮箱(仅用于日志)")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.cookie, args.email))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
