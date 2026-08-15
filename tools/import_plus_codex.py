"""Import existing paid ChatGPT accounts into SUB2API through Codex OAuth."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import async_playwright

from common import oauth_codex as ox
from common import proxy_switch
from common.account_records import (
    is_icloud_email,
    is_outlook_email,
    masked_email,
    parse_account_text,
)
from common.browser import open_and_connect, teardown
from common.mailbox import check_refresh_token, get_code_by_token, get_code_outlook_pw, prelogin_outlook
from common.session_export import save_codex_oauth_credentials
from common.uploaders import _origin
from config import SUB2API_EMAIL, SUB2API_GROUP, SUB2API_PASSWORD, SUB2API_URL


OPENAI_SENDERS = ("openai", "noreply", "no-reply", "chatgpt")
OPENAI_SUBJECTS = ("code", "verify", "verification", "openai", "chatgpt", "login")
PAID_PLANS = {"plus", "pro", "team", "business", "enterprise", "edu"}
SESSION_COOKIE_NAMES = {"__secure-next-auth.session-token", "session-token"}


def _session_cookies(record: dict) -> list[dict]:
    cookies = []
    for raw in record.get("cookies") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        value = str(raw.get("value") or "").strip()
        if not name or not value:
            continue
        item = dict(raw)
        item.update({"name": name, "value": value})
        item.setdefault("domain", ".chatgpt.com")
        item.setdefault("path", "/")
        cookies.append(item)
    token = str(record.get("session_token") or record.get("access_token") or "").strip()
    if token and not any(str(item.get("name") or "").lower() in SESSION_COOKIE_NAMES for item in cookies):
        cookies.append({
            "name": "__Secure-next-auth.session-token",
            "value": token,
            "domain": ".chatgpt.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
        })
    return cookies


async def _bootstrap_session(context, page, record: dict) -> dict:
    cookies = _session_cookies(record)
    if not cookies:
        raise RuntimeError("直接 token 没有可用的 ChatGPT session cookie")
    await context.clear_cookies()
    await context.add_cookies(cookies)
    try:
        await page.goto("https://chatgpt.com/", timeout=60000, wait_until="domcontentloaded")
    except Exception:
        pass
    session = None
    for _ in range(8):
        try:
            session = await page.evaluate(
                "() => fetch('/api/auth/session',{credentials:'include'}).then(r=>r.ok?r.json():null).catch(()=>null)"
            )
        except Exception:
            session = None
        if isinstance(session, dict) and session.get("accessToken"):
            break
        await asyncio.sleep(1.5)
    if not isinstance(session, dict) or not session.get("accessToken"):
        raise RuntimeError("直接 token 不是有效的 ChatGPT session cookie；普通 access token 不能直接兑换 Codex refresh_token")
    return session


class MailCodeProvider:
    """Read the OpenAI email OTP from Graph, iCloud API, or Outlook browser login."""

    def __init__(self, record: dict, context, main_page, max_wait: int = 150):
        self.record = record
        self.context = context
        self.main_page = main_page
        self.max_wait = max(30, int(max_wait))
        self.graph_ready = False
        self.icloud_ready = False
        self.icloud_existing_codes = set()
        self.icloud_api_url = str(record.get("mail_api_url") or "").strip()
        self.icloud_api_key = str(record.get("mail_api_key") or "").strip()
        self.icloud_token = str(record.get("two_factor") or "").strip()
        self.mail_page = None
        self.mail_prelogged = False

    async def prepare(self):
        email = self.record.get("email") or ""
        provider = str(self.record.get("provider") or "").lower()
        if provider in {"icloud", "apple"} or is_icloud_email(email):
            self.icloud_ready = True
            try:
                self.icloud_existing_codes = await ox._icloud_existing_codes(
                    email,
                    token=self.icloud_token,
                    api_key=self.icloud_api_key,
                    base_url=self.icloud_api_url or None,
                )
            except Exception:
                self.icloud_existing_codes = set()
            return "icloud-api"

        refresh_token = self.record.get("refresh_token") or ""
        client_id = self.record.get("client_id") or ""
        if refresh_token and client_id:
            validation = await asyncio.to_thread(check_refresh_token, refresh_token, client_id)
            if validation.get("ok"):
                self.graph_ready = True
                return "outlook-graph"
            print(
                "  [mail] Graph refresh token unavailable; "
                f"falling back to browser ({validation.get('reason') or 'unknown'})"
            )

        password = self.record.get("password") or ""
        if not password:
            raise RuntimeError("邮箱 Graph token 不可用且没有邮箱密码兜底")
        if not is_outlook_email(email):
            raise RuntimeError("非 Outlook 邮箱需要配置 iCloud 接码 API 或直接提供有效 session token")
        self.mail_page = await self.context.new_page()
        self.mail_prelogged = await prelogin_outlook(self.mail_page, email, password)
        if not self.mail_prelogged:
            raise RuntimeError("Outlook 邮箱密码登录失败，无法自动取 OpenAI 验证码")
        await self.main_page.bring_to_front()
        return "outlook-browser"

    async def __call__(self, account_email: str, received_after: float):
        code = None
        if self.icloud_ready:
            from common.temp_email import poll_verification_code

            code = await poll_verification_code(
                account_email,
                "icloud",
                email=account_email,
                token=self.icloud_token or None,
                api_key=self.icloud_api_key or None,
                base_url=self.icloud_api_url or None,
                max_wait=self.max_wait,
                poll_interval=6,
                sender_hint=OPENAI_SENDERS,
                subject_hint=OPENAI_SUBJECTS,
                code_regex=r"\b(\d{6})\b",
                exclude_codes=tuple(self.icloud_existing_codes),
            )
            if code:
                self.icloud_existing_codes.add(str(code))
            return code
        if self.graph_ready:
            code = await asyncio.to_thread(
                get_code_by_token,
                account_email,
                self.record["refresh_token"],
                self.record["client_id"],
                OPENAI_SENDERS,
                OPENAI_SUBJECTS,
                r"\b(\d{6})\b",
                120,
                5,
                received_after,
            )
        if not code and self.record.get("password"):
            if self.mail_page is None:
                self.mail_page = await self.context.new_page()
                self.mail_prelogged = False
            code = await get_code_outlook_pw(
                self.mail_page,
                account_email,
                self.record["password"],
                sender_hint=OPENAI_SENDERS,
                subject_hint=OPENAI_SUBJECTS,
                code_regex=r"\b(\d{6})\b",
                max_wait=self.max_wait,
                poll=8,
                skip_login=self.mail_prelogged,
            )
            self.mail_prelogged = bool(code) or self.mail_prelogged
            await self.main_page.bring_to_front()
        return code


def require_phone_verification(metadata: dict, allow_unverified: bool = False) -> str:
    status = str((metadata or {}).get("codex_phone_status") or "not_verified").strip().lower()
    if status != "verified" and not allow_unverified:
        raise RuntimeError("本次 OAuth 未完成手机号接码验证，已中止 SUB2API 导入")
    return status if status == "verified" else "skipped"


def _result_path(value: str) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    root = Path(os.environ.get("REG_FACTORY_DATA_DIR") or Path.cwd())
    return root / "runtime" / "plus_codex" / "results.jsonl"


def _write_result(path: Path, result: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_result = {key: value for key, value in result.items() if key != "_output_token"}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe_result, ensure_ascii=False) + "\n")


def _output_path(value: str) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    root = Path(os.environ.get("REG_FACTORY_DATA_DIR") or Path.cwd())
    return root / "runtime" / "plus_codex" / "tokens.sub2api.txt"


def _sub2api_token_content(credentials: dict) -> str:
    """Serialize OAuth credentials in the session JSON accepted by SUB2API."""
    payload = dict(credentials or {})
    if payload.get("access_token") and not payload.get("accessToken"):
        payload["accessToken"] = payload["access_token"]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _write_output_token(path: Path, result: dict):
    token = str(result.get("_output_token") or "").strip()
    if not token:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(token + "\n")


def _check_paid(plan_type: str, args):
    normalized = str(plan_type or "").strip().lower()
    if args.require_paid and normalized and normalized not in PAID_PLANS:
        raise RuntimeError(f"账号套餐为 {normalized}，不是已开通的 Plus/付费套餐")
    return normalized


async def _save_and_create(origin, sub2api_token, group_id, credentials, email, result, args):
    if not credentials.get("refresh_token"):
        raise RuntimeError("OAuth 凭据缺少 refresh_token")
    save_codex_oauth_credentials(credentials, email=credentials.get("email") or email)
    if getattr(args, "output_format", "none") == "sub2api":
        result["_output_token"] = _sub2api_token_content(credentials)
    if getattr(args, "no_import", False):
        result["status"] = "success"
        result["stage"] = "done"
        result["message"] = "OAuth credentials saved; SUB2API import skipped"
        return
    result["stage"] = "sub2api"
    account = await asyncio.to_thread(
        ox.create_oauth_account,
        origin,
        sub2api_token,
        credentials,
        [group_id],
        credentials.get("email") or email or "codex-oauth",
    )
    result["sub2api_account_id"] = (account or {}).get("id")
    result["status"] = "success"
    result["stage"] = "done"
    result["message"] = "手机号接码验证通过，SUB2API 导入成功"


async def import_one(index, total, record, playwright, origin, sub2api_token, group_id, args):
    email = record.get("email") or ""
    masked = masked_email(email)
    started = time.time()
    result = {
        "email": email,
        "source_type": record.get("source_type") or "mailbox",
        "status": "failed",
        "stage": "start",
        "phone_status": "not_verified",
        "plan_type": record.get("plan_type") or "",
        "sub2api_account_id": None,
        "message": "",
        "finished_at": "",
    }
    browser_client = profile_id = None
    print(f"\n[{index}/{total}] {masked} 开始 Plus Codex 导入 ({result['source_type']})")
    try:
        # A complete Codex credential bundle can only be imported when the
        # caller explicitly records that its phone verification already passed.
        if record.get("source_type") == "oauth_token":
            result["stage"] = "token"
            result["phone_status"] = require_phone_verification(record, getattr(args, "skip_phone", False))
            credentials = dict(record.get("oauth_credentials") or {})
            credentials["codex_phone_status"] = result["phone_status"]
            result["plan_type"] = _check_paid(credentials.get("plan_type"), args)
            await _save_and_create(origin, sub2api_token, group_id, credentials, email, result, args)
        else:
            from register_chatgpt import clash_browser_proxy_fields

            use_clash = (args.node or "auto").lower() not in {"none", "off", "direct"}
            browser_client, profile_id, _browser, context, page = await open_and_connect(
                name=f"plus_codex_{index}_{int(started)}",
                p=playwright,
                browser_options=clash_browser_proxy_fields() if use_clash else None,
            )

            if record.get("source_type") in {"session_token", "access_token"}:
                result["stage"] = "session"
                session = await _bootstrap_session(context, page, record)
                user = session.get("user") or {}
                account = session.get("account") or {}
                email = email or str(user.get("email") or "").strip()
                masked = masked_email(email)
                result["email"] = email
                result["plan_type"] = str(account.get("planType") or account.get("plan_type") or "").lower()
                record = {**record, "email": email}

            result["stage"] = "mailbox"
            mail_provider = None
            if record.get("email") and (
                record.get("password") or record.get("refresh_token") or is_icloud_email(record.get("email"))
            ):
                mail_provider = MailCodeProvider(
                    record, context, page, max_wait=args.timeout
                )
                mail_mode = await mail_provider.prepare()
                print(f"  [mail] {masked} 取码方式: {mail_mode}")

            result["stage"] = "oauth"
            metadata = {}
            phone_budget = args.phone_attempts * args.sms_timeout + 180
            code, session_id, state, message = await ox.authorize_with_retry(
                page,
                lambda: ox.generate_auth_url(origin, sub2api_token),
                account_email=email,
                phone_skip_attempts=args.phone_attempts if getattr(args, "skip_phone", False) else 0,
                skip_timeout=args.timeout,
                phone_timeout=max(args.timeout, phone_budget),
                debug_dump=None,
                sms_provider=args.sms_provider,
                result_metadata=metadata,
                email_code_provider=mail_provider,
                allow_phone=not getattr(args, "skip_phone", False),
                totp_secret=record.get("two_factor") or "",
            )
            if not code:
                raise RuntimeError(message or "Codex OAuth 授权未完成")

            result["phone_status"] = require_phone_verification(metadata, getattr(args, "skip_phone", False))
            result["stage"] = "exchange"
            exchanged = await asyncio.to_thread(
                ox.exchange_code, origin, sub2api_token, session_id, code, state
            )
            credentials = ox.build_oauth_credentials(exchanged)
            credentials["codex_phone_status"] = result["phone_status"]
            result["plan_type"] = _check_paid(credentials.get("plan_type") or result["plan_type"], args)
            await _save_and_create(origin, sub2api_token, group_id, credentials, email, result, args)

        print(
            f"  [OK] {masked} -> "
            f"{'SUB2API #' + str(result['sub2api_account_id']) if result['sub2api_account_id'] else 'local credentials'} "
            f"plan={result['plan_type'] or 'unknown'} phone={result['phone_status']}"
        )
    except Exception as exc:
        result["message"] = str(exc)[:240]
        print(f"  [FAIL] {masked} stage={result['stage']}: {result['message']}")
    finally:
        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        result["elapsed_seconds"] = round(time.time() - started, 1)
        if browser_client and profile_id:
            await teardown(
                browser_client,
                profile_id,
                delete=not (args.keep_on_fail and result["status"] != "success"),
            )
    return result


async def run(args):
    source_path = Path(args.accounts_file).expanduser().resolve()
    try:
        raw = source_path.read_text(encoding="utf-8-sig")
    finally:
        if args.delete_input:
            source_path.unlink(missing_ok=True)
    records, errors = parse_account_text(raw)
    if errors:
        lines = ", ".join(str(item["line"]) for item in errors[:10])
        raise RuntimeError(f"账号格式错误或重复：第 {lines} 行")
    if not records:
        raise RuntimeError("没有可导入的账号")
    if args.dry_run:
        kinds = ", ".join(sorted({str(item.get("source_type") or "mailbox") for item in records}))
        print(f"[dry-run] 已解析 {len(records)} 个账号 ({kinds})，未登录、未接码、未导入")
        return 0
    if not (SUB2API_URL and SUB2API_EMAIL and SUB2API_PASSWORD):
        raise RuntimeError("SUB2API_URL/EMAIL/PASSWORD 未配置")

    os.environ["CODEX_ADDPHONE_ATTEMPTS"] = str(args.phone_attempts)
    os.environ["CODEX_SMS_TIMEOUT"] = str(args.sms_timeout)
    origin = _origin(SUB2API_URL)
    token = await asyncio.to_thread(ox.sub2api_login, origin, SUB2API_EMAIL, SUB2API_PASSWORD)
    group_id = None if args.no_import else await asyncio.to_thread(
        ox.find_group_id, origin, token, args.group
    )
    print(
        f"[batch] {len(records)} 个 Plus 账号 -> SUB2API group={args.group}(#{group_id})，"
        f"并发={args.concurrency}，接码={args.sms_provider}，换号={args.phone_attempts}"
    )

    from register_chatgpt import select_chatgpt_node

    select_chatgpt_node(args.node, allow_blocked=True)
    result_path = _result_path(args.results)
    output_path = _output_path(args.output) if args.output_format == "sub2api" else None
    semaphore = asyncio.Semaphore(args.concurrency)
    write_lock = asyncio.Lock()

    async with async_playwright() as playwright:
        async def worker(index, record):
            async with semaphore:
                result = await import_one(index, len(records), record, playwright, origin, token, group_id, args)
                async with write_lock:
                    await asyncio.to_thread(_write_result, result_path, result)
                    if output_path:
                        await asyncio.to_thread(_write_output_token, output_path, result)
                return result

        results = await asyncio.gather(
            *(worker(index, record) for index, record in enumerate(records, start=1))
        )

    success = sum(item["status"] == "success" for item in results)
    verified = sum(item["phone_status"] == "verified" for item in results)
    print(
        f"\n[batch] 完成：成功 {success}/{len(results)}，本次手机接码验证 {verified}，"
        f"失败 {len(results) - success}"
    )
    print(f"[batch] 结果已写入（不含密码和 token）: {result_path}")
    if output_path:
        print(f"[batch] SUB2API token output: {output_path}")
    return 0 if success == len(results) else 2


def build_parser():
    parser = argparse.ArgumentParser(description="已开通 Plus 账号批量 Codex OAuth 导入 SUB2API")
    parser.add_argument("--accounts-file", required=True, help="账号、Cookie 或 token 文件路径")
    parser.add_argument("--group", default=SUB2API_GROUP, help="SUB2API OpenAI 分组")
    parser.add_argument("--concurrency", type=int, default=1, choices=range(1, 6))
    parser.add_argument("--node", default="auto", help="ChatGPT Clash 节点；auto 自动探测")
    parser.add_argument("--sms-provider", choices=("auto", "custom", "smsman", "firefox", "hero"), default="auto")
    parser.add_argument("--phone-attempts", type=int, default=3, choices=range(1, 11))
    parser.add_argument("--sms-timeout", type=int, default=180)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--output-format", choices=("none", "sub2api"), default="none",
                        help="额外凭据输出格式")
    parser.add_argument("--output", default="", help="额外 token 输出文件路径")
    parser.add_argument("--skip-phone", action="store_true",
                        help="不执行手机号验证；适合只保存凭据或导出 token")
    parser.add_argument("--no-import", action="store_true",
                        help="只保存或输出 OAuth 凭据，不创建 SUB2API 账号")
    parser.add_argument("--results", default="", help="结果 JSONL 路径")
    parser.add_argument("--delete-input", action="store_true")
    parser.add_argument("--keep-on-fail", action="store_true")
    parser.add_argument("--allow-non-paid", dest="require_paid", action="store_false")
    parser.add_argument("--dry-run", action="store_true")
    parser.set_defaults(require_paid=True)
    return parser


def main():
    args = build_parser().parse_args()
    if args.sms_timeout < 30:
        raise SystemExit("--sms-timeout must be at least 30 seconds")
    proxy_switch.apply_platform_environment("chatgpt")
    try:
        return asyncio.run(run(args))
    except Exception as exc:
        print(f"[FAIL] {str(exc)[:300]}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
