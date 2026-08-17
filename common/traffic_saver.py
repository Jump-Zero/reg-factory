"""Playwright request filtering for metered residential proxy sessions."""

from __future__ import annotations

from collections import Counter
import os
from urllib.parse import urlparse

from common.playwright_runtime import install_shutdown_guard
from common.task_context import task_environment


_MODES = {"off", "balanced", "aggressive", "extreme"}
_BALANCED_TYPES = {"image", "font", "media"}
_AGGRESSIVE_TYPES = _BALANCED_TYPES | {"stylesheet"}
_EXTREME_TYPES = _AGGRESSIVE_TYPES | {"manifest", "texttrack"}

# Outlook's sign-up and OAuth pages use CSS to decide whether controls are
# visible. Keep styles there even in aggressive mode while still blocking the
# heavier image/font/media classes.
_STYLE_REQUIRED_DOMAINS = {
    # Authentication pages need their layout CSS even in extreme mode. Without
    # it BitBrowser can expose a zero-sized viewport and challenge iframes stay
    # in a loading shell.
    "anthropic.com",
    "cdn.office.net",
    "claude.com",
    "claude.ai",
    "live.com",
    "microsoft.com",
    "microsoftonline.com",
    "microsoftonline-p.com",
    "msauth.net",
    "msftauth.net",
    "oaistatic.com",
    "openai.com",
    "office.com",
    "x.ai",
    "auth.x.ai",
    "github.com",
    "githubassets.com",
}

# Challenge assets are intentionally exempt, including image challenges.
_ALLOW_DOMAINS = {
    "arkoselabs.com",
    "challenges.cloudflare.com",
    "cloudflare.com",
    "fpt.live.com",
    "funcaptcha.com",
    "hcaptcha.com",
    "hsprotect.net",
    "px-cdn.net",
    "px-cloud.net",
    "turnstile.com",
}

# These endpoints are optional page analytics, not registration APIs. They are
# blocked only in aggressive mode because some sites include telemetry in risk
# scoring.
_TELEMETRY_DOMAINS = {
    "amplitude.com",
    "clarity.ms",
    "datadoghq.com",
    "doubleclick.net",
    "fullstory.com",
    "google-analytics.com",
    "googletagmanager.com",
    "hotjar.com",
    "newrelic.com",
    "segment.com",
    "segment.io",
    "sentry.io",
}

_EXTREME_TELEMETRY_DOMAINS = _TELEMETRY_DOMAINS | {
    "adnxs.com",
    "ads-twitter.com",
    "braze.com",
    "facebook.net",
    "googleadservices.com",
    "googlesyndication.com",
    "intercom.io",
    "mixpanel.com",
    "mouseflow.com",
    "posthog.com",
    "scorecardresearch.com",
    "statcounter.com",
}

_EXTREME_OPTIONAL_SUFFIXES = (
    ".map",
    "/favicon.ico",
    "/favicon.svg",
    "/manifest.json",
    "/site.webmanifest",
)

_EXTREME_BITBROWSER_ARGS = (
    "--disable-background-networking",
    "--disable-breakpad",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-sync",
    "--no-default-browser-check",
    "--no-first-run",
    "--window-size=1280,800",
    "--disable-features=AutofillServerCommunication,MediaRouter,OptimizationHints",
)


def _domain_matches(host: str, domains: set[str]) -> bool:
    normalized = str(host or "").strip(".").lower()
    return any(normalized == domain or normalized.endswith(f".{domain}") for domain in domains)


def configured_mode(environ=None) -> str:
    """Return the configured saver mode, active only on residential egress."""
    env = task_environment(os.environ) if environ is None else environ
    value = str(env.get("REG_FACTORY_RESIDENTIAL_TRAFFIC_MODE") or "balanced").strip().lower()
    mode = value if value in _MODES else "balanced"
    try:
        from common import proxy_switch

        if proxy_switch.proxy_mode(env) != "residential":
            return "off"
    except Exception:
        return "off"
    return mode


def _platform(environ=None) -> str:
    try:
        from common import proxy_switch

        return proxy_switch.platform_name(environ)
    except Exception:
        env = task_environment(os.environ) if environ is None else environ
        return str(env.get("REG_FACTORY_PLATFORM") or "").strip().lower()


def should_block(url: str, resource_type: str, mode: str, headers=None) -> bool:
    """Decide whether a browser request is safe to omit."""
    normalized_mode = str(mode or "off").lower()
    if normalized_mode not in {"balanced", "aggressive", "extreme"}:
        return False
    host = (urlparse(str(url or "")).hostname or "").lower()
    if _domain_matches(host, _ALLOW_DOMAINS):
        return False
    normalized_type = str(resource_type or "").lower()
    style_required_auth = _domain_matches(host, _STYLE_REQUIRED_DOMAINS)
    if normalized_mode == "extreme" and not style_required_auth:
        blocked_types = _EXTREME_TYPES
    elif normalized_mode == "aggressive" and not style_required_auth:
        blocked_types = _AGGRESSIVE_TYPES
    else:
        blocked_types = _BALANCED_TYPES
    if normalized_type in blocked_types:
        return True
    if normalized_mode in {"aggressive", "extreme"} and _domain_matches(host, _TELEMETRY_DOMAINS):
        return True
    if normalized_mode != "extreme":
        return False
    if _domain_matches(host, _EXTREME_TELEMETRY_DOMAINS):
        return True
    path = (urlparse(str(url or "")).path or "").lower()
    if path.endswith(_EXTREME_OPTIONAL_SUFFIXES):
        return True
    request_headers = {
        str(key).lower(): str(value).lower()
        for key, value in dict(headers or {}).items()
    }
    purpose = f"{request_headers.get('purpose', '')} {request_headers.get('sec-purpose', '')}"
    return "prefetch" in purpose or "prerender" in purpose


def bitbrowser_profile_defaults(environ=None) -> dict:
    """Return profile defaults that do not override BitBrowser's startup page."""
    # Forcing ``about:blank`` saved a small amount of startup traffic, but a
    # delayed CDP connection could leave the profile parked there indefinitely.
    # Request filtering and Chromium's background-network switches provide the
    # material savings without changing the profile's startup URL.
    return {}


def bitbrowser_open_payload(profile_id, environ=None) -> dict:
    """Suppress Chromium background traffic on metered BitBrowser profiles."""
    payload = {"id": profile_id}
    # ChatGPT's Cloudflare/Turnstile bootstrap relies on browser background
    # networking; keep the request filter safe but omit these startup switches.
    if configured_mode(environ) == "extreme" and _platform(environ) != "chatgpt":
        payload["args"] = list(_EXTREME_BITBROWSER_ARGS)
    return payload


async def install(context, environ=None) -> str:
    """Install one context-wide filter and return the active mode."""
    # BitBrowser closes Chromium through its local API. Playwright can finish
    # a detached protocol future one event-loop tick later.
    install_shutdown_guard()
    mode = configured_mode(environ)
    if mode == "off" or getattr(context, "_reg_factory_traffic_saver", False):
        return mode
    env = task_environment(os.environ) if environ is None else environ
    try:
        from common import proxy_switch

        platform = proxy_switch.platform_name(env) or "global"
    except Exception:
        platform = str(env.get("REG_FACTORY_PLATFORM") or "global").strip().lower()
    if mode == "extreme" and platform == "chatgpt":
        # Keep heavy assets filtered, but do not abort prefetch/telemetry
        # requests that are part of the auth bootstrap on this platform.
        mode = "balanced"
        print("  [traffic] chatgpt extreme -> balanced auth-safe filter")
    route_method = getattr(context, "route", None)
    if not callable(route_method) or not getattr(context, "_reg_factory_route_support", True):
        return "off"
    blocked = Counter()
    setattr(context, "_reg_factory_traffic_stats", blocked)
    setattr(context, "_reg_factory_traffic_mode", mode)
    setattr(context, "_reg_factory_traffic_platform", platform)

    async def _handle(route):
        request = route.request
        try:
            if getattr(context, "_reg_factory_traffic_bypass", False):
                await route.continue_()
                return
            if should_block(
                request.url,
                request.resource_type,
                mode,
                getattr(request, "headers", None),
            ):
                blocked[str(request.resource_type or "other").lower()] += 1
                total = sum(blocked.values())
                if total in {25, 100, 250, 500}:
                    details = ", ".join(
                        f"{kind}={count}" for kind, count in sorted(blocked.items())
                    )
                    print(f"  [traffic] blocked={total} ({details})")
                await route.abort()
            else:
                await route.continue_()
        except Exception as exc:
            message = str(exc).lower()
            if any(marker in message for marker in (
                "target page, context or browser has been closed",
                "request context disposed",
                "route is already handled",
            )):
                return
            raise

    try:
        await route_method("**/*", _handle)
        setattr(context, "_reg_factory_traffic_saver", True)
    except (AttributeError, NotImplementedError, TypeError):
        return "off"
    print(f"  [traffic] platform={platform} residential browser saver={mode}")
    return mode


def set_bypass(context, enabled=True) -> bool:
    """Temporarily let every request through without replacing the route."""
    if not getattr(context, "_reg_factory_traffic_saver", False):
        return False
    setattr(context, "_reg_factory_traffic_bypass", bool(enabled))
    return True


def stats(context) -> dict[str, int]:
    """Return request counts without exposing URLs or credentials."""
    values = getattr(context, "_reg_factory_traffic_stats", {})
    return {str(key): int(value) for key, value in dict(values).items()}


def log_summary(context) -> dict[str, int]:
    values = stats(context)
    if getattr(context, "_reg_factory_traffic_saver", False) is True:
        total = sum(values.values())
        details = ", ".join(f"{kind}={count}" for kind, count in sorted(values.items()))
        suffix = f" ({details})" if details else ""
        platform = getattr(context, "_reg_factory_traffic_platform", "global")
        mode = getattr(context, "_reg_factory_traffic_mode", "unknown")
        print(
            f"  [traffic] platform={platform} summary mode={mode} "
            f"blocked={total}{suffix}"
        )
    return values
