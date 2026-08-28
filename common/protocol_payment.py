"""Protocol payment-link catalog and optional local-engine discovery.

The WebUI keeps its own small contract instead of importing an external tool at
startup.  A locally installed protocol engine is only loaded by the dedicated
batch worker, so normal authorization/import runs remain self-contained.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PAYMENT_METHODS: tuple[dict[str, Any], ...] = (
    {"id": "paypal", "label": "PayPal", "country": "US", "currency": "USD", "batch_enabled": True, "payment_execution": "paypal_auto"},
    {"id": "gopay", "label": "GoPay", "country": "ID", "currency": "IDR", "batch_enabled": True, "payment_execution": "gopay_wallet"},
    {"id": "gcash", "label": "GCash", "country": "PH", "currency": "PHP", "batch_enabled": True},
    {"id": "grabpay", "label": "GrabPay", "country": "PH", "currency": "PHP", "batch_enabled": True},
    {"id": "upi", "label": "UPI", "country": "IN", "currency": "INR", "batch_enabled": True},
    {"id": "ideal", "label": "iDEAL", "country": "NL", "currency": "EUR", "batch_enabled": True},
    {"id": "pix", "label": "PIX", "country": "BR", "currency": "BRL", "batch_enabled": True},
    {"id": "kakao", "label": "Kakao Pay", "country": "KR", "currency": "KRW", "batch_enabled": True},
    {"id": "blik", "label": "BLIK", "country": "PL", "currency": "PLN", "batch_enabled": False, "payment_execution": "single_code"},
    {"id": "twint", "label": "TWINT", "country": "CH", "currency": "CHF", "batch_enabled": True},
    {"id": "direct_card", "label": "Direct Card Checkout", "country": "PH", "currency": "PHP", "batch_enabled": True},
    {"id": "momo", "label": "MoMo", "country": "VN", "currency": "VND", "batch_enabled": True},
)

def _catalog_methods(engine_root: object = "") -> list[dict[str, Any]]:
    root = resolve_protocol_engine_root(engine_root)
    if root:
        try:
            payload = json.loads((root / "payment_methods.json").read_text(encoding="utf-8-sig"))
            methods = payload.get("methods") if isinstance(payload, dict) else None
            if isinstance(methods, list):
                normalized = []
                for raw in methods:
                    if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
                        continue
                    item = dict(raw)
                    item["id"] = str(item["id"]).strip().lower()
                    item["label"] = str(item.get("display_name") or item["id"])
                    item["batch_enabled"] = bool(item.get("batch_enabled", True))
                    if item["id"] == "paypal":
                        item["payment_execution"] = "paypal_auto"
                    elif item["id"] == "gopay":
                        item["payment_execution"] = "gopay_wallet"
                    elif item["id"] == "blik":
                        item["payment_execution"] = "single_code"
                    normalized.append(item)
                if normalized:
                    return normalized
        except (OSError, ValueError, TypeError):
            pass
    return [dict(item) for item in PAYMENT_METHODS]


def payment_method(method: object, engine_root: object = "") -> dict[str, Any] | None:
    """Resolve one method using the installed engine's canonical catalog."""
    requested = str(method or "").strip().lower()
    for item in _catalog_methods(engine_root):
        aliases = {str(value or "").strip().lower() for value in item.get("aliases") or []}
        if requested == item["id"] or requested in aliases:
            return dict(item)
    return None


def resolve_protocol_engine_root(value: object = "") -> Path | None:
    """Locate a compatible local GPT-Register-Tool checkout, if installed."""
    project_root = Path(__file__).resolve().parents[1]
    data_root = Path(os.environ.get("REG_FACTORY_DATA_DIR") or project_root).resolve()
    candidates = (
        str(value or "").strip(),
        os.environ.get("REG_FACTORY_PROTOCOL_PAYMENT_ROOT", "").strip(),
        str(data_root.parent / "GPT-Register-Tool"),
        str(project_root.parent / "GPT-Register-Tool"),
    )
    seen: set[Path] = set()
    for raw in candidates:
        if not raw:
            continue
        try:
            root = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if root in seen:
            continue
        seen.add(root)
        if (root / "sms_tool" / "payment_link_manager.py").is_file() and (root / "payment_methods.json").is_file():
            return root
    return None


def protocol_catalog(engine_root: object = "") -> list[dict[str, Any]]:
    """Return UI-safe channel metadata and whether the local bridge can run it."""
    root = resolve_protocol_engine_root(engine_root)
    paypal_ready = paypal_payment_ready(root)
    try:
        from common import gopay_service

        gopay_status = gopay_service.status()
    except Exception:
        gopay_status = {"ready": False, "available_accounts": 0}
    return [
        {
            **item,
            "available": bool(root),
            "batch_payment_enabled": item.get("payment_execution") in {"paypal_auto", "gopay_wallet"},
            "payment_available": bool(
                root
                and (
                    item.get("payment_execution") == "paypal_auto"
                    or (
                        item.get("payment_execution") == "gopay_wallet"
                        and gopay_status.get("ready")
                    )
                )
            ),
            "payment_configured": bool(
                (item.get("payment_execution") == "paypal_auto" and paypal_ready)
                or (
                    item.get("payment_execution") == "gopay_wallet"
                    and int(gopay_status.get("available_accounts") or 0) > 0
                )
            ),
            "payment_reason": (
                "可使用引擎默认资料，也可在本次任务中临时录入"
                if item.get("payment_execution") == "paypal_auto" and paypal_ready
                else "需要在本次任务中录入卡片、地址和手机号接码资料"
                if item.get("payment_execution") == "paypal_auto"
                else f"GoPay 钱包池可用账号 {int(gopay_status.get('available_accounts') or 0)} 个"
                if item.get("payment_execution") == "gopay_wallet" and gopay_status.get("ready")
                else "GoPay 钱包引擎不可用"
                if item.get("payment_execution") == "gopay_wallet"
                else "BLIK 仅支持单账号六位码支付，不支持批量"
                if item.get("payment_execution") == "single_code"
                else "该渠道需在提取的链接或二维码中完成渠道确认"
            ),
            "reason": (
                "该渠道的上游协议不支持批量"
                if not item["batch_enabled"]
                else "本机未找到协议引擎"
                if not root
                else ""
            ),
        }
        for item in _catalog_methods(root or engine_root)
    ]


def paypal_payment_ready(engine_root: object = "") -> bool:
    """Check PayPal auto-pay prerequisites without returning payment details."""
    root = resolve_protocol_engine_root(engine_root)
    if not root:
        return False
    try:
        data = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return False
    config = data.get("paypal_auto") if isinstance(data, dict) else None
    return paypal_payment_config_ready(config)


def paypal_payment_config_ready(config: object) -> bool:
    """Validate the non-secret shape required by the PayPal execution adapter."""
    if not isinstance(config, dict):
        return False
    has_phone = bool(config.get("phone_numbers") or config.get("phone_number"))
    return bool(config.get("cards") and config.get("addresses") and has_phone)
