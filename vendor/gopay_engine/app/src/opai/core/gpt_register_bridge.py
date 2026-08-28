"""No-op compatibility bridge for the GoPay-only share package.

The full GPT/email/OAuth registration project is intentionally omitted from
this distribution.  Payment inbox imports this bridge for compatibility, but
the GoPay UI never starts those routes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class GptRegisterBridge:
    def __init__(self, root: Path | str | None = None, **_: Any) -> None:
        self.root = Path(root or "gpt_register")

    def list_accounts(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"accounts": [], "total": 0}

    def success_pool(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def summary(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"emails": {}, "tasks_total": 0, "tasks_running": 0}

    def __getattr__(self, name: str):
        def unavailable(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"error": "此分享包仅包含 GoPay 注册/登录、号码池、账号和支付功能"}

        return unavailable
