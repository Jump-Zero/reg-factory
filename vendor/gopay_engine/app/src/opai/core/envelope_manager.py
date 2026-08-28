"""
GoPay Envelope (Red Packet) Link Manager

Manages multiple envelope links, tracks their status (active/expired/depleted),
and provides claim functionality for newly registered accounts.

Usage:
    mgr = EnvelopeManager()
    mgr.add_url("https://app.gopay.co.id/NF8p/7eo868i7")
    mgr.add_url("https://app.gopay.co.id/NF8p/ymccante")
    result = mgr.claim_one(client)  # claims from first available envelope
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tls_client

log = logging.getLogger(__name__)

STORE_FILE = Path(__file__).parent / "envelope_links.json"


@dataclass
class EnvelopeLink:
    url: str
    deeplink_id: str
    link_id: Optional[str] = None
    status: str = "active"  # active / expired / depleted / error
    total_recipients: int = 0
    claimed_recipients: int = 0
    expired_at: Optional[str] = None
    added_at: str = ""
    last_checked: str = ""
    error_msg: str = ""

    def to_dict(self):
        return {
            "url": self.url,
            "deeplink_id": self.deeplink_id,
            "link_id": self.link_id,
            "status": self.status,
            "total_recipients": self.total_recipients,
            "claimed_recipients": self.claimed_recipients,
            "expired_at": self.expired_at,
            "added_at": self.added_at,
            "last_checked": self.last_checked,
            "error_msg": self.error_msg,
        }

    @staticmethod
    def from_dict(d):
        return EnvelopeLink(**d)


class EnvelopeManager:
    def __init__(self, store_path: Optional[Path] = None):
        self.store_path = store_path or STORE_FILE
        self.links: list[EnvelopeLink] = []
        self._load()

    def _load(self):
        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                self.links = [EnvelopeLink.from_dict(d) for d in data]
            except Exception as e:
                log.warning("Failed to load envelope links: %s", e)
                self.links = []

    def _save(self):
        self.store_path.write_text(
            json.dumps([l.to_dict() for l in self.links], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _resolve_url(self, url: str) -> tuple[Optional[str], Optional[str]]:
        """Resolve a GoPay short URL to extract deeplink_id and optional link_id."""
        try:
            s = tls_client.Session(client_identifier="okhttp4_android_13")
            resp = s.get(url, headers={
                "User-Agent": "GoPay/2.7.0 (com.gojek.gopay; build:2070; Android, 9)",
            })
            html = resp.text

            deeplink_id = None
            link_id = None

            # Extract envelope_request_id
            m = re.search(r"envelope_request_id[:%\s\"]*([0-9a-f]{24})", html)
            if m:
                deeplink_id = m.group(1)

            # Extract link_id if present
            m2 = re.search(r"link_id[:%\s\"]*([0-9a-f]{24})", html)
            if m2:
                link_id = m2.group(1)

            return deeplink_id, link_id
        except Exception as e:
            log.error("Failed to resolve URL %s: %s", url, e)
            return None, None

    def add_url(self, url: str) -> Optional[EnvelopeLink]:
        """Add a GoPay envelope URL. Resolves it and stores the deeplink_id."""
        # Check duplicate
        for l in self.links:
            if l.url == url:
                log.info("URL already exists: %s (status=%s)", url, l.status)
                return l

        deeplink_id, link_id = self._resolve_url(url)
        if not deeplink_id:
            log.error("Could not resolve deeplink_id from URL: %s", url)
            return None

        # Check if same deeplink_id already exists from different URL
        for l in self.links:
            if l.deeplink_id == deeplink_id:
                log.info("Deeplink ID %s already tracked (url=%s)", deeplink_id, l.url)
                return l

        link = EnvelopeLink(
            url=url,
            deeplink_id=deeplink_id,
            link_id=link_id,
            added_at=self._now(),
        )
        self.links.append(link)
        self._save()
        log.info("Added envelope: %s → deeplink_id=%s link_id=%s", url, deeplink_id, link_id)
        return link

    def add_deeplink_id(self, deeplink_id: str, link_id: Optional[str] = None) -> EnvelopeLink:
        """Add an envelope by deeplink_id directly (no URL resolution needed)."""
        for l in self.links:
            if l.deeplink_id == deeplink_id:
                return l
        link = EnvelopeLink(
            url=f"deeplink://{deeplink_id}",
            deeplink_id=deeplink_id,
            link_id=link_id,
            added_at=self._now(),
        )
        self.links.append(link)
        self._save()
        return link

    def check_status(self, link: EnvelopeLink, client) -> str:
        """Check and update the status of an envelope link using an authenticated client."""
        try:
            r = client._gopay_get(f"/v1/festivals/envelope-requests/{link.deeplink_id}")
            link.last_checked = self._now()

            if r["status"] != 200:
                err = r["body"]
                code = ""
                if isinstance(err, dict):
                    errors = err.get("errors", [])
                    if errors:
                        code = errors[0].get("code", "")
                        link.error_msg = errors[0].get("message", "")
                if code == "GoPay-36006":
                    link.status = "expired"
                else:
                    link.status = "error"
                    link.error_msg = f"{r['status']} {code}"
                self._save()
                return link.status

            data = r["body"].get("data", {})
            link.expired_at = data.get("expired_at", "")
            group = data.get("group_envelope_details", {})
            link.total_recipients = group.get("total_recipients", 0)
            link.claimed_recipients = group.get("total_claimed_recipients", 0)
            status = data.get("status", "")

            # Check expiry
            if link.expired_at:
                try:
                    exp = datetime.fromisoformat(link.expired_at.replace("Z", "+00:00"))
                    if datetime.now(timezone.utc) > exp:
                        link.status = "expired"
                        self._save()
                        return link.status
                except Exception:
                    pass

            # Check if fully claimed
            if link.total_recipients > 0 and link.claimed_recipients >= link.total_recipients:
                link.status = "depleted"
            elif status in ("EXPIRED", "CANCELLED"):
                link.status = "expired"
            else:
                link.status = "active"

            self._save()
            return link.status

        except Exception as e:
            link.status = "error"
            link.error_msg = str(e)
            self._save()
            return link.status

    def refresh_all(self, client):
        """Refresh status of all links using an authenticated client."""
        for link in self.links:
            if link.status in ("expired", "depleted"):
                continue
            self.check_status(link, client)
            time.sleep(1)

    def get_active(self) -> list[EnvelopeLink]:
        """Return all active (claimable) links."""
        return [l for l in self.links if l.status == "active"]

    def claim_one(self, client) -> Optional[dict]:
        """Try to claim from the first available active envelope.

        Returns the claim result dict, or None if no active envelopes.
        Automatically marks expired/depleted envelopes.
        """
        for link in self.links:
            if link.status not in ("active",):
                continue

            # Refresh status first
            self.check_status(link, client)
            if link.status != "active":
                log.info("Envelope %s is %s, skipping", link.deeplink_id[:12], link.status)
                continue

            # Try to claim
            log.info("Claiming envelope %s (%d/%d claimed)",
                     link.deeplink_id[:12], link.claimed_recipients, link.total_recipients)
            try:
                r = client.envelope_claim(link.deeplink_id)
                if r["status"] == 200 and r["body"].get("success"):
                    log.info("Envelope claimed successfully!")
                    link.claimed_recipients += 1
                    if link.claimed_recipients >= link.total_recipients:
                        link.status = "depleted"
                    self._save()
                    return r
                else:
                    code = ""
                    errors = r["body"].get("errors", [])
                    if errors:
                        code = errors[0].get("code", "")
                        msg = errors[0].get("message", "")
                    else:
                        code = str(r["status"])
                        msg = str(r["body"])

                    if code == "GoPay-36006":
                        link.status = "expired"
                        log.info("Envelope expired: %s", msg)
                    elif code == "GoPay-36009":
                        log.info("Already claimed by this account, trying next")
                        continue
                    elif code == "GoPay-36008":
                        link.status = "error"
                        link.error_msg = "link not found"
                        log.warning("Envelope link not found")
                    else:
                        link.error_msg = f"{code}: {msg}"
                        log.warning("Envelope claim failed: %s %s", code, msg)
                    self._save()
                    continue

            except Exception as e:
                log.error("Claim error: %s", e)
                continue

        log.info("No active envelopes available")
        return None

    def summary(self) -> str:
        """Return a human-readable summary of all envelope links."""
        lines = []
        for i, l in enumerate(self.links):
            status_icon = {"active": "✅", "expired": "⏰", "depleted": "🔴", "error": "⚠️"}.get(l.status, "❓")
            claimed = f"{l.claimed_recipients}/{l.total_recipients}" if l.total_recipients else "?"
            lines.append(f"  [{i}] {status_icon} {l.status:8s} {claimed:7s} {l.deeplink_id[:16]}... {l.url}")
        return f"Envelopes ({len(self.links)}):\n" + "\n".join(lines) if lines else "No envelopes configured"
