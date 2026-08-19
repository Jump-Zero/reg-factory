"""Short-lived persistent quarantine for proxy nodes that trigger risk checks."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path


_LOCK = threading.RLock()
_DEFAULT_TTL = 30 * 60


def _data_root() -> Path:
    return Path(os.environ.get("REG_FACTORY_DATA_DIR") or Path.cwd()).resolve()


def state_path() -> Path:
    return _data_root() / "runtime" / "state" / "chatgpt_node_taints.json"


def _ttl_seconds(value=None) -> int:
    raw = value if value is not None else os.environ.get(
        "CHATGPT_NODE_TAINT_SECONDS", _DEFAULT_TTL
    )
    try:
        return max(60, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_TTL


def _read(now=None) -> dict[str, dict]:
    now = float(time.time() if now is None else now)
    try:
        payload = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    entries = payload.get("nodes", payload) if isinstance(payload, dict) else {}
    if not isinstance(entries, dict):
        return {}
    active = {}
    for node, record in entries.items():
        key = str(node or "").strip()
        if not key or not isinstance(record, dict):
            continue
        try:
            expires_at = float(record.get("expires_at") or 0)
        except (TypeError, ValueError):
            continue
        if expires_at > now:
            active[key] = {
                "tainted_at": float(record.get("tainted_at") or now),
                "expires_at": expires_at,
                "reason": str(record.get("reason") or "risk")[:160],
                "count": max(1, int(record.get("count") or 1)),
            }
    return active


def _write(entries: dict[str, dict]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="chatgpt-node-taints-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"version": 1, "updated_at": time.time(), "nodes": entries},
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def taint(node, reason="risk", ttl=None, now=None) -> dict | None:
    """Taint a node and persist it until its short cooling period expires."""
    key = str(node or "").strip()
    if not key:
        return None
    stamp = float(time.time() if now is None else now)
    with _LOCK:
        entries = _read(stamp)
        previous = entries.get(key) or {}
        record = {
            "tainted_at": stamp,
            "expires_at": stamp + _ttl_seconds(ttl),
            "reason": str(reason or "risk")[:160],
            "count": max(1, int(previous.get("count") or 0) + 1),
        }
        entries[key] = record
        _write(entries)
        return {"node": key, **record}


def active(now=None) -> dict[str, dict]:
    """Return active taints, pruning expired entries from the persisted file."""
    stamp = float(time.time() if now is None else now)
    with _LOCK:
        entries = _read(stamp)
        path = state_path()
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                current_nodes = current.get("nodes", {}) if isinstance(current, dict) else {}
                if isinstance(current_nodes, dict) and set(current_nodes) != set(entries):
                    _write(entries)
            except (OSError, ValueError, TypeError):
                pass
        return entries


def is_tainted(node, now=None) -> bool:
    return str(node or "").strip() in active(now)


def filter_nodes(nodes, now=None) -> list[str]:
    taints = active(now)
    return [
        node for node in nodes
        if str(node or "").strip() and str(node).strip() not in taints
    ]


def clear(node=None) -> int:
    """Clear one node or all taints; useful for operator recovery."""
    with _LOCK:
        entries = _read()
        if node is None:
            count = len(entries)
            entries = {}
        else:
            key = str(node).strip()
            count = 1 if key in entries else 0
            entries.pop(key, None)
        if count:
            _write(entries)
        return count
