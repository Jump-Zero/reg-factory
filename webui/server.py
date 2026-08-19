# -*- coding: utf-8 -*-
"""
webui/server.py — reg-factory 本地 Web 面板后端(FastAPI)。

只绑 127.0.0.1(含 .env 密钥编辑，绝不监听公网)。职责：
  - 提供脚本 schema / .env 配置 给前端渲染表单
  - 把表单提交拼成命令行，subprocess 后台跑，SSE 实时推 stdout
  - 探测 BitBrowser / Clash 在线状态 + 当前节点

启动：  python -m uvicorn webui.server:app --port 8799   (或用 start.bat)
"""
import asyncio
import base64
import contextlib
import hmac
import io
import importlib.util
import json
import os
import re
import signal
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

# 启动前由系统显式提供的变量始终优先于 WebUI 保存的 .env。
BOOT_ENV = dict(os.environ)

# 项目根 = webui 的上一级
ROOT = (
    getattr(sys, "_MEIPASS", "")
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
WEBUI = os.path.join(ROOT, "webui")
ENV_PATH = os.environ.get("REG_FACTORY_ENV_FILE") or os.path.join(ROOT, ".env")
ENV_EXAMPLE = os.path.join(ROOT, ".env.example")
K12_DIR = os.path.join(ROOT, "codex_k12")
K12_SERVER = os.path.join(K12_DIR, "server", "index.ts")
K12_TSX_CLI = os.path.join(K12_DIR, "node_modules", "tsx", "dist", "cli.mjs")
K12_DIST_INDEX = os.path.join(K12_DIR, "dist", "index.html")
K12_LOG_PATH = os.path.join(K12_DIR, "server.log")
PLUS_DIR = os.path.join(ROOT, "vendor", "chatgpt_plus")

sys.path.insert(0, WEBUI)
sys.path.insert(0, ROOT)
from webui import scripts as schema  # noqa: E402


# The updater inherits the current service environment.  Values originally
# loaded from .env can therefore look like explicit boot-time overrides after
# a restart and mask newer settings saved from the WebUI.  The network panel
# is the runtime control plane for these keys, so its saved values must remain
# authoritative for both this process and newly spawned registration tasks.
_PROXY_ENV_KEYS = (
    "PROXY_MODE",
    "REG_FACTORY_PROXY_MODE",
    "OUTLOOK_PROXY_MODE",
    "CLAUDE_PROXY_MODE",
    "CHATGPT_PROXY_MODE",
    "GROK_PROXY_MODE",
    "KIRO_PROXY_MODE",
    "GITHUB_PROXY_MODE",
    "CLASH_API",
    "CLASH_SECRET",
    "CLASH_PROXY",
    "CLASH_GROUP",
    "CLASH_FIXED_NODE",
    "REG_FACTORY_PLUS_LINK_ROUTE",
    "REG_FACTORY_PLUS_BIND_ROUTE",
    "REG_FACTORY_PLUS_LINK_PROXY_OVERRIDE",
    "REG_FACTORY_PLUS_BIND_PROXY_OVERRIDE",
    "REG_FACTORY_PROXY",
    "REG_FACTORY_PROXY_POOL",
    "REG_FACTORY_PROXY_ROTATE_URL",
    "REG_FACTORY_PROXY_ROTATE_METHOD",
    "REG_FACTORY_RESIDENTIAL_TRAFFIC_MODE",
    "REG_FACTORY_MAX_CONCURRENCY",
    "REG_FACTORY_ALLOW_SHARED_EGRESS",
    "CHATGPT_RESIDENTIAL_ROTATE_RETRIES",
)

_CUSTOM_BROWSER_ENV_KEYS = {
    "FINGERPRINT_BROWSER",
    "CUSTOM_BROWSER_API",
    "CUSTOM_BROWSER_API_MODE",
    "CUSTOM_BROWSER_API_KEY",
    "CUSTOM_BROWSER_API_AUTH_HEADER",
    "CUSTOM_BROWSER_API_AUTH_PREFIX",
    "CUSTOM_BROWSER_API_HEADERS",
    "CUSTOM_BROWSER_API_TIMEOUT",
    "CUSTOM_BROWSER_API_VERIFY_TLS",
    "CUSTOM_BROWSER_API_ID_FIELD",
    "CUSTOM_BROWSER_API_HEALTH_PATH",
    "CUSTOM_BROWSER_API_CREATE_PATH",
    "CUSTOM_BROWSER_API_LIST_PATH",
    "CUSTOM_BROWSER_API_OPEN_PATH",
    "CUSTOM_BROWSER_API_CLOSE_PATH",
    "CUSTOM_BROWSER_API_DELETE_PATH",
    "CUSTOM_BROWSER_API_UPDATE_PATH",
    "CUSTOM_BROWSER_API_CREATE_METHOD",
    "CUSTOM_BROWSER_API_LIST_METHOD",
    "CUSTOM_BROWSER_API_OPEN_METHOD",
    "CUSTOM_BROWSER_API_CLOSE_METHOD",
    "CUSTOM_BROWSER_API_DELETE_METHOD",
    "CUSTOM_BROWSER_API_UPDATE_METHOD",
    "CUSTOM_BROWSER_API_FORWARD_FIELDS",
}

# The same updater inheritance problem affects provider credentials after they
# are edited in the WebUI.  Keep this list narrow so unrelated explicit system
# environment overrides retain their existing precedence.
_LIVE_ENV_KEYS = frozenset(_PROXY_ENV_KEYS) | _CUSTOM_BROWSER_ENV_KEYS | {
    "ICLOUD_MAIL_API_BASE",
    "ICLOUD_MAIL_API_KEY",
    "ICLOUD_MAIL_TYPE",
    "ICLOUD_MAIL_SERVICE",
}


def _asset_api_denied(request: Request):
    configured = _read_config_val("REG_FACTORY_ASSET_API_KEY", "").strip()
    if configured:
        authorization = request.headers.get("authorization", "")
        bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        provided = request.headers.get("x-api-key", "") or bearer
        if hmac.compare_digest(provided, configured):
            return None
        return JSONResponse({"error": "资产 API key 无效"}, status_code=401)
    client_host = request.client.host if request.client else ""
    if client_host in {"127.0.0.1", "::1", "localhost"}:
        return None
    return JSONResponse(
        {"error": "未配置 REG_FACTORY_ASSET_API_KEY 时，资产 API 仅允许本机访问"},
        status_code=403,
    )


def _asset_result(callback):
    from common.asset_store import AssetError

    try:
        return callback()
    except AssetError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)[:240]}, status_code=400)


def _git_version():
    """Return the commit loaded by this WebUI process."""
    version_file = os.path.join(ROOT, "VERSION")
    if getattr(sys, "frozen", False):
        try:
            with open(version_file, encoding="utf-8") as handle:
                version = handle.read().strip()
            if version:
                return version
        except OSError:
            pass
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with open(version_file, encoding="utf-8") as handle:
            version = handle.read().strip()
        if version:
            return version
    except OSError:
        pass
    return "archive"


WEBUI_VERSION = _git_version()


def _ensure_proxy_env():
    """接码等公网服务直连不通(sms-man 直连超时)，必须经 Clash。把 CLASH_PROXY 注进本进程
    环境，让 common.sms 的 requests(trust_env) 自动走代理；localhost API 直连(NO_PROXY)。"""
    try:
        from common import proxy_switch
        proxy = proxy_switch.effective_proxy_url()
    except Exception:
        proxy = ""
    if proxy and not os.environ.get("HTTPS_PROXY"):
        os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = proxy
        os.environ["http_proxy"] = os.environ["https_proxy"] = proxy
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost,::1"


app = FastAPI(title="reg-factory WebUI")

# 运行中的任务：run_id -> {proc, lines:[], done:bool, script, cmd, started}
RUNS = {}
_run_seq = [0]

# 接码助手：内存记录当前租用的 sms-man 号  pkey -> {phone, rented_at, codes:[], service}
SMS_RENTS = {}
SMS_RENT_TTL = 1200  # 20 分钟租期(秒)

# 只管理由本 WebUI 拉起的 K12 子进程；外部已启动的服务不会在退出时被误杀。
K12_PROCESS = None
K12_LOG_HANDLE = None
K12_START_TASK = None
K12_LOCK = asyncio.Lock()

# Plus 工作台使用内置 zkky 服务；网络出口优先住宅 IP，缺失时回退 Clash。
PLUS_PORT = 5601
PLUS_BATCH_SIZE = 27
PLUS_HTTP_SERVER = None
PLUS_SERVER_THREAD = None
PLUS_SERVER_MODULE = None
PLUS_SERVER_LOCK = threading.Lock()

# 资产号池扫描在后台线程执行，WebUI 只保存无敏感字段的进度。
ASSET_SCAN_TASK = None
ASSET_SCAN_STATE = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "error": "",
    "progress": {"completed": 0, "total": 0, "current": ""},
    "quarantine": {"moved_accounts": 0, "moved_files": 0},
}
ASSET_SCAN_LOCK = threading.Lock()

# 更新由独立进程执行；当前 WebUI 会在 updater 停止自身前返回 202。
UPDATE_PROCESS = None
UPDATE_LOG_HANDLE = None
UPDATE_RESULT_PATH = os.path.join(
    os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or ROOT,
    "runtime",
    "update-result.json",
)
UPDATE_STATE = {
    "status": "idle",
    "message": "",
    "started_at": "",
}
try:
    with open(UPDATE_RESULT_PATH, encoding="utf-8-sig") as handle:
        _previous_update_result = json.load(handle)
    _previous_update_status = str(_previous_update_result.get("status") or "").lower()
    if _previous_update_status in {"completed", "up_to_date", "failed"}:
        UPDATE_STATE.update({
            "status": "completed" if _previous_update_status != "failed" else "failed",
            "message": str(_previous_update_result.get("message") or "")[:240],
            "started_at": str(_previous_update_result.get("updated_at") or ""),
        })
except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
    pass


# ============================================================ 配置/状态读取
def _read_config_val(key, default="", allow_empty=False):
    """从环境/.env 读一个值(用于探测 Clash/BitBrowser 地址)。"""
    val = os.environ.get(key)
    if val or (allow_empty and key in os.environ):
        return val
    try:
        with open(ENV_PATH, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    if k.strip() == key:
                        value = v.strip().strip('"').strip("'")
                        return value if allow_empty else (value or default)
    except Exception:
        pass
    return default


def _http_alive(url, timeout=3, headers=None, verify_tls=True):
    try:
        req = urllib.request.Request(url, headers=headers or {})
        handlers = [urllib.request.ProxyHandler({})]
        if not verify_tls:
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as exc:
        if headers and exc.code in {401, 403}:
            return False
        return True  # 其他 4xx = 服务活着(拒绝裸请求)
    except Exception:
        return False


def _k12_url():
    raw = _read_config_val("K12_CONSOLE_URL", "http://127.0.0.1:8806").strip().rstrip("/")
    try:
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid K12_CONSOLE_URL")
    except Exception:
        return "http://127.0.0.1:8806/"
    return raw + "/"


def _k12_is_local(url):
    try:
        return urllib.parse.urlparse(url).hostname in {"127.0.0.1", "localhost", "::1"}
    except Exception:
        return False


def _k12_alive():
    return _http_alive(urllib.parse.urljoin(_k12_url(), "api/health"), timeout=1.5)


def _k12_status(message=""):
    alive = _k12_alive()
    node = shutil.which("node")
    missing = []
    if not os.path.isfile(os.path.join(K12_DIR, "package.json")):
        missing.append("codex_k12 子项目")
    if not node:
        missing.append("Node.js 20+")
    if not os.path.isfile(K12_TSX_CLI):
        missing.append("Node 依赖")
    if not os.path.isfile(K12_DIST_INDEX):
        missing.append("生产构建")
    ready = not missing and _k12_is_local(_k12_url())
    managed = bool(K12_PROCESS and K12_PROCESS.returncode is None)
    if alive:
        detail = "服务在线"
    elif message:
        detail = message
    elif missing:
        detail = "缺少 " + "、".join(missing) + "，请重新运行 install.bat / install.sh"
    elif not _k12_is_local(_k12_url()):
        detail = "远程 K12 地址当前不可达，主面板不会自动启动远程服务"
    else:
        detail = "服务已安装但尚未启动"
    return {"alive": alive, "ready": ready, "managed": managed, "url": _k12_url(), "message": detail}


def _plus_health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{PLUS_PORT}/health", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if (
            payload.get("service") == "reg-factory-chatgpt-plus"
            and int(payload.get("max_concurrency") or 0) == PLUS_BATCH_SIZE
        ):
            return payload
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return {}


def _plus_status(message=""):
    sub2api_ready = all(
        _read_config_val(key, "").strip()
        for key in ("SUB2API_URL", "SUB2API_EMAIL", "SUB2API_PASSWORD")
    )
    providers = []
    if _read_config_val("SMSMAN_TOKEN", "").strip():
        providers.append("smsman")
    if (
        _read_config_val("SMS_TOKEN", "").strip()
        and _read_config_val("SMS_PROJECT_ID_OPENAI", "").strip()
    ):
        providers.append("firefox")
    if _read_config_val("HERO_SMS_API_KEY", "").strip():
        providers.append("hero")
    active = sum(
        1 for rec in RUNS.values()
        if rec.get("script") == "plus_codex_import" and not rec.get("done")
    )
    ready = sub2api_ready and bool(providers)
    detail = message or (
        f"正在导入 {active} 个批次" if active else
        "Plus Codex 批量导入已就绪" if ready else
        "请先配置 SUB2API 和至少一个手机号接码平台"
    )
    return {
        "alive": ready,
        "ready": ready,
        "managed": bool(active),
        "active": active,
        "providers": providers,
        "sub2api_ready": sub2api_ready,
        "message": detail,
    }


def _update_script(result_path=""):
    if getattr(sys, "frozen", False):
        if os.name != "nt":
            return None
        path = os.path.join(ROOT, "update-portable.ps1")
        if not os.path.isfile(path):
            return None
        command = [
            shutil.which("powershell.exe") or "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            path,
            "-InstallDir",
            os.path.dirname(os.path.abspath(sys.executable)),
            "-ProcessId",
            str(os.getpid()),
        ]
        if result_path:
            command.extend(["-ResultPath", result_path])
        for option, default in (("--host", "127.0.0.1"), ("--port", "8799")):
            value = default
            try:
                index = sys.argv.index(option)
                value = sys.argv[index + 1]
            except (ValueError, IndexError):
                pass
            command.extend([
                "-ListenHost" if option == "--host" else "-ListenPort",
                str(value),
            ])
        return command
    if os.name == "nt":
        path = os.path.join(ROOT, "update.ps1")
        if not os.path.isfile(path):
            return None
        return [
            shutil.which("powershell.exe") or "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            path,
            "-Root",
            ROOT,
        ]
    path = os.path.join(ROOT, "update.sh")
    if not os.path.isfile(path):
        return None
    return ["bash", path, "--root", ROOT]


def _update_child_env():
    """Keep registration proxies out of GitHub update subprocesses."""
    child_env = os.environ.copy()
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        child_env.pop(name, None)
    bypass = [
        value.strip()
        for value in str(child_env.get("NO_PROXY") or child_env.get("no_proxy") or "").split(",")
        if value.strip()
    ]
    for host in ("127.0.0.1", "localhost", "::1", "github.com", "api.github.com", "uploads.github.com"):
        if host not in bypass:
            bypass.append(host)
    child_env["NO_PROXY"] = child_env["no_proxy"] = ",".join(bypass)
    child_env["REG_FACTORY_NONINTERACTIVE"] = "1"
    return child_env


def _read_update_result():
    if not UPDATE_RESULT_PATH or not os.path.isfile(UPDATE_RESULT_PATH):
        return {}
    try:
        with open(UPDATE_RESULT_PATH, encoding="utf-8-sig") as handle:
            result = json.load(handle)
        return result if isinstance(result, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _update_status():
    global UPDATE_PROCESS, UPDATE_LOG_HANDLE
    process = UPDATE_PROCESS
    if process is not None:
        returncode = process.poll()
        if returncode is None:
            result = _read_update_result()
            result_status = str(result.get("status") or "").strip().lower()
            target = str(result.get("target_version") or "").strip()
            stage_messages = {
                "checking": "正在检查最新版本",
                "downloading": f"正在下载 v{target}" if target else "正在下载最新版本",
                "installing": f"正在安装 v{target}" if target else "正在安装最新版本",
            }
            UPDATE_STATE["status"] = "running"
            UPDATE_STATE["message"] = str(
                stage_messages.get(result_status)
                or result.get("message")
                or "正在下载并安装最新版本"
            )[:240]
        elif UPDATE_STATE["status"] == "running":
            result = _read_update_result()
            result_status = str(result.get("status") or "").strip().lower()
            if returncode == 0 and result_status in {"completed", "up_to_date"}:
                UPDATE_STATE["status"] = "completed"
                current = str(result.get("current_version") or "").strip()
                target = str(result.get("target_version") or "").strip()
                if result_status == "up_to_date":
                    version = current or target
                    UPDATE_STATE["message"] = f"已是最新版本 v{version}" if version else "已是最新版本"
                else:
                    UPDATE_STATE["message"] = (
                        f"更新完成：v{current} -> v{target}"
                        if current and target
                        else "更新程序已完成"
                    )
            else:
                UPDATE_STATE["status"] = "failed"
                UPDATE_STATE["message"] = str(
                    result.get("message")
                    or f"更新失败（退出码 {returncode}），请查看 runtime/update.log"
                )[:240]
            if UPDATE_LOG_HANDLE:
                UPDATE_LOG_HANDLE.close()
                UPDATE_LOG_HANDLE = None
    result = dict(UPDATE_STATE)
    result["available"] = bool(_update_script())
    return result


async def _start_k12_service():
    global K12_PROCESS, K12_LOG_HANDLE
    async with K12_LOCK:
        status = _k12_status()
        if status["alive"] or not status["ready"]:
            return status
        if K12_PROCESS and K12_PROCESS.returncode is None:
            return _k12_status("服务进程正在启动")

        if K12_LOG_HANDLE:
            K12_LOG_HANDLE.close()
            K12_LOG_HANDLE = None

        parsed = urllib.parse.urlparse(status["url"])
        child_env = _child_env()
        child_env["HOST"] = "127.0.0.1"
        child_env["PORT"] = str(parsed.port or (443 if parsed.scheme == "https" else 80))
        node = shutil.which("node")
        os.makedirs(K12_DIR, exist_ok=True)
        K12_LOG_HANDLE = open(K12_LOG_PATH, "a", encoding="utf-8")
        try:
            K12_PROCESS = await asyncio.create_subprocess_exec(
                node, K12_TSX_CLI, K12_SERVER,
                cwd=K12_DIR,
                env=child_env,
                stdout=K12_LOG_HANDLE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:
            K12_LOG_HANDLE.close()
            K12_LOG_HANDLE = None
            K12_PROCESS = None
            return _k12_status(f"启动失败：{str(exc)[:120]}")

        for _ in range(48):
            await asyncio.sleep(0.25)
            if _k12_alive():
                return _k12_status()
            if K12_PROCESS.returncode is not None:
                break
        code = K12_PROCESS.returncode
        if code is not None and K12_LOG_HANDLE:
            K12_LOG_HANDLE.close()
            K12_LOG_HANDLE = None
        return _k12_status(f"服务未能就绪" + (f"（退出码 {code}）" if code is not None else "，请查看 codex_k12/server.log"))


async def _stop_k12_service():
    global K12_PROCESS, K12_LOG_HANDLE
    proc = K12_PROCESS
    K12_PROCESS = None
    if proc and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
    if K12_LOG_HANDLE:
        K12_LOG_HANDLE.close()
        K12_LOG_HANDLE = None


def _plus_proxy_url(env):
    """Prefer a configured residential endpoint, then fall back to Clash."""
    residential = _plus_residential_proxy_url(env)
    if residential:
        return residential
    return str(env.get("CLASH_PROXY") or "http://127.0.0.1:7897").strip()


def _plus_residential_proxy_url(env):
    try:
        from common import direct_proxy

        residential = direct_proxy.configured_proxy(environ=env)
        if residential:
            return residential.url
    except (TypeError, ValueError):
        pass
    return ""


def _plus_route_proxy_url(env, route, fallback=""):
    """Resolve a Plus stage route without exposing credentials to the UI."""
    residential = _plus_residential_proxy_url(env)
    clash = str(env.get("CLASH_PROXY") or "http://127.0.0.1:7897").strip()
    value = str(route or "").strip().lower()
    if value == "residential":
        return residential or clash or fallback
    if value == "clash" or value.startswith("clash:"):
        return clash or residential or fallback
    return fallback or residential or clash


def _plus_route_node(route):
    value = str(route or "").strip()
    return value[6:].strip() if value.lower().startswith("clash:") else ""


def _plus_bind_proxy_url(env, link_proxy=""):
    """Use a separate card-network egress when Clash is available.

    Checkout extraction talks to ChatGPT and prefers the configured residential
    endpoint. Stripe card binding/payment is routed through Clash by default so
    the two stages do not reuse the same public IP. Explicit overrides are kept
    in the environment for installations with a dedicated card endpoint.
    """
    override = str(env.get("REG_FACTORY_PLUS_BIND_PROXY_OVERRIDE") or "").strip()
    if override:
        return override
    clash = str(env.get("CLASH_PROXY") or "http://127.0.0.1:7897").strip()
    link = str(link_proxy or _plus_proxy_url(env)).strip()
    if clash and clash != link:
        return clash
    return link or clash


def _plus_runtime_environment():
    env = _child_env("chatgpt")
    data_root = os.path.abspath(os.environ.get("REG_FACTORY_DATA_DIR") or ROOT)
    runtime_dir = os.path.join(data_root, "runtime", "chatgpt_plus")
    os.makedirs(runtime_dir, exist_ok=True)
    clash = str(env.get("CLASH_PROXY") or "http://127.0.0.1:7897").strip()
    # Plus checkout and payment stay on the selected ChatGPT egress by
    # default. A residential route remains available only when explicitly
    # requested through REG_FACTORY_PLUS_*_ROUTE or *_PROXY_OVERRIDE.
    link_route = str(env.get("REG_FACTORY_PLUS_LINK_ROUTE") or "clash").strip()
    bind_route = str(env.get("REG_FACTORY_PLUS_BIND_ROUTE") or "clash").strip()
    link_proxy = str(
        env.get("REG_FACTORY_PLUS_LINK_PROXY_OVERRIDE")
        or _plus_route_proxy_url(env, link_route, _plus_proxy_url(env))
    ).strip()
    bind_proxy = str(
        env.get("REG_FACTORY_PLUS_BIND_PROXY_OVERRIDE")
        or _plus_route_proxy_url(env, bind_route, _plus_bind_proxy_url(env, link_proxy))
    ).strip()
    link_node = "" if env.get("REG_FACTORY_PLUS_LINK_PROXY_OVERRIDE") else _plus_route_node(link_route)
    bind_node = "" if env.get("REG_FACTORY_PLUS_BIND_PROXY_OVERRIDE") else _plus_route_node(bind_route)
    values = {
        "REG_FACTORY_DATA_DIR": data_root,
        "REG_FACTORY_PLUS_QUEUE_FILE": os.path.join(runtime_dir, "registration_queue.json"),
        "REG_FACTORY_PLUS_FINGERPRINT_STORE": os.path.join(runtime_dir, "fingerprint_profiles.json"),
        "REG_FACTORY_PLUS_CONFIG": os.path.join(PLUS_DIR, "standalone_config.json"),
        # Keep the legacy value for callers that only know one Plus proxy;
        # stage-specific values are consumed by the vendored workbench.
        "REG_FACTORY_PLUS_PROXY": link_proxy,
        "REG_FACTORY_PLUS_LINK_PROXY": link_proxy,
        "REG_FACTORY_PLUS_BIND_PROXY": bind_proxy,
        "REG_FACTORY_PLUS_LINK_CLASH_NODE": link_node,
        "REG_FACTORY_PLUS_BIND_CLASH_NODE": bind_node,
    }
    for key, value in values.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return values


def _load_plus_server_module():
    global PLUS_SERVER_MODULE
    if PLUS_SERVER_MODULE is not None:
        return PLUS_SERVER_MODULE
    _plus_runtime_environment()
    if PLUS_DIR not in sys.path:
        sys.path.insert(0, PLUS_DIR)
    module_path = os.path.join(PLUS_DIR, "server.py")
    spec = importlib.util.spec_from_file_location("reg_factory_chatgpt_plus_server", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载内置 zkky 服务")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    PLUS_SERVER_MODULE = module
    return module


def _start_plus_service_sync():
    global PLUS_HTTP_SERVER, PLUS_SERVER_THREAD, PLUS_PORT
    _plus_runtime_environment()
    with PLUS_SERVER_LOCK:
        if _plus_health():
            return _plus_status()
        if not _plus_status()["ready"]:
            return _plus_status("内置 zkky 文件不完整")
        try:
            from http.server import ThreadingHTTPServer

            module = _load_plus_server_module()
            try:
                server = ThreadingHTTPServer(("127.0.0.1", PLUS_PORT), module.Handler)
            except OSError:
                # A stale manually started local workbench must not prevent the
                # main WebUI from exposing the current embedded build.
                candidate = None
                for port in range(PLUS_PORT + 1, PLUS_PORT + 20):
                    with socket.socket() as probe:
                        try:
                            probe.bind(("127.0.0.1", port))
                        except OSError:
                            continue
                    candidate = port
                    break
                if candidate is None:
                    raise
                PLUS_PORT = candidate
                server = ThreadingHTTPServer(("127.0.0.1", PLUS_PORT), module.Handler)
            server.daemon_threads = True
            thread = threading.Thread(
                target=server.serve_forever,
                name="reg-factory-chatgpt-plus",
                daemon=True,
            )
            PLUS_HTTP_SERVER = server
            PLUS_SERVER_THREAD = thread
            thread.start()
        except OSError as exc:
            PLUS_HTTP_SERVER = None
            PLUS_SERVER_THREAD = None
            return _plus_status(f"本地端口 {PLUS_PORT} 启动失败: {str(exc)[:100]}")
        except Exception as exc:
            PLUS_HTTP_SERVER = None
            PLUS_SERVER_THREAD = None
            return _plus_status(f"内置 zkky 启动失败: {str(exc)[:120]}")
    for _ in range(30):
        if _plus_health():
            return _plus_status()
        time.sleep(0.05)
    return _plus_status("本地工作台未能就绪")


def _stop_plus_service_sync():
    global PLUS_HTTP_SERVER, PLUS_SERVER_THREAD
    with PLUS_SERVER_LOCK:
        server = PLUS_HTTP_SERVER
        thread = PLUS_SERVER_THREAD
        PLUS_HTTP_SERVER = None
        PLUS_SERVER_THREAD = None
    if server is not None:
        with contextlib.suppress(Exception):
            server.shutdown()
        with contextlib.suppress(Exception):
            server.server_close()
    if thread and thread.is_alive():
        thread.join(timeout=3)




# ============================================================ .env 读写(保留注释/顺序)
def _parse_env_file(path):
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_env_file(path, updates):
    """把 updates(dict) 写回 .env：已存在的行原地改值(保留注释/顺序)，新 key 追加到末尾。"""
    lines = []
    seen = set()
    if os.path.isfile(path):
        lines = open(path, encoding="utf-8").read().splitlines()
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.partition("=")[0].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        out.append(line)
    # 新增的 key
    extra = [k for k in updates if k not in seen]
    if extra:
        out.append("")
        out.append("# ---- 由 WebUI 配置页新增 ----")
        for k in extra:
            out.append(f"{k}={updates[k]}")
    # 原子写
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)


def _apply_saved_env(updates):
    """让当前 WebUI 与后续子进程看到新配置，同时保留启动前系统变量的优先级。"""
    for key, value in updates.items():
        if key in BOOT_ENV and key not in _LIVE_ENV_KEYS:
            continue
        if value == "":
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    import importlib
    for name in ("config", "common.direct_proxy", "common.proxy_switch", "common.sms", "common.temp_email"):
        module = sys.modules.get(name)
        if module is not None:
            importlib.reload(module)

    if "HTTPS_PROXY" not in BOOT_ENV:
        try:
            from common import proxy_switch
            proxy = proxy_switch.effective_proxy_url()
        except Exception:
            proxy = ""
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            if proxy:
                os.environ[key] = proxy
            else:
                os.environ.pop(key, None)
        if proxy:
            os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost,::1"


# ============================================================ 连通测试
def _direct_get(url, headers=None, timeout=8, verify_tls=True):
    """直连 GET(显式绕过代理——Clash 控制器/BitBrowser 都是 localhost)。
    返回 (status_code, body_text)。连不上抛异常。"""
    handlers = [urllib.request.ProxyHandler({})]  # 空 = 不走任何代理
    if not verify_tls:
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers=headers or {})
    with opener.open(req, timeout=timeout) as r:
        return r.status, r.read(8192).decode("utf-8", "replace")


def _test_clash():
    """测 Clash 控制器：GET /version 带 Bearer secret。区分 连不上 / 密码错 / OK。"""
    api = _read_config_val("CLASH_API", "http://127.0.0.1:9097").rstrip("/")
    secret = _read_config_val("CLASH_SECRET", "")
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    try:
        code, body = _direct_get(api + "/version", headers=headers, timeout=6)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "密码(secret)错误或未设置 —— 检查 CLASH_SECRET"
        return False, f"控制器返回 HTTP {e.code}"
    except Exception as e:
        return False, f"连不上控制器({api})：{str(e)[:60]}。确认 Clash Verge 已开 External Controller"
    ver = ""
    try:
        import json as _j
        ver = _j.loads(body).get("version", "")
    except Exception:
        pass
    # 顺带报当前节点
    node = ""
    try:
        from common import proxy_switch as ps
        node = ps.current_node() or ""
    except Exception:
        pass
    return True, f"控制器连通 ✓ 内核版本 {ver}" + (f"，当前节点 {node}" if node else "")


def _fingerprint_provider():
    return (
        _read_config_val("FINGERPRINT_BROWSER", "bitbrowser")
        or os.environ.get("BROWSER_PROVIDER")
        or "bitbrowser"
    ).strip().lower()


def _test_bitbrowser():
    """Test selected fingerprint browser local API."""
    provider = _fingerprint_provider()
    headers = {}
    verify_tls = True
    if provider in {"bundled", "embedded", "local", "custom", "chrome", "chromium"}:
        from common.bundled_browser import find_browser_path

        browser = find_browser_path()
        if browser:
            return True, f"Chrome/Chromium ready: {os.path.basename(browser)}"
        return False, "未找到 Chrome/Chromium，请配置 CUSTOM_BROWSER_PATH"
    if provider in {"adspower", "ads_power", "ads"}:
        api = _read_config_val("ADSPOWER_API", "http://127.0.0.1:50325").rstrip("/")
        name = "AdsPower"
        paths = ("/status", "/")
    elif provider in {"custom_api", "api"}:
        api = _read_config_val("CUSTOM_BROWSER_API", "").rstrip("/")
        if not api:
            return False, "CUSTOM_BROWSER_API 未配置"
        name = "Custom Browser"
        health = _read_config_val("CUSTOM_BROWSER_API_HEALTH_PATH", "/health").strip() or "/health"
        paths = (health, "/status", "/")
        key = (_read_config_val("CUSTOM_BROWSER_API_KEY", "") or _read_config_val("CUSTOM_BROWSER_API_TOKEN", "")).strip()
        header = _read_config_val("CUSTOM_BROWSER_API_AUTH_HEADER", "Authorization").strip() or "Authorization"
        prefix = _read_config_val("CUSTOM_BROWSER_API_AUTH_PREFIX", "Bearer ", allow_empty=True)
        if prefix.strip().lower() in {"bearer", "token", "basic", "apikey", "api-key"}:
            prefix = prefix.strip() + " "
        headers = {header: f"{prefix}{key}"} if key else {}
        extra_headers = _read_config_val("CUSTOM_BROWSER_API_HEADERS", "").strip()
        if extra_headers:
            try:
                value = json.loads(extra_headers)
                if not isinstance(value, dict):
                    raise ValueError("must be a JSON object")
                headers.update({str(item_key): str(item_value) for item_key, item_value in value.items()})
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                return False, f"CUSTOM_BROWSER_API_HEADERS JSON 无效: {exc}"
        verify_tls = _read_config_val("CUSTOM_BROWSER_API_VERIFY_TLS", "true").strip().lower() in {
            "1", "true", "yes", "on"
        }
    else:
        api = _read_config_val("BITBROWSER_API", "http://127.0.0.1:54345").rstrip("/")
        name = "BitBrowser"
        paths = ("/health", "/")
        headers = {}
    for path in paths:
        try:
            target = path if path.startswith(("http://", "https://")) else api + (
                path if path.startswith("/") else "/" + path
            )
            code, _ = _direct_get(
                target,
                headers=headers,
                timeout=5,
                verify_tls=verify_tls,
            )
            return True, f"{name} API 连通 ✓ (HTTP {code})"
        except urllib.error.HTTPError as exc:
            if provider in {"custom_api", "api"} and exc.code in {401, 403}:
                return False, f"{name} API 鉴权失败 (HTTP {exc.code})，请检查 Key、请求头和前缀"
            if provider in {"custom_api", "api"} and exc.code in {404, 405}:
                last = f"HTTP {exc.code}"
                continue
            return True, f"{name} API 在线 ✓ (服务响应)"
        except Exception as e:
            last = str(e)[:60]
    return False, f"连不上 {name}({api})：{last}。确认客户端已启动"


def _proxied_get(url, timeout=20):
    """经 Clash 代理 GET(sms-man/firefox 等公网接码服务直连不通，必须走代理)。
    返回 (status, body_text)。"""
    try:
        from common import proxy_switch
        proxy = proxy_switch.effective_proxy_url()
    except Exception:
        proxy = ""
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(handler)
    with opener.open(url, timeout=timeout) as r:
        return r.status, r.read(4096).decode("utf-8", "replace")


def _test_smsman():
    """测 sms-man 接码：经代理查询(直连超时)。get-balance 偶发 500，回退查 applications 验 token。"""
    token = _read_config_val("SMSMAN_TOKEN", "")
    if not token:
        return False, "未配置 SMSMAN_TOKEN"
    base = _read_config_val("SMSMAN_API_BASE", "https://api.sms-man.com/control").rstrip("/")
    import json as _j
    last = ""
    # get-balance 偶发 500/HTML 故障页，回退 applications；都试，识别"服务端故障"
    for path, pretty in (("get-balance", "balance"), ("applications", "applications")):
        try:
            code, body = _proxied_get(base + f"/{path}?" + urllib.parse.urlencode({"token": token}), timeout=18)
            b = body.lstrip()
            if b.startswith("<") or "<html" in b[:200].lower():
                last = f"sms-man 返回错误页(HTTP {code})——平台接口暂时故障/限流，非 token 问题，稍后再试"
                continue
            d = _j.loads(body)
            if path == "get-balance" and isinstance(d, dict) and ("balance" in d or "money" in d):
                return True, f"sms-man 连通 ✓ 余额 {d.get('balance') or d.get('money')}"
            if path == "applications":
                n = len(d) if isinstance(d, (dict, list)) else 0
                if n and not (isinstance(d, dict) and d.get("error_code")):
                    return True, f"sms-man 连通 ✓ (token 有效，服务数 {n})"
            if isinstance(d, dict) and (d.get("error_code") or d.get("error_msg")):
                return False, f"sms-man token 无效：{d.get('error_msg') or d.get('error_code')}"
        except Exception as e:
            last = f"sms-man 请求失败(经代理)：{str(e)[:70]}。确认 Clash 在线"
    return False, last or "sms-man 无有效响应(平台可能故障,稍后再试)"


def _test_firefox():
    """测 firefox.fun 接码：用官方 myInfo 动作验证持久 token。"""
    api_name = _read_config_val("SMS_API_NAME", "").strip()
    token = _read_config_val("SMS_TOKEN", "")
    if not api_name:
        return False, "未配置 SMS_API_NAME"
    if not token:
        return False, "未配置 SMS_TOKEN"
    base = _read_config_val("SMS_API_BASE", "http://www.firefox.fun/yhapi.ashx")
    try:
        code, body = _proxied_get(base + "?" + urllib.parse.urlencode({"act": "myInfo", "token": token}), timeout=15)
        body = body.strip()
        # firefox 返回 1|... 表示成功，0|... 表示错误
        if body.startswith("1"):
            return True, "firefox.fun 连通，APIName 与 token 已配置，token 有效"
        return False, f"firefox.fun 返回：{body[:80]}（token 可能无效）"
    except Exception as e:
        return False, f"firefox.fun 请求失败：{str(e)[:80]}"


def _test_yyds():
    """Create one YYDS inbox using the values currently shown in the config form."""
    key = _read_config_val("YYDS_API_KEY", "").strip()
    base = _read_config_val("YYDS_BASE_URL", "https://maliapi.215.im").strip()
    if not key:
        return False, "未配置 YYDS_API_KEY"
    try:
        from common.temp_email import create_mailbox
        mb = create_mailbox(provider="yyds", api_key=key, base_url=base)
        return True, f"YYDS 连通，建号成功 ✓ {mb['email']}"
    except Exception as e:
        detail = str(e)[:180]
        if "404" in detail:
            detail += "；Base URL 应为 https://maliapi.215.im（不要附加 /v1/accounts）"
        return False, detail


def _test_outlook_recovery_mailbox():
    record = _read_config_val("OUTLOOK_GRAPH_RECOVERY_OUTLOOK_MAILBOX", "").strip()
    if not record:
        return False, "请先填写 OUTLOOK_GRAPH_RECOVERY_OUTLOOK_MAILBOX"
    try:
        from common.mailbox import check_mailbox_access, parse_outlook_recovery_mailbox

        mailbox = parse_outlook_recovery_mailbox(record)
        validation = check_mailbox_access(
            mailbox["email"], mailbox["refresh_token"], mailbox["client_id"]
        )
        if not validation.get("ok"):
            return False, "Graph API 验证失败: " + str(
                validation.get("reason") or "unknown_error"
            )
        return True, f"Graph API 验证成功：{mailbox['email']}"
    except Exception as exc:
        return False, str(exc)[:180]


def _test_k12():
    status = _k12_status()
    return status["alive"], status["message"] + f"（{status['url']}）"


_TESTERS = {
    "k12": _test_k12,
    "clash": _test_clash,
    "bitbrowser": _test_bitbrowser,
    "smsman": _test_smsman,
    "firefox": _test_firefox,
    "yyds": _test_yyds,
    "outlook-recovery": _test_outlook_recovery_mailbox,
}


@app.post("/api/test/{target}")
async def api_test(target: str, request: Request):
    # 先把页面上当前(可能未保存的)配置临时写进环境，让测试用最新值
    try:
        data = await request.json()
    except Exception:
        data = {}
    overrides = (data or {}).get("env") or {}
    saved = {}
    allowed = set(schema.env_keys()) | {"SMSMAN_API_BASE", "SMS_API_BASE"}
    for k, v in overrides.items():
        if k in allowed and v is not None and (v != "" or k in _CUSTOM_BROWSER_ENV_KEYS):
            saved[k] = os.environ.get(k)
            os.environ[k] = str(v)
    try:
        fn = _TESTERS.get(target)
        if not fn:
            return JSONResponse({"ok": False, "msg": f"未知测试目标: {target}"}, status_code=400)
        ok, msg = await asyncio.to_thread(fn)
        return {"ok": ok, "msg": msg}
    finally:
        # 还原临时覆盖(不污染进程环境；真正保存走 /api/env)
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


# ============================================================ API
@app.get("/api/assets/summary")
def api_asset_summary(request: Request):
    denied = _asset_api_denied(request)
    if denied:
        return denied
    from common import asset_store

    return _asset_result(asset_store.summary)


@app.get("/api/assets/emails")
def api_asset_email(
    request: Request,
    index: int | None = None,
    format: str = "json",
    email_provider: str = "",
    pristine_only: bool = False,
    normal_only: bool = False,
    no_graph_only: bool = False,
):
    denied = _asset_api_denied(request)
    if denied:
        return denied
    from common import asset_store

    return _asset_result(
        lambda: asset_store.get_email(
            index=index,
            output_format=format,
            claim_once=True,
            email_provider=email_provider,
            pristine_only=pristine_only,
            no_graph_only=no_graph_only,
            verified_only=normal_only and not no_graph_only,
        )
    )


@app.get("/api/assets/cookies/{platform}")
def api_asset_cookie(
    platform: str,
    request: Request,
    format: str = "raw",
    index: int | None = None,
    codex_phone_status: str = "",
    email_provider: str = "",
):
    denied = _asset_api_denied(request)
    if denied:
        return denied
    from common import asset_store

    return _asset_result(
        lambda: asset_store.get_platform_asset(
            platform,
            output_format=format,
            index=index,
            claim_once=True,
            codex_phone_status=codex_phone_status,
            email_provider=email_provider,
        )
    )


@app.post("/api/assets/cursors/reset")
async def api_asset_cursor_reset(request: Request):
    denied = _asset_api_denied(request)
    if denied:
        return denied
    try:
        data = await request.json()
    except Exception:
        data = {}
    from common import asset_store

    scope = data.get("scope", "all") if isinstance(data, dict) else "all"
    return _asset_result(lambda: asset_store.reset_cursor(scope))


def _asset_export_zip(results: list[dict], resource: str, output_format: str) -> bytes:
    buffer = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if resource == "emails" or output_format == "email_four":
            if output_format == "json":
                archive.writestr(
                    "emails.json",
                    json.dumps([item.get("data") for item in results], ensure_ascii=False, indent=2),
                )
            else:
                lines = [str(item.get("data") or "") for item in results]
                name = "chatgpt-registration-emails.txt" if resource == "chatgpt" else "emails.txt"
                archive.writestr(name, "\n".join(lines) + "\n")
            manifest.extend({
                "platform": resource if resource != "emails" else "outlook",
                "email": str(item.get("email") or ""),
                "source": str(item.get("source") or ""),
                "format": output_format,
            } for item in results)
        else:
            for index, item in enumerate(results, start=1):
                data = item.get("data")
                extension = "txt" if isinstance(data, str) else "json"
                email = str(item.get("email") or item.get("source") or f"account-{index}")
                safe_name = re.sub(r"[^a-zA-Z0-9@._-]+", "-", email).strip("-.") or f"account-{index}"
                preferred = str(item.get("file_name") or "").strip()
                if preferred:
                    safe_name = re.sub(r"[^a-zA-Z0-9@._-]+", "-", os.path.basename(preferred)).strip("-.")
                    if safe_name.lower().endswith(f".{extension}"):
                        safe_name = safe_name[: -(len(extension) + 1)]
                payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
                archive.writestr(f"{resource}/{index:04d}-{safe_name}.{extension}", payload)
                manifest.append({
                    "platform": resource,
                    "email": str(item.get("email") or ""),
                    "source": str(item.get("source") or ""),
                    "format": output_format,
                })
        archive.writestr(
            "manifest.json",
            json.dumps({"resource": resource, "format": output_format, "count": len(results), "items": manifest}, ensure_ascii=False, indent=2),
        )
    return buffer.getvalue()


@app.post("/api/assets/export")
async def api_asset_export(request: Request):
    denied = _asset_api_denied(request)
    if denied:
        return denied
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return JSONResponse({"error": "request body must be an object"}, status_code=400)
    resource = str(data.get("resource") or "emails").strip().lower()
    output_format = str(data.get("format") or ("four" if resource == "emails" else "raw")).strip().lower()
    consume = data.get("consume", True)
    normal_only = data.get("normal_only", True)
    include_claimed = data.get("include_claimed", consume)
    if not all(isinstance(value, bool) for value in (consume, normal_only, include_claimed)):
        return JSONResponse({"error": "consume, normal_only and include_claimed must be boolean"}, status_code=400)
    from common import asset_store

    try:
        def build_export():
            results = asset_store.export_batch(
                resource,
                output_format=output_format,
                limit=data.get("limit", 100),
                verified_only=normal_only,
                email_provider=str(data.get("email_provider") or ""),
                codex_phone_status=str(data.get("codex_phone_status") or ""),
                include_claimed=include_claimed,
            )
            payload = _asset_export_zip(results, resource, output_format)
            lifecycle = (
                asset_store.archive_asset_results(results, bucket="exported", reason="manual_batch_export")
                if consume else {"moved_accounts": 0, "moved_files": 0}
            )
            return results, payload, lifecycle

        results, payload, lifecycle = await asyncio.to_thread(build_export)
    except asset_store.AssetError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)[:240]}, status_code=400)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return Response(
        payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="asset-export-{resource}-{stamp}.zip"',
            "X-Asset-Count": str(len(results)),
            "X-Asset-Consumed": str(lifecycle.get("moved_accounts", 0)),
        },
    )


def _asset_scan_payload(progress_only=False):
    scan = {
        **ASSET_SCAN_STATE,
        "progress": dict(ASSET_SCAN_STATE.get("progress") or {}),
    }
    if progress_only:
        return {"scan": scan}
    from common import asset_scanner

    report = asset_scanner.get_report()
    report["scan"] = scan
    # 合并邮箱分类(自主导入/自主注册)和母/子邮箱关系到 outlook 扫描项
    meta = _load_emails_meta()
    source_counts = {"imported": 0, "registered": 0}
    for item in report.get("items", []):
        if item.get("platform") == "outlook" and item.get("email"):
            m = meta.get(item["email"].lower(), {})
            src = m.get("source", "imported")
            item["mail_source"] = src
            item["parent_email"] = m.get("parent_email", "")
            item["is_parent"] = m.get("is_parent", False)
            item["child_count"] = len(m.get("child_emails", []))
            item["child_emails"] = m.get("child_emails", [])
            source_counts[src] = source_counts.get(src, 0) + 1
        else:
            item["mail_source"] = ""
            item["parent_email"] = ""
            item["is_parent"] = False
            item["child_count"] = 0
    report["mail_source_counts"] = source_counts
    return report


def _set_asset_scan_progress(value):
    ASSET_SCAN_STATE["progress"] = {
        "completed": max(0, int(value.get("completed") or 0)),
        "total": max(0, int(value.get("total") or 0)),
        "current": str(value.get("current") or "")[:160],
    }


def _scan_assets_sync(
    platforms,
    concurrency=1,
    account_concurrency=4,
    timeout=15,
    progress=None,
    emails=None,
    force=False,
    include_plus_trial=False,
):
    from common import asset_scanner

    with ASSET_SCAN_LOCK:
        return asset_scanner.scan_pool(
            platforms=platforms,
            concurrency=concurrency,
            account_concurrency=account_concurrency,
            timeout=timeout,
            progress=progress,
            emails=emails,
            force=force,
            include_plus_trial=include_plus_trial,
        )


async def _run_asset_scan(
    platforms,
    concurrency,
    account_concurrency,
    timeout,
    emails=None,
    force=False,
    quarantine_bad=True,
    include_plus_trial=False,
):
    global ASSET_SCAN_TASK
    from common import asset_scanner

    loop = asyncio.get_running_loop()

    def progress(value):
        loop.call_soon_threadsafe(_set_asset_scan_progress, value)

    try:
        report = await asyncio.to_thread(
            _scan_assets_sync,
            platforms=platforms,
            concurrency=concurrency,
            account_concurrency=account_concurrency,
            timeout=timeout,
            progress=progress,
            emails=emails,
            force=force,
            include_plus_trial=include_plus_trial,
        )
        if quarantine_bad:
            from common import asset_store

            ASSET_SCAN_STATE["quarantine"] = await asyncio.to_thread(
                asset_store.quarantine_scan_report, report
            )
        else:
            ASSET_SCAN_STATE["quarantine"] = {"moved_accounts": 0, "moved_files": 0}
        ASSET_SCAN_STATE["finished_at"] = report.get("finished_at", "")
        ASSET_SCAN_STATE["error"] = ""
    except Exception as exc:
        ASSET_SCAN_STATE["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ASSET_SCAN_STATE["error"] = str(exc)[:240]
    finally:
        ASSET_SCAN_STATE["running"] = False
        ASSET_SCAN_TASK = None


@app.get("/api/assets/scan")
def api_asset_scan_get(request: Request, progress_only: bool = False):
    denied = _asset_api_denied(request)
    if denied:
        return denied
    return _asset_result(lambda: _asset_scan_payload(progress_only=progress_only))


@app.post("/api/assets/scan")
async def api_asset_scan_start(request: Request):
    global ASSET_SCAN_TASK
    denied = _asset_api_denied(request)
    if denied:
        return denied
    if ASSET_SCAN_STATE["running"]:
        return JSONResponse(
            {"error": "号池扫描正在运行", **_asset_scan_payload()},
            status_code=409,
        )
    try:
        data = await request.json()
    except Exception:
        data = {}
    from common import asset_scanner

    requested = (data or {}).get("platforms") or list(asset_scanner.PLATFORMS)
    if not isinstance(requested, list):
        return JSONResponse({"error": "platforms 必须是数组"}, status_code=400)
    platforms = [str(item).strip().lower() for item in requested if str(item).strip()]
    invalid = sorted(set(platforms).difference(asset_scanner.PLATFORMS))
    if invalid:
        return JSONResponse({"error": f"不支持的平台：{', '.join(invalid)}"}, status_code=400)
    try:
        concurrency = min(2, max(1, int((data or {}).get("concurrency") or 1)))
        account_concurrency = min(8, max(1, int((data or {}).get("account_concurrency") or 4)))
        timeout = min(60, max(5, int((data or {}).get("timeout") or 15)))
    except (TypeError, ValueError):
        return JSONResponse({"error": "concurrency 和 timeout 必须是整数"}, status_code=400)

    raw_emails = (data or {}).get("emails")
    emails = None
    if isinstance(raw_emails, list) and raw_emails:
        emails = [str(e).strip() for e in raw_emails if str(e).strip()]
    # A POST represents an explicit/manual scan. Reuse remains available to
    # programmatic scanner callers, but the WebUI action must probe accounts.
    force = (data or {}).get("force", True)
    if not isinstance(force, bool):
        return JSONResponse({"error": "force 必须是布尔值"}, status_code=400)
    quarantine_bad = (data or {}).get("quarantine_bad", True)
    if not isinstance(quarantine_bad, bool):
        return JSONResponse({"error": "quarantine_bad 必须是布尔值"}, status_code=400)
    include_plus_trial = (data or {}).get("include_plus_trial", False)
    if not isinstance(include_plus_trial, bool):
        return JSONResponse({"error": "include_plus_trial 必须是布尔值"}, status_code=400)

    current = await asyncio.to_thread(asset_scanner.get_report)
    if emails:
        email_set = {e.lower() for e in emails}
        total = sum(1 for item in current["items"] if item.get("platform") in set(platforms) and (item.get("email") or "").lower() in email_set)
    else:
        total = sum(1 for item in current["items"] if item.get("platform") in set(platforms))
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ASSET_SCAN_STATE.update({
        "running": True,
        "started_at": started_at,
        "finished_at": "",
        "error": "",
        "progress": {"completed": 0, "total": total, "current": ""},
        "quarantine": {"moved_accounts": 0, "moved_files": 0},
    })
    ASSET_SCAN_TASK = asyncio.create_task(
        _run_asset_scan(
            platforms,
            concurrency,
            account_concurrency,
            timeout,
            emails=emails,
            force=force,
            quarantine_bad=quarantine_bad,
            include_plus_trial=include_plus_trial,
        )
    )
    return {
        "ok": True,
        "platforms": platforms,
        "safe_mode": {
            "platform_concurrency": concurrency,
            "account_concurrency": account_concurrency,
            "force": force,
            "quarantine_bad": quarantine_bad,
            "include_plus_trial": include_plus_trial,
        },
        "scan": dict(ASSET_SCAN_STATE),
    }


@app.get("/api/scripts")
def api_scripts():
    return {"scripts": schema.SCRIPTS}


@app.get("/api/links")
def api_links():
    return {"links": getattr(schema, "EXTERNAL_LINKS", [])}


@app.get("/api/embeds")
def api_embeds():
    return {"embeds": getattr(schema, "EMBED_PAGES", [])}


@app.get("/api/k12/status")
def api_k12_status():
    return _k12_status()


@app.post("/api/k12/start")
async def api_k12_start():
    return await _start_k12_service()


@app.get("/api/chatgpt-plus/status")
async def api_chatgpt_plus_status():
    return _plus_status()


@app.post("/api/chatgpt-plus/start")
async def api_chatgpt_plus_start():
    return _plus_status()


@app.post("/api/chatgpt-plus/import-codex")
async def api_chatgpt_plus_import_codex(request: Request):
    data = await request.json()
    account_text = str((data or {}).get("accounts") or "")
    if not account_text.strip():
        return JSONResponse({"error": "请粘贴至少一个 Plus 账号"}, status_code=400)
    if len(account_text) > 5_000_000:
        return JSONResponse({"error": "批量账号内容超过 5 MB"}, status_code=413)

    from common.account_records import canonical_account_line, parse_account_text

    records, errors = parse_account_text(account_text)
    if errors:
        return JSONResponse(
            {"error": "账号格式错误或存在重复邮箱", "details": errors[:20]},
            status_code=400,
        )
    if not records:
        return JSONResponse({"error": "没有可导入的账号"}, status_code=400)
    if len(records) > 100:
        return JSONResponse({"error": "单批最多导入 100 个账号"}, status_code=400)

    def bounded_int(key, default, minimum, maximum):
        try:
            value = int((data or {}).get(key, default))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须是整数") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} 必须在 {minimum}-{maximum} 之间")
        return value

    try:
        concurrency = bounded_int("concurrency", 1, 1, 5)
        phone_attempts = bounded_int("phone_attempts", 3, 1, 10)
        sms_timeout = bounded_int("sms_timeout", 180, 30, 600)
        timeout = bounded_int("timeout", 600, 120, 3600)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    sms_provider = str((data or {}).get("sms_provider") or "auto").strip().lower()
    if sms_provider not in {"auto", "custom", "smsman", "firefox", "hero"}:
        return JSONResponse({"error": "未知手机号接码平台"}, status_code=400)
    skip_phone = bool((data or {}).get("skip_phone"))
    no_import = bool((data or {}).get("no_import"))
    output_format = str((data or {}).get("output_format") or "none").strip().lower()
    if output_format not in {"none", "sub2api"}:
        return JSONResponse({"error": "未知 token 输出格式"}, status_code=400)
    output_path = str((data or {}).get("output") or "").strip()[:500]
    if sms_provider == "custom" and not skip_phone:
        from common import custom_sms

        custom_pool = await asyncio.to_thread(custom_sms.summary)
        required = min(concurrency, len(records))
        if custom_pool["available"] < required:
            return JSONResponse(
                {
                    "error": (
                        f"自定义号码池可用 {custom_pool['available']} 个，"
                        f"当前并发启动至少需要 {required} 个"
                    )
                },
                status_code=400,
            )
    node = str((data or {}).get("node") or "auto").strip()[:120] or "auto"
    group = str(
        (data or {}).get("group")
        or _read_config_val("SUB2API_GROUP", "codex")
        or "codex"
    ).strip()[:120]

    data_root = os.path.abspath(os.environ.get("REG_FACTORY_DATA_DIR") or ROOT)
    runtime_dir = os.path.join(data_root, "runtime", "plus_codex")
    os.makedirs(runtime_dir, exist_ok=True)
    descriptor, input_path = tempfile.mkstemp(
        prefix="accounts-", suffix=".txt", dir=runtime_dir, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(canonical_account_line(item) for item in records) + "\n")
        with contextlib.suppress(OSError):
            os.chmod(input_path, 0o600)

        script = schema.script_by_id("plus_codex_import")
        args = {
            "--accounts-file": input_path,
            "--group": group,
            "--concurrency": concurrency,
            "--node": node,
            "--sms-provider": sms_provider,
            "--phone-attempts": phone_attempts,
            "--sms-timeout": sms_timeout,
            "--timeout": timeout,
            "--skip-phone": skip_phone,
            "--no-import": no_import,
            "--output-format": output_format,
            "--delete-input": True,
            "--keep-on-fail": bool((data or {}).get("keep_on_fail")),
        }
        if output_path:
            args["--output"] = output_path
        task_env = _child_env("chatgpt")
        from common import proxy_switch

        await asyncio.to_thread(proxy_switch.ensure_proxy_mode, task_env)
        started = await _start_managed_run(
            _build_cmd(script, args), "plus_codex_import", task_env, data_root
        )
        RUNS[started["run_id"]]["sensitive_input_path"] = input_path
        return {
            **started,
            "accepted": len(records),
            "accepted_emails": [str(record.get("email") or "").strip().lower() for record in records],
        }
    except Exception as exc:
        with contextlib.suppress(OSError):
            os.unlink(input_path)
        return JSONResponse({"error": str(exc)[:240]}, status_code=400)


def _decode_access_token_claims(token):
    parts = str(token or "").split(".")
    if len(parts) < 2:
        raise ValueError("invalid JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))


def _chatgpt_protocol_accounts(emails=None, limit=100):
    """Load current saved ChatGPT sessions for the protocol-link worker.

    Tokens never leave this process except through the short-lived, local input
    file consumed by the child worker.  The API only returns redacted counts.
    """
    filter_requested = emails is not None
    requested = {
        str(value or "").strip().lower()
        for value in (emails or [])
        if str(value or "").strip()
    }
    data_root = os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or ROOT
    tokens_dir = os.path.join(data_root, "tokens", "chatgpt")
    accounts = []
    seen = set()
    if os.path.isdir(tokens_dir):
        for name in os.listdir(tokens_dir):
            if not name.endswith(".session.json"):
                continue
            path = os.path.join(tokens_dir, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    session = json.load(handle)
                token = str(session.get("accessToken") or session.get("access_token") or "").strip()
                claims = _decode_access_token_claims(token)
                if float(claims.get("exp") or 0) <= time.time() or token in seen:
                    continue
                user = session.get("user") if isinstance(session.get("user"), dict) else {}
                account = session.get("account") if isinstance(session.get("account"), dict) else {}
                email = str(user.get("email") or session.get("email") or claims.get("email") or "").strip().lower()
                if not email or (filter_requested and email not in requested):
                    continue
                seen.add(token)
                accounts.append({
                    "email": email,
                    "access_token": token,
                    "account_id": str(account.get("id") or session.get("account_id") or "").strip(),
                    "modified_at": os.path.getmtime(path),
                })
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    accounts.sort(key=lambda item: item["modified_at"], reverse=True)
    bounded_limit = max(1, min(100, int(limit or 100)))
    return accounts[:bounded_limit]


def _protocol_pool_eligible_emails():
    """Return cached, redacted pool identities that previously passed trial scan."""
    from common import asset_scanner

    # Only an explicitly zero-priced offer may enter the protocol pool.
    allowed = {"zero_price"}
    report = asset_scanner.get_report()
    emails = []
    seen = set()
    for item in report.get("items", []):
        if not isinstance(item, dict) or item.get("platform") != "chatgpt":
            continue
        email = str(item.get("email") or "").strip().lower()
        if not email or email in seen or str(item.get("plus_trial") or "") not in allowed:
            continue
        seen.add(email)
        emails.append(email)
    return emails


def _luhn_valid(number):
    total = 0
    parity = len(number) % 2
    for index, character in enumerate(number):
        digit = int(character)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _parse_paypal_payment_details(value):
    """Normalize optional, per-run PayPal details without persisting raw input."""
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("payment_details 必须是对象")

    raw_cards = str(value.get("cards") or "").strip()
    raw_addresses = str(value.get("addresses") or "").strip()
    raw_phones = str(value.get("phones") or "").strip()
    if not any((raw_cards, raw_addresses, raw_phones)):
        return {}
    if not all((raw_cards, raw_addresses, raw_phones)):
        raise ValueError("本次支付资料必须同时填写卡片、账单地址和手机号接码")

    def lines(raw, label):
        values = [line.strip() for line in raw.splitlines() if line.strip()]
        if not values or len(values) > 100:
            raise ValueError(f"{label} 必须是 1 到 100 行")
        return values

    cards = []
    for line in lines(raw_cards, "卡片"):
        parts = [part.strip() for part in line.split("|")]
        if len(parts) == 3 and "/" in parts[1]:
            number, expiry, cvv = parts
            expiry_parts = [part.strip() for part in expiry.split("/", 1)]
            if len(expiry_parts) != 2:
                raise ValueError("卡片格式应为 卡号|MM/YY|CVC")
            month, year = expiry_parts
        elif len(parts) == 4:
            number, month, year, cvv = parts
        else:
            raise ValueError("卡片格式应为 卡号|MM/YY|CVC")
        number = re.sub(r"[ -]", "", number)
        if not re.fullmatch(r"\d{12,19}", number) or not _luhn_valid(number):
            raise ValueError("卡号格式或校验位不正确")
        if not re.fullmatch(r"\d{1,2}", month) or not 1 <= int(month) <= 12:
            raise ValueError("卡片有效期月份不正确")
        if not re.fullmatch(r"\d{2}|\d{4}", year):
            raise ValueError("卡片有效期年份不正确")
        full_year = 2000 + int(year) if len(year) == 2 else int(year)
        current = time.localtime()
        if (full_year, int(month)) < (current.tm_year, current.tm_mon) or full_year > current.tm_year + 20:
            raise ValueError("卡片已过期或有效期年份超出范围")
        if not re.fullmatch(r"\d{3,4}", cvv):
            raise ValueError("CVC 必须是 3 或 4 位数字")
        cards.append({
            "number": number,
            "exp_month": f"{int(month):02d}",
            "exp_year": str(full_year),
            "cvv": cvv,
        })

    addresses = []
    for line in lines(raw_addresses, "账单地址"):
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 4 or not all(parts):
            raise ValueError("账单地址格式应为 街道|城市|州|邮编")
        line1, city, state, postal_code = parts
        if max(map(len, parts)) > 120 or len(state) > 40:
            raise ValueError("账单地址字段过长")
        if not re.fullmatch(r"[A-Za-z0-9 -]{3,12}", postal_code):
            raise ValueError("账单邮编格式不正确")
        addresses.append({"line1": line1, "city": city, "state": state, "postal_code": postal_code})

    phones = []
    for line in lines(raw_phones, "手机号接码"):
        separator = "----" if "----" in line else "|"
        parts = [part.strip() for part in line.split(separator, 1)]
        if len(parts) != 2 or not re.fullmatch(r"\+\d{8,15}", parts[0]):
            raise ValueError("手机号接码格式应为 +国家码手机号----短信记录 URL")
        parsed_url = urllib.parse.urlsplit(parts[1])
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("短信记录 URL 必须是有效的 http(s) 地址")
        phones.append({"phone": parts[0], "sms_api_url": parts[1]})

    return {
        "cards": cards,
        "addresses": addresses,
        "phone_numbers": phones,
        "reverse_engineering": True,
    }


def _protocol_batch_script():
    return {
        "file": "tools/run_protocol_payment_batch.py",
        "args": [
            {"flag": "--accounts-file", "type": "str"},
            {"flag": "--method", "type": "str"},
            {"flag": "--operation", "type": "str"},
            {"flag": "--payment-confirmed", "type": "bool"},
            {"flag": "--engine-root", "type": "str"},
            {"flag": "--workers", "type": "int"},
            {"flag": "--timeout", "type": "int"},
            {"flag": "--report", "type": "str"},
            {"flag": "--delete-input", "type": "bool"},
        ],
    }


@app.get("/api/chatgpt-plus/protocol-status")
def api_chatgpt_plus_protocol_status():
    from common.protocol_payment import paypal_payment_ready, protocol_catalog, resolve_protocol_engine_root

    root = resolve_protocol_engine_root()
    pool_emails = _protocol_pool_eligible_emails()
    payment_ready = paypal_payment_ready(root or "")
    return {
        "ok": True,
        "engine_ready": bool(root),
        "account_count": len(_chatgpt_protocol_accounts(limit=100)),
        "pool_eligible_count": len(_chatgpt_protocol_accounts(pool_emails, limit=100)),
        "payment_ready": bool(root),
        "payment_configured": payment_ready,
        "payment_message": "可使用引擎默认支付资料" if payment_ready else "支付时需录入本次任务资料",
        "methods": protocol_catalog(root or ""),
        "message": "协议引擎已就绪" if root else "未找到协议引擎；设置 REG_FACTORY_PROTOCOL_PAYMENT_ROOT 后可用",
    }


@app.post("/api/chatgpt-plus/protocol-batch")
async def api_chatgpt_plus_protocol_batch(request: Request):
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return JSONResponse({"error": "请求必须是有效的 JSON 对象"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"error": "请求必须是 JSON 对象"}, status_code=400)
    from common.protocol_payment import payment_method, paypal_payment_ready, resolve_protocol_engine_root

    method = payment_method(data.get("method"))
    if not method:
        return JSONResponse({"error": "未知协议渠道"}, status_code=400)
    if not method["batch_enabled"]:
        return JSONResponse({"error": f"{method['label']} 的上游协议不支持批量提链"}, status_code=400)
    engine_root = resolve_protocol_engine_root()
    if not engine_root:
        return JSONResponse(
            {"error": "未找到协议引擎；设置 REG_FACTORY_PROTOCOL_PAYMENT_ROOT 后重试"},
            status_code=503,
        )
    operation = str(data.get("operation") or "extract").strip().lower()
    if operation not in {"extract", "pay"}:
        return JSONResponse({"error": "未知协议操作"}, status_code=400)
    payment_config = {}
    if operation == "pay":
        if method["id"] != "paypal":
            return JSONResponse({"error": "当前只有 PayPal 支持批量协议直接支付；其他渠道需提链后确认"}, status_code=400)
        if data.get("confirm_payment") is not True:
            return JSONResponse({"error": "执行真实支付前必须明确勾选支付确认"}, status_code=400)
        try:
            payment_config = _parse_paypal_payment_details(data.get("payment_details"))
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not payment_config and not paypal_payment_ready(engine_root):
            return JSONResponse({"error": "请录入本次 PayPal 卡片、账单地址和手机号接码资料"}, status_code=400)
    raw_emails = data.get("emails") or []
    if not isinstance(raw_emails, list) or len(raw_emails) > 100:
        return JSONResponse({"error": "emails 必须是最多 100 条的数组"}, status_code=400)
    source = str(data.get("source") or "saved").strip().lower()
    if source not in {"saved", "pool"}:
        return JSONResponse({"error": "未知账号来源"}, status_code=400)
    try:
        workers = 1 if operation == "pay" else max(1, min(4, int(data.get("workers") or 1)))
    except (TypeError, ValueError):
        return JSONResponse({"error": "批量并发必须是整数"}, status_code=400)
    selected_emails = _protocol_pool_eligible_emails() if source == "pool" else (raw_emails or None)
    candidates = _chatgpt_protocol_accounts(selected_emails, limit=100)
    if not candidates:
        message = "号池中没有已缓存为 Plus 优惠资格且会话可用的账号" if source == "pool" else "没有找到可用的本地 ChatGPT 会话"
        return JSONResponse({"error": message}, status_code=400)

    semaphore = asyncio.Semaphore(4)

    async def check_one(item):
        async with semaphore:
            return item, await asyncio.to_thread(_plus_trial_gate_sync, item)

    checks = await asyncio.gather(*(check_one(item) for item in candidates))
    # A campaign/discount is not proof of a 0 yuan checkout.
    allowed = {"zero_price"}
    eligible = [item for item, result in checks if result.get("plus_trial") in allowed]
    skipped = [
        {"email": result.get("email") or item["email"], "reason": result.get("plus_trial") or "unknown"}
        for item, result in checks
        if result.get("plus_trial") not in allowed
    ]
    if not eligible:
        return JSONResponse(
            {"error": "没有命中明确 0 元试用资格的账号，未创建协议任务", "skipped": skipped},
            status_code=422,
        )

    data_root = os.path.abspath(os.environ.get("REG_FACTORY_DATA_DIR") or ROOT)
    runtime_dir = os.path.join(data_root, "runtime", "protocol_payment")
    os.makedirs(runtime_dir, exist_ok=True)
    descriptor, input_path = tempfile.mkstemp(prefix="accounts-", suffix=".json", dir=runtime_dir, text=True)
    report_path = os.path.join(runtime_dir, f"{method['id']}-{int(time.time())}.report.json")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "accounts": [
                        {"email": item["email"], "access_token": item["access_token"], "account_id": item["account_id"]}
                        for item in eligible
                    ],
                    "payment_config": payment_config,
                },
                handle,
                ensure_ascii=False,
            )
        with contextlib.suppress(OSError):
            os.chmod(input_path, 0o600)
        task_env = _child_env("chatgpt")
        runtime_values = _plus_runtime_environment()
        task_env.update(runtime_values)
        task_env["REG_FACTORY_PROTOCOL_PAYMENT_ROOT"] = str(engine_root)
        task_env["REG_FACTORY_PROTOCOL_CHECKOUT_PROXY"] = runtime_values.get("REG_FACTORY_PLUS_LINK_PROXY", "")
        task_env["REG_FACTORY_PROTOCOL_APPROVE_PROXY"] = runtime_values.get("REG_FACTORY_PLUS_BIND_PROXY", "")
        from common import proxy_switch

        await asyncio.to_thread(proxy_switch.ensure_proxy_mode, task_env)
        args = {
            "--accounts-file": input_path,
            "--method": method["id"],
            "--operation": operation,
            "--payment-confirmed": operation == "pay",
            "--engine-root": str(engine_root),
            "--workers": workers,
            "--timeout": 300,
            "--report": report_path,
            "--delete-input": True,
        }
        started = await _start_managed_run(
            _build_cmd(_protocol_batch_script(), args), "plus_protocol_batch", task_env, data_root
        )
        RUNS[started["run_id"]]["sensitive_input_path"] = input_path
        return {
            **started,
            "accepted": len(eligible),
            "skipped": skipped,
            "payment_method": method["id"],
            "operation": operation,
            "source": source,
            "report": os.path.basename(report_path),
        }
    except Exception as exc:
        with contextlib.suppress(OSError):
            os.unlink(input_path)
        return JSONResponse({"error": str(exc)[:240]}, status_code=400)


def _chatgpt_plus_free_ats(limit=PLUS_BATCH_SIZE):
    data_root = os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or ROOT
    tokens_dir = os.path.join(data_root, "tokens", "chatgpt")
    accounts = []
    seen = set()
    if os.path.isdir(tokens_dir):
        for name in os.listdir(tokens_dir):
            if not name.endswith(".session.json"):
                continue
            path = os.path.join(tokens_dir, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    session = json.load(handle)
                account = session.get("account") or {}
                if str(account.get("planType") or "").strip().lower() != "free":
                    continue
                token = str(session.get("accessToken") or "").strip()
                claims = _decode_access_token_claims(token)
                if float(claims.get("exp") or 0) <= time.time() or token in seen:
                    continue
                seen.add(token)
                user = session.get("user") or {}
                accounts.append({
                    "access_token": token,
                    "email": str(user.get("email") or claims.get("email") or ""),
                    "modified_at": os.path.getmtime(path),
                })
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    accounts.sort(key=lambda item: item["modified_at"], reverse=True)
    bounded_limit = max(1, min(500, int(limit or PLUS_BATCH_SIZE)))
    return accounts[:bounded_limit], len(accounts)


@app.get("/api/chatgpt-plus/export-ats")
def api_chatgpt_plus_export_ats(limit: int = PLUS_BATCH_SIZE):
    del limit
    return JSONResponse(
        {"error": "Plus 提链和绑卡功能已移除，请使用 Codex OAuth 批量导入"},
        status_code=410,
    )


_PLUS_CHECKOUT_GATE_PATHS = frozenset({
    "/card-bind/session",
    "/standalone-flow/preflight",
    "/standalone-flow/quick-checkout",
    "/standalone-flow/quick-checkout-batch",
})


def _plus_trial_gate_sync(payload: dict) -> dict:
    """Return a redacted trial decision for one checkout payload."""
    token = str(payload.get("access_token") or payload.get("accessToken") or "").strip()
    email = str(payload.get("email") or "").strip()
    if not token:
        return {
            "email": email,
            "plus_trial": "unknown",
            "detail": "missing access token",
            "evidence": "local:missing_access_token",
        }
    try:
        from common.asset_scanner import _scan_chatgpt_plus_trial

        token_view = {
            "account_id": str(payload.get("account_id") or "").strip(),
        }
        result = _scan_chatgpt_plus_trial(
            {"platform": "chatgpt", "email": email, "_token": token_view},
            token,
            20,
        )
        return {
            "email": email,
            "plus_trial": str(result.get("plus_trial") or "unknown"),
            "detail": str(result.get("plus_trial_detail") or ""),
            "evidence": str(result.get("plus_trial_evidence") or ""),
        }
    except Exception as exc:  # keep the gate fail-closed and redact credentials
        return {
            "email": email,
            "plus_trial": "unknown",
            "detail": f"trial check failed: {type(exc).__name__}",
            "evidence": "accounts_check:error",
        }


def _plus_gate_items(path: str, payload: dict) -> list[dict]:
    if path == "/standalone-flow/quick-checkout-batch":
        source = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
        return [
            item.get("payload")
            for item in source
            if isinstance(item, dict) and isinstance(item.get("payload"), dict)
        ]
    return [payload]


async def _plus_trial_gate(path: str, method: str, body: bytes):
    """Reject Checkout/payment requests unless every account has a Plus offer."""
    if method.upper() != "POST" or path not in _PLUS_CHECKOUT_GATE_PATHS:
        return None
    try:
        payload = json.loads(body or b"{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return JSONResponse({"ok": False, "error": "request body must be JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "request body must be an object"}, status_code=400)
    items = _plus_gate_items(path, payload)
    if not items:
        return JSONResponse({"ok": False, "error": "no checkout accounts supplied"}, status_code=400)

    semaphore = asyncio.Semaphore(4)

    async def check_one(item: dict) -> dict:
        async with semaphore:
            return await asyncio.to_thread(_plus_trial_gate_sync, item)

    results = await asyncio.gather(*(check_one(item) for item in items))
    # Fail closed unless the latest read-only check proved a zero price.
    allowed = {"zero_price"}
    blocked = [item for item in results if item.get("plus_trial") not in allowed]
    if blocked:
        return JSONResponse(
            {
                "ok": False,
                "error": "提链/支付仅允许明确 0 元试用资格的账号",
                "accounts": blocked,
            },
            status_code=422,
        )
    return None


async def _proxy_local_plus(request: Request, upstream_path: str, body: bytes | None = None):
    _plus_runtime_environment()
    if not _plus_health():
        await asyncio.to_thread(_start_plus_service_sync)
    upstream_url = f"http://127.0.0.1:{PLUS_PORT}{upstream_path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in (
            "host", "connection", "transfer-encoding", "content-length",
            "accept-encoding",
        )
    }
    fwd_headers["Accept-Encoding"] = "identity"
    if body is None:
        body = await request.body()

    def _do_proxy():
        req = urllib.request.Request(
            upstream_url,
            data=body or None,
            headers=fwd_headers,
            method=request.method,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=120) as resp:
                data = resp.read()
                ct = resp.headers.get("Content-Type", "application/octet-stream")
                cache = resp.headers.get("Cache-Control", "no-store")
                return resp.status, data, ct, cache
        except urllib.error.HTTPError as exc:
            data = exc.read()
            ct = exc.headers.get("Content-Type", "application/octet-stream")
            cache = exc.headers.get("Cache-Control", "no-store")
            return exc.code, data, ct, cache

    try:
        code, data, content_type, cache_control = await asyncio.to_thread(_do_proxy)
    except (OSError, urllib.error.URLError) as exc:
        return JSONResponse(
            {"ok": False, "error": f"本地 Plus 工作台不可用: {str(exc)[:120]}"},
            status_code=503,
        )
    return Response(
        content=data,
        status_code=code,
        headers={"Content-Type": content_type, "Cache-Control": cache_control},
    )


@app.api_route("/chatgpt-plus/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy_chatgpt_plus_page(request: Request, path: str):
    upstream_path = "/" + path.lstrip("/") if path else "/"
    return await _proxy_local_plus(request, upstream_path)


@app.api_route(
    "/api/chatgpt-plus/workbench/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
)
async def proxy_chatgpt_plus_api(request: Request, path: str):
    upstream_path = "/" + path.lstrip("/") if path else "/"
    body = await request.body() if request.method.upper() == "POST" else b""
    blocked = await _plus_trial_gate(upstream_path, request.method, body)
    if blocked is not None:
        return blocked
    return await _proxy_local_plus(request, upstream_path, body=body)


# ============================================================ 邮箱池批量导入
EMAILS_FILE = os.path.join(os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or ROOT, "emails.txt")
EMAILS_META_FILE = os.path.join(os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or ROOT, "emails_meta.json")
import re as _re
import json as _json
_EMAIL_RE = _re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# UUID 格式（Microsoft client_id 标准格式）
_UUID_RE = _re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", _re.I)


def _load_emails_meta():
    """读取邮箱分类元数据。返回 {email_lower: {"source": "imported"|"registered"}}。"""
    if not os.path.isfile(EMAILS_META_FILE):
        return {}
    try:
        with open(EMAILS_META_FILE, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_emails_meta(meta):
    """保存邮箱分类元数据。"""
    try:
        with open(EMAILS_META_FILE, "w", encoding="utf-8") as f:
            _json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _mark_email_source(email, source):
    """标记单个邮箱的来源分类。source: imported / registered。"""
    meta = _load_emails_meta()
    meta[email.lower()] = {"source": source}
    _save_emails_meta(meta)


def _parse_mail_line(line):
    """Parse and normalize a mailbox without accepting token-only records."""
    from common.account_records import canonical_account_line, parse_account_line

    try:
        record = parse_account_line(line)
    except ValueError:
        return None
    if record.get("source_type") != "mailbox" or not record.get("email"):
        return None
    return canonical_account_line(record).split("----")


def _existing_emails():
    emails = set()
    if os.path.isfile(EMAILS_FILE):
        for line in open(EMAILS_FILE, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                emails.add(line.split("----")[0].strip().lower())
    return emails


@app.get("/api/mailpool")
def api_mailpool_get():
    total = 0
    if os.path.isfile(EMAILS_FILE):
        for line in open(EMAILS_FILE, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                total += 1
    return {"total": total}


@app.post("/api/mailpool")
async def api_mailpool_import(request: Request):
    data = await request.json()
    text = (data or {}).get("text") or ""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    added, skipped, bad, updated = 0, 0, 0, 0
    bad_samples = []
    imported_emails = []

    # 读取已有行，构建 email -> (行号, 解析字段列表) 映射
    existing_lines = []
    existing_map: dict[str, int] = {}
    if os.path.isfile(EMAILS_FILE):
        existing_lines = [
            raw.rstrip("\n") for raw in open(EMAILS_FILE, encoding="utf-8")
        ]
        for idx, raw in enumerate(existing_lines):
            stripped = raw.strip()
            if stripped and not stripped.startswith("#"):
                email_key = stripped.split("----")[0].strip().lower()
                if email_key and email_key not in existing_map:
                    existing_map[email_key] = idx

    new_lines = []  # 追加的新行
    file_dirty = False  # 是否需要重写文件

    for ln in lines:
        if not ln.strip():
            continue
        parsed = _parse_mail_line(ln)
        if not parsed:
            bad += 1
            if len(bad_samples) < 5:
                bad_samples.append(ln.strip()[:60])
            continue
        email = parsed[0].lower()
        normalized = "----".join(parsed)

        if email in existing_map:
            idx = existing_map[email]
            old_line = existing_lines[idx].strip()
            if old_line == normalized:
                skipped += 1
            else:
                existing_lines[idx] = normalized
                file_dirty = True
                updated += 1
                imported_emails.append(email)
        else:
            existing_map[email] = len(existing_lines)
            existing_lines.append(normalized)
            new_lines.append(normalized)
            imported_emails.append(email)
            added += 1
            file_dirty = True

    if file_dirty:
        with open(EMAILS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(existing_lines))
            if existing_lines:
                f.write("\n")
        meta = _load_emails_meta()
        for em in imported_emails:
            meta[em] = {"source": "imported"}
        _save_emails_meta(meta)
    total = len(_existing_emails())
    return {"ok": True, "added": added, "skipped": skipped, "bad": bad,
            "updated": updated, "bad_samples": bad_samples, "total": total}


@app.get("/api/mailpool/list")
def api_mailpool_list():
    """列出邮箱池所有邮箱，含分类信息和母/子邮箱关系。"""
    meta = _load_emails_meta()
    emails = []
    if not os.path.isfile(EMAILS_FILE):
        return {"total": 0, "emails": [], "counts": {"imported": 0, "registered": 0}}
    idx = 0
    for line in open(EMAILS_FILE, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _parse_mail_line(line)
        if not parsed:
            continue
        email = parsed[0]
        password = parsed[1] if len(parsed) >= 2 else ""
        token = parsed[2] if len(parsed) >= 3 else ""
        client_id = parsed[3] if len(parsed) >= 4 else ""
        email_lower = email.lower()
        m = meta.get(email_lower, {})
        source = m.get("source", "imported")
        emails.append({
            "index": idx,
            "email": email,
            "password": password,
            "has_token": bool(token),
            "has_client_id": bool(client_id),
            "source": source,
            "parent_email": m.get("parent_email", ""),
            "child_emails": m.get("child_emails", []),
            "is_parent": m.get("is_parent", False),
        })
        idx += 1
    counts = {
        "imported": sum(1 for e in emails if e["source"] == "imported"),
        "registered": sum(1 for e in emails if e["source"] == "registered"),
    }
    return {"total": len(emails), "emails": emails, "counts": counts}


@app.post("/api/mailpool/split")
async def api_mailpool_split(request: Request):
    """分裂 Outlook 邮箱（plus addressing 别名）。
    body: {"parent_email": "user@outlook.com", "count": 3}
    生成 user+xxx1@outlook.com 等子邮箱，继承母邮箱的密码/token/client_id。
    """
    import random
    import string
    data = await request.json()
    parent_email = str((data or {}).get("parent_email", "")).strip()
    count = int((data or {}).get("count", 1))
    if not parent_email or not _EMAIL_RE.match(parent_email):
        return {"ok": False, "msg": "母邮箱地址无效"}
    if count < 1 or count > 20:
        return {"ok": False, "msg": "分裂数量须在 1-20 之间"}

    # 从 emails.txt 找到母邮箱的完整记录
    parent_record = None
    if os.path.isfile(EMAILS_FILE):
        for line in open(EMAILS_FILE, encoding="utf-8"):
            parsed = _parse_mail_line(line)
            if parsed and parsed[0].lower() == parent_email.lower():
                parent_record = parsed
                break
    if not parent_record:
        return {"ok": False, "msg": f"母邮箱 {parent_email} 不在邮箱池中"}

    # 解析母邮箱 local_part 和 domain
    local, _, domain = parent_email.partition("@")
    if "+" in local:
        # 如果母邮箱本身已是子邮箱，取其基础部分作为母
        local = local.split("+")[0]

    existing = _existing_emails()
    meta = _load_emails_meta()
    generated = []
    for _ in range(count):
        # 生成随机 tag（6 位字母数字）
        for _try in range(50):
            tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
            child_email = f"{local}+{tag}@{domain}"
            if child_email.lower() not in existing and child_email.lower() not in {e.lower() for e in generated}:
                break
        else:
            continue
        # 子邮箱继承母邮箱的密码/token/client_id
        child_fields = [child_email] + parent_record[1:]
        # 写入 emails.txt
        need_nl = os.path.isfile(EMAILS_FILE) and os.path.getsize(EMAILS_FILE) > 0
        with open(EMAILS_FILE, "a", encoding="utf-8") as f:
            if need_nl:
                f.write("\n")
            f.write("----".join(child_fields) + "\n")
        existing.add(child_email.lower())
        generated.append(child_email)

        # 更新 meta：子邮箱记录 parent_email，母邮箱记录 child_emails
        child_key = child_email.lower()
        parent_key = parent_email.lower()
        meta[child_key] = {
            "source": meta.get(parent_key, {}).get("source", "imported"),
            "parent_email": parent_email,
        }
        parent_meta = meta.get(parent_key, {"source": meta.get(parent_key, {}).get("source", "imported")})
        parent_meta["is_parent"] = True
        children = parent_meta.get("child_emails", [])
        if child_email not in children:
            children.append(child_email)
        parent_meta["child_emails"] = children
        meta[parent_key] = parent_meta

    _save_emails_meta(meta)
    total = len(_existing_emails())
    return {
        "ok": True,
        "parent_email": parent_email,
        "generated": generated,
        "count": len(generated),
        "total": total,
    }


@app.post("/api/mailpool/unsplit")
async def api_mailpool_unsplit(request: Request):
    """解除子邮箱的分裂关系（将子邮箱变回独立邮箱，从母邮箱的 child_emails 中移除）。
    body: {"email": "user+abc123@outlook.com"}
    """
    data = await request.json()
    email = str((data or {}).get("email", "")).strip()
    if not email:
        return {"ok": False, "msg": "未指定邮箱"}
    meta = _load_emails_meta()
    key = email.lower()
    entry = meta.get(key, {})
    parent_email = entry.get("parent_email", "")
    if not parent_email:
        return {"ok": False, "msg": "该邮箱不是子邮箱"}
    # 从母邮箱的 child_emails 中移除
    parent_key = parent_email.lower()
    parent_meta = meta.get(parent_key, {})
    children = [c for c in parent_meta.get("child_emails", []) if c.lower() != key]
    parent_meta["child_emails"] = children
    if not children:
        parent_meta.pop("is_parent", None)
    meta[parent_key] = parent_meta
    # 清除子邮箱的 parent_email
    entry.pop("parent_email", None)
    meta[key] = entry
    _save_emails_meta(meta)
    return {"ok": True, "email": email, "parent_email": parent_email}


def _delete_platform_credentials(platform, targets):
    """删除指定平台下的凭据文件（cookie/token/accounts.txt），不影响 emails.txt。
    返回 {"deleted_files": N, "cleaned_accounts": N, "cleaned_uploads": N}。"""
    from pathlib import Path
    data_root = Path(os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or ROOT)
    deleted_files = 0
    cleaned_accounts = 0
    cleaned_uploads = 0

    # ---- 1. 清理 cookies/{platform}/accounts.txt 和 full_*.json ----
    cookie_dirs = [data_root / "cookies" / platform]
    if platform == "claude":
        cookie_dirs.append(data_root / "cookies")  # claude cookie 也存根目录
    for cdir in cookie_dirs:
        if not cdir.is_dir():
            continue
        # 读取 accounts.txt，找出 target 邮箱对应的 cookie_value
        acct_path = cdir / "accounts.txt"
        cookie_values_to_delete = set()
        if acct_path.is_file():
            kept_lines = []
            for raw in acct_path.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = raw.strip().split("|")
                if len(parts) >= 3 and parts[0].strip().lower() in targets:
                    cookie_values_to_delete.add(parts[2].strip())
                    cleaned_accounts += 1
                else:
                    kept_lines.append(raw)
            if cookie_values_to_delete:
                acct_path.write_text(
                    "\n".join(kept_lines) + ("\n" if kept_lines else ""),
                    encoding="utf-8",
                )
        # 删除包含目标 cookie_value 的 full_*.json
        for fpath in cdir.glob("full_*.json"):
            try:
                raw_cookies = _json.loads(fpath.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(raw_cookies, list):
                continue
            for item in raw_cookies:
                if isinstance(item, dict) and item.get("value") in cookie_values_to_delete:
                    try:
                        fpath.unlink()
                        deleted_files += 1
                    except Exception:
                        pass
                    break

    # ---- 2. 清理 tokens/{platform}/ 下的文件 ----
    token_dir = data_root / "tokens" / platform
    if token_dir.is_dir():
        for target_email in targets:
            # 删除 {email}.session.json / {email}.sso.json / {email}.account.json
            for pattern in [f"{target_email}*.json"]:
                for fpath in token_dir.glob(pattern):
                    try:
                        fpath.unlink()
                        deleted_files += 1
                    except Exception:
                        pass
            # ChatGPT 额外清理 c2a-{email}.json 和 codex-{email}-free.json
            if platform == "chatgpt":
                for extra in [f"c2a-{target_email}.json", f"codex-{target_email}-free.json"]:
                    fpath = token_dir / extra
                    if fpath.is_file():
                        try:
                            fpath.unlink()
                            deleted_files += 1
                        except Exception:
                            pass
        # 清理 uploaded_sub2api.txt
        upload_path = token_dir / "uploaded_sub2api.txt"
        if upload_path.is_file():
            kept = []
            for raw in upload_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if raw.strip().lower() in targets:
                    cleaned_uploads += 1
                else:
                    kept.append(raw)
            if cleaned_uploads:
                upload_path.write_text(
                    "\n".join(kept) + ("\n" if kept else ""),
                    encoding="utf-8",
                )

    # ---- 3. 清理 asset_scanner 缓存中该平台的记录 ----
    try:
        cache_path = data_root / "runtime" / "state" / "asset_scan_cache.json"
        if cache_path.is_file():
            cache = _json.loads(cache_path.read_text(encoding="utf-8", errors="replace"))
            items = cache.get("items", [])
            new_items = [
                item for item in items
                if not (
                    isinstance(item, dict)
                    and item.get("platform") == platform
                    and str(item.get("email", "")).lower() in targets
                )
            ]
            if len(new_items) != len(items):
                cache["items"] = new_items
                cache_path.write_text(_json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    return {
        "deleted_files": deleted_files,
        "cleaned_accounts": cleaned_accounts,
        "cleaned_uploads": cleaned_uploads,
    }


@app.post("/api/mailpool/delete")
async def api_mailpool_delete(request: Request):
    """批量删除选中邮箱。
    body: {"emails": [...], "platform": "outlook"|"chatgpt"|"claude"|"grok"|"kiro"}
    - outlook（默认）：完全删除 emails.txt + meta
    - chatgpt/claude/grok/kiro：仅删除该平台凭据文件，不影响邮箱本身
    """
    data = await request.json()
    targets = set(str(e).lower() for e in (data or {}).get("emails", []))
    platform = str((data or {}).get("platform", "outlook")).strip().lower()
    if not targets:
        return {"ok": False, "msg": "未选择邮箱"}
    if platform not in {"outlook", "chatgpt", "claude", "grok", "kiro"}:
        platform = "outlook"

    if platform == "outlook":
        # 完全删除：emails.txt + meta
        if not os.path.isfile(EMAILS_FILE):
            return {"ok": False, "msg": "emails.txt 不存在"}
        kept = []
        deleted = 0
        for line in open(EMAILS_FILE, encoding="utf-8"):
            raw = line.rstrip("\n\r")
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            email = stripped.split("----")[0].strip().lower()
            if email in targets:
                deleted += 1
            else:
                kept.append(raw)
        with open(EMAILS_FILE, "w", encoding="utf-8") as f:
            if kept:
                f.write("\n".join(kept) + "\n")
        # 同步清理 meta
        meta = _load_emails_meta()
        for t in targets:
            meta.pop(t, None)
        _save_emails_meta(meta)
        total = len(_existing_emails())
        return {"ok": True, "deleted": deleted, "total": total, "mode": "full"}
    else:
        # 平台级删除：仅删凭据文件
        result = _delete_platform_credentials(platform, targets)
        return {
            "ok": True,
            "platform": platform,
            "mode": "platform_only",
            "deleted_files": result["deleted_files"],
            "cleaned_accounts": result["cleaned_accounts"],
            "cleaned_uploads": result["cleaned_uploads"],
        }


@app.post("/api/mailpool/recycle-reserved")
async def api_mailpool_recycle_reserved(request: Request):
    """回收各平台 used 文件中处于 reserved 状态的邮箱（注册未完成被锁死的）。
    body: {"platforms": ["grok", ...]}  # 缺省回收全部平台
    """
    running = sum(1 for rec in RUNS.values() if not rec["done"])
    if running:
        return JSONResponse(
            {"ok": False, "error": f"当前有 {running} 个任务运行中，请先停止任务再回收"},
            status_code=409,
        )
    try:
        data = await request.json()
    except Exception:
        data = {}
    platforms = (data or {}).get("platforms") or None
    from common.emails import recycle_reserved
    result = await asyncio.to_thread(recycle_reserved, platforms)
    return {"ok": True, "result": result}


# ============================================================ Kiro → OmniRoute 导入
@app.post("/api/kiro/omni-import")
async def api_kiro_omni_import(request: Request):
    """把选中的 Kiro 账号导入到 OmniRoute。
    OmniRoute API:
      1. POST /api/auth/login {"password": "..."} → 获取 auth_token cookie
      2. POST /api/oauth/kiro/import {"refreshToken": "...", "region": "...", "clientId": "...", "clientSecret": "..."} → 逐个导入
    body: {"emails": ["email1@outlook.com", ...]}
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    emails = (data or {}).get("emails") or []
    if not emails:
        return JSONResponse({"ok": False, "msg": "未选择任何账号"})

    omni_url = _read_config_val("OMNIROUTE_URL", "").strip().rstrip("/")
    omni_password = _read_config_val("OMNIROUTE_PASSWORD", "").strip()
    if not omni_url:
        return JSONResponse({"ok": False, "msg": "未配置 OMNIROUTE_URL，请到环境配置页填写"})

    token_dir = os.path.join(
        ROOT, _read_config_val("TOKEN_OUTPUT_DIR", "tokens"), "kiro"
    )

    import glob as _glob
    accounts = []
    skipped = []
    for email in emails:
        token_path = os.path.join(token_dir, f"{email}.account.json")
        if not os.path.isfile(token_path):
            local = email.split("@")[0].split("+")[0]
            matches = _glob.glob(os.path.join(token_dir, f"{local}*.account.json"))
            if matches:
                token_path = matches[0]
            else:
                skipped.append(f"{email}: token 文件不存在")
                continue
        try:
            with open(token_path, encoding="utf-8") as f:
                token_data = json.load(f)
            refresh_token = token_data.get("refreshToken", "")
            if not refresh_token:
                skipped.append(f"{email}: 缺少 refreshToken")
                continue
            account = {
                "email": email,
                "refreshToken": refresh_token,
                "region": token_data.get("region", "us-east-1"),
            }
            client_id = token_data.get("clientId")
            client_secret = token_data.get("clientSecret")
            if client_id and client_secret:
                account["clientId"] = client_id
                account["clientSecret"] = client_secret
            accounts.append(account)
        except Exception as e:
            skipped.append(f"{email}: {e}")

    if not accounts:
        return JSONResponse({"ok": False, "msg": "没有可导入的账号", "skipped": skipped})

    try:
        import requests as _requests
        session = _requests.Session()

        # 1. 登录 OmniRoute 获取 auth cookie
        login_resp = session.post(
            f"{omni_url}/api/auth/login",
            json={"password": omni_password},
            timeout=15,
        )
        if not login_resp.ok:
            try:
                err_data = login_resp.json()
                raw_err = err_data.get("error", "") if isinstance(err_data, dict) else ""
                if isinstance(raw_err, dict):
                    err = raw_err.get("message", str(raw_err))
                else:
                    err = str(raw_err)
            except Exception:
                err = ""
            return JSONResponse({
                "ok": False,
                "msg": f"OmniRoute 登录失败: HTTP {login_resp.status_code} {err}",
                "skipped": skipped,
            })

        # 2. 逐个导入 Kiro 账号
        success_count = 0
        errors = []
        for acc in accounts:
            import_body = {
                "refreshToken": acc["refreshToken"],
                "region": acc.get("region", "us-east-1"),
            }
            if acc.get("clientId") and acc.get("clientSecret"):
                import_body["clientId"] = acc["clientId"]
                import_body["clientSecret"] = acc["clientSecret"]

            import_resp = session.post(
                f"{omni_url}/api/oauth/kiro/import",
                json=import_body,
                timeout=30,
            )
            if import_resp.ok:
                success_count += 1
            else:
                try:
                    err_data = import_resp.json()
                    raw_err = err_data.get("error", "") if isinstance(err_data, dict) else ""
                    if isinstance(raw_err, dict):
                        err_msg = raw_err.get("message", str(raw_err))
                    else:
                        err_msg = str(raw_err)
                except Exception:
                    err_msg = ""
                errors.append(f"{acc['email']}: {err_msg or f'HTTP {import_resp.status_code}'}")

        if success_count > 0:
            msg = f"成功导入 {success_count}/{len(accounts)} 个账号到 OmniRoute"
            if errors:
                msg += f"（{len(errors)} 个失败: {'; '.join(errors)}）"
            return JSONResponse({
                "ok": True,
                "msg": msg,
                "created": success_count,
                "total": len(accounts),
                "skipped": skipped,
                "errors": errors,
            })
        else:
            return JSONResponse({
                "ok": False,
                "msg": f"导入全部失败: {'; '.join(errors)}",
                "skipped": skipped,
                "errors": errors,
            })
    except Exception as e:
        return JSONResponse({
            "ok": False,
            "msg": f"请求 OmniRoute 失败: {e}",
            "skipped": skipped,
        })


# ============================================================ Grok → Sub2API 导入
@app.post("/api/assets/sub2api-import")
async def api_assets_sub2api_import(request: Request):
    """把选中的 Grok 账号导入到 Sub2API。
    读取 sso.json → upload_sub2api_grok (sso-to-oauth)
    body: {"emails": [...], "platform": "grok"}
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    emails = (data or {}).get("emails") or []
    platform = str((data or {}).get("platform", "")).strip().lower()
    if not emails:
        return JSONResponse({"ok": False, "msg": "未选择任何账号"})
    if platform != "grok":
        return JSONResponse({"ok": False, "msg": f"仅支持 grok 平台, 收到: {platform}"})

    sub2api_url = _read_config_val("SUB2API_URL", "").strip()
    sub2api_email = _read_config_val("SUB2API_EMAIL", "").strip()
    sub2api_password = _read_config_val("SUB2API_PASSWORD", "").strip()
    if not sub2api_url:
        return JSONResponse({"ok": False, "msg": "未配置 SUB2API_URL，请到环境配置页填写"})

    token_dir = os.path.join(
        ROOT, _read_config_val("TOKEN_OUTPUT_DIR", "tokens"), platform
    )

    import glob as _glob
    accounts = []
    skipped = []
    for email in emails:
        token_path = os.path.join(token_dir, f"{email}.sso.json")
        if not os.path.isfile(token_path):
            local = email.split("@")[0].split("+")[0]
            matches = _glob.glob(os.path.join(token_dir, f"{local}*.sso.json"))
            if matches:
                token_path = matches[0]
            else:
                skipped.append(f"{email}: sso 文件不存在")
                continue
        try:
            with open(token_path, encoding="utf-8") as f:
                token_data = json.load(f)
            sso = str(token_data.get("sso", "")).strip()
            if not sso:
                skipped.append(f"{email}: 缺少 sso")
                continue
            accounts.append({"email": email, "sso": sso})
        except Exception as e:
            skipped.append(f"{email}: {e}")

    if not accounts:
        return JSONResponse({"ok": False, "msg": "没有可导入的账号", "skipped": skipped})

    try:
        from common.uploaders import upload_sub2api_grok

        sub2api_grok_group = _read_config_val("SUB2API_GROK_GROUP", "grok").strip()
        sub2api_grok_proxy_id_raw = _read_config_val("SUB2API_GROK_PROXY_ID", "0").strip()
        try:
            sub2api_grok_proxy_id = int(sub2api_grok_proxy_id_raw) if sub2api_grok_proxy_id_raw else 0
        except ValueError:
            sub2api_grok_proxy_id = 0

        # 参照 register_grok_http.py：导入前先找一个能过 grok CF 的干净 Clash 节点
        local_proxy = ""
        try:
            from common import proxy_switch
            proxy_mode = proxy_switch.proxy_mode()
            if proxy_mode in ("clash_auto", "clash_fixed"):
                node = proxy_switch.find_working_node(
                    test_url="https://accounts.x.ai/sign-up?redirect=grok-com",
                    warmup_url="https://console.x.ai/home",
                    required_markers=("/_next/static/chunks/", "self.__next_f.push"),
                    verbose=False,
                )
                if node:
                    local_proxy = proxy_switch.effective_proxy_url().strip()
                else:
                    return JSONResponse({
                        "ok": False,
                        "msg": "没有可用的 Clash 节点能通过 grok.com Cloudflare 校验，请切换节点后重试",
                        "skipped": skipped,
                    })
            else:
                local_proxy = proxy_switch.effective_proxy_url().strip()
        except Exception:
            local_proxy = ""
            try:
                from common.proxy_switch import effective_proxy_url
                local_proxy = effective_proxy_url().strip()
            except Exception:
                pass

        success_count = 0
        errors = []
        for acc in accounts:
            ok, msg = upload_sub2api_grok(
                sub2api_url, sub2api_email, sub2api_password,
                sub2api_grok_group, acc["sso"], account_email=acc["email"],
                proxy_id=sub2api_grok_proxy_id or None,
                local_proxy=local_proxy,
            )
            if ok:
                success_count += 1
                try:
                    from common.token_upload_state import mark_uploaded
                    mark_uploaded("grok", "sub2api", acc["email"])
                except Exception:
                    pass
            else:
                errors.append(f"{acc['email']}: {msg}")

        if success_count > 0:
            msg = f"成功导入 {success_count}/{len(accounts)} 个账号到 Sub2API"
            if errors:
                msg += f"（{len(errors)} 个失败: {'; '.join(errors)}）"
            return JSONResponse({
                "ok": True,
                "msg": msg,
                "created": success_count,
                "total": len(accounts),
                "skipped": skipped,
                "errors": errors,
            })
        else:
            return JSONResponse({
                "ok": False,
                "msg": f"导入全部失败: {'; '.join(errors)}",
                "skipped": skipped,
                "errors": errors,
            })
    except Exception as e:
        return JSONResponse({
            "ok": False,
            "msg": f"请求 Sub2API 失败: {e}",
            "skipped": skipped,
        })


# ============================================================ sms-man 接码助手
def _gmail_service_default():
    return _read_config_val("SMSMAN_APP_ID_GMAIL", "") or "google"


@app.post("/api/sms/rent")
async def api_sms_rent(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    service = (data or {}).get("service") or _gmail_service_default()
    country = str((data or {}).get("country") or "0")
    prefer_multi = (data or {}).get("prefer_multi", True)
    if not _read_config_val("SMSMAN_TOKEN", ""):
        return {"ok": False, "msg": "未配置 SMSMAN_TOKEN，请到配置页填写"}
    try:
        from common import sms
        res = await asyncio.to_thread(sms.smsman_rent, service, country, bool(prefer_multi), "", ())
    except Exception as e:
        return {"ok": False, "msg": f"租号异常: {str(e)[:120]}"}
    if not res:
        return {"ok": False, "msg": f"租号失败(服务 '{service}' 无货/余额不足/服务名错)。可在配置页测试 sms-man，或换服务名"}
    phone, pkey, can_multi = res
    rented_at = time.time()
    SMS_RENTS[pkey] = {"phone": phone, "rented_at": rented_at, "codes": [], "service": service, "can_multi": can_multi}
    return {"ok": True, "phone": phone, "pkey": pkey, "service": service, "can_multi": can_multi, "ttl": SMS_RENT_TTL}


@app.post("/api/sms/code")
async def api_sms_code(request: Request):
    data = await request.json()
    pkey = (data or {}).get("pkey")
    rec = SMS_RENTS.get(pkey)
    if not rec:
        return {"ok": False, "msg": "无此租号(可能已释放)"}
    elapsed = time.time() - rec["rented_at"]
    if elapsed > SMS_RENT_TTL:
        return {"ok": False, "expired": True, "msg": "号码已超 20 分钟租期，请重新获取号码"}
    try:
        from common import sms
        since = rec["codes"][-1] if rec["codes"] else None
        # 留出余量不超过剩余租期
        budget = int(min(90, max(15, SMS_RENT_TTL - elapsed)))
        code = await asyncio.to_thread(sms.smsman_peek_code, pkey, budget, 5, False, since)
    except Exception as e:
        return {"ok": False, "msg": f"取码异常: {str(e)[:120]}"}
    if not code:
        return {"ok": False, "msg": "暂未收到新验证码(可稍后再点)", "codes": rec["codes"],
                "elapsed": int(elapsed)}
    if code not in rec["codes"]:
        rec["codes"].append(code)
    return {"ok": True, "code": code, "codes": rec["codes"], "elapsed": int(elapsed)}


@app.post("/api/sms/release")
async def api_sms_release(request: Request):
    data = await request.json()
    pkey = (data or {}).get("pkey")
    if pkey in SMS_RENTS:
        try:
            from common import sms
            await asyncio.to_thread(sms._smsman_release, pkey)
        except Exception:
            pass
        SMS_RENTS.pop(pkey, None)
    return {"ok": True}


@app.get("/api/sms/rents")
def api_sms_rents():
    now = time.time()
    out = []
    for pkey, rec in list(SMS_RENTS.items()):
        elapsed = now - rec["rented_at"]
        if elapsed > SMS_RENT_TTL + 60:
            SMS_RENTS.pop(pkey, None)  # 过期太久自动清理
            continue
        out.append({"pkey": pkey, "phone": rec["phone"], "service": rec.get("service"),
                    "can_multi": rec.get("can_multi", False),
                    "codes": rec["codes"], "elapsed": int(elapsed),
                    "remain": max(0, int(SMS_RENT_TTL - elapsed))})
    return {"rents": out, "ttl": SMS_RENT_TTL}


@app.get("/api/sms/custom")
async def api_custom_sms_get():
    from common import custom_sms

    return await asyncio.to_thread(custom_sms.summary)


@app.post("/api/sms/custom")
async def api_custom_sms_import(request: Request):
    data = await request.json()
    text = str((data or {}).get("text") or "")
    if not text.strip():
        return JSONResponse({"error": "请粘贴至少一个号码和记录 URL"}, status_code=400)
    if len(text) > 1_000_000:
        return JSONResponse({"error": "自定义号码批量内容不能超过 1 MB"}, status_code=413)
    if len([line for line in text.splitlines() if line.strip()]) > 1000:
        return JSONResponse({"error": "单批最多导入 1000 个号码"}, status_code=400)
    from common import custom_sms

    return await asyncio.to_thread(custom_sms.import_text, text)


@app.get("/", response_class=HTMLResponse)
def index():
    return open(os.path.join(WEBUI, "static", "index.html"), encoding="utf-8").read()


@app.post("/api/update")
def api_update():
    global UPDATE_PROCESS, UPDATE_LOG_HANDLE, UPDATE_RESULT_PATH
    status = _update_status()
    if status["status"] == "running":
        return JSONResponse({"ok": False, "error": "更新已经在进行中", "update": status}, status_code=409)
    running = sum(1 for rec in RUNS.values() if not rec["done"])
    if running:
        return JSONResponse(
            {"ok": False, "error": f"当前有 {running} 个任务运行中，请先停止任务", "update": status},
            status_code=409,
        )

    command = _update_script()
    if not command:
        return JSONResponse(
            {"ok": False, "error": "当前安装方式不包含可用的自动更新程序，请下载最新 Release", "update": status},
            status_code=501,
        )

    data_root = os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or ROOT
    log_dir = os.path.join(data_root, "runtime")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "update.log")
    UPDATE_RESULT_PATH = os.path.join(log_dir, "update-result.json")
    try:
        os.remove(UPDATE_RESULT_PATH)
    except FileNotFoundError:
        pass
    if UPDATE_LOG_HANDLE:
        UPDATE_LOG_HANDLE.close()
    UPDATE_LOG_HANDLE = open(log_path, "a", encoding="utf-8")
    child_env = _update_child_env()
    command = _update_script(UPDATE_RESULT_PATH)
    process_options = {
        "cwd": (
            os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
            if getattr(sys, "frozen", False)
            else ROOT
        ),
        "env": child_env,
        "stdin": subprocess.DEVNULL,
        "stdout": UPDATE_LOG_HANDLE,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        process_options["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        process_options["start_new_session"] = True
    try:
        UPDATE_PROCESS = subprocess.Popen(command, **process_options)
    except Exception as exc:
        UPDATE_LOG_HANDLE.close()
        UPDATE_LOG_HANDLE = None
        UPDATE_PROCESS = None
        UPDATE_STATE.update({
            "status": "failed",
            "message": f"更新启动失败: {str(exc)[:160]}",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        return JSONResponse({"ok": False, "error": UPDATE_STATE["message"]}, status_code=500)

    UPDATE_STATE.update({
        "status": "running",
        "message": "正在下载并安装最新版本",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    return JSONResponse({"ok": True, "update": _update_status()}, status_code=202)


@app.get("/api/status")
def api_status():
    provider = _fingerprint_provider()
    browser_headers = {}
    browser_verify_tls = True
    if provider in {"bundled", "embedded", "local", "custom", "chrome", "chromium"}:
        from common.bundled_browser import find_browser_path

        bb = find_browser_path()
        provider_label = "custom" if provider in {"custom", "chrome", "chromium"} else "bundled"
    elif provider in {"adspower", "ads_power", "ads"}:
        bb = _read_config_val("ADSPOWER_API", "http://127.0.0.1:50325")
        provider_label = "adspower"
    elif provider in {"custom_api", "api"}:
        bb = _read_config_val("CUSTOM_BROWSER_API", "")
        provider_label = "custom_api"
        key = (_read_config_val("CUSTOM_BROWSER_API_KEY", "") or _read_config_val("CUSTOM_BROWSER_API_TOKEN", "")).strip()
        if key:
            header = _read_config_val("CUSTOM_BROWSER_API_AUTH_HEADER", "Authorization").strip() or "Authorization"
            prefix = _read_config_val("CUSTOM_BROWSER_API_AUTH_PREFIX", "Bearer ", allow_empty=True)
            if prefix.strip().lower() in {"bearer", "token", "basic", "apikey", "api-key"}:
                prefix = prefix.strip() + " "
            browser_headers = {header: f"{prefix}{key}"}
        extra_headers = _read_config_val("CUSTOM_BROWSER_API_HEADERS", "").strip()
        if extra_headers:
            try:
                value = json.loads(extra_headers)
                if isinstance(value, dict):
                    browser_headers.update({str(item_key): str(item_value) for item_key, item_value in value.items()})
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        browser_verify_tls = _read_config_val("CUSTOM_BROWSER_API_VERIFY_TLS", "true").strip().lower() in {
            "1", "true", "yes", "on"
        }
    else:
        bb = _read_config_val("BITBROWSER_API", "http://127.0.0.1:54345")
        provider_label = "bitbrowser"
    mode = "clash_auto"
    proxy = ""
    network = False
    node = None
    try:
        from common import proxy_switch as ps
        mode = ps.proxy_mode()
        proxy = ps.effective_proxy_url()
        node = ps.current_node()
        network = bool(proxy) if mode == "residential" else (True if mode == "direct" else _test_clash()[0])
    except Exception:
        node = None
    return {
        "pid": os.getpid(),
        "version": WEBUI_VERSION,
        "root": ROOT,
        "data_root": os.environ.get("REG_FACTORY_DATA_DIR") or ROOT,
        "bitbrowser": os.path.isfile(bb) if provider_label in {"bundled", "custom"} else _http_alive(
            bb, headers=browser_headers, verify_tls=browser_verify_tls
        ),
        "browser_provider": provider_label,
        "clash": network,
        "network": network,
        "proxy_mode": mode,
        "direct_proxy": mode == "residential" and bool(proxy),
        "k12": _k12_alive(),
        "chatgpt_plus": _plus_status()["ready"],
        "node": node,
        "running": sum(1 for r in RUNS.values() if not r["done"]),
        "update": _update_status(),
    }


def _proxy_panel_data(include_nodes=False):
    from common import proxy_switch as ps

    config = {key: _read_config_val(key, "") for key in _PROXY_ENV_KEYS}
    config["PROXY_MODE"] = ps.proxy_mode()
    for platform in ("OUTLOOK", "CLAUDE", "CHATGPT", "GROK", "KIRO"):
        config[f"{platform}_PROXY_MODE"] = config[f"{platform}_PROXY_MODE"] or "inherit"
    config["CLASH_API"] = config["CLASH_API"] or "http://127.0.0.1:9097"
    config["CLASH_PROXY"] = config["CLASH_PROXY"] or "http://127.0.0.1:7897"
    config["CLASH_GROUP"] = config["CLASH_GROUP"] or "GLOBAL"
    config["REG_FACTORY_PROXY_ROTATE_METHOD"] = config["REG_FACTORY_PROXY_ROTATE_METHOD"] or "GET"
    config["REG_FACTORY_RESIDENTIAL_TRAFFIC_MODE"] = config["REG_FACTORY_RESIDENTIAL_TRAFFIC_MODE"] or "balanced"
    config["REG_FACTORY_MAX_CONCURRENCY"] = config["REG_FACTORY_MAX_CONCURRENCY"] or "10"
    config["REG_FACTORY_ALLOW_SHARED_EGRESS"] = config["REG_FACTORY_ALLOW_SHARED_EGRESS"] or "false"
    config["CHATGPT_RESIDENTIAL_ROTATE_RETRIES"] = config["CHATGPT_RESIDENTIAL_ROTATE_RETRIES"] or "3"
    config["REG_FACTORY_PROXY_POOL"] = config["REG_FACTORY_PROXY_POOL"].replace(",", "\n")
    nodes = []
    if include_nodes or config["PROXY_MODE"] in {"clash_auto", "clash_fixed"}:
        try:
            nodes = ps.available_clash_nodes(config["CLASH_GROUP"])
        except Exception:
            nodes = []
    try:
        current = ps.current_node()
    except Exception:
        current = None
    return {
        "config": config,
        "nodes": nodes,
        "current": current,
        "effective_proxy": ps.effective_proxy_url(),
        "routes": {
            platform: ps.proxy_mode(ps.platform_environment(os.environ, platform))
            for platform in ("outlook", "claude", "chatgpt", "grok", "kiro")
        },
    }


def _proxy_error_message(exc, proxy_values=()):
    from common import direct_proxy

    message = str(exc).strip() or type(exc).__name__
    for raw in proxy_values:
        raw = str(raw or "").strip()
        if not raw:
            continue
        try:
            spec = direct_proxy.parse_proxy(raw)
        except (TypeError, ValueError):
            continue
        if spec is None:
            continue
        message = message.replace(raw, spec.server)
        for secret in (spec.username, spec.password):
            if secret:
                message = message.replace(secret, "***")
    return message[:240]


@app.get("/api/proxy")
def api_proxy_get(nodes: bool = False):
    try:
        return _proxy_panel_data(include_nodes=nodes)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": _proxy_error_message(exc)}, status_code=500
        )


@app.post("/api/proxy")
async def api_proxy_set(request: Request):
    from common import direct_proxy

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON request"}, status_code=400)
    incoming = (data or {}).get("config") or {}
    updates = {key: str(incoming.get(key) or "").strip() for key in _PROXY_ENV_KEYS}
    mode = updates["PROXY_MODE"] or "clash_auto"
    if mode not in {"clash_auto", "clash_fixed", "residential"}:
        return JSONResponse({"ok": False, "error": "不支持的代理模式"}, status_code=400)
    updates["PROXY_MODE"] = mode
    platform_modes = {
        platform: updates[f"{platform.upper()}_PROXY_MODE"] or "inherit"
        for platform in ("outlook", "claude", "chatgpt", "grok", "kiro")
    }
    for platform, platform_mode in platform_modes.items():
        if platform_mode not in {"inherit", "clash_auto", "clash_fixed", "residential"}:
            return JSONResponse(
                {"ok": False, "error": f"{platform} 的代理模式无效"}, status_code=400
            )
        updates[f"{platform.upper()}_PROXY_MODE"] = platform_mode
    pool_values = [
        item.strip()
        for item in updates["REG_FACTORY_PROXY_POOL"].replace("\r", "").replace(",", "\n").replace(";", "\n").split("\n")
        if item.strip()
    ]
    try:
        if updates["REG_FACTORY_PROXY"]:
            direct_proxy.parse_proxy(updates["REG_FACTORY_PROXY"])
        for value in pool_values:
            direct_proxy.parse_proxy(value)
        for key in ("REG_FACTORY_PLUS_LINK_PROXY_OVERRIDE", "REG_FACTORY_PLUS_BIND_PROXY_OVERRIDE"):
            if updates[key]:
                direct_proxy.parse_proxy(updates[key])
        for key in ("REG_FACTORY_PLUS_LINK_ROUTE", "REG_FACTORY_PLUS_BIND_ROUTE"):
            route = updates[key].lower()
            if route and route != "residential" and route != "clash" and not route.startswith("clash:"):
                raise ValueError(f"invalid Plus route: {updates[key]}")
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": f"住宅代理格式错误: {exc}"}, status_code=400)
    all_modes = {mode, *platform_modes.values()}
    if "clash_fixed" in all_modes and not updates["CLASH_FIXED_NODE"]:
        return JSONResponse({"ok": False, "error": "固定节点模式必须选择 CLASH_FIXED_NODE"}, status_code=400)
    if "residential" in all_modes and not (updates["REG_FACTORY_PROXY"] or pool_values):
        return JSONResponse({"ok": False, "error": "动态住宅 IP 模式至少需要一个代理地址"}, status_code=400)
    method = updates["REG_FACTORY_PROXY_ROTATE_METHOD"] or "GET"
    if method.upper() not in {"GET", "POST"}:
        return JSONResponse({"ok": False, "error": "换 IP 接口方法只能是 GET 或 POST"}, status_code=400)
    updates["REG_FACTORY_PROXY_ROTATE_METHOD"] = method.upper()
    traffic_mode = updates["REG_FACTORY_RESIDENTIAL_TRAFFIC_MODE"] or "balanced"
    if traffic_mode not in {"off", "balanced", "aggressive", "extreme"}:
        return JSONResponse({"ok": False, "error": "住宅流量模式无效"}, status_code=400)
    updates["REG_FACTORY_RESIDENTIAL_TRAFFIC_MODE"] = traffic_mode
    try:
        maximum = int(updates["REG_FACTORY_MAX_CONCURRENCY"] or "10")
    except ValueError:
        return JSONResponse({"ok": False, "error": "最大并发数必须是整数"}, status_code=400)
    if not 1 <= maximum <= 100:
        return JSONResponse({"ok": False, "error": "最大并发数必须在 1 到 100 之间"}, status_code=400)
    updates["REG_FACTORY_MAX_CONCURRENCY"] = str(maximum)
    shared = updates["REG_FACTORY_ALLOW_SHARED_EGRESS"].lower()
    if shared not in {"", "0", "1", "false", "true", "no", "yes", "off", "on"}:
        return JSONResponse({"ok": False, "error": "允许共享出口的配置值无效"}, status_code=400)
    updates["REG_FACTORY_ALLOW_SHARED_EGRESS"] = (
        "true" if shared in {"1", "true", "yes", "on"} else "false"
    )
    try:
        updates["CHATGPT_RESIDENTIAL_ROTATE_RETRIES"] = str(max(1, int(updates["CHATGPT_RESIDENTIAL_ROTATE_RETRIES"] or "3")))
    except ValueError:
        return JSONResponse({"ok": False, "error": "ChatGPT 轮换上限必须是整数"}, status_code=400)
    updates["REG_FACTORY_PROXY_POOL"] = ",".join(pool_values)

    try:
        if not os.path.isfile(ENV_PATH) and os.path.isfile(ENV_EXAMPLE):
            shutil.copy(ENV_EXAMPLE, ENV_PATH)
        _write_env_file(ENV_PATH, updates)
        _apply_saved_env(updates)
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": "Network configuration could not be saved: "
                + _proxy_error_message(
                    exc, [updates["REG_FACTORY_PROXY"], *pool_values]
                ),
            },
            status_code=500,
        )
    try:
        from common import proxy_switch as ps
        applied = await asyncio.to_thread(ps.ensure_proxy_mode)
        return {"ok": True, "applied": applied, **_proxy_panel_data()}
    except Exception as exc:
        return {
            "ok": False,
            "saved": True,
            "error": f"配置已保存，但当前未能应用: {str(exc)[:160]}",
            **_proxy_panel_data(),
        }


def _proxy_target_env(platform: str = "") -> dict:
    normalized = str(platform or "").strip().lower()
    if normalized and normalized not in {"outlook", "claude", "chatgpt", "grok", "kiro"}:
        raise ValueError("测试平台仅支持 outlook、claude、chatgpt、grok、kiro")
    return _child_env(normalized)


@app.post("/api/proxy/rotate")
async def api_proxy_rotate(platform: str = ""):
    try:
        from common import proxy_switch as ps
        target_env = _proxy_target_env(platform)
        result = await asyncio.to_thread(ps.rotate_proxy, None, target_env)
        result["platform"] = platform or "global"
        return result
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:180]}, status_code=400)


@app.post("/api/proxy/test")
async def api_proxy_test(platform: str = ""):
    try:
        target_env = _proxy_target_env(platform)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": _proxy_error_message(exc)}, status_code=400
        )

    def _test():
        from common import direct_proxy
        from common import proxy_switch as ps

        proxy = ps.effective_proxy_url(target_env)
        if not proxy:
            raise RuntimeError("当前模式没有配置有效代理")
        from curl_cffi import requests as creq

        attempts = 3 if ps.proxy_mode(target_env) == "residential" else 1
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                response = creq.get(
                    "https://api.ipify.org?format=json",
                    impersonate="chrome131",
                    proxies={"http": proxy, "https": proxy},
                    timeout=20,
                )
                response.raise_for_status()
                ip = response.json().get("ip") or response.text.strip()
                return ip, attempt
            except Exception as exc:
                last_error = str(exc).replace(proxy, direct_proxy.redact_proxy(proxy))
                spec = direct_proxy.parse_proxy(proxy)
                for secret in (spec.username, spec.password):
                    if secret:
                        last_error = last_error.replace(secret, "***")
                last_error = last_error[:180]
        raise RuntimeError(last_error or "住宅代理出口检测失败")

    try:
        from common import proxy_switch as ps
        ip, attempts = await asyncio.to_thread(_test)
        return {
            "ok": True,
            "ip": ip,
            "attempts": attempts,
            "platform": platform or "global",
            "mode": ps.proxy_mode(target_env),
            "node": ps.current_node(environ=target_env),
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:180]}, status_code=400)


@app.get("/api/env")
def api_env_get():
    # 若无 .env 用模板兜底
    cur = _parse_env_file(ENV_PATH)
    if not cur and os.path.isfile(ENV_EXAMPLE):
        cur = _parse_env_file(ENV_EXAMPLE)
    groups = []
    for g in schema.ENV_SCHEMA:
        items = []
        for it in g["items"]:
            items.append({
                "key": it["key"],
                "label": it.get("label", it["key"]),
                "value": cur.get(it["key"], ""),
                "required": it.get("required", False),
                "secret": it.get("secret", False),
                "help": it.get("help", ""),
                "default": it.get("default", ""),
                "type": it.get("type", "str"),
                "choices": it.get("choices", []),
                "datalist": it.get("datalist", []),
                "advanced": it.get("advanced", False),
                "smart": it.get("smart", False),
            })
        groups.append({
            "group": g["group"],
            "notice": g.get("notice", ""),
            "notice_level": g.get("notice_level", ""),
            "tests": g.get("tests", []),
            "items": items,
        })
    return {"groups": groups, "env_exists": os.path.isfile(ENV_PATH)}


@app.post("/api/env")
async def api_env_set(request: Request):
    data = await request.json()
    updates = data.get("env") or {}
    # 只接受 schema 里声明的 key，避免写入垃圾
    allowed = set(schema.env_keys())
    updates = {k: ("" if v is None else str(v)) for k, v in updates.items() if k in allowed}
    if not os.path.isfile(ENV_PATH) and os.path.isfile(ENV_EXAMPLE):
        # 首次保存：以模板为底
        import shutil
        shutil.copy(ENV_EXAMPLE, ENV_PATH)
    _write_env_file(ENV_PATH, updates)
    _apply_saved_env(updates)
    return {"ok": True, "saved": len(updates), "effective_now": True}


def _build_cmd(script, args):
    """把前端提交的 args(dict) 按 schema 拼成命令行 list。"""
    cmd = [sys.executable]
    if getattr(sys, "frozen", False):
        cmd += ["-u", "--task", script["file"]]
    else:
        cmd += ["-u", os.path.join(ROOT, script["file"])]
    positional = []
    by_flag = {a["flag"]: a for a in script["args"]}
    for flag, spec in by_flag.items():
        if flag not in args:
            continue
        val = args[flag]
        typ = spec["type"]
        if spec.get("positional"):
            if val not in (None, "", []):
                positional.append(str(val))
            continue
        if typ == "bool":
            if val:
                cmd.append(flag)
        elif typ == "multi":
            if val:
                cmd.append(flag)
                cmd.extend(str(v) for v in val)
        else:
            if val not in (None, "", []):
                cmd.append(flag)
                cmd.append(str(val))
    cmd.extend(positional)
    return cmd


def _child_env(platform: str = ""):
    """构造新任务环境；保存后的 .env 无需重启 WebUI 即可生效。"""
    env = dict(os.environ)
    saved_env = _parse_env_file(ENV_PATH)
    managed_keys = set(saved_env)
    if os.path.isfile(ENV_EXAMPLE):
        managed_keys.update(_parse_env_file(ENV_EXAMPLE))
    # Do not leak values loaded from an earlier .env after the active file
    # changes or removes them. Explicit startup values keep precedence.
    for key in managed_keys:
        if key not in BOOT_ENV or key in _LIVE_ENV_KEYS:
            if key in saved_env:
                env[key] = saved_env[key]
            else:
                env.pop(key, None)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Child tasks can spawn other platform workers; retain this marker so Grok
    # avoids navigating the registration browser away for an optional Device Flow.
    env["REG_FACTORY_WEBUI_TASK"] = "1"
    try:
        from common import proxy_switch
        if not platform:
            env.pop("REG_FACTORY_PLATFORM", None)
        env = proxy_switch.platform_environment(env, platform)
        proxy = proxy_switch.effective_proxy_url(env)
    except Exception:
        proxy = ""
    if proxy:
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = env["https_proxy"] = proxy
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
    return env


def _managed_task_files():
    return {
        str(item.get("file") or "").replace("\\", "/").lower()
        for item in schema.SCRIPTS
        if item.get("file")
    }


def _is_managed_task_process(command_line, executable_path=""):
    """Match only reg-factory task workers, never the WebUI or browser processes."""
    command = str(command_line or "").replace("\\", "/").lower()
    executable = str(executable_path or "").replace("\\", "/").lower()
    if not command:
        return False
    task_files = _managed_task_files()
    if executable.endswith("/reg-factory.exe"):
        return "--task" in command and any(task in command for task in task_files)
    if not executable.endswith(("/python.exe", "/pythonw.exe", "/python", "/python3")):
        return False
    allowed_roots = {
        os.path.abspath(ROOT).replace("\\", "/").lower(),
        os.path.abspath(os.environ.get("REG_FACTORY_DATA_DIR") or ROOT)
        .replace("\\", "/").lower(),
    }
    return any(root in command for root in allowed_roots) and any(
        task in command for task in task_files
    )


def _list_orphaned_task_processes():
    if os.name != "nt":
        return []
    ps_script = (
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8-sig",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return []
        payload = json.loads(completed.stdout.strip())
        rows = payload if isinstance(payload, list) else [payload]
    except Exception:
        return []
    matches = []
    for row in rows:
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if pid in {0, os.getpid()}:
            continue
        if _is_managed_task_process(row.get("CommandLine"), row.get("ExecutablePath")):
            matches.append({"pid": pid, "command": row.get("CommandLine") or ""})
    return matches


def _pid_exists(pid):
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def _terminate_process_tree(pid):
    pid = int(pid or 0)
    if pid <= 0 or pid == os.getpid():
        return False
    if not _pid_exists(pid):
        return True
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode == 0 or not _pid_exists(pid)
    try:
        os.killpg(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    return not _pid_exists(pid)


def _cleanup_registered_browser_profiles(owner=None):
    """Close and remove profiles created by reg-factory child tasks.

    BitBrowser launches Chrome outside the Python process tree, so taskkill on
    the task PID alone cannot close those windows. The registry contains only
    profiles created by this project and is safe to process on Stop All.
    """
    try:
        from bitbrowser import BitBrowser
        from common.browser_registry import active_profiles, unregister
        records = active_profiles(owner=owner)
    except Exception:
        return {"closed": 0, "failed": []}
    if owner is None:
        # Global cleanup also adopts generated profiles created by older builds
        # before the per-run registry was introduced.
        try:
            browser = BitBrowser()
            listed = browser.list_browsers(page=0, page_size=200)
            known = {str(item.get("id") or "") for item in records}
            for item in (listed.get("data", {}).get("list") or []):
                name = str(item.get("name") or "").strip().lower()
                remark = str(item.get("remark") or "").strip().lower()
                generated_name = name.startswith((
                    "outlook_loop_", "chatgpt_", "claude_", "grok_", "kiro_", "github_", "mail_",
                ))
                generated_remark = "outlook reg loop" in remark or "reg-factory" in remark
                if (generated_name or generated_remark) and str(item.get("id") or "") not in known:
                    records.append({"id": item.get("id"), "api_base": getattr(browser, "api_base", "")})
        except Exception:
            pass
    closed = 0
    failed = []
    for record in records:
        profile_id = str(record.get("id") or "").strip()
        if not profile_id:
            continue
        try:
            browser = BitBrowser(api_base=record.get("api_base") or None)
            try:
                browser.close_browser(profile_id)
            except Exception:
                pass
            browser.delete_browser(profile_id)
            unregister(profile_id)
            closed += 1
        except Exception as exc:
            failed.append({"id": profile_id, "error": str(exc)[:160]})
    return {"closed": closed, "failed": failed}


async def _start_managed_run(cmd, sid, task_env, task_cwd):
    os.makedirs(task_cwd, exist_ok=True)
    task_env = dict(task_env)
    task_env.setdefault("REG_FACTORY_RUN_ID", f"webui-{uuid.uuid4().hex}")
    process_options = (
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=task_cwd, env=task_env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        **process_options,
    )
    _run_seq[0] += 1
    run_id = f"r{_run_seq[0]}"
    rec = {"proc": proc, "lines": [], "done": False, "stopped": False,
           "returncode": None, "script": sid,
           "cmd": " ".join(cmd), "started": time.strftime("%H:%M:%S"),
           "run_owner": task_env["REG_FACTORY_RUN_ID"],
           "keep_on_fail": "--keep-on-fail" in cmd}
    RUNS[run_id] = rec

    def _sanitize_line(value):
        line = str(value or "")
        if sid not in {"plus_codex_import", "plus_protocol_batch"}:
            return line
        # OTPs and rented numbers must not be copied into the WebUI log stream.
        line = re.sub(r"\b\d{6}\b", "******", line)
        line = re.sub(r"(?<!\d)\+(\d{8,15})\b", lambda match: "+***" + match.group(1)[-4:], line)
        line = re.sub(r"\bM\.[A-Za-z0-9*!._-]{24,}\b", "[redacted-token]", line)
        line = re.sub(
            r"(?i)(access[_-]?token|authorization|refresh[_-]?token|card|cvc)([=:])[^\s,}]+",
            r"\1\2[redacted]",
            line,
        )
        return line

    async def _pump():
        try:
            async for raw in proc.stdout:
                rec["lines"].append(_sanitize_line(raw.decode("utf-8", "replace").rstrip("\n")))
                if len(rec["lines"]) > 5000:
                    rec["lines"] = rec["lines"][-4000:]
        except Exception as e:
            rec["lines"].append(f"[webui] 读取输出异常: {e}")
        finally:
            await proc.wait()
            rec["returncode"] = proc.returncode
            rec["lines"].append(f"[webui] 进程结束 exit={proc.returncode}")
            keep_failed_profiles = (
                rec["keep_on_fail"] and proc.returncode != 0 and not rec["stopped"]
            )
            if not keep_failed_profiles:
                cleanup = await asyncio.to_thread(
                    _cleanup_registered_browser_profiles, rec["run_owner"]
                )
                if cleanup["closed"] or cleanup["failed"]:
                    rec["lines"].append(
                        f"[webui] 浏览器环境清理 closed={cleanup['closed']} failed={len(cleanup['failed'])}"
                    )
            rec["done"] = True
            sensitive_input = rec.get("sensitive_input_path")
            if sensitive_input:
                with contextlib.suppress(OSError):
                    os.unlink(sensitive_input)

    asyncio.create_task(_pump())
    return {"run_id": run_id, "cmd": rec["cmd"]}


@app.post("/api/run")
async def api_run(request: Request):
    data = await request.json()
    sid = data.get("script")
    args = data.get("args") or {}
    script = schema.script_by_id(sid)
    if not script:
        return JSONResponse({"error": f"未知脚本: {sid}"}, status_code=400)
    task_env = _child_env(script.get("platform", ""))
    task_env["REG_FACTORY_RUN_ID"] = f"webui-{uuid.uuid4().hex}"
    try:
        from common import proxy_switch
        await asyncio.to_thread(proxy_switch.ensure_proxy_mode, task_env)
    except Exception as exc:
        return JSONResponse({"error": f"网络出口配置未应用: {str(exc)[:160]}"}, status_code=400)
    cmd = _build_cmd(script, args)
    task_cwd = os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or ROOT
    return await _start_managed_run(cmd, sid, task_env, task_cwd)


@app.get("/api/logs/{run_id}")
async def api_logs(run_id: str):
    rec = RUNS.get(run_id)
    if not rec:
        return JSONResponse({"error": "无此任务"}, status_code=404)

    async def _stream():
        idx = 0
        while True:
            lines = rec["lines"]
            while idx < len(lines):
                yield f"data: {lines[idx]}\n\n"
                idx += 1
            if rec["done"] and idx >= len(rec["lines"]):
                result = json.dumps(
                    {
                        "returncode": rec["returncode"],
                        "stopped": rec["stopped"],
                    },
                    ensure_ascii=False,
                )
                yield f"event: done\ndata: {result}\n\n"
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/stop/{run_id}")
async def api_stop(run_id: str):
    rec = RUNS.get(run_id)
    if not rec:
        return JSONResponse({"error": "无此任务"}, status_code=404)
    if not rec["done"]:
        rec["stopped"] = True
        stopped = await asyncio.to_thread(_terminate_process_tree, rec["proc"].pid)
        owner = rec.get("run_owner")
        browser_cleanup = (
            await asyncio.to_thread(_cleanup_registered_browser_profiles, owner)
            if owner
            else {"closed": 0, "failed": []}
        )
        return {
            "ok": stopped and not browser_cleanup["failed"],
            "stopped": 1 if stopped else 0,
            "browser_profiles": browser_cleanup,
        }
    return {"ok": True, "stopped": 0}


@app.post("/api/stop-all")
async def api_stop_all():
    tracked = {}
    for rec in RUNS.values():
        if rec.get("done"):
            continue
        rec["stopped"] = True
        pid = int(getattr(rec.get("proc"), "pid", 0) or 0)
        if pid > 0:
            tracked[pid] = rec

    discovered = await asyncio.to_thread(_list_orphaned_task_processes)
    targets = set(tracked)
    orphaned = 0
    for item in discovered:
        pid = int(item.get("pid") or 0)
        if pid > 0 and pid not in targets:
            targets.add(pid)
            orphaned += 1

    stopped = 0
    failed = []
    for pid in sorted(targets):
        if await asyncio.to_thread(_terminate_process_tree, pid):
            stopped += 1
        else:
            failed.append(pid)
    browser_cleanup = await asyncio.to_thread(_cleanup_registered_browser_profiles)
    return {
        "ok": not failed,
        "stopped": stopped,
        "tracked": len(tracked),
        "orphaned": orphaned,
        "failed": failed,
        "browser_profiles": browser_cleanup,
    }


@app.on_event("startup")
async def startup_local_services():
    global K12_START_TASK
    auto_start = _read_config_val("K12_AUTO_START", "1").strip().lower() not in {"0", "false", "no", "off"}
    if auto_start and not _k12_alive():
        K12_START_TASK = asyncio.create_task(_start_k12_service())


@app.on_event("shutdown")
async def shutdown_local_services():
    global K12_START_TASK
    if K12_START_TASK and not K12_START_TASK.done():
        K12_START_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await K12_START_TASK
    K12_START_TASK = None
    await _stop_k12_service()
    await asyncio.to_thread(_stop_plus_service_sync)
    await asyncio.to_thread(_cleanup_registered_browser_profiles)


_ensure_proxy_env()
app.mount("/static", StaticFiles(directory=os.path.join(WEBUI, "static")), name="static")
app.mount("/assets", StaticFiles(directory=os.path.join(ROOT, "assets")), name="assets")
