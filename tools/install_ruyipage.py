"""Install the managed RuyiPage Firefox runtime."""

from ruyipage._runtime import install


def main():
    print("[ruyipage] downloading the managed Firefox runtime...")
    result = install()
    state = "already installed" if result.get("cached") else "installed"
    print(f"[ruyipage] {state}: {result.get('executable_path', '')}")


if __name__ == "__main__":
    main()
