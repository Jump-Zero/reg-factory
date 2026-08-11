"""Managed RuyiPage Firefox runtime state and first-use installation."""

from __future__ import annotations

import contextlib
import os
import threading
import time


_STATE_LOCK = threading.Lock()
_INSTALL_LOCK = threading.Lock()
_STATE = {
    "state": "idle",
    "message": "尚未检测 RuyiPage Firefox runtime",
    "path": "",
}


@contextlib.contextmanager
def _cross_process_install_lock(timeout: float = 900):
    from ruyipage._runtime.paths import browsers_root

    root = browsers_root()
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, ".reg-factory-install.lock")
    handle = open(path, "a+b")
    if handle.seek(0, os.SEEK_END) == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    deadline = time.monotonic() + max(1, timeout)
    locked = False
    try:
        while not locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待其他进程安装 RuyiPage Firefox 超时")
                _set_state("installing", "正在等待另一进程完成 RuyiPage Firefox 安装")
                time.sleep(0.5)
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _set_state(state: str, message: str, path: str = "") -> dict:
    with _STATE_LOCK:
        _STATE.update(state=state, message=message, path=path)
        return dict(_STATE)


def runtime_status(explicit_path: str = "") -> dict:
    configured = str(explicit_path or "").strip()
    try:
        import ruyipage
        from ruyipage import resolve_firefox_path
    except ImportError as exc:
        return _set_state("unavailable", f"RuyiPage 模块不可用：{exc}")

    candidate = str(resolve_firefox_path(configured or None) or "").strip()
    if candidate and os.path.isfile(candidate):
        return _set_state(
            "ready",
            f"RuyiPage {ruyipage.__version__} Firefox 已就绪",
            candidate,
        )
    if configured:
        return {
            "state": "failed",
            "message": f"RUYIPAGE_BROWSER_PATH 不存在：{configured}",
            "path": configured,
        }
    with _STATE_LOCK:
        current = dict(_STATE)
    if current["state"] in {"installing", "failed"}:
        return current
    return _set_state("missing", "RuyiPage Firefox 未安装，将在首次使用时自动下载")


def ensure_runtime(explicit_path: str = "", force: bool = False) -> dict:
    configured = str(explicit_path or "").strip()
    current = runtime_status(configured)
    if current["state"] == "ready" and not force:
        return {**current, "cached": True}
    if configured and current["state"] == "failed":
        raise RuntimeError(current["message"])

    with _INSTALL_LOCK:
        current = runtime_status(configured)
        if current["state"] == "ready" and not force:
            return {**current, "cached": True}
        try:
            with _cross_process_install_lock():
                current = runtime_status(configured)
                if current["state"] == "ready" and not force:
                    return {**current, "cached": True}
                _set_state("installing", "正在自动下载并安装 RuyiPage Firefox")
                from ruyipage._runtime import install

                result = install(force=force)
                path = str(result.get("executable_path") or "").strip()
                if not path or not os.path.isfile(path):
                    raise RuntimeError("安装完成后未找到 Firefox 可执行文件")
                ready = _set_state("ready", "RuyiPage Firefox 安装完成", path)
                return {**result, **ready}
        except Exception as exc:
            _set_state("failed", f"RuyiPage Firefox 自动安装失败：{str(exc)[:180]}")
            raise
