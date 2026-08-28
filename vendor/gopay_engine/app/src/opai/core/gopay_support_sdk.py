"""Support SDK encrypted body replay from the latest real-device capture.

The Android SDK entry point for these payloads is native
``AppPlayer.appCodecV2(...)``.  Until that native routine is ported, keep the
latest valid encrypted envelopes in a local corpus and replay the matching
body shape for the real support endpoints.
"""
from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORT_PATHS = {
    "/v1/support/customer/initiate",
    "/v1/support/customer/actions",
    "/v1/support/customer/session",
    "/v1/support/customer/activity",
}


def _default_corpus_path() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "config" / "support_sdk_body_corpus.json"
        if candidate.exists():
            return candidate
    return here.parents[4] / "config" / "support_sdk_body_corpus.json"


def _corpus_path() -> Path:
    override = os.environ.get("OPAI_GOPAY_SUPPORT_BODY_CORPUS", "").strip()
    return Path(override).expanduser() if override else _default_corpus_path()


@dataclass(frozen=True)
class SupportBodyTemplate:
    path: str
    body: dict[str, str]
    index: int = 0


class SupportSdkBodyProvider:
    """Load and rotate encrypted support SDK bodies captured from a real app."""

    def __init__(self, corpus_path: str | Path | None = None):
        self.corpus_path = Path(corpus_path).expanduser() if corpus_path else _corpus_path()
        self._templates = self._load(self.corpus_path)
        self._cursors: dict[str, int] = {}

    @staticmethod
    def _load(path: Path) -> dict[str, list[SupportBodyTemplate]]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

        grouped: dict[str, list[SupportBodyTemplate]] = {}
        for item in data.get("items", []):
            body = item.get("body")
            request_path = str(item.get("path") or "")
            if request_path not in SUPPORT_PATHS or not isinstance(body, dict):
                continue
            if not {"support_lang", "support_code", "support_id", "data"} <= set(body):
                continue
            grouped.setdefault(request_path, []).append(
                SupportBodyTemplate(
                    path=request_path,
                    index=int(item.get("index") or 0),
                    body={k: str(body[k]) for k in ("support_lang", "support_code", "support_id", "data")},
                )
            )
        return grouped

    def available_paths(self) -> set[str]:
        return {path for path, items in self._templates.items() if items}

    def next_body(self, path: str, *, prefer_shortest: bool = False) -> dict[str, str]:
        templates = self._templates.get(path) or []
        if not templates:
            raise RuntimeError(f"missing encrypted support SDK body for {path}: {self.corpus_path}")

        if prefer_shortest:
            template = min(templates, key=lambda item: len(item.body.get("data", "")))
        else:
            pos = self._cursors.get(path, random.randrange(len(templates)))
            template = templates[pos % len(templates)]
            self._cursors[path] = (pos + 1) % len(templates)
        return dict(template.body)


_PROVIDER: SupportSdkBodyProvider | None = None


def get_support_body_provider() -> SupportSdkBodyProvider:
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = SupportSdkBodyProvider()
    return _PROVIDER


def support_body_lengths(body: dict[str, Any]) -> dict[str, int]:
    return {key: len(str(body.get(key, ""))) for key in ("support_lang", "support_code", "support_id", "data")}
