#!/usr/bin/env python3
"""Export saved Kiro accounts in hank9999/kiro.rs credentials.json format."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.session_export import export_kiro_rs_credentials


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export Kiro credentials as a kiro.rs-compatible JSON array."
    )
    parser.add_argument(
        "--output",
        help="Output path; defaults to tokens/kiro/credentials.json.",
    )
    args = parser.parse_args(argv)
    path, credentials = export_kiro_rs_credentials(args.output or "")
    print(json.dumps({"output": path, "accounts": len(credentials)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
