"""Install the managed RuyiPage Firefox runtime."""

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.ruyipage_runtime import ensure_runtime, runtime_status


def main():
    configured_path = os.environ.get("RUYIPAGE_BROWSER_PATH", "").strip()
    current = runtime_status(configured_path)
    if current.get("state") == "ready":
        print(f"[ruyipage] already installed: {current.get('path', '')}")
        return

    print("[ruyipage] downloading the managed Firefox runtime...")
    result = ensure_runtime(configured_path)
    state = "already installed" if result.get("cached") else "installed"
    path = result.get("executable_path") or result.get("path", "")
    print(f"[ruyipage] {state}: {path}")


if __name__ == "__main__":
    main()
