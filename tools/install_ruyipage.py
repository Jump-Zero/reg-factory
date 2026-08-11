"""Install the managed RuyiPage Firefox runtime."""

from common.ruyipage_runtime import ensure_runtime, runtime_status


def main():
    current = runtime_status()
    if current.get("state") == "ready":
        print(f"[ruyipage] already installed: {current.get('path', '')}")
        return

    print("[ruyipage] downloading the managed Firefox runtime...")
    result = ensure_runtime()
    state = "already installed" if result.get("cached") else "installed"
    path = result.get("executable_path") or result.get("path", "")
    print(f"[ruyipage] {state}: {path}")


if __name__ == "__main__":
    main()
