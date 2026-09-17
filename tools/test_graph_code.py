"""Read-only Microsoft Graph mailbox/verification-code smoke test.

This script reuses ``common.mailbox`` so it tests the same code path used by
the WebUI.  It only exchanges the supplied refresh token and reads messages;
it does not send mail, change mailbox state, or save credentials.

Examples (run from the repository root)::

    python tools/test_graph_code.py --check-only \
      --email helper@outlook.com \
      --refresh-token "$env:GRAPH_REFRESH_TOKEN" \
      --client-id "$env:GRAPH_CLIENT_ID"

    python tools/test_graph_code.py --wait 90 --received-after 2026-09-09T05:30:00Z \
      --email helper@outlook.com \
      --refresh-token "$env:GRAPH_REFRESH_TOKEN" \
      --client-id "$env:GRAPH_CLIENT_ID"

The four-field record form accepted by the WebUI is also supported through
``--record`` or ``OUTLOOK_GRAPH_RECOVERY_OUTLOOK_MAILBOX``::

    email----password----refresh_token----client_id
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ``python tools/test_graph_code.py`` puts ``tools/`` (not the repository
# root) on sys.path.  Add the root explicitly so the script works exactly as
# documented without requiring PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import mailbox


def _parse_received_after(value: str | None) -> float | None:
    """Convert an ISO-8601 timestamp or epoch value to Unix seconds."""
    if not value:
        return None
    raw = value.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _build_record(args: argparse.Namespace) -> dict:
    record = args.record or os.environ.get("OUTLOOK_GRAPH_RECOVERY_OUTLOOK_MAILBOX", "")
    if record:
        return mailbox.parse_outlook_recovery_mailbox(record)

    missing = [
        name
        for name, value in (
            ("email", args.email or os.environ.get("GRAPH_EMAIL", "")),
            ("refresh-token", args.refresh_token or os.environ.get("GRAPH_REFRESH_TOKEN", "")),
            ("client-id", args.client_id or os.environ.get("GRAPH_CLIENT_ID", "")),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "missing "
            + ", ".join(missing)
            + "; use --record or GRAPH_EMAIL/GRAPH_REFRESH_TOKEN/GRAPH_CLIENT_ID"
        )

    return {
        "email": args.email or os.environ["GRAPH_EMAIL"],
        "refresh_token": args.refresh_token or os.environ["GRAPH_REFRESH_TOKEN"],
        "client_id": args.client_id or os.environ["GRAPH_CLIENT_ID"],
        "provider": "outlook",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a Microsoft Graph mailbox and optionally read a verification code."
    )
    parser.add_argument("--record", help="email----password----refresh_token----client_id")
    parser.add_argument("--email", help="Outlook mailbox address (or GRAPH_EMAIL)")
    parser.add_argument("--refresh-token", help="Graph refresh token (or GRAPH_REFRESH_TOKEN)")
    parser.add_argument("--client-id", help="OAuth client id (or GRAPH_CLIENT_ID)")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="only validate token and inbox/junk access; do not wait for a code",
    )
    parser.add_argument("--wait", type=int, default=60, help="maximum code wait time in seconds (default: 60)")
    parser.add_argument("--poll", type=float, default=5, help="poll interval in seconds (default: 5)")
    parser.add_argument(
        "--received-after",
        help="ignore older messages; ISO-8601 (for example 2026-09-09T05:30:00Z) or epoch seconds",
    )
    parser.add_argument(
        "--sender",
        action="append",
        help="sender substring filter; may be repeated (default: Microsoft security senders)",
    )
    parser.add_argument(
        "--subject",
        action="append",
        help="subject substring filter; may be repeated (default: security/verification code)",
    )
    parser.add_argument(
        "--exclude-code",
        action="append",
        default=[],
        help="code to ignore; may be repeated",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        record = _build_record(args)
        received_after = _parse_received_after(args.received_after)
    except (ValueError, TypeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    email = record["email"]
    print(f"[Graph] mailbox={email}")
    print("[Graph] exchanging refresh token and checking inbox/junk access ...")
    access = mailbox.check_mailbox_access(
        email,
        record["refresh_token"],
        record["client_id"],
    )
    if not access.get("ok"):
        print(
            "[FAIL] Graph mailbox check failed: "
            f"reason={access.get('reason') or 'unknown'} "
            f"folders={access.get('folder_status') or {}}"
        )
        return 1

    print(f"[OK] Graph access granted; folders={access.get('folder_status') or {}}")
    if args.check_only:
        return 0

    senders = tuple(args.sender) if args.sender else mailbox.MICROSOFT_RECOVERY_SENDERS
    subjects = tuple(args.subject) if args.subject else mailbox.MICROSOFT_RECOVERY_SUBJECTS
    print(f"[Graph] waiting up to {max(args.wait, 0)}s for a verification code ...")
    code = mailbox.get_code_by_token(
        email,
        record["refresh_token"],
        client_id=record["client_id"],
        sender_contains=senders,
        subject_contains=subjects,
        max_wait=max(args.wait, 0),
        poll=max(args.poll, 0),
        received_after=received_after,
        exclude_codes=args.exclude_code,
    )
    if code:
        print(f"[OK] verification code={code}")
        return 0

    print("[TIMEOUT] no matching verification code found")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
