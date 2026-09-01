#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "Starting standalone Codex K12 on http://127.0.0.1:8806 ..."
if [[ -x ./reg-factory ]]; then
  exec ./reg-factory --k12
elif [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python -m uvicorn k12.server:app --host 127.0.0.1 --port 8806
else
  echo "Install reg-factory or the main Python environment first." >&2
  exit 1
fi
