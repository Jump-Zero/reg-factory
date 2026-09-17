"""Batch-authorize Outlook accounts and persist Microsoft Graph refresh tokens.

Input is one account per line: ``email----password``.  The ``http`` and
``browser`` Graph authorization methods are explicit and independent.  HTTP
failures are recorded in ``outlook_no_graph.txt`` for a later browser run;
they never start a browser automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_accounts(path: str) -> list[tuple[str, str]]:
    accounts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for number, raw in enumerate(Path(path).expanduser().read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"-{4,}", line, maxsplit=1)
        if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
            raise ValueError(f"line {number}: expected email----password")
        email = parts[0].strip().lower()
        if email in seen:
            continue
        seen.add(email)
        accounts.append((email, parts[1].strip()))
    return accounts


def _pending_graph_path(environ=None) -> Path:
    env = os.environ if environ is None else environ
    root = Path(str(env.get("REG_FACTORY_DATA_DIR") or ROOT)).expanduser()
    return root / "outlook_no_graph.txt"


def record_pending_account(item: tuple[str, str], environ=None) -> Path:
    """Persist an account for a later explicit browser authorization run."""
    from common.file_lock import file_lock

    email, password = item
    path = _pending_graph_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        existing = set()
        if path.is_file():
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line and not line.startswith("#"):
                    existing.add(line.split("----", 1)[0].strip().lower())
        if email.lower() not in existing:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{email}----{password}\n")
    print(f"[authorize] pending browser authorization recorded: {email} -> {path}", flush=True)
    return path


def remove_pending_account(item: tuple[str, str], environ=None) -> None:
    """Remove a successfully browser-authorized account from the retry file."""
    from common.file_lock import file_lock

    email = item[0].strip().lower()
    path = _pending_graph_path(environ)
    if not path.is_file():
        return
    with file_lock(path):
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [
            line for line in lines
            if not line.strip()
            or line.lstrip().startswith("#")
            or line.split("----", 1)[0].strip().lower() != email
        ]
        path.write_text("\n".join(kept).rstrip() + ("\n" if kept else ""), encoding="utf-8")


def _authorize_browser(item: tuple[str, str], index: int, proxy: str = "") -> dict | None:
    """Run the existing Graph page flow in a dedicated CloakBrowser context."""
    email, password = item

    async def _run() -> dict | None:
        import register_outlook_standalone as standalone
        from common.cloak_browser import CloakBrowserHandle

        name = f"graph_authorize_{index}"
        handle = CloakBrowserHandle(None, profile_id="", name=name)
        try:
            handle, _profile_id, context = await handle.launch(
                name,
                {"proxy_str": proxy} if proxy else {},
            )
            await standalone.install_traffic_saver(context)
            page = await context.new_page()
            return await standalone.extract_graph_token(
                page, context, email, password, index
            )
        finally:
            if handle and getattr(handle, "context", None):
                await handle.close_browser_async()

    try:
        return asyncio.run(_run())
    except Exception as exc:
        print(
            f"[authorize {index}] {email}: Cloak browser authorization error: "
            f"{type(exc).__name__}: {str(exc)[:160]}",
            flush=True,
        )
        return None


def authorize_one(
    item: tuple[str, str],
    index: int,
    proxy: str = "",
    method: str = "http",
    environ=None,
) -> dict | None:
    email, password = item
    selected_method = str(method or "http").strip().lower()
    if selected_method not in {"http", "browser"}:
        raise ValueError("method must be http or browser")

    print(f"[authorize {index}] {email}: starting Graph OAuth method={selected_method}", flush=True)
    if proxy:
        print(f"[authorize {index}] {email}: using dedicated proxy", flush=True)

    if selected_method == "browser":
        result = _authorize_browser(item, index, proxy)
    else:
        from tools.extract_graph_tokens import get_graph_token

        try:
            result = (
                get_graph_token(email, password, index, proxy=proxy)
                if proxy
                else get_graph_token(email, password, index)
            )
        except Exception as exc:
            print(
                f"[authorize {index}] {email}: HTTP Graph error: "
                f"{type(exc).__name__}: {str(exc)[:160]}",
                flush=True,
            )
            result = None
    if not result or not result.get("refresh_token"):
        print(f"[authorize {index}] {email}: {selected_method} authorization failed", flush=True)
        record_pending_account(item, environ)
        return None
    remove_pending_account(item, environ)
    print(f"[authorize {index}] {email}: authorized via {selected_method}", flush=True)
    return {
        "email": email,
        "password": password,
        "refresh_token": result["refresh_token"],
        "client_id": result.get("client_id") or "",
    }


def dedicated_proxy_urls(account_count: int, environ=None) -> list[str]:
    """Return one distinct residential endpoint for every account.

    Dedicated Graph authorization must never silently reuse an endpoint for
    another account.  A single rotating endpoint is not sufficient to prove
    that two OAuth sessions use different public IPs, so this function only
    accepts a pool with enough distinct endpoints.
    """
    try:
        requested = int(account_count)
    except (TypeError, ValueError):
        requested = 0
    if requested <= 0:
        return []

    from common import direct_proxy

    values = []
    seen = set()
    for item in direct_proxy.proxy_pool(environ):
        url = item.url
        if url in seen:
            continue
        seen.add(url)
        values.append(url)
    if len(values) < requested:
        raise ValueError(
            "dedicated Graph proxy requires at least "
            f"{requested} distinct REG_FACTORY_PROXY_POOL endpoints; "
            f"found {len(values)}"
        )
    return values[:requested]


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch authorize Outlook accounts for Microsoft Graph")
    parser.add_argument("--input", "-i", required=True, help="one email----password per line")
    parser.add_argument("--concurrency", "-c", type=int, default=3)
    parser.add_argument("--no-update-pool", action="store_true")
    parser.add_argument(
        "--method",
        choices=("http", "browser"),
        default="http",
        help="Graph authorization method: http, or explicit CloakBrowser mode",
    )
    parser.add_argument("--dedicated-proxy", action="store_true", help="为每个账号固定分配住宅代理池端点")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 10:
        parser.error("--concurrency must be between 1 and 10")
    try:
        accounts = load_accounts(args.input)
    except (OSError, ValueError) as exc:
        print(f"[authorize] input error: {exc}", file=sys.stderr)
        return 2
    if not accounts:
        print("[authorize] no accounts")
        return 0

    proxy_urls: list[str] = []
    if args.dedicated_proxy:
        try:
            proxy_urls = dedicated_proxy_urls(len(accounts), os.environ)
        except ValueError as exc:
            print(f"[authorize] {exc}", file=sys.stderr, flush=True)
            return 2
        print(
            f"[authorize] dedicated Graph egress enabled: "
            f"{len(proxy_urls)} accounts -> {len(proxy_urls)} distinct endpoints",
            flush=True,
        )

    def _submit(account, index):
        proxy = proxy_urls[index - 1] if proxy_urls else ""
        return authorize_one(account, index, proxy, args.method, os.environ)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.concurrency, len(accounts))) as pool:
        futures = [pool.submit(_submit, account, index) for index, account in enumerate(accounts, 1)]
        results = [result for future in futures if (result := future.result())]

    if results and not args.no_update_pool:
        from common.outlook_recovery import upsert_refresh_tokens

        update = upsert_refresh_tokens(results)
        print(
            f"[authorize] pool updated: updated={update['updated']} appended={update['appended']} "
            f"errors_cleared={update['errors_cleared']}",
            flush=True,
        )
    print(f"[authorize] complete: {len(results)}/{len(accounts)} authorized")
    return 0 if len(results) == len(accounts) else 1


if __name__ == "__main__":
    from common import proxy_switch

    proxy_switch.apply_platform_environment("outlook")
    raise SystemExit(main())
