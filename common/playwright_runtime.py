"""Small event-loop guards for browser processes closed outside Playwright."""

from __future__ import annotations

import asyncio


_TARGET_CLOSED_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "context has been closed",
)
_GUARD_ATTRIBUTE = "_reg_factory_playwright_shutdown_guard"


def is_expected_shutdown_error(context: dict) -> bool:
    """Return whether an asyncio background failure only reports browser exit."""
    error = context.get("exception")
    message = str(error or context.get("message") or "").lower()
    return type(error).__name__ == "TargetClosedError" or any(
        marker in message for marker in _TARGET_CLOSED_MARKERS
    )


def install_shutdown_guard(loop=None) -> None:
    """Suppress detached Playwright close futures while preserving other errors."""
    active_loop = loop or asyncio.get_running_loop()
    if getattr(active_loop, _GUARD_ATTRIBUTE, False) is True:
        return
    previous_handler = active_loop.get_exception_handler()

    def _handle(target_loop, context):
        if is_expected_shutdown_error(context):
            return
        if previous_handler is not None:
            previous_handler(target_loop, context)
        else:
            target_loop.default_exception_handler(context)

    setattr(active_loop, _GUARD_ATTRIBUTE, True)
    active_loop.set_exception_handler(_handle)
