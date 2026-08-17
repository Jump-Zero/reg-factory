"""Cross-process registry for browser profiles created by reg-factory."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from common.file_lock import file_lock


def _path() -> Path:
    root = os.environ.get("REG_FACTORY_DATA_DIR", "").strip()
    if not root:
        root = str(Path(__file__).resolve().parent.parent)
    path = Path(root) / "runtime" / "active_browser_profiles.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def owner_id() -> str:
    configured = os.environ.get("REG_FACTORY_RUN_ID", "").strip()
    return configured or f"pid:{os.getpid()}"


def _load(handle) -> dict:
    try:
        handle.seek(0)
        value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save(handle, value: dict) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(value, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
    handle.flush()


def register(profile_id, *, name="", provider="bitbrowser", api_base="") -> None:
    key = str(profile_id or "").strip()
    if not key:
        return
    path = _path()
    with file_lock(path):
        with path.open("a+", encoding="utf-8") as handle:
            records = _load(handle)
            records[key] = {
                "id": key,
                "name": str(name or "")[:200],
                "provider": str(provider or "bitbrowser")[:40],
                "api_base": str(api_base or "")[:240],
                "owner": owner_id(),
                "pid": os.getpid(),
                "created_at": time.time(),
            }
            _save(handle, records)


def unregister(profile_id) -> None:
    key = str(profile_id or "").strip()
    if not key:
        return
    path = _path()
    with file_lock(path):
        if not path.exists():
            return
        with path.open("a+", encoding="utf-8") as handle:
            records = _load(handle)
            if key in records:
                records.pop(key, None)
                _save(handle, records)


def active_profiles(*, owner=None) -> list[dict]:
    path = _path()
    with file_lock(path):
        if not path.exists():
            return []
        with path.open("a+", encoding="utf-8") as handle:
            records = _load(handle)
    values = [item for item in records.values() if isinstance(item, dict)]
    if owner is not None:
        values = [item for item in values if item.get("owner") == owner]
    return values
