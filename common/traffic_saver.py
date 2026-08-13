"""Playwright request filtering for metered residential proxy sessions."""

from __future__ import annotations

from collections import Counter
import os
from urllib.parse import urlparse

from common.task_context import task_environment


_MODES = {"off", "balanced", "aggressive"}
_BALANCED_TYPES = {"image", "font", "media"}
_AGGRESSIVE_TYPES = _BALANCED_TYPES | {"stylesheet"}

# Outlook's sign-up and OAuth pages use CSS to decide whether controls are
# visible. Keep styles there even in aggressive mode while still blocking the
# heavier image/font/media classes.
_STYLE_REQUIRED_DOMAINS = {
    "cdn.office.net",
    "live.com",
    "microsoft.com",
    "microsoftonline.com",
    "microsoftonline-p.com",
    "msauth.net",
    "msftauth.net",
    "office.com",
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


def should_block(url: str, resource_type: str, mode: str) -> bool:
    """Decide whether a browser request is safe to omit."""
    normalized_mode = str(mode or "off").lower()
    if normalized_mode not in {"balanced", "aggressive"}:
        return False
    host = (urlparse(str(url or "")).hostname or "").lower()
    if _domain_matches(host, _ALLOW_DOMAINS):
        return False
    normalized_type = str(resource_type or "").lower()
    microsoft_auth = _domain_matches(host, _STYLE_REQUIRED_DOMAINS)
    blocked_types = (
        _AGGRESSIVE_TYPES
        if normalized_mode == "aggressive" and not microsoft_auth
        else _BALANCED_TYPES
    )
    if normalized_type in blocked_types:
        return True
    return normalized_mode == "aggressive" and _domain_matches(host, _TELEMETRY_DOMAINS)


async def install(context, environ=None) -> str:
    """Install one context-wide filter and return the active mode."""
    mode = configured_mode(environ)
    if mode == "off" or getattr(context, "_reg_factory_traffic_saver", False):
        return mode
    route_method = getattr(context, "route", None)
    if not callable(route_method) or not getattr(context, "_reg_factory_route_support", True):
        return "off"
    blocked = Counter()
    setattr(context, "_reg_factory_traffic_stats", blocked)

    async def _handle(route):
        request = route.request
        try:
            if should_block(request.url, request.resource_type, mode):
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
    print(f"  [traffic] residential browser saver={mode}")
    return mode


def stats(context) -> dict[str, int]:
    """Return request counts without exposing URLs or credentials."""
    values = getattr(context, "_reg_factory_traffic_stats", {})
    return {str(key): int(value) for key, value in dict(values).items()}


def log_summary(context) -> dict[str, int]:
    values = stats(context)
    if values:
        total = sum(values.values())
        details = ", ".join(f"{kind}={count}" for kind, count in sorted(values.items()))
        print(f"  [traffic] summary blocked={total} ({details})")
    return values
