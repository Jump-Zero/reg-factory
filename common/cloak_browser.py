"""CloakBrowser adapter used by the existing async registration flow.

The reference project launches Cloak's native fingerprint Chromium directly
instead of creating a second Playwright CDP client.  This small handle keeps
the BitBrowser-compatible lifecycle expected by ``common.browser`` while
letting Cloak own the browser context and fingerprint.
"""

from __future__ import annotations

import os
import uuid

from config import (
    CLOAK_EXTRA_ARGS,
    CLOAK_FINGERPRINT_SEED,
    CLOAK_GEOIP,
    CLOAK_HEADLESS,
    CLOAK_HUMANIZE,
    CLOAK_LICENSE_KEY,
    CLOAK_LOCALE,
    CLOAK_TIMEZONE,
    CLOAK_USER_DATA_DIR,
)


def _proxy_url(browser_options: dict | None = None) -> str | None:
    options = dict(browser_options or {})
    if str(options.get("proxyType") or "").lower() == "noproxy":
        return None
    raw = options.get("proxy_str") or options.get("proxyUrl") or options.get("proxy")
    if raw:
        return str(raw).replace("socks5h://", "socks5://")
    try:
        from common import proxy_switch

        value = proxy_switch.effective_proxy_url()
        return str(value or "").replace("socks5h://", "socks5://") or None
    except Exception:
        return None


class CloakBrowserHandle:
    provider_name = "cloak"

    def __init__(self, context, *, profile_id: str, name: str):
        self.context = context
        self.profile_id = str(profile_id)
        self.name = str(name)
        self.browser = getattr(context, "browser", None) or context

    async def close_browser_async(self, profile_id: str | None = None):
        if profile_id and str(profile_id) != self.profile_id:
            return {"success": True}
        try:
            await self.context.close()
        except Exception:
            pass
        return {"success": True}

    def close_browser(self, profile_id: str | None = None):
        # common.browser.teardown prefers the async method for this provider.
        return {"success": True}

    def delete_browser(self, profile_id: str | None = None):
        try:
            from common.browser_registry import unregister

            unregister(profile_id or self.profile_id)
        except Exception:
            pass
        return {"success": True}

    async def launch(self, name: str, browser_options: dict | None = None):
        try:
            from cloakbrowser import launch_context_async, launch_persistent_context_async
        except ImportError as exc:
            raise RuntimeError(
                "未安装 CloakBrowser，请执行：pip install \"cloakbrowser[geoip]>=0.4.10\""
            ) from exc

        args = list(CLOAK_EXTRA_ARGS or [])
        if CLOAK_FINGERPRINT_SEED:
            args.append(f"--fingerprint={CLOAK_FINGERPRINT_SEED.strip()}")
        proxy = _proxy_url(browser_options)
        options = {
            "headless": bool(CLOAK_HEADLESS),
            "humanize": bool(CLOAK_HUMANIZE),
            "geoip": bool(CLOAK_GEOIP),
        }
        if proxy:
            options["proxy"] = proxy
        if args:
            options["args"] = args
        if CLOAK_LOCALE.strip():
            options["locale"] = CLOAK_LOCALE.strip()
        if CLOAK_TIMEZONE.strip():
            options["timezone"] = CLOAK_TIMEZONE.strip()
        if CLOAK_LICENSE_KEY.strip():
            options["license_key"] = CLOAK_LICENSE_KEY.strip()

        user_data_dir = CLOAK_USER_DATA_DIR.strip()
        try:
            if user_data_dir:
                context = await launch_persistent_context_async(user_data_dir, **options)
            else:
                context = await launch_context_async(**options)
        except ImportError as exc:
            # geoip2 is optional. Cloak's native fingerprint remains usable
            # without automatic language/timezone lookup.
            if not options.get("geoip") or "geoip" not in str(exc).lower():
                raise
            options["geoip"] = False
            print("  CloakBrowser geoip2 unavailable; continuing with native fingerprint")
            if user_data_dir:
                context = await launch_persistent_context_async(user_data_dir, **options)
            else:
                context = await launch_context_async(**options)
        profile_id = f"cloak-{uuid.uuid4().hex}"
        handle = CloakBrowserHandle(context, profile_id=profile_id, name=name)
        try:
            from common.browser_registry import register

            register(profile_id, name=name, provider="cloak")
        except Exception:
            pass
        print(f"  CloakBrowser profile created: {name} ({profile_id})")
        return handle, profile_id, context
