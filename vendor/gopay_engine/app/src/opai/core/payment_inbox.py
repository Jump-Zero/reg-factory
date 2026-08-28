"""Payment Inbox: 服务端 + 客户端 + 存储 + HTML 视图。

**用途**：opai-team manual-paypal 模式下不再本地起浏览器付款，而是把
``(account_name, account_email, plan_kind, checkout_url, paypal_url?)`` 推到这个
inbox 服务上；人工浏览器打开服务页面看到待付款列表，去 PayPal 完成付款后
点 "Mark Paid"；本地脚本轮询 ``/api/jobs/<id>`` 看到 ``status=paid`` 才继续
后续 OAuth/CPA 流程。

**为什么同时存 paypal_url 和 checkout_url（用户要求）**：PayPal goto 链接里的
``ba_token`` 几小时就过期；checkout_url（Stripe 结账页）寿命更长。前者过期后
用户可在结账页重新点 PayPal 拿新的 ba_token 继续付。

**架构**：
- 存储：SQLite 单文件 ``<inbox_dir>/payment_inbox.db`` + WAL 模式；启动时自动从旧
  ``payment_inbox.json`` 迁移一次（见 ``_migrate_json_to_sqlite``）。
- 服务：``http.server.ThreadingHTTPServer`` + 自定义 handler（与 ``opai paypal serve`` 一致风格，零新依赖）；
  per-thread SQLite 连接在 ``_InboxHandler.finish`` 里显式关闭，避免请求线程泄漏 connection。
- 客户端：``urllib.request`` 简易封装 POST / GET / PUT。

**安全**：两套互不冲突的认证方式，命中**任一**即放行；都没配则全开放（仅内网用）。
1. **HTTP Basic Auth**：``OPAI_PAYMENT_INBOX_BASIC_USER`` + ``OPAI_PAYMENT_INBOX_BASIC_PASS``，
   浏览器访问 HTML 视图时弹出登录框最直观；未通过返 ``401 + WWW-Authenticate: Basic``。
2. **Token**：``OPAI_PAYMENT_INBOX_TOKEN``，``Authorization: Bearer <token>`` /
   ``X-Auth-Token`` header / ``?token=...`` URL 入参（写 cookie 后续不传） / cookie。
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from .payment_fingerprint import ensure_account_payment_fingerprint, payment_fingerprint_headers
    from .gpt_register_bridge import GptRegisterBridge
except ImportError:  # Allows running this file directly as a script.
    from opai.core.payment_fingerprint import ensure_account_payment_fingerprint, payment_fingerprint_headers
    from opai.core.gpt_register_bridge import GptRegisterBridge

log = logging.getLogger(__name__)

WEB_REWARD_BALANCE_WAIT_SEC = int(os.environ.get("OPAI_GOPAY_WEB_REWARD_WAIT_SEC", "0"))
WEB_REWARD_BALANCE_POLL_SEC = int(os.environ.get("OPAI_GOPAY_WEB_REWARD_POLL_SEC", "10"))
PIN_CHALLENGE_RETRY_DELAYS = tuple(
    int(x.strip())
    for x in os.environ.get("OPAI_GOPAY_PIN_CHALLENGE_RETRY_DELAYS", "20,45").split(",")
    if x.strip()
)
PROJECT_ROOT = Path(__file__).resolve().parents[4]
GPT_REGISTER_ROOT = Path(os.environ.get("OPAI_GPT_REGISTER_ROOT", str(PROJECT_ROOT / "gpt_register"))).expanduser()
_GOPAY_ACCOUNTS_LOCK = threading.RLock()


def _normalize_proxy_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    if "@" in value:
        return f"http://{value}"
    parts = value.split(":")
    if len(parts) >= 4 and parts[1].isdigit():
        host, port = parts[0], parts[1]
        user = parts[2]
        password = ":".join(parts[3:])
        return f"http://{user}:{password}@{host}:{port}"
    return f"http://{value}"


def _default_gopay_proxy() -> str:
    return _normalize_proxy_url(
        os.environ.get("OPAI_GOPAY_DEFAULT_PROXY")
        or os.environ.get("OPAI_GOPAY_REGISTER_PROXY")
        or ""
    )

def _rotate_proxy_sid(proxy: str) -> str:
    if "-sid-" not in proxy:
        return proxy
    import secrets
    sid = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
    return re.sub(r"-sid-[^-:/@]+", f"-sid-{sid}", proxy, count=1)


def _preflight_gopay_proxy(proxy: str) -> dict[str, Any]:
    from opai.core.gojek_client import probe_proxy_egress

    return probe_proxy_egress(_normalize_proxy_url(proxy))


def _proxy_preflight_error(proxy: str, result: dict[str, Any]) -> str:
    from opai.core.gojek_client import mask_proxy_url

    detail = result.get("error") or result.get("raw") or f"HTTP {result.get('status')}"
    return f"代理预检失败: {mask_proxy_url(proxy)} {detail}"


def _mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return value[:2] + "***"
    return value[:4] + "..." + value[-4:]


def _sms_api_status(otp_box: Any = None, include_balance: bool = False) -> dict[str, Any]:
    from opai.core.sms_helpers import get_sms_api_key, load_selected_env_file, sms_api, sms_api_base_url

    load_selected_env_file(("OPAI_SMSBOWER_", "OPAI_HEROSMS_"))
    api_key = get_sms_api_key("")
    env_file = os.environ.get("OPAI_GOPAY_SMS_ENV_FILE", "")
    service = os.environ.get("OPAI_SMSBOWER_SERVICE") or os.environ.get("OPAI_HEROSMS_SERVICE") or "ni"
    country = os.environ.get("OPAI_SMSBOWER_COUNTRY") or os.environ.get("OPAI_HEROSMS_COUNTRY") or "6"
    status: dict[str, Any] = {
        "provider": "SMSBower API",
        "api_base_url": sms_api_base_url(),
        "api_key_configured": bool(api_key),
        "api_key": _mask_secret(api_key),
        "env_file": env_file,
        "service": service,
        "country": country,
        "otp_ttl_sec": getattr(otp_box, "OTP_TTL", 300),
        "pending_otp": otp_box.list_all() if otp_box is not None else {},
    }
    if api_key and include_balance:
        try:
            raw = sms_api(api_key, "getBalance", retries=1)
            status["balance_raw"] = raw
            m = re.search(r"(-?\d+(?:\.\d+)?)", raw)
            if m:
                status["balance"] = m.group(1)
            status["ok"] = not raw.upper().startswith(("BAD_", "ERROR", "NO_"))
        except Exception as exc:
            status["ok"] = False
            status["balance_error"] = str(exc)
    else:
        status["ok"] = bool(api_key)
        if not api_key:
            status["balance_error"] = "SMS API key not configured"
        else:
            status["balance_raw"] = "未刷新"
    return status


def _sms_env_path() -> Path:
    raw = (os.environ.get("OPAI_GOPAY_SMS_ENV_FILE") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return PROJECT_ROOT / "config" / "sms.env"


def _read_env_pairs(path: Path) -> dict[str, str]:
    pairs: dict[str, str] = {}
    if not path.exists():
        return pairs
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            pairs[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return pairs


def _write_sms_config(data: dict[str, Any]) -> dict[str, Any]:
    path = _sms_env_path()
    pairs = _read_env_pairs(path)
    current_key = pairs.get("OPAI_SMSBOWER_API_KEY") or os.environ.get("OPAI_SMSBOWER_API_KEY", "")
    api_key = str(data.get("api_key") or "").strip()
    if api_key:
        pairs["OPAI_SMSBOWER_API_KEY"] = api_key
    elif current_key:
        pairs["OPAI_SMSBOWER_API_KEY"] = current_key
    pairs["OPAI_SMSBOWER_API_KEY_FILE"] = str(data.get("api_key_file") or pairs.get("OPAI_SMSBOWER_API_KEY_FILE") or "").strip()
    pairs["OPAI_SMSBOWER_API_BASE_URL"] = str(data.get("api_base_url") or "https://smsbower.page").strip().rstrip("/") or "https://smsbower.page"
    pairs["OPAI_SMSBOWER_SERVICE"] = str(data.get("service") or "ni").strip() or "ni"
    pairs["OPAI_SMSBOWER_COUNTRY"] = str(data.get("country") or "6").strip() or "6"
    ordered = [
        "OPAI_SMSBOWER_API_KEY",
        "OPAI_SMSBOWER_API_KEY_FILE",
        "OPAI_SMSBOWER_API_BASE_URL",
        "OPAI_SMSBOWER_SERVICE",
        "OPAI_SMSBOWER_COUNTRY",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# 秒到网页保存的短信 API 配置\n"
        + "\n".join(f"{key}={pairs.get(key, '')}" for key in ordered)
        + "\n",
        encoding="utf-8",
    )
    os.environ["OPAI_GOPAY_SMS_ENV_FILE"] = str(path)
    for key in ordered:
        os.environ[key] = pairs.get(key, "")
    # Keep legacy aliases in sync for older helpers.
    os.environ["OPAI_HEROSMS_API_KEY"] = pairs.get("OPAI_SMSBOWER_API_KEY", "")
    os.environ["OPAI_HEROSMS_API_BASE_URL"] = pairs.get("OPAI_SMSBOWER_API_BASE_URL", "")
    os.environ["OPAI_HEROSMS_SERVICE"] = pairs.get("OPAI_SMSBOWER_SERVICE", "")
    os.environ["OPAI_HEROSMS_COUNTRY"] = pairs.get("OPAI_SMSBOWER_COUNTRY", "")
    return {"ok": True, "path": str(path), "api_key_configured": bool(pairs.get("OPAI_SMSBOWER_API_KEY"))}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


PaymentInboxJob = dict[str, Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_inbox_path() -> Path:
    """Storage 路径：默认放在 ``<ROOT_DIR>/config/payment_inbox.json``。"""
    override = (os.environ.get("OPAI_PAYMENT_INBOX_PATH") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    try:
        from opai.config import CONFIG_DIR
        return CONFIG_DIR / "payment_inbox.json"
    except Exception:
        # 退化路径：相对当前目录
        return Path("payment_inbox.json").resolve()


def _default_inbox_db_path() -> Path:
    """SQLite db 路径:同目录下的 ``payment_inbox.db``(取代旧 JSON)。

    优先 ``OPAI_PAYMENT_INBOX_DB_PATH``,否则用 ``_default_inbox_path()`` 的目录 +
    ``payment_inbox.db``。
    """
    override = (os.environ.get("OPAI_PAYMENT_INBOX_DB_PATH") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    json_path = _default_inbox_path()
    return json_path.with_name("payment_inbox.db")


_SCHEMA_VERSION = 3

_SCHEMA_SQL_V1 = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT    PRIMARY KEY NOT NULL,
    account_name    TEXT    NOT NULL DEFAULT '',
    account_email   TEXT    NOT NULL DEFAULT '',
    plan_kind       TEXT    NOT NULL DEFAULT 'team',
    checkout_url    TEXT    NOT NULL DEFAULT '',
    paypal_url      TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'paid', 'cancelled', 'expired')),
    created_at      TEXT    NOT NULL,
    expires_at      TEXT    NOT NULL DEFAULT '',
    paid_at         TEXT    NOT NULL DEFAULT '',
    cancelled_at    TEXT    NOT NULL DEFAULT '',
    claimed_at      TEXT    NOT NULL DEFAULT '',
    notes           TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_email          ON jobs(account_email);
CREATE INDEX IF NOT EXISTS idx_jobs_plan_kind      ON jobs(plan_kind);
"""

# v2:加 ``provider`` / ``provider_url`` 通用支付通道字段。
# - ``provider``: ``paypal`` (默认) / ``gopay`` / 未来其他;无 CHECK 约束以便加新通道不用迁移 schema
# - ``provider_url``: 通用 redirect URL,印尼 GoPay 走 midtrans 的 ``app.midtrans.com/snap/v4/redirection/<id>``
# v1→v2 迁移:把 ``paypal_url`` 同步到 ``provider_url`` 留底,旧字段 paypal_url **保留不删**(向后兼容)。
_SCHEMA_SQL_V2_MIGRATION = """
ALTER TABLE jobs ADD COLUMN provider     TEXT NOT NULL DEFAULT 'paypal';
ALTER TABLE jobs ADD COLUMN provider_url TEXT NOT NULL DEFAULT '';
UPDATE jobs SET provider_url = paypal_url WHERE paypal_url != '';
"""

# v3:加 ``oauth_status`` 字段,跟踪付款后的 OAuth/CPA 续跑状态(用于服务重启时的 resume)。
# - 空串(默认):还没启动 OAuth,或不需要 OAuth 后处理
# - ``in_progress``:正在跑 OAuth/CPA;若 worker 中断重启,resume 入口会重试
# - ``completed``:OAuth + CPA 已落库,subscribe_team 入口看到该状态即整段跳过
# - ``failed``:多次重试仍失败,人工介入(查 notes 字段)
# 无 CHECK 约束,新状态值不用再迁移 schema。
_SCHEMA_SQL_V3_MIGRATION = """
ALTER TABLE jobs ADD COLUMN oauth_status TEXT NOT NULL DEFAULT '';
"""


def _open_connection(path: Path) -> sqlite3.Connection:
    """打开一个 SQLite 连接 + 设置 PRAGMA。

    数据库级 PRAGMA(``journal_mode=WAL``)第一次设置后持久;后续 connection 上设也是 no-op。
    连接级 PRAGMA(``synchronous`` / ``foreign_keys`` / ``busy_timeout``)每个连接都得设。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), isolation_level=None, timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA synchronous = NORMAL")
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def _apply_schema(c: sqlite3.Connection) -> None:
    """根据 ``PRAGMA user_version`` 跑 schema migration。

    v0 → v1: 从空建表 + 索引(初始 SQLite 重构)。
    v1 → v2: 加 ``provider`` / ``provider_url`` 通用支付通道字段。
    v2 → v3: 加 ``oauth_status`` 字段(本次,用于 subscribe_team 重启 resume)。
    """
    cur = c.execute("PRAGMA user_version")
    version = cur.fetchone()[0]
    if version < 1:
        c.executescript(_SCHEMA_SQL_V1)
        c.execute("PRAGMA user_version = 1")
    if version < 2:
        # ALTER TABLE 不能在 BEGIN..COMMIT 里跑(SQLite 不支持事务里改 schema),executescript
        # 自己 implicit-commit 处理。失败时已添加的列下次启动会让 ALTER 抛 "duplicate column",
        # 走 IF NOT EXISTS 兜底。
        try:
            c.executescript(_SCHEMA_SQL_V2_MIGRATION)
        except sqlite3.OperationalError as exc:
            # 上次迁移半成功:列已加但 user_version 没设。容忍,继续置 version。
            if "duplicate column name" not in str(exc).lower():
                raise
            log.warning("payment_inbox: v2 ALTER 部分已生效(%s),跳过 ALTER 直接置 version", exc)
        c.execute("PRAGMA user_version = 2")
    if version < 3:
        try:
            c.executescript(_SCHEMA_SQL_V3_MIGRATION)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
            log.warning("payment_inbox: v3 ALTER 部分已生效(%s),跳过 ALTER 直接置 version", exc)
        c.execute("PRAGMA user_version = 3")


def _migrate_json_to_sqlite(json_path: Path, c: sqlite3.Connection) -> int:
    """一次性把旧 ``payment_inbox.json`` 内容导入 SQLite。

    成功后把 JSON 改名为 ``<json_path>.migrated.<ts>`` 留底,**不删除**(用户可手动清理)。
    JSON 不存在或为空则什么也不做,返回 0。
    返回:迁入的 job 数。
    """
    if not json_path.exists():
        return 0
    try:
        with json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        log.exception("payment_inbox: 读旧 JSON 失败,跳过迁移 %s", json_path)
        return 0
    jobs: list[dict[str, Any]] = []
    if isinstance(data, list):
        jobs = [j for j in data if isinstance(j, dict)]
    elif isinstance(data, dict) and isinstance(data.get("jobs"), list):
        jobs = [j for j in data["jobs"] if isinstance(j, dict)]
    if not jobs:
        # 空 JSON 也改名,避免下次启动重跑迁移
        _rename_migrated(json_path)
        return 0

    cols = (
        "id", "account_name", "account_email", "plan_kind",
        "checkout_url", "paypal_url", "provider", "provider_url",
        "status", "created_at",
        "expires_at", "paid_at", "cancelled_at", "claimed_at", "notes",
    )
    rows = []
    for j in jobs:
        # 缺字段用空串/默认补,避免 CHECK 失败
        status = (j.get("status") or "pending")
        if status not in ("pending", "paid", "cancelled", "expired"):
            log.warning("payment_inbox: 迁移时遇到未知 status=%r,改 pending", status)
            status = "pending"
        # JSON 时代没有 provider/provider_url 概念,统一回填 paypal
        paypal_url_v = j.get("paypal_url") or ""
        provider_v = (j.get("provider") or "paypal").strip().lower() or "paypal"
        # 老 JSON 没 provider_url,用 paypal_url 兜底;若 JSON 已经写过 provider_url(理论上不会)就尊重它
        provider_url_v = j.get("provider_url") or paypal_url_v
        rows.append((
            j.get("id") or uuid.uuid4().hex[:16],
            j.get("account_name") or "",
            j.get("account_email") or "",
            j.get("plan_kind") or "team",
            j.get("checkout_url") or "",
            paypal_url_v,
            provider_v,
            provider_url_v,
            status,
            j.get("created_at") or _now_iso(),
            j.get("expires_at") or "",
            j.get("paid_at") or "",
            j.get("cancelled_at") or "",
            j.get("claimed_at") or "",
            j.get("notes") or "",
        ))
    placeholders = ",".join(["?"] * len(cols))
    c.execute("BEGIN")
    try:
        c.executemany(
            f"INSERT OR REPLACE INTO jobs ({','.join(cols)}) VALUES ({placeholders})",
            rows,
        )
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    log.info("payment_inbox: 已从 %s 迁入 %d 条 job 到 SQLite", json_path, len(rows))
    _rename_migrated(json_path)
    return len(rows)


def _rename_migrated(json_path: Path) -> None:
    """JSON 迁移完毕改名为 ``<name>.migrated.<unix_ts>``,留底不删。"""
    ts = int(time.time())
    dest = json_path.with_suffix(json_path.suffix + f".migrated.{ts}")
    try:
        json_path.rename(dest)
        log.info("payment_inbox: 旧 JSON 已重命名为 %s", dest.name)
    except OSError as exc:
        log.warning("payment_inbox: 重命名旧 JSON 失败(%s),下次启动可能重跑迁移", exc)


class InboxStore:
    """SQLite 单文件 + WAL 模式的 inbox 存储。

    - 路径:默认 ``<inbox_dir>/payment_inbox.db``,可由 ``OPAI_PAYMENT_INBOX_DB_PATH`` 覆盖
    - 启动时检测同目录旧 ``payment_inbox.json``,**一次性自动迁移**(见 ``_migrate_json_to_sqlite``)
    - per-thread connection(``threading.local``):``ThreadingHTTPServer`` 每请求一个线程
    - WAL 模式 reader/writer 不互阻塞;高并发 ``GET /api/jobs`` 不再排队
    """

    def __init__(self, path: Path | None = None):
        """``path`` 兼容老 JSON 路径或新 SQLite 路径:
        - ``.json`` 后缀 → 使用同目录 ``payment_inbox.db``,并把 JSON 当迁移源
        - ``.db`` 后缀(或别的)→ 直接当 SQLite 路径
        - ``None`` → 用 ``_default_inbox_db_path()``,JSON 源用 ``_default_inbox_path()``
        """
        if path is None:
            self.path = _default_inbox_db_path()
            self._legacy_json_path = _default_inbox_path()
        elif path.suffix == ".json":
            # 兼容老 caller(测试 fixture / 旧代码)传 .json 路径
            self.path = path.with_suffix(".db")
            self._legacy_json_path = path
        else:
            self.path = path
            self._legacy_json_path = path.with_suffix(".json")

        self._tls = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False

    def _conn(self) -> sqlite3.Connection:
        """Per-thread connection;首次调用时建表 + 触发 JSON 迁移。"""
        c = getattr(self._tls, "conn", None)
        if c is None:
            c = _open_connection(self.path)
            self._ensure_schema_once(c)
            self._tls.conn = c
        return c

    def _ensure_schema_once(self, c: sqlite3.Connection) -> None:
        """全局只跑一次:apply schema + JSON 迁移。"""
        with self._init_lock:
            if self._initialized:
                return
            _apply_schema(c)
            try:
                _migrate_json_to_sqlite(self._legacy_json_path, c)
            except Exception:
                log.exception("payment_inbox: JSON 迁移异常(继续启动)")
            self._initialized = True

    def close_thread_connection(self) -> None:
        """Close the current thread's SQLite connection if open.

        Call this from request-handler ``finish()`` so per-request threads in
        ``ThreadingHTTPServer`` don't leak SQLite connections (each thread holds
        its own via ``threading.local``).
        """
        c = getattr(self._tls, "conn", None)
        if c is None:
            return
        try:
            c.close()
        except Exception:
            log.debug("payment_inbox: thread conn close failed", exc_info=True)
        try:
            del self._tls.conn
        except AttributeError:
            pass

    def list(
        self,
        *,
        status: str | None = None,
        email: str | None = None,
        plan_kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: str = "created_desc",
    ) -> tuple[list[PaymentInboxJob], int]:
        c = self._conn()
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if email:
            # 大小写不敏感子串(LIKE + LOWER)
            where.append("LOWER(account_email) LIKE ?")
            params.append(f"%{email.lower()}%")
        if plan_kind:
            where.append("plan_kind = ?")
            params.append(plan_kind)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        # ORDER 子句:created_at 主键 + rowid 二级(insertion order tie-break,
        # 防止同一 microsecond 内多条 created_at 相等时排序不稳定)
        direction = "ASC" if order == "created_asc" else "DESC"
        order_sql = f"ORDER BY created_at {direction}, rowid {direction}"

        # total(过滤后总数,不含 limit/offset)
        total = c.execute(
            f"SELECT COUNT(*) AS n FROM jobs {where_sql}", params
        ).fetchone()["n"]

        if limit is not None and limit > 0:
            page_sql = f"SELECT * FROM jobs {where_sql} {order_sql} LIMIT ? OFFSET ?"
            page_params = list(params) + [limit, max(0, offset)]
        elif offset > 0:
            # offset 但无 limit:用 LIMIT -1 OFFSET N(SQLite 里 LIMIT -1 = 不限)
            page_sql = f"SELECT * FROM jobs {where_sql} {order_sql} LIMIT -1 OFFSET ?"
            page_params = list(params) + [offset]
        else:
            page_sql = f"SELECT * FROM jobs {where_sql} {order_sql}"
            page_params = list(params)

        rows = c.execute(page_sql, page_params).fetchall()
        jobs = [dict(r) for r in rows]
        return jobs, int(total)

    def get(self, job_id: str) -> PaymentInboxJob | None:
        c = self._conn()
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def create(
        self,
        *,
        account_name: str,
        account_email: str,
        plan_kind: str,
        checkout_url: str,
        paypal_url: str | None = None,
        provider: str = "paypal",
        provider_url: str | None = None,
        expires_at: str | None = None,
        notes: str = "",
    ) -> PaymentInboxJob:
        # ``provider_url`` 默认从 paypal_url 兜底,保证旧 caller(只传 paypal_url)行为不变;
        # 新 caller(GoPay 等)显式传 ``provider`` + ``provider_url``,paypal_url 留空。
        eff_provider = (provider or "paypal").strip().lower() or "paypal"
        eff_paypal_url = paypal_url or ""
        if provider_url is None:
            eff_provider_url = eff_paypal_url if eff_provider == "paypal" else ""
        else:
            eff_provider_url = provider_url or ""
        c = self._conn()
        for _attempt in range(3):
            jid = uuid.uuid4().hex[:16]
            now = _now_iso()
            try:
                c.execute(
                    """
                    INSERT INTO jobs (
                        id, account_name, account_email, plan_kind,
                        checkout_url, paypal_url, provider, provider_url, status,
                        created_at, expires_at, paid_at, cancelled_at,
                        claimed_at, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, '', '', '', ?)
                    """,
                    (
                        jid, account_name, account_email, plan_kind,
                        checkout_url, eff_paypal_url, eff_provider, eff_provider_url,
                        now, expires_at or "", notes,
                    ),
                )
                break
            except sqlite3.IntegrityError as exc:
                # PRIMARY KEY 冲突(uuid 撞库,理论 ~0)→ 重生成
                if "UNIQUE constraint" not in str(exc):
                    raise
                continue
        else:
            raise RuntimeError("payment_inbox: 3 次 uuid 都撞库,放弃")
        # 读出来返(保持原 JSON 实现的"返回完整 job dict"行为)
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        return dict(row)

    # 允许 patch 写的字段(不含 id — 不可变;``created_at`` 在白名单内仅供 prune 测试
    # 伪造历史;生产代码不应该改 created_at)
    _PATCH_ALLOWED_FIELDS = frozenset({
        "account_name", "account_email", "plan_kind",
        "checkout_url", "paypal_url", "provider", "provider_url", "status",
        "expires_at", "paid_at", "cancelled_at", "claimed_at", "notes",
        "oauth_status",
        "created_at",
    })

    def patch(self, job_id: str, updates: dict[str, Any]) -> PaymentInboxJob | None:
        c = self._conn()
        clean = {k: v for k, v in updates.items() if k in self._PATCH_ALLOWED_FIELDS}
        if not clean:
            # 啥都没传 → 直接返当前 job(行为兼容)
            return self.get(job_id)
        cols = list(clean.keys())
        set_sql = ", ".join(f"{col}=?" for col in cols)
        params = [clean[k] for k in cols] + [job_id]
        row = c.execute(
            f"UPDATE jobs SET {set_sql} WHERE id=? RETURNING *",
            params,
        ).fetchone()
        return dict(row) if row else None

    def delete(self, job_id: str) -> bool:
        c = self._conn()
        cur = c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cur.rowcount > 0

    def expire_overdue(self) -> int:
        """把 ``status='pending'`` 且 ``expires_at`` < 当前时间的 job 标 expired。返回处理数。"""
        c = self._conn()
        now_iso = _now_iso()
        cur = c.execute(
            """
            UPDATE jobs
            SET status = 'expired'
            WHERE status = 'pending'
              AND expires_at != ''
              AND expires_at < ?
            """,
            (now_iso,),
        )
        return cur.rowcount

    def prune_old(self, retention_sec: float, *, keep_pending: bool = True) -> int:
        """删除 created_at 早于 ``now - retention_sec`` 的**终态** job(paid/cancelled/expired)。

        ``keep_pending=True``(默认)— 即便 created_at 很老,pending 不删(等真正付款)。
        ``retention_sec <= 0`` → no-op,返回 0(与原行为一致)。
        """
        if retention_sec <= 0:
            return 0
        c = self._conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=retention_sec)).isoformat()
        if keep_pending:
            sql = """
                DELETE FROM jobs
                WHERE status IN ('paid', 'cancelled', 'expired')
                  AND created_at != ''
                  AND created_at < ?
            """
        else:
            sql = """
                DELETE FROM jobs
                WHERE created_at != '' AND created_at < ?
            """
        cur = c.execute(sql, (cutoff,))
        return cur.rowcount

    def claim_next_pending(
        self,
        *,
        prefer_paypal_url: bool = False,
        prefer_oldest: bool = False,
        ttl_sec: float = 60.0,
        provider: str = "",
    ) -> "PaymentInboxJob | None":
        """**原子地** select + claim 下一条 pending job(单条 SQL,不会双 claim)。

        Args:
            prefer_paypal_url: 历史名;v2 起语义为"有可点的支付链接"——
                只选 ``paypal_url`` 或 ``provider_url`` 非空的。都没有则放弃(返 None)。
            prefer_oldest: True 用 ``created_at ASC`` 排序;否则 ``DESC``。
            ttl_sec: claim TTL 秒数。``claimed_at`` 早于 ``now - ttl_sec`` 的 job 视为可重新 claim。
            provider: 可选，只 claim 指定 provider 的 job（如 ``"gopay"``）。
        """
        c = self._conn()
        now_iso = _now_iso()
        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(seconds=ttl_sec)
        ).isoformat()

        order_sql = "ASC" if prefer_oldest else "DESC"
        pp_filter = "AND (paypal_url != '' OR provider_url != '')" if prefer_paypal_url else ""
        provider_filter = f"AND provider = '{provider}'" if provider else ""

        sql = f"""
            UPDATE jobs SET claimed_at = ?
            WHERE id = (
                SELECT id FROM jobs
                WHERE status = 'pending' {pp_filter} {provider_filter}
                  AND (claimed_at = '' OR claimed_at < ?)
                ORDER BY created_at {order_sql}
                LIMIT 1
            )
            RETURNING *
        """
        row = c.execute(sql, (now_iso, cutoff_iso)).fetchone()
        return dict(row) if row else None

    def set_status_if_pending(
        self,
        job_id: str,
        new_status: str,
    ) -> "PaymentInboxJob | None":
        """幂等状态转移:仅当当前 ``status='pending'`` 时改;否则返回当前 job 不动。

        - ``new_status='paid'`` → 同一事务设 ``paid_at=now``
        - ``new_status='cancelled'`` → 设 ``cancelled_at=now``
        - ``new_status='expired'`` → 不设额外时间戳

        多线程并发同时调本方法,只有一个会真改,其它返回首改后的最终 job(各字段一致)。
        """
        if new_status not in ("paid", "cancelled", "expired"):
            raise ValueError(f"unsupported status: {new_status!r}")
        c = self._conn()
        now = _now_iso()
        if new_status == "paid":
            sql = """
                UPDATE jobs SET status='paid', paid_at=?
                WHERE id=? AND status='pending'
                RETURNING *
            """
            params = (now, job_id)
        elif new_status == "cancelled":
            sql = """
                UPDATE jobs SET status='cancelled', cancelled_at=?
                WHERE id=? AND status='pending'
                RETURNING *
            """
            params = (now, job_id)
        else:  # expired
            sql = """
                UPDATE jobs SET status='expired'
                WHERE id=? AND status='pending'
                RETURNING *
            """
            params = (job_id,)
        row = c.execute(sql, params).fetchone()
        if row:
            return dict(row)
        # rowcount=0:status 已不再 pending(被别人改过)→ 返回当前 job
        return self.get(job_id)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def _server_token() -> str:
    return (os.environ.get("OPAI_PAYMENT_INBOX_TOKEN") or "").strip()


def _server_retention_sec() -> float:
    """终态 job（paid/cancelled/expired）保留秒数；超出后台线程每小时清一次。
    默认 ``604800`` = 7 天，最小 3600（1 小时）；``OPAI_PAYMENT_INBOX_RETENTION_SEC`` 可调，
    设 ``0`` 关闭自动清理（永远保留，需要手动 DELETE）。
    """
    raw = (os.environ.get("OPAI_PAYMENT_INBOX_RETENTION_SEC") or "").strip()
    try:
        v = float(raw) if raw else 7 * 24 * 3600.0
    except (TypeError, ValueError):
        v = 7 * 24 * 3600.0
    if v <= 0:
        return 0.0
    return max(3600.0, v)


def _server_claim_ttl_sec() -> float:
    """已被某用户「点开支付链接」(claim) 的 pending job 在 list 视图里临时隐藏的秒数。
    防止多人浏览面板同时点同一条 job 造成竞争。默认 60s，env ``OPAI_PAYMENT_INBOX_CLAIM_TTL_SEC`` 可调（最小 5）。

    仅在 ``OPAI_PAYMENT_INBOX_CLAIM_BEHAVIOR=hide`` 模式下生效；
    默认 ``sort_bottom`` 模式不隐藏 claim，TTL 不再起作用。
    """
    raw = (os.environ.get("OPAI_PAYMENT_INBOX_CLAIM_TTL_SEC") or "").strip()
    try:
        v = float(raw) if raw else 60.0
    except (TypeError, ValueError):
        v = 60.0
    return max(5.0, v)


def _server_claim_behavior() -> str:
    """``sort_bottom``（默认）：claim 过的 job 排到列表最底，bulk-open 跳过它们；
    用户能继续在底部看到「我点过这条链接」的订单，避免误删 + 防止漏掉「需要手动确认订阅」的边缘 case。

    ``hide``（旧行为）：claim 后 TTL 内隐藏，TTL 过完再回到顶部。

    设 ``OPAI_PAYMENT_INBOX_CLAIM_BEHAVIOR=hide`` 恢复旧行为。
    """
    v = (os.environ.get("OPAI_PAYMENT_INBOX_CLAIM_BEHAVIOR") or "sort_bottom").strip().lower()
    if v in ("hide", "filter", "ttl"):
        return "hide"
    return "sort_bottom"


def _is_job_actively_claimed(job: PaymentInboxJob, ttl_sec: float, now: datetime | None = None) -> bool:
    """判断 job 是否处于"已被 claim 但仍在 TTL 内"——这种状态下 list 视图里隐藏该 job，
    供其它人继续看到的列表里就不会再看到它，避免重复点击。"""
    if (job.get("status") or "") != "pending":
        return False
    cl = (job.get("claimed_at") or "").strip()
    if not cl:
        return False
    try:
        ts = datetime.fromisoformat(cl.replace("Z", "+00:00"))
    except Exception:
        return False
    n = now or datetime.now(timezone.utc)
    return (n - ts).total_seconds() < ttl_sec


def _job_has_claim(job: PaymentInboxJob) -> bool:
    """是否曾经被点开过支付链接（不看 TTL，纯看是否有 ``claimed_at``）。"""
    return (job.get("status") or "") == "pending" and bool((job.get("claimed_at") or "").strip())


def _claim_ts(job: PaymentInboxJob) -> str:
    """供排序用的 claim 时间戳（claim 越新越靠后）。"""
    return (job.get("claimed_at") or "").strip()


def _server_basic_auth() -> tuple[str, str] | None:
    """Returns (user, pass) tuple if both env are set; else None (basic auth disabled)."""
    u = (os.environ.get("OPAI_PAYMENT_INBOX_BASIC_USER") or "").strip()
    p = (os.environ.get("OPAI_PAYMENT_INBOX_BASIC_PASS") or "").strip()
    if u and p:
        return u, p
    return None


def _gopay_accounts_path() -> Path:
    override = (os.environ.get("OPAI_GOPAY_ACCOUNTS_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.cwd() / "config" / "gopay_worker_accounts.json"


def _gopay_envelope_store_path() -> Path:
    override = (os.environ.get("OPAI_GOPAY_ENVELOPE_STORE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _gopay_accounts_path().parent / "envelope_links.json"


def _envelope_manager():
    from opai.core.envelope_manager import EnvelopeManager

    return EnvelopeManager(_gopay_envelope_store_path())


def _list_gopay_envelopes() -> dict[str, Any]:
    mgr = _envelope_manager()
    return {
        "path": str(_gopay_envelope_store_path()),
        "links": [link.to_dict() for link in mgr.links],
    }


def _replace_gopay_envelope_url(url: str) -> dict[str, Any]:
    url = (url or "").strip()
    path = _gopay_envelope_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not url:
        path.write_text("[]\n", encoding="utf-8")
        return {"path": str(path), "links": [], "message": "红包链接已清空"}
    mgr = _envelope_manager()
    old_links = list(mgr.links)
    mgr.links = []
    mgr._save()
    link = mgr.add_url(url)
    if not link:
        mgr.links = old_links
        mgr._save()
        raise ValueError("红包链接解析失败，请确认是 GoPay 节日红包短链")
    return {
        "path": str(path),
        "links": [item.to_dict() for item in mgr.links],
        "message": "红包链接已保存",
    }


def _snap_state_path() -> Path:
    override = (os.environ.get("OPAI_MIDTRANS_SNAP_STATE_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _gopay_accounts_path().parent / "midtrans_snap_state.json"


def _load_snap_states() -> dict[str, dict[str, Any]]:
    path = _snap_state_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.debug("payment_inbox: read snap states failed", exc_info=True)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _write_snap_states(states: dict[str, dict[str, Any]]) -> None:
    path = _snap_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(states, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_gopay_accounts() -> list[dict[str, Any]]:
    raw = _load_gopay_accounts_raw()
    out = []
    changed = False
    for item in raw:
        before_profile = item.get("payment_fingerprint")
        payment_profile = ensure_account_payment_fingerprint(item)
        if before_profile != payment_profile:
            changed = True
        binding_status = str(item.get("midtrans_binding_status") or "").strip()
        binding_message = str(item.get("midtrans_binding_message") or "").strip()
        try:
            balance_value = int(item.get("balance", 0) or 0)
        except Exception:
            balance_value = 0
        if binding_status == "success":
            use_status = "used_success"
            use_label = "已成功"
        elif binding_status == "failed":
            use_status = "used_failed"
            use_label = "失败"
        elif binding_status in {"reserved", "running", "linking", "waiting_otp"}:
            use_status = "reserved"
            use_label = "已绑定/运行中"
        elif str(item.get("midtrans_binding_order_id") or item.get("midtrans_binding_job_id") or "").strip():
            use_status = "reserved"
            use_label = "已绑定"
        elif balance_value < 1:
            use_status = "no_balance"
            use_label = "余额不足"
            if not binding_message:
                binding_message = "0 Rp 不参与自动支付"
        else:
            use_status = "available"
            use_label = "可用"
        out.append({
            "phone": item.get("phone", ""),
            "local": item.get("local", ""),
            "customer_id": item.get("customer_id", "") or item.get("account_id", ""),
            "account_id": item.get("account_id", "") or item.get("customer_id", ""),
            "registered_at": item.get("registered_at", ""),
            "balance": balance_value,
            "activation_id": item.get("activation_id", ""),
            "use_status": use_status,
            "use_label": use_label,
            "use_message": binding_message,
            "midtrans_binding_status": binding_status,
            "midtrans_binding_job_id": item.get("midtrans_binding_job_id", ""),
            "midtrans_binding_order_id": item.get("midtrans_binding_order_id", ""),
            "midtrans_binding_email": item.get("midtrans_binding_email", ""),
            "midtrans_binding_updated_at": item.get("midtrans_binding_updated_at", "") or item.get("midtrans_binding_at", ""),
            "payment_fingerprint": payment_profile,
        })
    if changed:
        _write_gopay_accounts_raw(raw)
    return out


def _load_gopay_accounts_raw() -> list[dict[str, Any]]:
    with _GOPAY_ACCOUNTS_LOCK:
        path = _gopay_accounts_path()
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.debug("payment_inbox: read accounts failed", exc_info=True)
            return []
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]


def _write_gopay_accounts_raw(accounts: list[dict[str, Any]]) -> None:
    with _GOPAY_ACCOUNTS_LOCK:
        path = _gopay_accounts_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(accounts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _find_gopay_account(phone: str) -> tuple[dict[str, Any] | None, int]:
    target = _digits(phone)
    accounts = _load_gopay_accounts_raw()
    for idx, item in enumerate(accounts):
        item_phone = _digits(item.get("phone", ""))
        item_local = _digits(item.get("local", ""))
        if target and (
            target == item_phone
            or (item_local and target == item_local)
            or (item_phone and item_phone.endswith(target))
            or (item_local and target.endswith(item_local))
        ):
            return item, idx
    return None, -1


def _gopay_binding_block_reason(account: dict[str, Any], *, order_id: str = "", job_id: str = "") -> str:
    status = str(account.get("midtrans_binding_status") or "").strip()
    existing_job_id = str(account.get("midtrans_binding_job_id") or "").strip()
    existing_order_id = str(account.get("midtrans_binding_order_id") or "").strip()
    order_id = str(order_id or "").strip()
    job_id = str(job_id or "").strip()
    message = str(account.get("midtrans_binding_message") or "").strip()
    if not (status or existing_job_id or existing_order_id):
        return ""
    if status in {"reserved", "running", "linking", "waiting_otp"}:
        if (order_id and existing_order_id == order_id) or (job_id and existing_job_id == job_id):
            return ""
    if status == "success":
        return f"GoPay 账号已支付成功，不能再次使用: {message or existing_order_id or existing_job_id}"
    if status == "failed":
        return f"GoPay 账号已有失败记录，不能再次使用: {message or existing_order_id or existing_job_id}"
    return f"GoPay 账号已绑定 Midtrans 任务，不能重复使用: {message or existing_order_id or existing_job_id}"


def _refresh_gopay_balance(phone: str) -> dict[str, Any]:
    account, idx = _find_gopay_account(phone)
    if account is None:
        raise ValueError(f"账号不存在: {phone}")
    client = _gopay_client_from_account(account, phone)
    try:
        client.refresh_token()
    except Exception:
        log.debug("balance refresh token failed; trying existing token", exc_info=True)
    resp = client.get_balance()
    if resp.get("status") != 200:
        raise RuntimeError(f"余额查询失败: {resp.get('status')} {str(resp.get('body', ''))[:300]}")
    data = resp.get("body", {}).get("data", [])
    balance = 0
    if isinstance(data, list) and data:
        balance = int(data[0].get("balance", {}).get("value", 0) or 0)
    accounts = _load_gopay_accounts_raw()
    if 0 <= idx < len(accounts):
        ensure_account_payment_fingerprint(accounts[idx])
        accounts[idx]["balance"] = balance
        accounts[idx]["access_token"] = client.auth.access_token or accounts[idx].get("access_token", "")
        accounts[idx]["refresh_token"] = client.auth.refresh_token or accounts[idx].get("refresh_token", "")
        _write_gopay_accounts_raw(accounts)
    return {"phone": account.get("phone") or phone, "balance": balance, "status": resp.get("status")}


def _gopay_payment_sms_active(account: dict[str, Any]) -> bool:
    max_age = max(60, int(os.environ.get("OPAI_AUTO_FLOW_GOPAY_SMS_MAX_AGE_SEC", "900") or "900"))
    registered_at = str(account.get("registered_at") or account.get("added_at") or "").strip()
    if registered_at:
        try:
            ts = datetime.fromisoformat(registered_at.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - ts).total_seconds() > max_age:
                return False
        except Exception:
            log.debug("payment sms age parse failed", exc_info=True)
    aid = str(account.get("activation_id") or account.get("aid") or "").strip()
    if not aid:
        return False
    try:
        from opai.core.sms_helpers import get_sms_api_key, sms_api

        api_key = get_sms_api_key("")
        if not api_key:
            return False
        resp = str(sms_api(api_key, "getStatus", {"id": aid}) or "")
        upper = resp.upper()
        if "NO ACCESS" in upper or "STATUS_CANCEL" in upper or "BAD_KEY" in upper:
            return False
        return True
    except Exception:
        log.debug("payment sms active check failed", exc_info=True)
        return False


def _mark_gopay_sms_done(account: dict[str, Any]) -> None:
    aid = str(account.get("activation_id") or account.get("aid") or "").strip()
    if not aid:
        return
    try:
        from opai.core.sms_helpers import get_sms_api_key, sms_done

        api_key = get_sms_api_key("")
        if api_key:
            sms_done(api_key, aid)
    except Exception:
        log.debug("payment sms_done failed", exc_info=True)


def _reserve_gopay_midtrans_binding(
    phone: str,
    *,
    job_id: str,
    order_id: str = "",
    midtrans_url: str = "",
    email: str = "",
) -> bool:
    with _GOPAY_ACCOUNTS_LOCK:
        accounts = _load_gopay_accounts_raw()
        for account in accounts:
            if str(account.get("phone") or "") != phone:
                continue
            existing_job = str(account.get("midtrans_binding_job_id") or "")
            existing_order = str(account.get("midtrans_binding_order_id") or "")
            existing_status = str(account.get("midtrans_binding_status") or "").strip()
            if existing_status in {"success", "failed"}:
                return False
            same_order = bool(order_id and existing_order and existing_order == order_id)
            same_job = bool(job_id and existing_job and existing_job == job_id)
            if existing_order and existing_order != order_id:
                return False
            if existing_job and existing_job != job_id and not same_order:
                return False
            account["midtrans_binding_status"] = "reserved"
            account["midtrans_binding_job_id"] = job_id
            account["midtrans_binding_order_id"] = order_id
            account["midtrans_binding_url"] = midtrans_url
            account["midtrans_binding_email"] = email
            account["midtrans_binding_at"] = _now_iso()
            account["midtrans_binding_message"] = "已预占 Midtrans 授权链，等待支付"
            _write_gopay_accounts_raw(accounts)
            return True
    return False


def _update_gopay_midtrans_binding_status(phone: str, status: str, *, message: str = "") -> None:
    accounts = _load_gopay_accounts_raw()
    for account in accounts:
        if str(account.get("phone") or "") != phone:
            continue
        account["midtrans_binding_status"] = status
        account["midtrans_binding_updated_at"] = _now_iso()
        if message:
            account["midtrans_binding_message"] = message[:300]
        break
    _write_gopay_accounts_raw(accounts)


def _is_prestart_binding_failure(message: str) -> bool:
    text = str(message or "")
    return (
        "GoPay 账号已绑定 Midtrans 任务" in text
        and "已预占 Midtrans 授权链" in text
    )


def _clear_gopay_midtrans_binding(account: dict[str, Any], *, message: str = "") -> None:
    for key in (
        "midtrans_binding_status",
        "midtrans_binding_job_id",
        "midtrans_binding_order_id",
        "midtrans_binding_url",
        "midtrans_binding_email",
        "midtrans_binding_at",
        "midtrans_binding_updated_at",
    ):
        account.pop(key, None)
    account["midtrans_binding_message"] = message or "预占未进入支付，已释放"


def _normalize_gopay_binding_history() -> None:
    accounts = _load_gopay_accounts_raw()
    changed = False
    snap_states = _load_snap_states()
    auto_jobs: dict[str, dict[str, Any]] = {}
    auto_path = PROJECT_ROOT / "config" / "auto_flow_tasks.json"
    if auto_path.exists():
        try:
            raw = json.loads(auto_path.read_text(encoding="utf-8"))
            auto_jobs = {
                str(row.get("id") or ""): row
                for row in raw.get("jobs", [])
                if isinstance(row, dict) and row.get("id")
            }
        except Exception:
            auto_jobs = {}
    payment_jobs: dict[str, dict[str, Any]] = {}
    payment_path = PROJECT_ROOT / "config" / "payment_tasks.json"
    if payment_path.exists():
        try:
            raw = json.loads(payment_path.read_text(encoding="utf-8"))
            payment_jobs = {
                str(row.get("id") or ""): row
                for row in raw.get("jobs", [])
                if isinstance(row, dict) and row.get("id")
            }
        except Exception:
            payment_jobs = {}
    for account in accounts:
        status = str(account.get("midtrans_binding_status") or "").strip()
        message = str(account.get("midtrans_binding_message") or "").strip()
        if status == "failed" and _is_prestart_binding_failure(message):
            _clear_gopay_midtrans_binding(account, message="预占未进入支付，已释放，可重新使用")
            changed = True
            continue
        if status not in {"reserved", "running", "linking", "waiting_otp"}:
            continue
        job_id = str(account.get("midtrans_binding_job_id") or "").strip()
        history_job = auto_jobs.get(job_id) or payment_jobs.get(job_id)
        if history_job and str(history_job.get("status") or "") in {"failed", "success"}:
            final_status = str(history_job.get("status") or "")
            final_message = str(history_job.get("payment_message") or history_job.get("message") or final_status)[:300]
            if final_status != "success" and _is_prestart_binding_failure(final_message):
                _clear_gopay_midtrans_binding(account, message="预占未进入支付，已释放，可重新使用")
                changed = True
                continue
            account["midtrans_binding_status"] = "success" if final_status == "success" else "failed"
            account["midtrans_binding_message"] = final_message
            account["midtrans_binding_updated_at"] = _now_iso()
            changed = True
            continue
        snap = _extract_midtrans_snap_token(str(account.get("midtrans_binding_url") or ""))
        snap_state = snap_states.get(snap) if snap else None
        snap_status = str((snap_state or {}).get("status") or "").strip()
        if snap_status in {"success", "failed", "fraud_denied"}:
            account["midtrans_binding_status"] = "success" if snap_status == "success" else "failed"
            account["midtrans_binding_message"] = str((snap_state or {}).get("reason") or snap_status)
            account["midtrans_binding_updated_at"] = _now_iso()
            changed = True
    if changed:
        _write_gopay_accounts_raw(accounts)


def _is_transient_pin_challenge_error(resp: dict[str, Any]) -> bool:
    status = int(resp.get("status", 0) or 0)
    text = str(resp.get("body", ""))
    lowered = text.lower()
    return (
        status in (400, 500, 502, 503, 504)
        and (
            "gopay-22107" in lowered
            or "technical error" in lowered
            or "try again after a few minutes" in lowered
            or "we're working on it" in lowered
        )
    )


def _claim_gopay_reward(phone: str) -> dict[str, Any]:
    account, idx = _find_gopay_account(phone)
    if account is None:
        raise ValueError(f"账号不存在: {phone}")
    client = _gopay_client_from_account(account, phone)
    try:
        client.refresh_token()
    except Exception:
        log.debug("reward refresh token failed; trying existing token", exc_info=True)

    messages: list[str] = []

    def warmup(label: str, fn) -> None:
        try:
            resp = fn()
            status = resp.get("status")
            if status in (200, 201, 204):
                messages.append(f"{label}完成")
            else:
                messages.append(f"{label}返回 {status}，继续")
        except Exception as exc:
            log.debug("account warmup failed: %s", label, exc_info=True)
            messages.append(f"{label}异常，继续")

    hook = client.pin_post_registration_hook()
    first_hook_status = int(hook.get("status", 0) or 0)
    if first_hook_status in (200, 201):
        messages.append(f"钱包激活 hook 第 1 次完成: {first_hook_status}")
    else:
        messages.append(f"钱包激活 hook 第 1 次返回 {first_hook_status}，先执行真机初始化")
    warmup("App consent 同步", client.accept_signup_consents)
    warmup("GoPay 首页初始化", client.gopay_home_v3)
    warmup("支付方式初始化", client.gopay_get_profiles)
    warmup("余额初始化", client.gopay_get_balances)
    warmup("钱包卡片余额组件", client.wallet_card_balance)
    warmup("钱包卡片 widget", client.wallet_card_widget)
    warmup("Push Token 绑定", client.update_push_token)
    warmup("Courier Token 初始化", client.courier_token)
    warmup("GoFin Token 初始化", client.gofin_token)
    warmup("安全评分首页刷新", lambda: client.security_meter("gopay_home"))
    warmup("安全评分安全页刷新", lambda: client.security_meter("account_safety_home"))
    warmup(
        "安全提示展示回传",
        lambda: client.security_meter(
            "security_meter",
            view_count=1,
            click_count=0,
            security_aware_identifier="cyber_security_zero_policy",
        ),
    )
    if first_hook_status not in (200, 201):
        try:
            refresh = client.refresh_token()
            messages.append(f"hook 补偿前 token refresh 返回 {refresh.get('status')}")
        except Exception:
            messages.append("hook 补偿前 token refresh 异常，继续补打 hook")
        second_hook = client.pin_post_registration_hook()
        second_hook_status = int(second_hook.get("status", 0) or 0)
        if second_hook_status in (200, 201):
            messages.append(f"钱包激活 hook 第 2 次完成: {second_hook_status}")
            warmup("余额补刷新", client.gopay_get_balances)
            warmup("安全评分补刷新", lambda: client.security_meter("security_meter"))
        else:
            messages.append(f"钱包激活 hook 第 2 次返回 {second_hook_status}，继续等余额但可能不会触发系统赠送")
    messages.append("已执行真机钱包初始化并刷新余额")

    balance_resp = _refresh_gopay_balance(account.get("phone") or phone)
    wait_sec = max(0, WEB_REWARD_BALANCE_WAIT_SEC)
    poll_sec = max(3, WEB_REWARD_BALANCE_POLL_SEC)
    if int(balance_resp.get("balance", 0) or 0) <= 0 and wait_sec > 0:
        messages.append(f"余额暂为 0 Rp，继续轮询 {wait_sec}s")
        deadline = time.time() + wait_sec
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            time.sleep(min(poll_sec, max(0.0, deadline - time.time())))
            balance_resp = _refresh_gopay_balance(account.get("phone") or phone)
            if int(balance_resp.get("balance", 0) or 0) > 0:
                messages.append(f"余额到账: {balance_resp.get('balance', 0)} Rp")
                break
            messages.append(f"余额轮询 {attempt}: 仍为 {balance_resp.get('balance', 0)} Rp")
    elif int(balance_resp.get("balance", 0) or 0) <= 0:
        messages.append("余额暂为 0 Rp，已返回；可稍后再点查余额")
    accounts = _load_gopay_accounts_raw()
    if 0 <= idx < len(accounts):
        ensure_account_payment_fingerprint(accounts[idx])
        accounts[idx]["access_token"] = client.auth.access_token or accounts[idx].get("access_token", "")
        accounts[idx]["refresh_token"] = client.auth.refresh_token or accounts[idx].get("refresh_token", "")
        accounts[idx]["device_token"] = client.device_token or accounts[idx].get("device_token", "")
        accounts[idx]["device_uniqueid"] = client.uniqueid
        accounts[idx]["device_session_id"] = client.session_id
        accounts[idx]["last_reward_claim_at"] = _now_iso()
        _write_gopay_accounts_raw(accounts)
    return {
        "phone": account.get("phone") or phone,
        "claimed": False,
        "balance": balance_resp.get("balance", 0),
        "message": "；".join(messages),
        "claim_status": None,
    }


def _claim_gopay_envelope(phone: str) -> dict[str, Any]:
    account, idx = _find_gopay_account(phone)
    if account is None:
        raise ValueError(f"账号不存在: {phone}")
    mgr = _envelope_manager()
    if not mgr.get_active():
        raise ValueError("没有 active 红包链接，请先在网页保存红包链接")
    client = _gopay_client_from_account(account, phone)
    try:
        client.refresh_token()
    except Exception:
        log.debug("envelope refresh token failed; trying existing token", exc_info=True)
    result = mgr.claim_one(client)
    if not result:
        raise RuntimeError("节日红包没有可领取的 active 链接")
    balance_resp = _refresh_gopay_balance(account.get("phone") or phone)
    accounts = _load_gopay_accounts_raw()
    if 0 <= idx < len(accounts):
        ensure_account_payment_fingerprint(accounts[idx])
        accounts[idx]["access_token"] = client.auth.access_token or accounts[idx].get("access_token", "")
        accounts[idx]["refresh_token"] = client.auth.refresh_token or accounts[idx].get("refresh_token", "")
        accounts[idx]["last_envelope_claim_at"] = _now_iso()
        accounts[idx]["balance"] = balance_resp.get("balance", accounts[idx].get("balance", 0))
        _write_gopay_accounts_raw(accounts)
    ok = result.get("status") in (200, 201) and result.get("body", {}).get("success")
    return {
        "phone": account.get("phone") or phone,
        "claimed": bool(ok),
        "claim_status": result.get("status"),
        "claim_body": result.get("body"),
        "balance": balance_resp.get("balance", 0),
        "message": "节日红包领取完成" if ok else f"节日红包领取失败: {result.get('status')} {str(result.get('body', ''))[:300]}",
        "envelopes": _list_gopay_envelopes().get("links", []),
    }


def _gopay_client_from_account(account: dict[str, Any], phone: str = ""):
    from opai.core.gojek_client import GojekClient

    proxy = _normalize_proxy_url(account.get("proxy", "")) or _default_gopay_proxy()
    client = GojekClient.from_phone(account.get("phone") or phone, proxy=proxy)
    client.auth.access_token = account.get("access_token", "")
    client.auth.refresh_token = account.get("refresh_token", "")
    client.user_uuid = account.get("customer_id", "") or account.get("account_id", "")
    client.auth.account_id = account.get("account_id", "") or account.get("customer_id", "")
    if account.get("device_uniqueid"):
        client.uniqueid = account.get("device_uniqueid", "")
    if account.get("device_token"):
        client.device_token = account.get("device_token", "")
    if account.get("device_session_id"):
        client.session_id = account.get("device_session_id", "")
    return client


def _save_gopay_account_tokens_and_pin(idx: int, client, new_pin: str = "") -> None:
    accounts = _load_gopay_accounts_raw()
    if not (0 <= idx < len(accounts)):
        return
    if client.auth.access_token:
        accounts[idx]["access_token"] = client.auth.access_token
    if client.auth.refresh_token:
        accounts[idx]["refresh_token"] = client.auth.refresh_token
    if new_pin:
        accounts[idx]["pin"] = new_pin
    if getattr(client, "user_uuid", ""):
        accounts[idx]["customer_id"] = client.user_uuid
    if getattr(client.auth, "account_id", ""):
        accounts[idx]["account_id"] = client.auth.account_id
    ensure_account_payment_fingerprint(accounts[idx])
    accounts[idx]["updated_at"] = _now_iso()
    _write_gopay_accounts_raw(accounts)


_HTML_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>OPAI Payment Inbox</title>
<style>
/* GoPay-only product surface: keep registration/login, account pool and payment. */
button[data-view="tasks"],button[data-view="email_orders"],button[data-view="oauth_tokens"],button[data-view="oauth_tasks"],button[data-view="otp"],button[data-view="plus_pool"],
button[data-top-tab="oauth_tokens"],button[data-top-tab="email_orders"],button[data-top-tab="oauth_tasks"],button[data-top-tab="subscription_tasks"],button[data-top-tab="register_tasks"]{display:none!important}
#view-tasks,#view-email_orders,#view-oauth_tokens,#view-oauth_tasks,#view-otp,#view-plus_pool{display:none!important}
#view-register > *:not(.gopay-only){display:none!important}
body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:1em;background:#f7f7f9;color:#222;}
h1{font-size:18px;margin:0 0 0.5em 0;}
.bar{margin-bottom:0.6em;color:#666;font-size:13px;}
table{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.06);}
th,td{padding:8px 10px;border-bottom:1px solid #eee;font-size:13px;vertical-align:top;text-align:left;}
th{background:#fafafa;font-weight:600;}
tr.s-pending{background:#fffbf3;}
tr.s-pending.has-claim{background:#eaeaf2;color:#777;}  /* 已点过支付链接：灰底沉底 */
tr.s-pending.has-claim td b{color:#777;font-weight:500;}
tr.s-pending.has-claim td.urls a{color:#888;text-decoration:line-through;}
tr.s-paid{background:#f3fff5;color:#666;}
tr.s-expired,tr.s-cancelled{background:#f5f5f5;color:#999;}
.status{font-weight:600;text-transform:uppercase;font-size:11px;letter-spacing:.5px;padding:2px 6px;border-radius:3px;}
.s-pending .status{background:#ffd966;color:#664d00;}
.s-paid .status{background:#7fc28b;color:#fff;}
.s-expired .status{background:#bbb;color:#fff;}
.s-cancelled .status{background:#999;color:#fff;}
a{color:#0a58ca;word-break:break-all;}
.small{font-size:11px;color:#888;}
button{margin:0 4px 0 0;padding:4px 10px;border:1px solid #ccc;background:#fff;border-radius:3px;cursor:pointer;font-size:12px;}
button.primary{background:#28a745;color:#fff;border-color:#28a745;}
button.danger{background:#dc3545;color:#fff;border-color:#dc3545;}
button:hover{filter:brightness(0.95);}
.urls a{display:block;margin:2px 0;}
</style>
</head>
<body>
<h1>OPAI Payment Inbox</h1>
<div class="bar">
  <span id="summary">loading…</span>
  &nbsp;|&nbsp;
  状态：
  <select id="filter_status" onchange="resetAndLoad()">
    <option value="pending" selected>pending（待付）</option>
    <option value="">全部</option>
    <option value="paid">paid</option>
    <option value="cancelled">cancelled</option>
    <option value="expired">expired</option>
  </select>
  &nbsp;|&nbsp;
  邮箱包含：<input id="filter_email" oninput="debouncedReload()" placeholder="子串" style="width:160px;">
  &nbsp;|&nbsp;
  每页：
  <select id="page_size" onchange="resetAndLoad()">
    <option value="20">20</option>
    <option value="50" selected>50</option>
    <option value="100">100</option>
    <option value="200">200</option>
  </select>
  &nbsp;|&nbsp;
  <button onclick="bulkOpen('provider_url')" class="primary">批量开 10 个支付链接</button>
  <button onclick="bulkOpen('checkout_url')">批量开 10 个 Checkout</button>
</div>
<div class="bar">
  <button onclick="prevPage()" id="btn_prev">上页</button>
  <span id="page_info">page 1</span>
  <button onclick="nextPage()" id="btn_next">下页</button>
  &nbsp;|&nbsp;
  自动刷新 1s（点过的 60s 内对其他人临时隐藏）
</div>
<table>
<thead><tr>
  <th>账号</th><th>Plan</th><th>状态</th><th>创建</th><th>过期</th><th>支付链接</th><th>操作</th>
</tr></thead>
<tbody id="rows"><tr><td colspan="7">loading…</td></tr></tbody>
</table>
<script>
const TOKEN = (() => {
  // 从 cookie 读 token；URL 里 ?token=... 也写一次 cookie
  const u = new URL(location.href);
  const t = u.searchParams.get('token');
  if (t) { document.cookie = `inbox_token=${t}; path=/; max-age=86400`; u.searchParams.delete('token'); history.replaceState(null,'',u.toString()); return t; }
  const m = document.cookie.match(/inbox_token=([^;]+)/);
  return m ? m[1] : '';
})();
function authHeaders() { return TOKEN ? {'X-Auth-Token': TOKEN} : {}; }
function fmtTs(s){ if(!s)return '-'; try{const d=new Date(s); return d.toLocaleString();}catch{return s;} }
function statusClass(s){ return 's-' + (s||'pending'); }
let _curPage = 0;     // 0-based
let _curTotal = 0;
let _reloadDebounce = null;
let _lastJobs = [];   // 缓存最近一次 list 结果，bulkOpen 同步消费（避免 await fetch 丢失 user gesture）
// 客户端"近期已消费" 黑名单（id → unix ms 过期点）：claim 是 async 火并忘，
// 服务端 claimed_at 落库前下一次 loadJobs 可能把已点过的 job 拉回；这层兜底
// 防止 bulkOpen 重复打开同一条。TTL 设成服务端 claim TTL 的 2 倍 + buffer。
const _recentlyConsumed = new Map();
const _CONSUMED_TTL_MS = 150 * 1000;  // 2.5 分钟，覆盖 server claim TTL=60s + 用户犹豫时间
function _markConsumed(id){ _recentlyConsumed.set(id, Date.now() + _CONSUMED_TTL_MS); }
function _isRecentlyConsumed(id){
  const exp = _recentlyConsumed.get(id);
  if (!exp) return false;
  if (Date.now() < exp) return true;
  _recentlyConsumed.delete(id);
  return false;
}
function resetAndLoad(){ _curPage = 0; loadJobs(); }
function debouncedReload(){ clearTimeout(_reloadDebounce); _reloadDebounce = setTimeout(resetAndLoad, 350); }
function prevPage(){ if (_curPage > 0) { _curPage--; loadJobs(); } }
function nextPage(){
  const limit = parseInt(document.getElementById('page_size').value, 10) || 50;
  if ((_curPage + 1) * limit < _curTotal) { _curPage++; loadJobs(); }
}
function buildQuery(){
  const status = document.getElementById('filter_status').value;
  const email = document.getElementById('filter_email').value.trim();
  const limit = parseInt(document.getElementById('page_size').value, 10) || 50;
  const offset = _curPage * limit;
  const params = new URLSearchParams({limit, offset});
  if (status) params.set('status', status);
  if (email) params.set('email', email);
  return params.toString();
}
async function loadJobs() {
  const r = await fetch('/api/jobs?' + buildQuery(), {headers: authHeaders()});
  if (!r.ok) {
    document.getElementById('rows').innerHTML = `<tr><td colspan=7>读取失败 ${r.status}: ${await r.text()}</td></tr>`;
    return;
  }
  const data = await r.json();
  const jobs = data.jobs || [];
  // 同时从 server 列表里抠掉本会话最近 N 秒已消费过的（防 claim race）
  const filtered = jobs.filter(j => !_isRecentlyConsumed(j.id));
  _lastJobs = filtered.slice();  // 缓存供 bulkOpen 同步使用
  _curTotal = data.total || 0;
  const limit = data.limit || jobs.length || 50;
  const visible = filtered;  // server 已过滤 claim+status；客户端再过本会话黑名单
  // 分页 UI
  const totalPages = Math.max(1, Math.ceil(_curTotal / Math.max(1, limit)));
  document.getElementById('page_info').textContent = `page ${_curPage + 1} / ${totalPages}（命中 ${_curTotal}）`;
  document.getElementById('btn_prev').disabled = _curPage <= 0;
  document.getElementById('btn_next').disabled = !data.has_more;
  document.getElementById('summary').textContent = `本页 ${jobs.length} 条 / 命中 ${_curTotal} 条`;
  document.getElementById('rows').innerHTML = visible.map(j => {
    // claim 标记：服务端已 sort_bottom 把这些排到列表底部，前端只负责加灰底 class +
    // bulkOpen 跳过它们。用户能看到「这条我点过」的提示，避免对已付款的订阅又重复付一次。
    const cls = statusClass(j.status) + (j.claimed_at ? ' has-claim' : '');
    const claimTag = j.claimed_at ? `<div class="small" style="color:#a35">已点过 ${fmtTs(j.claimed_at)}</div>` : '';
    // v3 oauth_status 状态标(只在非空时显示),给 manual-paypal 重启 resume 看
    const oauthTag = j.oauth_status ? `<div class="small" style="color:#246">oauth: ${escapeHtml(j.oauth_status)}</div>` : '';
    return `
    <tr class="${cls}" data-id="${j.id}">
      <td><b>${escapeHtml(j.account_name||'')}</b><div class="small">${escapeHtml(j.account_email||'')}</div>${claimTag}${oauthTag}</td>
      <td>${escapeHtml(j.plan_kind||'')}</td>
      <td><span class="status">${escapeHtml(j.status||'')}</span></td>
      <td class="small">${fmtTs(j.created_at)}</td>
      <td class="small">${fmtTs(j.expires_at)}</td>
      <td class="urls">
        ${(j.provider_url || j.paypal_url) ? `<a href="${escapeAttr(j.provider_url || j.paypal_url)}" target="_blank" onclick="onLinkClick(event, '${j.id}', '${escapeAttr(j.provider || 'paypal')}')">${escapeHtml((j.provider || 'paypal').toUpperCase())} goto</a>` : ''}
        ${j.checkout_url ? `<a href="${escapeAttr(j.checkout_url)}" target="_blank" onclick="onLinkClick(event, '${j.id}', 'checkout')">Checkout</a>` : ''}
      </td>
      <td>
        ${j.status==='pending'
          ? `<button class="primary" onclick="markPaid('${j.id}')">Mark Paid</button>
             <button class="danger" onclick="cancelJob('${j.id}')">Cancel</button>`
          : `<button onclick="del('${j.id}')">Delete</button>`}
      </td>
    </tr>`;
  }).join('') || '<tr><td colspan=7>(空，可能都被领走了；60s 后会重新出现)</td></tr>';
}
function escapeHtml(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function escapeAttr(s){ return escapeHtml(s); }
async function claim(id){
  // 标记 60s 临时占用：列表里其他用户看不到，避免多人争抢同一条
  try {
    const r = await fetch(`/api/jobs/${id}/claim`, {
      method:'PUT', headers: authHeaders(), credentials: 'same-origin',
    });
    if (!r.ok) console.warn('[inbox] claim failed', id, r.status);
  } catch(e){ console.warn('[inbox] claim exception', id, e); }
}
function _consumeJob(id){
  // 单点：黑名单 + DOM 删行 + 从 _lastJobs 缓存移除。任何"已经被处理过"的 job 都该这样调一次，
  // 避免后续 bulkOpen 从陈旧缓存里再次抓到同一条重复打开（即使下次 loadJobs 把它拉回，
  // _markConsumed 写的过期点会让 loadJobs 自动从 _lastJobs 里再过滤掉它）。
  _markConsumed(id);
  _lastJobs = _lastJobs.filter(j => j.id !== id);
  const tr = document.querySelector(`tr[data-id="${id}"]`);
  if (tr) tr.remove();
}
function onLinkClick(ev, id, kind){
  // 不阻止默认 → 链接照常在新 tab 打开；同步并发触发 claim 并 consume
  claim(id);
  _consumeJob(id);
}
function _tryOpenInNewTab(url){
  // 仅 window.open：返 null 即明确未开，给 fallback 面板。**不再叠加 <a>.click()** —
  // 部分浏览器 (Chrome 某些 build / Edge) 即使 window.open 已成功打开 tab，<a>.click() 也会
  // 再开一次，导致同一链接打开两次（用户实际反馈的 bug）。fallback 面板里的 <a> 是用户
  // 真鼠标点击，浏览器一定放行，不需要 anchor 兜底。
  try {
    const w = window.open(url, '_blank', 'noopener,noreferrer');
    return !!w;
  } catch(e) {
    return false;
  }
}
function _showFallbackPanel(targets, field){
  // 浏览器拦了多窗口 → 渲染一个面板，每个链接是真 <a target=_blank>，
  // 用户 **真实鼠标点一次** = 真 user gesture，浏览器一定放行。
  let panel = document.getElementById('_bulkFallback');
  if (panel) panel.remove();
  panel = document.createElement('div');
  panel.id = '_bulkFallback';
  panel.style.cssText = 'position:fixed;right:1em;bottom:1em;width:380px;max-height:70vh;overflow:auto;'
    + 'background:#fff;border:2px solid #dc3545;border-radius:6px;padding:12px;'
    + 'box-shadow:0 4px 16px rgba(0,0,0,.2);z-index:9999;font-size:13px;';
  panel.innerHTML = `
    <div style="margin-bottom:8px;color:#dc3545;font-weight:600;">
      ⚠ 浏览器拦截了批量弹窗（每次手势只允许 1 个）
    </div>
    <div style="margin-bottom:8px;color:#666;">
      点击下面每个链接（每个都是真实点击）即可打开。<br>
      或：地址栏右侧"已拦截弹窗"图标 → <b>始终允许</b>，下次就能一键开 10 个。
    </div>
    <div id="_bulkFallbackLinks"></div>
    <div style="margin-top:10px;text-align:right;">
      <button onclick="document.getElementById('_bulkFallback').remove()">关闭</button>
    </div>
  `;
  document.body.appendChild(panel);
  const list = panel.querySelector('#_bulkFallbackLinks');
  for (const j of targets) {
    const row = document.createElement('div');
    row.style.cssText = 'margin:4px 0;padding:6px;border:1px solid #eee;border-radius:3px;';
    const a = document.createElement('a');
    a.href = _jobActionUrl(j, field);
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = `${j.account_email || j.account_name} (${j.plan_kind})`;
    a.style.cssText = 'color:#0a58ca;text-decoration:none;display:block;';
    a.addEventListener('click', () => {
      claim(j.id);
      _consumeJob(j.id);
      row.style.opacity = '0.4';
      row.style.textDecoration = 'line-through';
    });
    row.appendChild(a);
    list.appendChild(row);
  }
}
// v2:provider_url(GoPay/PayPal 通用)优先,paypal_url 兜底(老 PayPal 数据迁移后两者一致;
// 老 PayPal 客户端推的 job 只有 paypal_url)。checkout_url 字段独立,直接读 j.checkout_url。
function _jobActionUrl(j, field){
  if (field === 'checkout_url') return j.checkout_url || '';
  return j.provider_url || j.paypal_url || '';
}
function bulkOpen(field){
  // **同步函数**：不能 await，否则 user gesture 在 fetch 后失效。
  // 数据来源是 loadJobs 缓存的 _lastJobs。
  if (!Array.isArray(_lastJobs) || _lastJobs.length === 0) {
    alert('当前列表为空，等几秒页面刷新后再点');
    return;
  }
  // 防 dup：即使 _lastJobs 在 race 下含同 id 多次，target 里每个 id 只会出现一次。
  // **跳过 claimed_at 已设的 job**：用户已经点过这条链接，可能正在付款 / 已经付完
  // 但订阅检测有问题，让用户手动到列表底部确认；批量打开只挑没碰过的，避免重复付款。
  const _seen = new Set();
  const target = _lastJobs.filter(j => {
    if (!j || j.status !== 'pending' || !_jobActionUrl(j, field)) return false;
    if (j.claimed_at) return false;  // 已点过的不参与批量打开
    if (_seen.has(j.id)) return false;
    if (_isRecentlyConsumed(j.id)) return false;  // 客户端黑名单兜底
    _seen.add(j.id);
    return true;
  }).slice(0, 10);
  if (!target.length) {
    alert('没有「全新未点过」的任务可打开。\\n（已点过的订单沉到列表底部，需要时手动点击）');
    return;
  }
  // 不用 confirm（确保 gesture 能直接走到 window.open 第一个）
  let opened = 0;
  const blocked = [];
  for (let i = 0; i < target.length; i++) {
    const j = target[i];
    const ok = _tryOpenInNewTab(_jobActionUrl(j, field));
    if (ok) {
      opened++;
      claim(j.id);
      _consumeJob(j.id);
    } else {
      blocked.push(j);
    }
  }
  if (blocked.length > 0) {
    // 把被拦的渲染到 fallback 面板，让用户真实点击逐个开
    _showFallbackPanel(blocked, field);
  }
  if (opened === 0) {
    console.warn('[inbox] 浏览器拦截了所有弹窗，已渲染 fallback 面板');
  }
  setTimeout(loadJobs, 800);
}
async function _doStateChange(id, path, label){
  try {
    const r = await fetch(`/api/jobs/${id}${path}`, {
      method: 'PUT', headers: authHeaders(), credentials: 'same-origin',
    });
    if (!r.ok) {
      alert(`${label} 失败：HTTP ${r.status} ${await r.text()}`);
      return false;
    }
    return true;
  } catch (e) {
    alert(`${label} 网络错误：${e}`);
    return false;
  }
}
async function markPaid(id){
  if(!confirm('确认已完成 PayPal 付款？')) return;
  if (await _doStateChange(id, '/paid', 'Mark Paid')) {
    _consumeJob(id);  // 同步删 DOM + 移除缓存，避免 bulkOpen 重复抓
    loadJobs();
  }
}
async function cancelJob(id){
  if(!confirm('取消该任务？')) return;
  if (await _doStateChange(id, '/cancel', 'Cancel')) {
    _consumeJob(id);
    loadJobs();
  }
}
async function del(id){
  if(!confirm('删除该记录？')) return;
  try {
    const r = await fetch(`/api/jobs/${id}`, {
      method: 'DELETE', headers: authHeaders(), credentials: 'same-origin',
    });
    if (!r.ok) { alert(`删除失败：HTTP ${r.status} ${await r.text()}`); return; }
    _consumeJob(id);
    loadJobs();
  } catch (e) { alert(`删除网络错误：${e}`); }
}
loadJobs();
setInterval(loadJobs, 1000);
</script>
</body>
</html>
"""

_ADMIN_HTML_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>秒到控制台</title>
<style>
:root{--bg:#f5f7fb;--panel:#fff;--line:#e8edf5;--text:#202635;--muted:#6b7280;--blue:#3f6df6;--green:#19a37c;--orange:#d97706;--red:#dc2626;--soft:#eef3ff}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"PingFang SC","Microsoft YaHei",sans-serif;font-size:14px}
.app{display:grid;grid-template-columns:244px 1fr;min-height:100vh}
.side{background:#fff;border-right:1px solid var(--line);padding:18px 12px;position:sticky;top:0;height:100vh;overflow:auto}
.brand{font-size:17px;font-weight:700;padding:6px 10px 4px}.brand-sub{padding:0 10px 16px;color:var(--muted);font-size:12px;line-height:1.5}
.nav button{width:100%;height:42px;border:0;background:transparent;border-radius:8px;display:flex;align-items:center;gap:10px;padding:0 12px;color:#384152;font-size:15px;cursor:pointer;text-align:left}
.nav button:hover{background:#f3f6fb}.nav button.active{background:#eaf0ff;color:#3867ff;font-weight:650}
.main{padding:18px 24px 28px;min-width:0}
.page-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:14px}.page-title{font-size:20px;font-weight:750}.page-sub{color:var(--muted);font-size:12px;margin-top:5px}.status-strip{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:10px;margin-bottom:14px}.status-item{background:#fff;border:1px solid var(--line);border-radius:8px;padding:12px}.status-item span{display:block;color:var(--muted);font-size:12px}.status-item b{display:block;margin-top:5px;font-size:16px}
	.tabs{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:10px;margin-bottom:16px}.tab{border:1px solid var(--line);background:#fff;border-radius:8px;min-height:54px;padding:8px 12px;color:#526071;white-space:nowrap;text-align:left;cursor:pointer}.tab b{display:block;color:#202635;font-size:18px;margin-top:3px}.tab.active{border-color:#b9c9ff;background:#eef3ff;color:#3867ff}
.filters,.card{background:#fff;border:1px solid var(--line);border-radius:8px}.filters{padding:18px;display:flex;gap:14px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
label{color:#4b5563}.input,.select{height:40px;border:1px solid #dce3ee;border-radius:7px;padding:0 12px;background:#fff;min-width:180px;color:#1f2937}
.btn{height:40px;border:1px solid #dce3ee;background:#fff;border-radius:7px;padding:0 14px;cursor:pointer;color:#344054}.btn:hover{filter:brightness(.97)}.btn:disabled{cursor:not-allowed;color:#98a2b3;background:#f3f6fb;border-color:#e5eaf2;filter:none}.btn.primary{background:var(--blue);border-color:var(--blue);color:#fff}.btn.primary:disabled{background:#e5eaf2;color:#98a2b3}.btn.danger{background:#fff1f1;border-color:#ffd2d2;color:#e54848}.btn.soft{background:#eef3ff;color:#3867ff;border-color:#dbe6ff}
.toolbar{display:flex;justify-content:space-between;align-items:center;padding:18px 18px 12px;gap:12px;flex-wrap:wrap}
.table-wrap{overflow:auto;padding:0 18px 16px}table{width:100%;border-collapse:collapse;min-width:1040px}th,td{border-bottom:1px solid #edf1f6;padding:12px 10px;text-align:left;vertical-align:middle;white-space:nowrap}th{font-weight:650;color:#697386;background:#fafbfc}td{color:#3f4654}.muted{color:var(--muted);font-size:12px}
.badge{display:inline-flex;align-items:center;min-width:82px;justify-content:center;height:28px;border-radius:7px;border:1px solid;padding:0 10px;font-weight:600}.success{color:#0e9f78;background:#e9fbf6;border-color:#99ead9}.running{color:#d97706;background:#fff7e6;border-color:#ffd89a}.failed{color:#dc2626;background:#fff1f2;border-color:#fecdd3}.pending{color:#3867ff;background:#eef3ff;border-color:#dbe6ff}.neutral{color:#526071;background:#f3f6fb;border-color:#dce3ee}
.grid{display:grid;grid-template-columns:minmax(320px,460px) 1fr;gap:16px}.form{padding:18px}.form h2,.card h2{margin:0 0 14px;font-size:18px}.field{display:grid;gap:7px;margin-bottom:13px}.field input,.field select,.field textarea{width:100%;border:1px solid #dce3ee;border-radius:7px;padding:0 12px}.field input,.field select{height:40px}.field textarea{min-height:72px;padding-top:10px;resize:vertical;font-family:inherit}
.log{background:#0f172a;color:#dbeafe;border-radius:8px;padding:12px;min-height:220px;max-height:420px;overflow:auto;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;white-space:pre-wrap;border:1px solid #1e293b;box-shadow:inset 0 0 0 1px rgba(255,255,255,.03)}.log-meta{border:1px solid var(--line);background:#fbfcff;border-radius:8px;padding:12px;margin-bottom:12px;display:grid;gap:6px}.log-meta div{display:flex;gap:8px;justify-content:space-between}.log-meta b{font-weight:650}.flow-steps{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:8px;margin-bottom:12px}.flow-step{border:1px solid var(--line);border-radius:8px;background:#fbfcff;padding:10px}.flow-step span{display:block;color:var(--muted);font-size:12px}.flow-step b{display:block;margin-top:4px;font-size:13px}.flow-step.success{border-color:#99ead9;background:#e9fbf6}.flow-step.running{border-color:#ffd89a;background:#fff7e6}.flow-step.failed{border-color:#fecdd3;background:#fff1f2}.prompt{border:1px solid #ffd89a;background:#fffaf0;border-radius:8px;padding:12px;margin:12px 0}.row-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.iconbtn{width:36px;height:36px;border:0;border-radius:7px;background:#eef2f7;color:#607086;cursor:pointer}.iconbtn.play{background:#e9efff;color:#4770ff}.iconbtn.stop{background:#ffe8e8;color:#ef4444}.summary{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:12px;margin-bottom:14px}.metric{background:#fff;border:1px solid var(--line);border-radius:8px;padding:14px}.metric b{display:block;font-size:22px;margin-top:6px}
.toggle-row{display:flex;align-items:center;justify-content:space-between;gap:14px;border:1px solid var(--line);background:#fbfcff;border-radius:8px;padding:12px 13px;margin-bottom:12px}.toggle-copy{display:grid;gap:3px}.switch{position:relative;width:44px;height:24px;flex:0 0 auto}.switch input{display:none}.slider{position:absolute;inset:0;border-radius:999px;background:#d8e0ec;cursor:pointer;transition:.16s}.slider:before{content:"";position:absolute;width:18px;height:18px;left:3px;top:3px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(15,23,42,.22);transition:.16s}.switch input:checked+.slider{background:var(--blue)}.switch input:checked+.slider:before{transform:translateX(20px)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
	@media(max-width:900px){.app{grid-template-columns:1fr}.side{position:relative;height:auto}.grid{grid-template-columns:1fr}.summary,.status-strip{grid-template-columns:repeat(2,1fr)}.tabs{grid-template-columns:repeat(2,minmax(120px,1fr))}.main{padding:14px}.page-head{display:block}}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand">秒到控制台</div>
    <div class="brand-sub">GPT 注册、2FA、Midtrans 与 GoPay 支付</div>
    <div class="nav">
      <button class="active" data-view="tasks">▣ 订阅任务</button>
      <button data-view="register">＋ 注册任务</button>
      <button data-view="email_orders">✉ 邮箱订单</button>
      <button data-view="oauth_tokens">◇ OAuth令牌</button>
      <button data-view="oauth_tasks">◷ OAuth授权</button>
      <button data-view="accounts">◎ GoPay账号</button>
      <button data-view="payment">▶ 支付任务</button>
      <button data-view="otp">✉ OTP收件箱</button>
      <button data-view="plus_pool">★ Plus成功池</button>
    </div>
  </aside>
	<main class="main">
    <div class="page-head">
      <div><div class="page-title">秒到自动化工作台</div><div class="page-sub">邮箱池、注册绑定 2FA、1 IDR 授权链和 GoPay 支付集中管理</div></div>
      <button class="btn" onclick="refreshTopStats()">刷新总览</button>
    </div>
	  <div class="tabs">
      <button class="tab" data-top-tab="oauth_tokens" onclick="showTopTab('oauth_tokens')">OAuth令牌<b id="top-oauth-tokens">0</b></button>
      <button class="tab" data-top-tab="email_orders" onclick="showTopTab('email_orders')">邮箱订单<b id="top-email-orders">0</b></button>
      <button class="tab" data-top-tab="oauth_tasks" onclick="showTopTab('oauth_tasks')">OAuth授权任务<b id="top-oauth-tasks">0</b></button>
      <button class="tab active" data-top-tab="subscription_tasks" onclick="showTopTab('subscription_tasks')">订阅任务<b id="top-subscription-tasks">0</b></button>
      <button class="tab" data-top-tab="sms_orders" onclick="showTopTab('sms_orders')">短信订单<b id="top-sms-orders">0</b></button>
      <button class="tab" data-top-tab="register_tasks" onclick="showTopTab('register_tasks')">注册任务<b id="top-register-tasks">0</b></button>
    </div>
    <section id="view-tasks">
      <div class="summary">
        <div class="metric"><span class="muted">总任务</span><b id="m-total">0</b></div>
        <div class="metric"><span class="muted">运行中</span><b id="m-running">0</b></div>
        <div class="metric"><span class="muted">成功</span><b id="m-paid">0</b></div>
        <div class="metric"><span class="muted">失败/取消</span><b id="m-failed">0</b></div>
      </div>
      <div class="filters">
        <label>账号</label><input id="emailFilter" class="input" placeholder="OpenAI/GoPay账号">
        <label>状态</label><select id="statusFilter" class="select"><option value="">全部</option><option value="pending" selected>运行中</option><option value="paid">成功</option><option value="cancelled">已取消</option><option value="expired">已过期</option></select>
        <button class="btn soft" onclick="loadJobs()">重置/查询</button>
        <button class="btn primary" onclick="showView('register')">＋ 创建注册任务</button>
      </div>
      <div class="card">
        <div class="toolbar"><strong>订阅任务</strong><div><button class="btn" onclick="loadJobs()">刷新</button><button class="btn danger" onclick="bulkCancel()">批量取消运行中</button></div></div>
        <div class="table-wrap"><table><thead><tr><th>ID</th><th>订阅协议</th><th>注册账号</th><th>支付账号</th><th>状态</th><th>消息</th><th>操作</th></tr></thead><tbody id="jobRows"></tbody></table></div>
      </div>
    </section>
    <section id="view-register" style="display:none">
      <div class="status-strip">
        <div class="status-item"><span>邮箱池可用</span><b id="regEmailReady">0</b></div>
        <div class="status-item"><span>成功账号池</span><b id="regSuccessTotal">0</b></div>
        <div class="status-item"><span>运行中任务</span><b id="regRunningTasks">0</b></div>
        <div class="status-item"><span>注册线路</span><b id="regRouteLabel">代理池</b></div>
      </div>
      <div class="grid">
        <div class="card form">
          <h2>创建注册任务</h2>
          <div class="field"><label>注册数量</label><input id="gptRegCount" type="number" min="1" max="200" value="1"></div>
          <div class="field"><label>并发线程</label><input id="gptRegWorkers" type="number" min="1" max="20" value="1"></div>
          <div class="field"><label>间隔秒数</label><input id="gptRegDelay" type="number" min="0" value="0"></div>
          <div class="toggle-row">
            <div class="toggle-copy"><strong>手动指定代理</strong><span class="muted">开启后本批注册固定使用下面的代理</span></div>
            <label class="switch"><input id="gptRegUseProxy" type="checkbox" onchange="syncGptRegisterProxy()"><span class="slider"></span></label>
          </div>
          <div class="field" id="gptRegProxyField" style="display:none"><label>代理地址</label><input id="gptRegProxy" placeholder="http://user:pass@host:port"></div>
          <div class="toggle-row">
            <div class="toggle-copy"><strong>失败继续下一个</strong><span class="muted">遇到坏邮箱或坏线路时继续取下一个邮箱</span></div>
            <label class="switch"><input id="gptRegContinue" type="checkbox" checked><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-copy"><strong>成功后自动授权支付</strong><span class="muted">新成功账号自动生成 Midtrans，缺 GoPay 号时自动补号再支付</span></div>
            <label class="switch"><input id="gptRegAutoFlow" type="checkbox" checked><span class="slider"></span></label>
          </div>
          <button class="btn primary" onclick="startGptRegister()">开始注册 + 绑定 2FA</button>
          <button class="btn" onclick="loadGptRegisterTasks()">刷新状态</button>
        </div>
        <div class="card form">
          <h2>注册日志</h2>
          <div class="log-meta" id="gptRegMeta">
            <div><span class="muted">当前任务</span><b>-</b></div>
            <div><span class="muted">日志文件</span><b>-</b></div>
            <div><span class="muted">运行摘要</span><b>-</b></div>
          </div>
          <div class="log" id="gptRegLog">等待创建任务...</div>
        </div>
      </div>
      <div class="card" style="margin-top:16px">
        <div class="toolbar"><strong>注册任务</strong><button class="btn" onclick="loadGptRegisterTasks()">刷新</button></div>
        <div class="table-wrap"><table><thead><tr><th>ID</th><th>数量/并发</th><th>线路</th><th>状态</th><th>消息</th><th>成功池</th><th>创建时间</th></tr></thead><tbody id="gptRegisterRows"></tbody></table></div>
      </div>
      <div class="card form gopay-only">
        <h2>GoPay 注册 / 登录</h2>
        <div class="field"><label>号码来源</label><select id="regSource"><option value="pool">号码池</option><option value="smsbower">SMSBower</option></select></div>
        <div class="field"><label>手机号（号码池模式）</label><input id="regPhone" placeholder="+62..."></div>
        <div class="field"><label>模式</label><select id="regTaskType"><option value="register">注册</option><option value="login">登录已有号</option></select></div>
        <div class="field"><label>PIN</label><input id="regPin" value="147258"></div>
        <div class="field"><label>代理</label><input id="regProxy" placeholder="socks5://user:pass@host:port"></div>
        <button class="btn primary" onclick="startRegister()">开始 GoPay 任务</button>
        <div class="log" id="regLog">等待任务...</div><div id="otpPrompt"></div>
        <div class="table-wrap"><table><thead><tr><th>ID</th><th>手机号</th><th>状态</th><th>消息</th><th>时间</th></tr></thead><tbody id="manualRows"></tbody></table></div>
      </div>
    </section>
    <section id="view-email_orders" style="display:none">
      <div class="grid">
        <div class="card form">
          <h2>导入邮箱</h2>
          <div class="field"><label>邮箱素材</label><textarea id="emailImportText" placeholder="email----password----client_id----refresh_token"></textarea></div>
          <button class="btn primary" onclick="importGptEmails()">导入邮箱池</button>
          <button class="btn" onclick="loadGptEmails()">刷新</button>
          <span class="muted" id="emailImportStatus"></span>
        </div>
        <div class="card form">
          <h2>邮箱池状态</h2>
          <div class="summary">
            <div class="metric"><span class="muted">可用</span><b id="emailAvailable">0</b></div>
            <div class="metric"><span class="muted">已用</span><b id="emailUsed">0</b></div>
            <div class="metric"><span class="muted">失败</span><b id="emailFailed">0</b></div>
            <div class="metric"><span class="muted">总数</span><b id="emailTotal">0</b></div>
          </div>
        </div>
      </div>
      <div class="card" style="margin-top:16px">
        <div class="toolbar"><strong>邮箱订单</strong><button class="btn" onclick="loadGptEmails()">刷新</button></div>
        <div class="table-wrap"><table><thead><tr><th>ID</th><th>邮箱</th><th>状态</th><th>备注</th><th>导入时间</th></tr></thead><tbody id="emailRows"></tbody></table></div>
      </div>
    </section>
    <section id="view-oauth_tokens" style="display:none">
      <div class="card form" style="margin-bottom:16px">
        <div class="toolbar" style="padding:0 0 12px"><strong>注册成功池</strong><div><button class="btn primary" onclick="copyGptSuccessPool()">复制全部</button><button class="btn" onclick="loadGptAccounts()">刷新</button></div></div>
        <div class="summary">
          <div class="metric"><span class="muted">成功账号</span><b id="gptSuccessPoolTotal">0</b></div>
          <div class="metric" style="grid-column:span 3"><span class="muted">池子文件</span><b id="gptSuccessPoolPath" style="font-size:13px;word-break:break-all"></b></div>
        </div>
        <div class="field"><textarea id="gptSuccessPoolText" class="mono" readonly placeholder="邮箱----密码----2fa----at"></textarea></div>
      </div>
      <div class="card form" style="margin-bottom:16px">
        <div class="toolbar" style="padding:0 0 12px"><strong>全流程自动授权</strong><button class="btn" onclick="loadAutoFlow()">刷新</button></div>
        <div class="row-actions" style="margin-bottom:12px;flex-wrap:wrap">
          <label class="switch"><input id="autoFlowEnabled" type="checkbox"><span class="slider"></span></label>
          <span class="muted">监听新成功账号</span>
          <input id="autoFlowPin" class="input" value="147258" style="min-width:110px;width:120px" placeholder="PIN">
          <input id="autoFlowProxy" class="input" style="min-width:260px" placeholder="可选：GoPay 注册/支付代理">
          <button class="btn primary" onclick="saveAutoFlowConfig()">保存</button>
        </div>
        <div class="log" id="autoFlowLog" style="min-height:180px;max-height:360px">等待任务...</div>
      </div>
      <div class="card">
        <div class="toolbar"><strong>OAuth令牌 / 注册成功账号</strong><div><button class="btn soft" onclick="refreshAllGptAccountStatus()">刷新状态</button><button class="btn" onclick="loadGptAccounts()">刷新</button></div></div>
        <div class="table-wrap"><table><thead><tr><th>ID</th><th>邮箱</th><th>密码</th><th>AT</th><th>2FA</th><th>状态</th><th>更新时间</th><th>操作</th></tr></thead><tbody id="gptAccountRows"></tbody></table></div>
      </div>
    </section>
    <section id="view-oauth_tasks" style="display:none">
      <div class="card">
        <div class="toolbar"><strong>OAuth授权任务</strong><button class="btn" onclick="loadGptRegisterTasks()">刷新</button></div>
        <div class="table-wrap"><table><thead><tr><th>ID</th><th>邮箱来源</th><th>邮箱</th><th>状态</th><th>错误</th><th>完成时间</th></tr></thead><tbody id="gptOauthTaskRows"></tbody></table></div>
      </div>
    </section>
    <section id="view-plus_pool" style="display:none">
      <div class="card form" style="margin-bottom:16px">
        <div class="toolbar" style="padding:0 0 12px"><strong>Plus成功池</strong><div><button class="btn primary" onclick="copyPlusPool()">复制全部</button><button class="btn soft" onclick="copySelectedPlusPool()">复制勾选</button><button class="btn soft" onclick="refreshAllGptAccountStatus().then(loadPlusPool)">刷新状态</button><button class="btn" onclick="loadPlusPool()">刷新</button></div></div>
        <div class="summary">
          <div class="metric"><span class="muted">Plus账号</span><b id="plusPoolTotal">0</b></div>
          <div class="metric" style="grid-column:span 3"><span class="muted">池子文件</span><b id="plusPoolPath" style="font-size:13px;word-break:break-all"></b></div>
        </div>
        <div class="field"><textarea id="plusPoolText" class="mono" readonly placeholder="邮箱----密码----2fa----状态----开通时间"></textarea></div>
      </div>
      <div class="card">
        <div class="toolbar"><strong>Plus账号明细</strong><button class="btn" onclick="loadPlusPool()">刷新</button></div>
        <div class="table-wrap"><table><thead><tr><th><input type="checkbox" id="plusSelectAll" onchange="togglePlusSelection(this.checked)"></th><th>邮箱</th><th>密码</th><th>2FA</th><th>状态</th><th>开通时间</th><th>操作</th></tr></thead><tbody id="plusPoolRows"></tbody></table></div>
      </div>
    </section>
    <section id="view-accounts" style="display:none">
      <div class="card form" style="margin-bottom:16px">
        <h2>节日红包配置</h2>
        <div class="field"><label>红包短链</label><input id="envelopeUrl" placeholder="https://app.gopay.co.id/... 或 https://gopay.co.id/..."></div>
        <button class="btn primary" onclick="saveEnvelope()">保存/替换红包链接</button>
        <button class="btn" onclick="clearEnvelope()">清空红包链接</button>
        <span class="muted" id="envelopeStatus"></span>
      </div>
      <div class="card">
        <div class="toolbar"><strong>GoPay账号</strong><span id="accountSummary" class="muted">共 0 个｜1RP：0｜0：0</span><button class="btn" onclick="loadAccounts()">刷新</button></div>
        <div class="table-wrap"><table><thead><tr><th>手机号</th><th>本地号</th><th>余额</th><th>使用状态</th><th>绑定任务</th><th>失败原因</th><th>Customer ID</th><th>注册时间</th><th>操作</th></tr></thead><tbody id="accountRows"></tbody></table></div>
      </div>
    </section>
    <section id="view-payment" style="display:none">
      <div class="grid">
        <div class="card form">
          <h2>发起 GoPay 支付</h2>
          <div class="field"><label>OpenAI账号</label><input id="checkoutEmail" placeholder="可选，用来标记订阅任务"></div>
          <div class="field"><label>OpenAI AT / AT JSON</label><textarea id="openaiAt" placeholder="可粘贴纯 access token，也可粘贴旧工具导出的完整 JSON"></textarea></div>
          <div class="row-actions" style="margin-bottom:14px">
            <button class="btn soft" onclick="generateMidtrans()">用 AT 生成 Midtrans 链接</button>
            <span class="muted" id="checkoutStatus"></span>
          </div>
          <div class="field"><label>GoPay账号</label><select id="payPhone"></select></div>
          <div class="field"><label>PIN</label><input id="payPin" value="147258"></div>
          <div class="field"><label>Midtrans 链接</label><input id="payUrl" placeholder="https://app.midtrans.com/snap/v4/redirection/..."></div>
          <button class="btn primary" onclick="startPayment()">直接支付</button>
          <button class="btn soft" onclick="claimAndPay()">领取订阅任务并支付</button>
          <button class="btn" onclick="checkSelectedBalance()">查余额</button>
          <div id="paymentPrompt"></div>
        </div>
        <div class="card form">
          <h2>支付日志</h2>
          <div class="flow-steps">
            <div class="flow-step" id="payFlowPrecheck"><span>1. 预检</span><b>等待</b></div>
            <div class="flow-step" id="payFlowLink"><span>2. 绑定</span><b>等待</b></div>
            <div class="flow-step" id="payFlowOtp"><span>3. OTP/PIN</span><b>等待</b></div>
            <div class="flow-step" id="payFlowDone"><span>4. 支付结果</span><b>等待</b></div>
          </div>
          <div class="log" id="paymentLog">等待支付任务...</div>
        </div>
      </div>
      <div class="card" style="margin-top:16px">
        <div class="toolbar"><strong>支付任务</strong><button class="btn" onclick="loadPaymentJobs()">刷新</button></div>
        <div class="table-wrap"><table><thead><tr><th>ID</th><th>账号</th><th>状态</th><th>消息</th><th>创建时间</th></tr></thead><tbody id="paymentRows"></tbody></table></div>
      </div>
    </section>
    <section id="view-otp" style="display:none">
      <div class="grid">
        <div class="card form">
          <h2>短信 API 接入</h2>
          <div class="field"><label>Base URL</label><input id="smsApiBaseUrl" placeholder="https://smsbower.page"></div>
          <div class="field"><label>API Key</label><input id="smsApiKey" placeholder="留空则保留当前 Key"></div>
          <div class="row-actions">
            <div class="field" style="flex:1;margin-bottom:0"><label>服务</label><input id="smsApiService" placeholder="ni"></div>
            <div class="field" style="flex:1;margin-bottom:0"><label>国家</label><input id="smsApiCountry" placeholder="6"></div>
          </div>
          <div class="row-actions" style="margin:12px 0 14px">
            <button class="btn primary" onclick="saveSmsConfig()">保存配置</button>
            <button class="btn" onclick="loadOtp(true)">刷新余额</button>
            <span class="muted" id="smsConfigStatus"></span>
          </div>
          <div class="table-wrap"><table><tbody id="smsApiRows"></tbody></table></div>
        </div>
        <div class="card form">
          <h2>API OTP 数据</h2>
          <div class="log" id="otpBox">等待读取...</div>
        </div>
      </div>
    </section>
  </main>
</div>
<script>
let activeManualId = '';
let activePaymentId = '';
let activePinId = '';
let activeGptTaskId = '';
let generatedInboxJobId = '';
let cachedAccounts = [];
let cachedPlusPool = [];
function authHeaders(){return {'Content-Type':'application/json'};}
const gopayRegisterNav=document.querySelector('button[data-view="register"]');if(gopayRegisterNav)gopayRegisterNav.textContent='GoPay注册/登录';
async function startRegister(){const body={source:document.getElementById('regSource')?.value||'pool',phone:document.getElementById('regPhone')?.value.trim()||'',pin:document.getElementById('regPin')?.value.trim()||'147258',country_code:'+62',force_live:false,login_existing:document.getElementById('regTaskType')?.value==='login',relogin_after_register:false,claim_envelope_after_register:false,proxy:document.getElementById('regProxy')?.value.trim()||''};const r=await fetch('/api/manual-register',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});const d=await r.json();if(!r.ok){alert(d.error||'创建失败');return}activeManualId=d.id;loadManualJobs()}
function showView(name){document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('active',b.dataset.view===name));['tasks','register','email_orders','oauth_tokens','oauth_tasks','accounts','payment','otp','plus_pool'].forEach(v=>{const el=document.getElementById('view-'+v);if(el)el.style.display=v===name?'block':'none'}); if(name==='accounts'){loadAccounts();loadEnvelopes();} if(name==='payment'){loadAccounts();loadPaymentJobs();} if(name==='register')loadGptRegisterTasks(); if(name==='email_orders')loadGptEmails(); if(name==='oauth_tokens')loadGptAccounts(); if(name==='oauth_tasks')loadGptRegisterTasks(); if(name==='otp')loadOtp(); if(name==='plus_pool')loadPlusPool();}
document.querySelectorAll('.nav button').forEach(b=>b.onclick=()=>showView(b.dataset.view));
function badge(s){const map={paid:['成功','success'],pending:['运行中','running'],cancelled:['取消','failed'],expired:['过期','failed'],running:['运行中','running'],waiting_otp:['等待OTP','pending'],success:['成功','success'],failed:['失败','failed'],already_registered:['已注册','running'],available:['可用','success'],no_balance:['余额不足','pending'],used_success:['已成功','success'],used_failed:['失败','failed'],reserved:['已绑定','running']};const x=map[s]||[s||'-','pending'];return `<span class="badge ${x[1]}">${x[0]}</span>`}
function fmt(s){if(!s)return '-';try{return new Date(s).toLocaleString()}catch{return s}}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function setTop(id,value){const el=document.getElementById(id);if(el)el.textContent=value}
function short(s,n=22){s=String(s||'');return s.length>n?s.slice(0,n)+'...':s}
function taskSummary(j){const es=j.email_summary||j.email_summary_start||{};const pool=j.success_pool_total??'-';return `邮箱 ${es.available??'-'}/${es.total??'-'}，成功池 ${pool}`}
function showTopTab(name){document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.topTab===name));if(name==='subscription_tasks'){showView('tasks');statusFilter.value='pending';loadJobs();return}if(name==='email_orders'){showView('email_orders');return}if(name==='oauth_tasks'){showView('oauth_tasks');return}if(name==='sms_orders'){showView('otp');return}if(name==='register_tasks'){showView('register');return}if(name==='oauth_tokens'){showView('oauth_tokens');return}}
async function updateAccountSummary(){try{const d=await fetch('/api/accounts').then(r=>r.json());const s=d.summary||{};const el=document.getElementById('accountSummary');if(el)el.innerHTML=`<button class="btn soft" onclick="filterAccounts('all')">共 ${s.total||0} 个</button> <button class="btn soft" onclick="filterAccounts('one')">1RP：${s.one_rp||0}</button> <button class="btn soft" onclick="filterAccounts('zero')">0：${s.zero||0}</button>`}catch(e){}}
function filterAccounts(kind){document.querySelectorAll('#accountRows tr[data-phone]').forEach(row=>{const b=Number((row.querySelector('.balance-cell')?.textContent||'').replace(/[^0-9.-]/g,''));row.style.display=(kind==='one'&&b!==1)||(kind==='zero'&&b!==0)?'none':''})}
setInterval(updateAccountSummary,2000)
async function refreshAccountCodes(){for(const row of document.querySelectorAll('#accountRows tr[data-phone]')){let c=row.querySelector('.otp-cell');if(!c){c=document.createElement('td');c.className='otp-cell';row.appendChild(c)}try{const d=await fetch('/api/accounts/'+encodeURIComponent(row.dataset.phone)+'/sms-code').then(r=>r.json());c.textContent=d.code||'暂无验证码'}catch(e){c.textContent='暂无验证码'}}}
setInterval(refreshAccountCodes,3000)
function syncRegisterProxy(){regProxyField.style.display=regUseProxy.checked?'grid':'none'}
function syncGptRegisterProxy(){gptRegProxyField.style.display=gptRegUseProxy.checked?'grid':'none'}
async function refreshTopStats(){try{const [jr,sr,gs,sp]=await Promise.all([fetch('/api/jobs?limit=500&include_claimed=1').then(r=>r.json()),fetch('/api/sms-status').then(r=>r.json()),fetch('/api/gpt/summary').then(r=>r.json()),fetch('/api/gpt/success-pool?limit=5000').then(r=>r.json())]);const jobs=jr.jobs||[];const pendingOtp=sr.pending_otp||{};const otpCount=Object.values(pendingOtp).reduce((n,v)=>n+(Array.isArray(v)?v.length:0),0);const emails=gs.emails||{};setTop('top-subscription-tasks',jr.total||jobs.length);setTop('top-email-orders',emails.total||0);setTop('top-oauth-tasks',gs.tasks_running||0);setTop('top-oauth-tokens',sp.total||gs.accounts_total||0);setTop('top-sms-orders',otpCount);setTop('top-register-tasks',gs.tasks_total||0);setTop('regEmailReady',emails.available||0);setTop('regSuccessTotal',sp.total||0);setTop('regRunningTasks',gs.tasks_running||0)}catch(e){}}
async function loadGptEmails(){const r=await fetch('/api/gpt/emails?limit=500');const d=await r.json();if(!r.ok){emailRows.innerHTML=`<tr><td colspan="5">${esc(d.error||'读取失败')}</td></tr>`;return}const s=d.summary||{};setTop('emailAvailable',s.available||0);setTop('emailUsed',s.used||0);setTop('emailFailed',s.failed||0);setTop('emailTotal',s.total||0);emailRows.innerHTML=(d.emails||[]).map(x=>`<tr><td>${esc(x.id||'')}</td><td>${esc(x.email||'')}</td><td>${badge(x.status||'available')}</td><td>${esc(x.note||'')}</td><td>${fmt(x.imported_at)}</td></tr>`).join('')||'<tr><td colspan="5">暂无邮箱</td></tr>';refreshTopStats()}
async function importGptEmails(){const text=emailImportText.value.trim();if(!text)return alert('先粘贴邮箱素材');emailImportStatus.textContent='导入中...';const r=await fetch('/api/gpt/emails/import',{method:'POST',headers:authHeaders(),body:JSON.stringify({text})});const d=await r.json();if(!r.ok){emailImportStatus.textContent=d.error||'导入失败';return}emailImportStatus.textContent=`已导入 ${d.inserted}，跳过 ${d.skipped}`;emailImportText.value='';loadGptEmails()}
async function loadGptSuccessPool(){const r=await fetch('/api/gpt/success-pool?limit=5000');const d=await r.json();if(!r.ok){gptSuccessPoolText.value=d.error||'读取失败';return d}gptSuccessPoolTotal.textContent=d.total||0;gptSuccessPoolPath.textContent=d.path||'';gptSuccessPoolText.value=(d.lines||[]).join('\n');return d}
async function copyGptSuccessPool(){const d=await loadGptSuccessPool();const text=(d.lines||[]).join('\n');if(!text)return alert('暂无成功账号');await navigator.clipboard.writeText(text);alert(`已复制 ${d.total||0} 条成功账号`)}
async function copyTextValue(text,msg='已复制'){if(!text)return alert('暂无可复制内容');await navigator.clipboard.writeText(text);alert(msg)}
async function loadPlusPool(){const r=await fetch('/api/gpt/plus-pool?limit=5000');const d=await r.json();if(!r.ok){plusPoolText.value=d.error||'读取失败';return d}cachedPlusPool=d.records||[];plusPoolTotal.textContent=d.total||0;plusPoolPath.textContent=d.path||'';plusPoolText.value=(d.lines||[]).join('\n');if(plusSelectAll)plusSelectAll.checked=false;plusPoolRows.innerHTML=cachedPlusPool.map((x,i)=>`<tr><td><input type="checkbox" class="plus-check" data-index="${i}"></td><td>${esc(x.email||'')}</td><td class="mono">${esc(x.password||'')}</td><td class="mono">${esc(x.twofa||'')}</td><td>${esc(x.status||'-')}</td><td>${fmt(x.opened_at)}</td><td><button class="btn soft" onclick="copyPlusLine(${i})">复制</button></td></tr>`).join('')||'<tr><td colspan="7">暂无 Plus 成功账号</td></tr>';return d}
async function copyPlusPool(){const d=await loadPlusPool();const text=(d.lines||[]).join('\n');if(!text)return alert('暂无 Plus 成功账号');await copyTextValue(text,`已复制 ${d.total||0} 条 Plus 成功账号`)}
function togglePlusSelection(checked){document.querySelectorAll('.plus-check').forEach(x=>x.checked=checked)}
async function copyPlusLine(i){const row=cachedPlusPool[i]||{};await copyTextValue(row.line||[row.email,row.password,row.twofa,row.status,row.opened_at].join('----'),'已复制该 Plus 账号')}
async function copySelectedPlusPool(){const lines=[...document.querySelectorAll('.plus-check:checked')].map(x=>cachedPlusPool[Number(x.dataset.index)]).filter(Boolean).map(x=>x.line||[x.email,x.password,x.twofa,x.status,x.opened_at].join('----'));if(!lines.length)return alert('先勾选 Plus 账号');await copyTextValue(lines.join('\n'),`已复制 ${lines.length} 条勾选账号`)}
async function loadGptAccounts(){const [ar]=await Promise.all([fetch('/api/gpt/accounts?limit=500').then(r=>r.json()),loadGptSuccessPool(),loadAutoFlow()]);const rows=ar.accounts||[];gptAccountRows.innerHTML=rows.map(x=>`<tr><td>${esc(x.id||'')}</td><td>${esc(x.email||'')}</td><td class="mono">${esc(x.password_masked||'')}</td><td class="mono">${esc(x.access_token_masked||'未生成')}</td><td class="mono">${esc(x.totp_secret_masked||'未绑定')}</td><td>${esc((x.flow&&x.flow.status)||x.plan_type||x.status||'-')}</td><td>${fmt(x.updated_at||x.created_at)}</td><td><div class="row-actions"><button class="btn soft" onclick="refreshGptAccountStatus('${esc(x.email||'')}')">刷新状态</button><button class="btn primary" onclick="startAutoFlow('${esc(x.email||'')}')">自动授权支付</button></div></td></tr>`).join('')||'<tr><td colspan="8">暂无成功账号</td></tr>';refreshTopStats()}
async function refreshGptAccountStatus(email){if(!email)return;const r=await fetch('/api/gpt/accounts/'+encodeURIComponent(email)+'/refresh-status',{method:'POST',headers:authHeaders(),body:'{}'});const d=await r.json();if(!r.ok){alert(d.error||'刷新状态失败');return}await loadGptAccounts();alert(`${email} 当前状态：${d.plan_type||'-'}`)}
async function refreshAllGptAccountStatus(){if(!confirm('刷新当前成功账号的 OpenAI 状态？'))return;const r=await fetch('/api/gpt/accounts/refresh-status',{method:'POST',headers:authHeaders(),body:JSON.stringify({limit:100})});const d=await r.json();if(!r.ok){alert(d.error||'批量刷新失败');return}await loadGptAccounts();alert(`状态刷新完成：成功 ${d.ok||0}，失败 ${d.failed||0}`)}
async function loadAutoFlow(){try{const r=await fetch('/api/auto-flow');const d=await r.json();if(!r.ok){if(autoFlowLog)autoFlowLog.textContent=d.error||'读取失败';return d}if(autoFlowEnabled)autoFlowEnabled.checked=!!d.enabled;if(autoFlowPin&&d.pin)autoFlowPin.value=d.pin;if(autoFlowProxy)autoFlowProxy.value=d.proxy||'';const rows=d.jobs||[];if(!rows.length){autoFlowLog.textContent='暂无全流程任务';return d}const latest=rows[0];const summary=rows.slice(0,6).map(j=>`[${fmt(j.updated_at||j.created_at)}] ${j.email||'-'} | ${j.status}/${j.stage||'-'} | ${j.message||''}`);const detail=(latest.logs||[]).map(x=>`[${fmt(x.at)}] ${x.message||''}`);autoFlowLog.textContent=['最新任务完整日志',`${latest.email||'-'} | ${latest.status}/${latest.stage||'-'} | ${latest.id||''}`,'',...detail,'','最近任务',...summary].join('\n');return d}catch(e){return {error:String(e)}}}
async function saveAutoFlowConfig(){const body={enabled:autoFlowEnabled.checked,pin:autoFlowPin.value.trim()||'147258',proxy:autoFlowProxy.value.trim(),new_only:true};const r=await fetch('/api/auto-flow/config',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});const d=await r.json();if(!r.ok){alert(d.error||'保存失败');return}loadAutoFlow()}
async function startAutoFlow(email){if(!email)return;const body={email,pin:(autoFlowPin&&autoFlowPin.value.trim())||'147258',proxy:(autoFlowProxy&&autoFlowProxy.value.trim())||'',force:true};const r=await fetch('/api/auto-flow/start',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});const d=await r.json();if(!r.ok){alert(d.error||'启动失败');return}loadAutoFlow();loadJobs()}
async function startGptRegister(){if(gptRegAutoFlow&&gptRegAutoFlow.checked){await fetch('/api/auto-flow/config',{method:'POST',headers:authHeaders(),body:JSON.stringify({enabled:true,pin:(autoFlowPin&&autoFlowPin.value.trim())||'147258',proxy:(autoFlowProxy&&autoFlowProxy.value.trim())||'',new_only:true})})}const body={count:Number(gptRegCount.value||1),workers:Number(gptRegWorkers.value||1),delay:Number(gptRegDelay.value||0),continue_on_fail:gptRegContinue.checked,proxy:gptRegUseProxy.checked?gptRegProxy.value.trim():''};const r=await fetch('/api/gpt/register-tasks',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});const d=await r.json();if(!r.ok){alert(d.error||'创建失败');return}activeGptTaskId=d.id;renderGptTaskDetail(d);loadGptRegisterTasks();refreshTopStats()}
async function loadGptRegisterTasks(){const r=await fetch('/api/gpt/register-tasks?limit=100');const d=await r.json();const tasks=d.tasks||[];const running=tasks.filter(x=>x.status==='running').length;setTop('regRunningTasks',running);if(tasks[0]){const es=tasks[0].email_summary||tasks[0].email_summary_start||{};setTop('regEmailReady',es.available??0);setTop('regSuccessTotal',tasks[0].success_pool_total??0);setTop('regRouteLabel',tasks[0].route_label||'代理池')}gptRegisterRows.innerHTML=tasks.map(j=>`<tr onclick="activeGptTaskId='${j.id}';renderGptTaskDetail(${JSON.stringify(j).replace(/"/g,'&quot;')})"><td>${esc(j.id)}</td><td>${esc(j.count)} / ${esc(j.workers)}</td><td>${esc(j.route_label||'代理池')}</td><td>${badge(j.status)}</td><td>${esc(j.message)}</td><td>${esc(j.success_pool_total??'-')}</td><td>${fmt(j.created_at)}</td></tr>`).join('')||'<tr><td colspan="7">暂无注册任务</td></tr>';gptOauthTaskRows.innerHTML=(d.gpt_jobs||[]).map(j=>`<tr><td>${esc(j.id)}</td><td>${esc(j.email_source||'')}</td><td>${esc(j.email||'')}</td><td>${badge(j.status)}</td><td>${esc(j.error_message||'')}</td><td>${fmt(j.completed_at)}</td></tr>`).join('')||'<tr><td colspan="6">暂无 OAuth 授权任务</td></tr>';if(activeGptTaskId){const j=tasks.find(x=>x.id===activeGptTaskId);if(j)renderGptTaskDetail(j)}else if(tasks[0]){activeGptTaskId=tasks[0].id;renderGptTaskDetail(tasks[0])}refreshTopStats()}
function renderGptTaskDetail(j){gptRegMeta.innerHTML=`<div><span class="muted">当前任务</span><b>${esc(j.id||'-')} ｜ ${esc(j.status||'-')}</b></div><div><span class="muted">日志文件</span><b class="mono">${esc(j.log_file||'-')}</b></div><div><span class="muted">运行摘要</span><b>${esc(taskSummary(j))}</b></div>`;gptRegLog.textContent=j.log_tail||j.message||'运行中'}
async function loadJobs(){const p=new URLSearchParams({limit:50,include_claimed:'1'});const st=document.getElementById('statusFilter').value;if(st)p.set('status',st);const e=document.getElementById('emailFilter').value.trim();if(e)p.set('email',e);const r=await fetch('/api/jobs?'+p);const d=await r.json();const jobs=d.jobs||[];document.getElementById('m-total').textContent=d.total||jobs.length;document.getElementById('m-running').textContent=jobs.filter(j=>j.status==='pending').length;document.getElementById('m-paid').textContent=jobs.filter(j=>j.status==='paid').length;document.getElementById('m-failed').textContent=jobs.filter(j=>['cancelled','expired'].includes(j.status)).length;document.getElementById('jobRows').innerHTML=jobs.map(j=>`<tr><td>${esc(j.id)}</td><td>OpenAI Plus - ${esc((j.provider||'GoPay').toUpperCase())}</td><td>${esc(j.account_email||j.account_name||'-')}</td><td>${esc(j.provider_url||j.paypal_url||'-')}</td><td>${badge(j.status)}</td><td>${esc(j.notes||j.oauth_status||'')}</td><td><div class="row-actions">${j.provider_url?`<button class="iconbtn play" onclick="window.open('${esc(j.provider_url)}','_blank')">▶</button>`:''}<button class="iconbtn stop" onclick="cancelJob('${j.id}')">■</button></div></td></tr>`).join('')||'<tr><td colspan="7">暂无任务</td></tr>';refreshTopStats()}
async function cancelJob(id){await fetch('/api/jobs/'+id+'/cancel',{method:'PUT'});loadJobs()}
async function bulkCancel(){if(!confirm('取消当前筛选下的运行中任务？'))return;const rows=[...document.querySelectorAll('#jobRows tr')];for(const tr of rows){const id=tr.children[0]?.textContent;if(id)await fetch('/api/jobs/'+id+'/cancel',{method:'PUT'})}loadJobs()}
async function startRegister(){const body={phone:regPhone.value.trim(),pin:regPin.value.trim(),country_code:regCountry.value.trim(),force_live:regForce.value==='1',login_existing:regTaskType.value==='login',relogin_after_register:false,claim_envelope_after_register:regTaskType.value==='register'&&regClaimEnvelope.checked,proxy:regUseProxy.checked?regProxy.value.trim():''};const r=await fetch('/api/manual-register',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});const d=await r.json();if(!r.ok){alert(d.error||'创建失败');return}activeManualId=d.id;renderManualDetail(d);loadManualJobs();refreshTopStats()}
async function loadManualJobs(){const r=await fetch('/api/manual-register');const d=await r.json();const jobs=d.jobs||[];document.getElementById('manualRows').innerHTML=jobs.map(j=>`<tr onclick="activeManualId='${j.id}';renderManualDetail(${JSON.stringify(j).replace(/"/g,'&quot;')})"><td>${esc(j.id)}</td><td>${esc(j.phone)}</td><td>${badge(j.status)}</td><td>${esc(j.message)}</td><td>${fmt(j.created_at)}</td></tr>`).join('')||'<tr><td colspan="5">暂无注册任务</td></tr>';if(activeManualId){const j=jobs.find(x=>x.id===activeManualId);if(j)renderManualDetail(j)}refreshTopStats()}
function renderManualDetail(j){const logs=(j.logs||[]).map(x=>`[${fmt(x.at)}] ${x.message}`).join('\n');document.getElementById('regLog').textContent=logs||j.message||'运行中';if(j.prompt){document.getElementById('otpPrompt').innerHTML=`<div class="prompt"><b>${esc(j.prompt.label)}</b><div class="muted">发送到 ${esc(j.prompt.phone)}</div><div class="field" style="margin-top:10px"><input id="manualOtpCode" placeholder="输入验证码"></div><button class="btn primary" onclick="submitManualOtp('${j.id}')">提交验证码</button></div>`}else{document.getElementById('otpPrompt').innerHTML=''}}
async function submitManualOtp(id){const code=document.getElementById('manualOtpCode').value.trim();if(!code)return;await fetch('/api/manual-register/'+id+'/otp',{method:'POST',headers:authHeaders(),body:JSON.stringify({code})});setTimeout(loadManualJobs,400)}
async function loadAccounts(){const r=await fetch('/api/accounts');const d=await r.json();const a=d.accounts||[];cachedAccounts=a;const usable=a.filter(x=>(x.use_status||'available')==='available'&&Number(x.balance||0)>=1);const opts=usable.map(x=>`<option value="${esc(x.phone)}">${esc(x.phone)} ｜ ${esc(x.balance)} Rp ｜ 可用</option>`).join('');const sel=document.getElementById('payPhone');if(sel)sel.innerHTML=opts||'<option value="">暂无可用 GoPay 账号</option>';document.getElementById('accountRows').innerHTML=a.map(x=>{const status=x.use_status||'available';const balance=Number(x.balance||0);const payable=status==='available'&&balance>=1;const canRefresh=!['used_success','used_failed','reserved'].includes(status);const bind=[x.midtrans_binding_order_id,x.midtrans_binding_email].filter(Boolean).join(' / ');const reason=(status==='used_failed'||status==='no_balance'||status==='reserved')?(x.use_message||'-'):'-';return `<tr data-phone="${esc(x.phone)}"><td>${esc(x.phone)}</td><td>${esc(x.local)}</td><td class="balance-cell">${esc(x.balance)} Rp</td><td>${badge(status)}</td><td class="mono">${esc(bind||'-')}</td><td>${esc(reason)}</td><td>${esc(x.customer_id||x.account_id||'-')}</td><td>${fmt(x.registered_at)}</td><td><button class="btn" onclick="checkBalance('${esc(x.phone)}')">查余额</button><button class="btn soft" onclick="claimReward('${esc(x.phone)}')" ${canRefresh?'':'disabled'}>补激活/查余额</button><button class="btn soft" onclick="claimEnvelope('${esc(x.phone)}')" ${payable?'':'disabled'}>领节日红包</button><button class="btn primary" onclick="preparePay('${esc(x.phone)}')" ${payable?'':'disabled'}>去支付</button><span class="muted balance-msg" style="margin-left:8px"></span></td></tr>`}).join('')||'<tr><td colspan="9">暂无账号</td></tr>';refreshTopStats()}
async function loadEnvelopes(){const s=document.getElementById('envelopeStatus');if(s)s.textContent='读取中...';const r=await fetch('/api/envelopes');const d=await r.json();const links=d.links||[];if(envelopeUrl)envelopeUrl.value=(links[0]&&links[0].url&&!links[0].url.startsWith('deeplink://'))?links[0].url:'';if(s)s.textContent=links.length?`已配置 ${links.length} 条，状态 ${links[0].status||'-'}`:'未配置红包链接'}
async function saveEnvelope(){const s=document.getElementById('envelopeStatus');if(s)s.textContent='保存并解析中...';const r=await fetch('/api/envelopes',{method:'POST',headers:authHeaders(),body:JSON.stringify({url:envelopeUrl.value.trim()})});const d=await r.json();if(!r.ok){if(s)s.textContent=d.error||'保存失败';return}if(s)s.textContent=d.message||'已保存';loadEnvelopes()}
async function clearEnvelope(){envelopeUrl.value='';await saveEnvelope()}
function preparePay(phone){showView('payment');setTimeout(()=>{payPhone.value=phone;const url=(payUrl&&payUrl.value||'').trim();const prompt=document.getElementById('paymentPrompt');if(!url||!url.includes('midtrans.com')){if(prompt)prompt.innerHTML='<div class="prompt"><b>缺少 Midtrans 链接</b><div class="muted">先在支付页生成或粘贴已确认优惠的 Midtrans 1 IDR 授权链。</div></div>';return}startPayment()},120)}
async function checkBalance(phone){const row=document.querySelector(`tr[data-phone="${CSS.escape(phone)}"]`);const msg=row?row.querySelector('.balance-msg'):null;const cell=row?row.querySelector('.balance-cell'):null;if(msg)msg.textContent='查询中...';const r=await fetch('/api/accounts/'+encodeURIComponent(phone)+'/balance',{method:'POST'});const d=await r.json();if(!r.ok){if(msg)msg.textContent=d.error||'余额查询失败';return}if(cell)cell.textContent=`${d.balance} Rp`;if(msg)msg.textContent='已更新';const opt=[...document.querySelectorAll('#payPhone option')].find(o=>o.value===phone);if(opt)opt.textContent=`${d.phone} ｜ ${d.balance} Rp`}
async function claimReward(phone){const row=document.querySelector(`tr[data-phone="${CSS.escape(phone)}"]`);const msg=row?row.querySelector('.balance-msg'):null;const cell=row?row.querySelector('.balance-cell'):null;if(msg)msg.textContent='处理中...';const r=await fetch('/api/accounts/'+encodeURIComponent(phone)+'/reward',{method:'POST'});const d=await r.json();if(!r.ok){if(msg)msg.textContent=d.error||'处理失败';return}if(cell)cell.textContent=`${d.balance} Rp`;if(msg)msg.textContent=d.message||'已刷新';const opt=[...document.querySelectorAll('#payPhone option')].find(o=>o.value===phone);if(opt)opt.textContent=`${d.phone} ｜ ${d.balance} Rp`}
async function claimEnvelope(phone){const row=document.querySelector(`tr[data-phone="${CSS.escape(phone)}"]`);const msg=row?row.querySelector('.balance-msg'):null;const cell=row?row.querySelector('.balance-cell'):null;if(msg)msg.textContent='领取红包中...';const r=await fetch('/api/accounts/'+encodeURIComponent(phone)+'/envelope',{method:'POST'});const d=await r.json();if(!r.ok){if(msg)msg.textContent=d.error||'领取失败';return}if(cell)cell.textContent=`${d.balance} Rp`;if(msg)msg.textContent=d.message||'已刷新';const opt=[...document.querySelectorAll('#payPhone option')].find(o=>o.value===phone);if(opt)opt.textContent=`${d.phone} ｜ ${d.balance} Rp`;loadEnvelopes()}
async function checkSelectedBalance(){if(!payPhone.value)return alert('先选择账号');checkBalance(payPhone.value)}
async function generateMidtrans(){const token=openaiAt.value.trim();if(!token)return alert('先填写 OpenAI AT');const status=document.getElementById('checkoutStatus');status.textContent='生成中，日本固定代理检测中，正在确认 Plus 优惠...';const body={access_token:token,cookie_header:'',device_id:'',account_email:checkoutEmail.value.trim()};try{const r=await fetch('/api/openai-checkout/midtrans',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});const d=await r.json();if(!r.ok){status.textContent=d.error||'生成失败';payUrl.value='';generatedInboxJobId='';return}payUrl.value=d.midtrans_url||'';generatedInboxJobId=(d.inbox_job&&d.inbox_job.id)||'';const amount=d.gross_amount&&d.currency?`${d.gross_amount} ${d.currency}`:'1 IDR';const country=d.egress_country?`，出口 ${d.egress_country}`:'';const promo=d.has_promo?'，优惠已确认':`，优惠状态 ${d.promo_status||'-'}`;status.textContent=generatedInboxJobId?`已生成 ${amount} 授权链${country}${promo}，任务 ${generatedInboxJobId}`:`已生成 ${amount} 授权链${country}${promo}`;loadJobs()}catch(e){status.textContent='生成失败: '+e;payUrl.value='';generatedInboxJobId=''}}
async function startPayment(){if(!payPhone.value)return alert('先选择 GoPay 账号');if(!payUrl.value.trim()||!payUrl.value.includes('midtrans.com'))return alert('先生成或粘贴 Midtrans 链接');const body={phone:payPhone.value,pin:payPin.value,midtrans_url:payUrl.value,inbox_job_id:generatedInboxJobId};const r=await fetch('/api/payment-tasks',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});const d=await r.json();if(!r.ok){alert(d.error||'支付任务创建失败');return}activePaymentId=d.id;renderPaymentDetail(d);loadPaymentJobs()}
async function claimAndPay(){const body={phone:payPhone.value,pin:payPin.value};const r=await fetch('/api/payment-tasks/claim-next',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});const d=await r.json();if(!r.ok){alert(d.error||'领取失败');return}activePaymentId=d.id;renderPaymentDetail(d);loadPaymentJobs();loadJobs()}
async function loadPaymentJobs(){const r=await fetch('/api/payment-tasks');const d=await r.json();const jobs=d.jobs||[];document.getElementById('paymentRows').innerHTML=jobs.map(j=>`<tr onclick="activePaymentId='${j.id}';renderPaymentDetail(${JSON.stringify(j).replace(/"/g,'&quot;')})"><td>${esc(j.id)}</td><td>${esc(j.phone)}</td><td>${badge(j.status)}</td><td>${esc(j.message)}</td><td>${fmt(j.created_at)}</td></tr>`).join('')||'<tr><td colspan="5">暂无支付任务</td></tr>';if(activePaymentId){const j=jobs.find(x=>x.id===activePaymentId);if(j)renderPaymentDetail(j)}}
function setFlow(id,state,text){const el=document.getElementById(id);if(!el)return;el.className='flow-step '+(state||'');const b=el.querySelector('b');if(b)b.textContent=text||'等待'}
function updatePaymentFlow(j){const status=j.status||'';const logs=(j.logs||[]).map(x=>String(x.message||'')).join('\n');setFlow('payFlowPrecheck',logs.includes('预检通过')||status?'success':'','完成');setFlow('payFlowLink',logs.includes('Linking complete')||logs.includes('GoPay linked')?'success':(logs.includes('linking')?'running':''),logs.includes('GoPay linked')?'完成':(logs.includes('linking')?'进行中':'等待'));setFlow('payFlowOtp',status==='waiting_otp'?'running':(logs.includes('OTP 已提交')||logs.includes('PIN verify')?'success':''),status==='waiting_otp'?'等待验证码':(logs.includes('OTP 已提交')||logs.includes('PIN verify')?'完成':'等待'));setFlow('payFlowDone',status==='success'?'success':(status==='failed'?'failed':(status==='running'?'running':'')),status==='success'?'成功':(status==='failed'?'失败':(status==='running'?'进行中':'等待')))}
function renderPaymentDetail(j){updatePaymentFlow(j);const logs=(j.logs||[]).map(x=>`[${fmt(x.at)}] ${x.message}`).join('\n');document.getElementById('paymentLog').textContent=logs||j.message||'运行中';if(j.prompt){document.getElementById('paymentPrompt').innerHTML=`<div class="prompt"><b>${esc(j.prompt.label)}</b><div class="muted">发送到 ${esc(j.prompt.phone)}</div><div class="field" style="margin-top:10px"><input id="paymentOtpCode" placeholder="输入支付验证码"></div><button class="btn primary" onclick="submitPaymentOtp('${j.id}')">提交验证码</button></div>`}else{document.getElementById('paymentPrompt').innerHTML=''}}
async function submitPaymentOtp(id){const code=document.getElementById('paymentOtpCode').value.trim();if(!code)return;await fetch('/api/payment-tasks/'+id+'/otp',{method:'POST',headers:authHeaders(),body:JSON.stringify({code})});setTimeout(loadPaymentJobs,400)}
async function saveSmsConfig(){const body={api_base_url:smsApiBaseUrl.value.trim(),api_key:smsApiKey.value.trim(),service:smsApiService.value.trim(),country:smsApiCountry.value.trim()};smsConfigStatus.textContent='保存中...';const r=await fetch('/api/sms-config',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});const d=await r.json();if(!r.ok){smsConfigStatus.textContent=d.error||'保存失败';return}smsApiKey.value='';smsConfigStatus.textContent='已保存';loadOtp(true)}
async function loadOtp(balance=false){const r=await fetch('/api/sms-status'+(balance?'?balance=1':''));const d=await r.json();if(smsApiBaseUrl&&!smsApiBaseUrl.value)smsApiBaseUrl.value=d.api_base_url||'';if(smsApiService&&!smsApiService.value)smsApiService.value=d.service||'ni';if(smsApiCountry&&!smsApiCountry.value)smsApiCountry.value=d.country||'6';const rows=[['API',d.provider||'-'],['Base URL',d.api_base_url||'-'],['Key',d.api_key_configured?`已配置 ${d.api_key||''}`:'未配置'],['余额',d.balance_raw||d.balance_error||'-'],['服务/国家',`${d.service||'-'} / ${d.country||'-'}`],['Env 文件',d.env_file||'-']];document.getElementById('smsApiRows').innerHTML=rows.map(x=>`<tr><th>${esc(x[0])}</th><td>${esc(x[1])}</td></tr>`).join('');document.getElementById('otpBox').textContent=JSON.stringify(d.pending_otp||{},null,2);refreshTopStats()}
document.getElementById('payUrl').addEventListener('input',()=>{generatedInboxJobId='';const s=document.getElementById('checkoutStatus');if(s)s.textContent=''});
setInterval(()=>{loadJobs(); if(document.getElementById('view-register').style.display!=='none')loadGptRegisterTasks(); if(document.getElementById('view-email_orders').style.display!=='none')loadGptEmails(); if(document.getElementById('view-oauth_tokens').style.display!=='none')loadGptAccounts(); if(document.getElementById('view-oauth_tasks').style.display!=='none')loadGptRegisterTasks(); if(document.getElementById('view-payment').style.display!=='none')loadPaymentJobs(); if(document.getElementById('view-otp').style.display!=='none')loadOtp(); if(document.getElementById('view-plus_pool').style.display!=='none')loadPlusPool()},3000);
loadJobs();loadAccounts();loadOtp();loadAutoFlow();refreshTopStats();
</script>
</body>
</html>
"""

_HTML_PAGE = _ADMIN_HTML_PAGE


class _OTPBox:
    """线程安全的 OTP 收发箱，供 GoPay 等服务使用。

    POST /api/otp          → 外部推送验证码 {"phone": "+62xxx", "code": "123456"}
    GET  /api/otp?phone=xx → GoPay 服务拉取验证码（自动消费，只取最新一条）
    """

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._codes: dict[str, list[dict]] = {}  # phone -> [{code, ts}, ...]

    OTP_TTL = 300  # 5 分钟过期

    def push(self, phone: str, code: str) -> None:
        import time as _t
        phone = phone.strip().lstrip("+")
        now = _t.time()
        with self._lock:
            self._codes.setdefault(phone, []).append({
                "code": code.strip(),
                "ts": now,
            })
            # 清理过期 + 只保留最新 10 条
            self._codes[phone] = [
                e for e in self._codes[phone]
                if now - e["ts"] < self.OTP_TTL
            ][-10:]
        log.info("otp_box: pushed code=%s for phone=%s", code, phone)

    def pop(self, phone: str, after_ts: float = 0) -> str | None:
        import time as _t
        phone = phone.strip().lstrip("+")
        now = _t.time()
        with self._lock:
            entries = self._codes.get(phone, [])
            for entry in reversed(entries):
                if entry["ts"] > after_ts and now - entry["ts"] < self.OTP_TTL:
                    entries.remove(entry)
                    return entry["code"]
            # 清理过期
            self._codes[phone] = [e for e in entries if now - e["ts"] < self.OTP_TTL]
        return None

    def list_all(self) -> dict:
        import time as _t
        now = _t.time()
        with self._lock:
            return {
                k: [e.copy() for e in v if now - e["ts"] < self.OTP_TTL]
                for k, v in self._codes.items()
            }


class _ManualRegisterManager:
    """Browser-driven manual registration runner.

    The HTTP UI starts a background registration thread. When the protocol flow
    needs an OTP, the thread waits on a condition variable until the browser
    posts the code.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._conds: dict[str, threading.Condition] = {}

    def _public(self, job: dict[str, Any]) -> dict[str, Any]:
        clean = dict(job)
        clean.pop("_otp", None)
        return clean

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [self._public(j) for j in self._jobs.values()]
        jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return jobs

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._public(job) if job else None

    def start(
        self,
        *,
        phone: str = "",
        pin: str,
        country_code: str = "62",
        signed_up_country: str = "ID",
        force_live: bool = False,
        login_existing: bool = False,
        relogin_after_register: bool = False,
        claim_envelope_after_register: bool = False,
        proxy: str = "",
        source: str = "pool",
    ) -> dict[str, Any]:
        from opai.core.gopay_protocol_worker import (
            _make_proxy,
            _normalize_phone,
            _normalize_phone_for_country,
        )

        source = source if source in {"pool", "smsbower"} else "pool"
        sms_api_key = sms_activation_id = ""
        if source == "smsbower":
            from opai.core.sms_helpers import get_sms_api_key, sms_get_number, sms_cancel
            sms_api_key = get_sms_api_key("")
            if not sms_api_key:
                raise ValueError("SMSBower API key is not configured")
            phone, sms_activation_id = sms_get_number(sms_api_key)
        cc = country_code.strip().lstrip("+") or "62"
        normalized = (
            _normalize_phone(phone)
            if cc == "62" and not force_live
            else _normalize_phone_for_country(phone, cc)
        )
        if not normalized:
            raise ValueError("手机号格式不对。印尼 GoPay 号码用 08...、8...、62... 或 +62...。")
        if cc != "62" and not force_live:
            raise ValueError("非 +62 号码真实请求需要 force_live=true。")

        use_proxy = _rotate_proxy_sid(_normalize_proxy_url(proxy) or _make_proxy())
        probe = _preflight_gopay_proxy(use_proxy)
        if not probe.get("ok"):
            raise ValueError(_proxy_preflight_error(use_proxy, probe))

        job_id = uuid.uuid4().hex[:12]
        now = _now_iso()
        cond = threading.Condition(self._lock)
        with self._lock:
            for old in self._jobs.values():
                if (
                    old.get("phone") == normalized
                    and old.get("status") in {"running", "waiting_otp"}
                    and bool(old.get("login_existing")) == bool(login_existing)
                ):
                    raise ValueError(f"同一个手机号已有未完成任务: {old.get('id')}")
            saved, _idx = _find_gopay_account(normalized)
            # A saved phone is blocked only for a new-registration request.
            # Existing-account login/re-login must continue into the login flow.
            if saved is not None and not login_existing:
                    self._jobs[job_id] = {
                        "id": job_id,
                        "source": source, "phone": normalized,
                        "sms_activation_id": sms_activation_id, "sms_provider": "smsbower" if source == "smsbower" else "sms_url",
                        "raw_phone": phone,
                        "pin": pin,
                        "country_code": f"+{cc}",
                        "signed_up_country": signed_up_country or "ID",
                        "force_live": bool(force_live),
                        "login_existing": bool(login_existing),
                        "relogin_after_register": bool(relogin_after_register),
                        "claim_envelope_after_register": bool(claim_envelope_after_register),
                        "proxy": use_proxy,
                        "status": "already_registered",
                        "message": "号码已在本机账号库，不能作为新号注册；请选择已有账号登录",
                        "created_at": now,
                        "updated_at": now,
                        "prompt": None,
                        "logs": [{"at": now, "message": "本机账号库已存在该手机号，已阻止重复注册"}],
                        "_otp": [],
                        "result": {
                            "phone": saved.get("phone") or normalized,
                            "local": saved.get("local", ""),
                            "already_registered": True,
                        },
                    }
                    return self._public(self._jobs[job_id])
            self._conds[job_id] = cond
            self._jobs[job_id] = {
                "id": job_id, "source": source,
                "phone": normalized,
                "sms_activation_id": sms_activation_id, "sms_provider": "smsbower" if source == "smsbower" else "sms_url",
                "raw_phone": phone,
                "pin": pin,
                "country_code": f"+{cc}",
                "signed_up_country": signed_up_country or "ID",
                "force_live": bool(force_live),
                "login_existing": bool(login_existing),
                "relogin_after_register": bool(relogin_after_register),
                "claim_envelope_after_register": bool(claim_envelope_after_register),
                "proxy": use_proxy,
                "status": "running",
                "message": f"代理预检通过: 出口 IP {probe.get('ip') or '-'}",
                "created_at": now,
                "updated_at": now,
                "prompt": None,
                "logs": [{"at": now, "message": f"代理预检通过: 出口 IP {probe.get('ip') or '-'}"}],
                "_otp": [],
            }

        t = threading.Thread(
            target=self._run,
            kwargs={
                "job_id": job_id,
                "phone": normalized,
                "source": source,
                "sms_api_key": sms_api_key,
                "sms_activation_id": sms_activation_id,
                "pin": pin,
                "country_code": f"+{cc}",
                "signed_up_country": signed_up_country or "ID",
                "proxy": use_proxy,
                "login_existing": bool(login_existing),
                "relogin_after_register": bool(relogin_after_register),
                "claim_envelope_after_register": bool(claim_envelope_after_register),
            },
            daemon=True,
            name=f"manual-register-{job_id}",
        )
        t.start()
        return self.get(job_id) or {}

    def submit_otp(self, job_id: str, code: str) -> dict[str, Any] | None:
        code = (code or "").strip()
        with self._lock:
            job = self._jobs.get(job_id)
            cond = self._conds.get(job_id)
            if not job or not cond:
                return None
            job.setdefault("_otp", []).append(code)
            job["message"] = "已收到验证码，继续执行"
            job["updated_at"] = _now_iso()
            cond.notify_all()
            return self._public(job)

    def _append_log(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.setdefault("logs", []).append({
                "at": _now_iso(),
                "message": message,
            })
            job["message"] = message
            job["updated_at"] = _now_iso()

    def _wait_otp(self, job_id: str, purpose: str, phone: str, timeout: int) -> str | None:
        deadline = time.time() + max(1, timeout)
        label_map = {
            "signup": "注册 OTP",
            "pin": "PIN OTP",
            "login": "登录 OTP",
        }
        label = label_map.get(purpose, "OTP")
        with self._lock:
            job = self._jobs.get(job_id)
            cond = self._conds.get(job_id)
            if not job or not cond:
                return None
            job["status"] = "waiting_otp"
            job["prompt"] = {
                "purpose": purpose,
                "label": label,
                "phone": phone,
                "timeout": timeout,
                "started_at": _now_iso(),
            }
            job["message"] = f"等待输入{label}"
            job["updated_at"] = _now_iso()
            job.setdefault("logs", []).append({"at": _now_iso(), "message": f"{label} 已发送到 {phone}"})

            while time.time() < deadline:
                codes = job.setdefault("_otp", [])
                if codes:
                    code = str(codes.pop(0)).strip()
                    job["prompt"] = None
                    job["status"] = "running"
                    job["message"] = f"已提交{label}"
                    job["updated_at"] = _now_iso()
                    return code
                cond.wait(timeout=min(1.0, max(0.1, deadline - time.time())))

            job["prompt"] = None
            job["status"] = "failed"
            job["message"] = f"{label} 输入超时"
            job["updated_at"] = _now_iso()
            return None

    def _run(
        self,
        *,
        job_id: str,
        source: str = "pool",
        phone: str,
        sms_api_key: str = "",
        sms_activation_id: str = "",
        pin: str,
        country_code: str,
        signed_up_country: str,
        proxy: str,
        login_existing: bool,
        relogin_after_register: bool,
        claim_envelope_after_register: bool,
    ) -> None:
        try:
            from opai.core.gopay_protocol_worker import (
                _get_envelope_did,
                _login_one_manual_existing,
                _register_one_from_phone,
            )

            self._append_log(job_id, f"开始真实请求 GoPay: {phone} country_code={country_code}")
            envelope_did = _get_envelope_did()
            if source == "smsbower":
                from opai.core.sms_helpers import sms_wait_code, sms_request_another, sms_done, sms_cancel
                wait_code = lambda purpose, timeout: sms_wait_code(sms_api_key, sms_activation_id, timeout=timeout)
                request_another_code = lambda: sms_request_another(sms_api_key, sms_activation_id)
            else:
                wait_code = lambda purpose, timeout: self._wait_otp(job_id, purpose, phone, timeout)
                request_another_code = None
            if login_existing:
                result = _login_one_manual_existing(
                    phone=phone,
                    pin=pin,
                    proxy=proxy,
                    wait_code=wait_code,
                    country_code=country_code,
                    return_failure=True,
                    status_cb=lambda message: self._append_log(job_id, message),
                )
                if result and result.get("failed") and "auth:error:user:not_found" in str(result.get("error") or "").lower():
                    self._append_log(job_id, "登录返回 user:not_found，自动切换注册流程")
                    result = _register_one_from_phone(phone=phone, aid=sms_activation_id or f"web-{job_id}", pin=pin, proxy=proxy, envelope_did=envelope_did, wait_code=wait_code, request_another_code=request_another_code, country_code=country_code, signed_up_country=signed_up_country, allow_unsupported_country=(country_code != "+62"), return_existing=True, return_failure=True, status_cb=lambda message: self._append_log(job_id, message), relogin_after_register=bool(relogin_after_register), claim_envelope_after_register=bool(claim_envelope_after_register))
            else:
                result = _register_one_from_phone(
                    phone=phone,
                    aid=sms_activation_id or f"web-{job_id}",
                    pin=pin,
                    proxy=proxy,
                    envelope_did=envelope_did,
                    wait_code=wait_code,
                    request_another_code=request_another_code,
                    country_code=country_code,
                    signed_up_country=signed_up_country,
                    allow_unsupported_country=(country_code != "+62"),
                    return_existing=True,
                    return_failure=True,
                    status_cb=lambda message: self._append_log(job_id, message),
                    relogin_after_register=bool(relogin_after_register),
                    claim_envelope_after_register=bool(claim_envelope_after_register),
                )
                if result and result.get("already_registered") and login_existing:
                    self._append_log(job_id, "检测到已注册号码，切换到已有账号登录流程")
                    result = _login_one_manual_existing(
                        phone=phone,
                        pin=pin,
                        proxy=proxy,
                        wait_code=wait_code,
                        country_code=country_code,
                        return_failure=True,
                        status_cb=lambda message: self._append_log(job_id, message),
                    )
            with self._lock:
                job = self._jobs.get(job_id)
                if not job:
                    return
                if result and result.get("failed"):
                    job["status"] = "failed"
                    job["message"] = result.get("error") or "注册失败"
                    job["result"] = {
                        "phone": result.get("phone", phone),
                        "local": result.get("local", ""),
                        "error": result.get("error", ""),
                    }
                elif result:
                    if result.get("already_registered"):
                        job["status"] = "already_registered"
                        job["message"] = "号码已注册，不能作为新号注册"
                    elif result.get("logged_in_existing"):
                        job["status"] = "success"
                        job["message"] = "已有账号登录完成"
                    elif result.get("relogged_in"):
                        job["status"] = "success"
                        job["message"] = "注册完成，已退出登录并重新登录更新 token"
                    else:
                        job["status"] = "success"
                        job["message"] = "注册完成"
                    job["result"] = {
                        "phone": result.get("phone", phone),
                        "local": result.get("local", ""),
                        "pin": pin,
                        "already_registered": bool(result.get("already_registered")),
                        "logged_in_existing": bool(result.get("logged_in_existing")),
                        "relogged_in": bool(result.get("relogged_in")),
                    }
                else:
                    job["status"] = "failed"
                    job["message"] = "注册失败，查看服务端日志里的接口返回"
                job["prompt"] = None
                job["updated_at"] = _now_iso()
            if source == "smsbower" and sms_activation_id:
                from opai.core.sms_helpers import sms_done, sms_cancel
                if result and not result.get("failed") and not result.get("already_registered"):
                    sms_done(sms_api_key, sms_activation_id)
                else:
                    sms_cancel(sms_api_key, sms_activation_id)
        except Exception as exc:
            log.exception("manual register job failed: %s", job_id)
            if source == "smsbower" and sms_activation_id:
                try:
                    from opai.core.sms_helpers import sms_cancel
                    sms_cancel(sms_api_key, sms_activation_id)
                except Exception:
                    log.debug("SMSBower cancellation failed", exc_info=True)
            with self._lock:
                job = self._jobs.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["message"] = str(exc)
                    job["prompt"] = None
                    job["updated_at"] = _now_iso()


class _WebPaymentManager:
    """Browser-driven Midtrans GoPay payment runner."""

    def __init__(self, store: InboxStore):
        self._store = store
        self._lock = threading.RLock()
        self._state_path = PROJECT_ROOT / "config" / "payment_tasks.json"
        state = self._read_state()
        self._jobs: dict[str, dict[str, Any]] = {
            str(job.get("id")): job
            for job in state.get("jobs", [])
            if isinstance(job, dict) and job.get("id")
        }
        self._conds: dict[str, threading.Condition] = {}
        self._snap_states: dict[str, dict[str, Any]] = _load_snap_states()
        changed = self._normalize_loaded_jobs()
        if not self._jobs:
            self._jobs.update(self._backfill_from_auto_flow_history())
            changed = bool(self._jobs) or changed
        if changed and self._jobs:
            self._save_state_locked()

    def _read_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            log.warning("payment task state read failed: %s", self._state_path, exc_info=True)
            return {}

    def _backfill_from_auto_flow_history(self) -> dict[str, dict[str, Any]]:
        path = PROJECT_ROOT / "config" / "auto_flow_tasks.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.debug("auto flow history backfill read failed: %s", path, exc_info=True)
            return {}
        jobs: dict[str, dict[str, Any]] = {}
        for row in data.get("jobs", []):
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip()
            if status not in {"success", "failed"}:
                continue
            midtrans_url = str(row.get("midtrans_url") or "").strip()
            if not midtrans_url:
                continue
            job_id = str(row.get("payment_task_id") or row.get("id") or "").strip()
            if not job_id:
                continue
            phone = str(row.get("phone") or row.get("gopay_phone") or row.get("email") or "").strip()
            message = str(row.get("payment_message") or row.get("message") or "").strip()
            if status == "success" and message == "payment completed":
                message = "支付完成"
            logs = row.get("logs") if isinstance(row.get("logs"), list) else []
            jobs[job_id] = {
                "id": job_id,
                "phone": phone,
                "local": _digits(phone),
                "midtrans_url": midtrans_url,
                "snap_token": _extract_midtrans_snap_token(midtrans_url),
                "midtrans_meta": {"order_id": row.get("order_id") or ""},
                "inbox_job_id": str(row.get("inbox_job_id") or ""),
                "auto_flow_job_id": str(row.get("id") or ""),
                "status": "success" if status == "success" else "failed",
                "message": message or ("支付完成" if status == "success" else "支付失败"),
                "created_at": str(row.get("created_at") or row.get("updated_at") or _now_iso()),
                "updated_at": str(row.get("updated_at") or row.get("created_at") or _now_iso()),
                "prompt": None,
                "logs": logs,
                "source": "auto_flow_backfill",
            }
        return jobs

    def _normalize_loaded_jobs(self) -> bool:
        changed = False
        for job in self._jobs.values():
            status = str(job.get("status") or "").strip()
            if status in {"running", "waiting_otp"}:
                job["status"] = "failed"
                job["message"] = "服务重启中断，已保留为失败历史"
                job["prompt"] = None
                job["updated_at"] = _now_iso()
                job.setdefault("logs", []).append({
                    "at": _now_iso(),
                    "message": "服务重启后无法继续这个网页支付任务，已保留为失败历史",
                })
                changed = True
            if job.get("status") == "success" and job.get("message") == "payment completed":
                job["message"] = "支付完成"
                changed = True
        return changed

    def _persistable_job(self, job: dict[str, Any]) -> dict[str, Any]:
        clean = dict(job)
        clean.pop("_otp", None)
        clean.pop("_cancel_requested", None)
        clean.pop("pin", None)
        return clean

    def _save_state_locked(self) -> None:
        rows = sorted(self._jobs.values(), key=lambda j: j.get("created_at", ""), reverse=True)[:500]
        payload = {
            "jobs": [self._persistable_job(row) for row in rows],
            "updated_at": _now_iso(),
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._state_path)

    def _public(self, job: dict[str, Any]) -> dict[str, Any]:
        clean = dict(job)
        clean.pop("_otp", None)
        clean.pop("_cancel_requested", None)
        clean.pop("pin", None)
        return clean

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [self._public(j) for j in self._jobs.values()]
        jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return jobs

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._public(job) if job else None

    def _save_snap_state_locked(self) -> None:
        _write_snap_states(self._snap_states)

    def _reserve_snap_locked(self, snap: str, *, job_id: str, phone: str, midtrans_url: str) -> None:
        existing = self._snap_states.get(snap) or {}
        existing_status = str(existing.get("status") or "").strip()
        allow_retry = (os.environ.get("OPAI_PAYMENT_ALLOW_SNAP_RETRY") or "").strip() == "1"
        retryable_status = existing_status in {"failed", "fraud_denied"}
        if existing_status and not retryable_status and not allow_retry:
            raise ValueError(
                "这条 Midtrans 链接已经跑过或正在运行，不能重复支付；"
                f"当前状态={existing_status}，请重新用 AT 生成新链接"
            )
        self._snap_states[snap] = {
            "snap": snap,
            "job_id": job_id,
            "phone": phone,
            "midtrans_url": midtrans_url,
            "status": "running",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        self._save_snap_state_locked()

    def _update_snap_state(self, snap: str, status: str, *, reason: str = "", job_id: str = "") -> None:
        if not snap:
            return
        with self._lock:
            state = self._snap_states.setdefault(snap, {"snap": snap, "created_at": _now_iso()})
            if job_id:
                state["job_id"] = job_id
            state["status"] = status
            if reason:
                state["reason"] = reason
            state["updated_at"] = _now_iso()
            self._save_snap_state_locked()

    def start(self, *, phone: str, pin: str, midtrans_url: str, inbox_job_id: str = "", proxy: str = "") -> dict[str, Any]:
        account, _idx = _find_gopay_account(phone)
        if account is None:
            raise ValueError(f"账号不存在: {phone}")
        payment_profile = ensure_account_payment_fingerprint(account)
        use_proxy = _normalize_proxy_url(proxy) or _normalize_proxy_url(account.get("proxy", "")) or _default_gopay_proxy()
        url = (midtrans_url or "").strip()
        if "midtrans.com" not in url:
            raise ValueError("Midtrans 链接不正确")
        snap = _extract_midtrans_snap_token(url)
        if not snap:
            raise ValueError("Midtrans 链接里没有 snap token")
        probe = _preflight_gopay_proxy(use_proxy)
        if not probe.get("ok"):
            raise ValueError(_proxy_preflight_error(use_proxy, probe))
        balance_info = _refresh_gopay_balance(account.get("phone") or phone)
        balance = int(balance_info.get("balance", 0) or 0)
        meta = _midtrans_transaction_meta(url, proxy=use_proxy, payment_fingerprint=payment_profile)
        _validate_payment_midtrans_meta(meta, balance=balance)
        block_reason = _gopay_binding_block_reason(account, order_id=str(meta.get("order_id") or ""))
        if block_reason:
            raise ValueError(block_reason)
        job_id = uuid.uuid4().hex[:12]
        now = _now_iso()
        cond = threading.Condition(self._lock)
        with self._lock:
            self._reserve_snap_locked(snap, job_id=job_id, phone=account.get("phone", phone), midtrans_url=url)
            if not _reserve_gopay_midtrans_binding(
                account.get("phone", phone),
                job_id=job_id,
                order_id=str(meta.get("order_id") or ""),
                midtrans_url=url,
                email="",
            ):
                raise ValueError("这个 GoPay 账号已经绑定过其他 Midtrans 链，不能重复使用")
            self._conds[job_id] = cond
            self._jobs[job_id] = {
                "id": job_id,
                "phone": account.get("phone", phone),
                "local": account.get("local", ""),
                "pin": pin or account.get("pin", ""),
                "midtrans_url": url,
                "snap_token": snap,
                "midtrans_meta": meta,
                "balance_before": balance,
                "payment_fingerprint": payment_profile,
                "proxy": use_proxy,
                "inbox_job_id": inbox_job_id,
                "status": "running",
                "message": f"预检通过: {meta.get('order_id')} {meta.get('gross_amount')} {meta.get('currency')}，余额 {balance} Rp",
                "created_at": now,
                "updated_at": now,
                "prompt": None,
                "logs": [
                    {"at": now, "message": f"代理预检通过: 出口 IP {probe.get('ip') or '-'}"},
                    {"at": now, "message": f"预检通过: {meta.get('order_id')} {meta.get('gross_amount')} {meta.get('currency')}，余额 {balance} Rp"},
                    {"at": now, "message": "GoPay 账号已预占；该号后续不会再绑定其他 Midtrans 链"},
                ],
                "_otp": [],
            }
            self._save_state_locked()
        t = threading.Thread(
            target=self._run,
            kwargs={
                "job_id": job_id,
                "phone": account.get("phone", phone),
                "local": account.get("local", ""),
                "pin": pin or account.get("pin", ""),
                "midtrans_url": url,
                "inbox_job_id": inbox_job_id,
                "proxy": use_proxy,
                "payment_fingerprint": payment_profile,
                "midtrans_client_key": str(meta.get("midtrans_client_key") or ""),
            },
            daemon=True,
            name=f"web-payment-{job_id}",
        )
        t.start()
        return self.get(job_id) or {}

    def claim_and_start(self, *, phone: str, pin: str, proxy: str = "") -> dict[str, Any]:
        job = self._store.claim_next_pending(
            prefer_paypal_url=True,
            prefer_oldest=True,
            ttl_sec=60.0,
            provider="gopay",
        )
        if job is None:
            raise LookupError("没有可领取的 GoPay 订阅任务")
        url = job.get("provider_url") or job.get("paypal_url") or ""
        if not url:
            raise ValueError("领取到的任务没有 Midtrans 链接")
        try:
            return self.start(phone=phone, pin=pin, midtrans_url=url, inbox_job_id=job["id"], proxy=proxy)
        except Exception:
            self._store.set_status_if_pending(job["id"], "cancelled")
            raise

    def submit_otp(self, job_id: str, code: str) -> dict[str, Any] | None:
        code = (code or "").strip()
        with self._lock:
            job = self._jobs.get(job_id)
            cond = self._conds.get(job_id)
            if not job or not cond:
                return None
            job.setdefault("_otp", []).append(code)
            job["message"] = "已收到支付 OTP，继续执行"
            job["updated_at"] = _now_iso()
            self._save_state_locked()
            cond.notify_all()
            return self._public(job)

    def _append_log(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if job.get("_cancel_requested"):
                raise RuntimeError("GoPay 支付已随主协议任务停止")
            job.setdefault("logs", []).append({"at": _now_iso(), "message": message})
            job["message"] = message
            job["updated_at"] = _now_iso()
            self._save_state_locked()

    def _wait_otp(self, job_id: str, phone: str, timeout: int) -> str | None:
        deadline = time.time() + max(1, timeout)
        with self._lock:
            job = self._jobs.get(job_id)
            cond = self._conds.get(job_id)
            if not job or not cond:
                return None
            job["status"] = "waiting_otp"
            snap = str(job.get("snap_token") or "")
            if snap:
                state = self._snap_states.setdefault(snap, {"snap": snap, "created_at": _now_iso()})
                state["status"] = "waiting_otp"
                state["updated_at"] = _now_iso()
                self._save_snap_state_locked()
            job["prompt"] = {
                "label": "支付 OTP",
                "phone": phone,
                "timeout": timeout,
                "started_at": _now_iso(),
            }
            job["message"] = "等待输入支付 OTP"
            job.setdefault("logs", []).append({"at": _now_iso(), "message": f"支付 OTP 已发送到 {phone}"})
            job["updated_at"] = _now_iso()
            self._save_state_locked()

        try:
            account, _idx = _find_gopay_account(phone)
            aid = str((account or {}).get("activation_id") or (account or {}).get("aid") or "").strip()
            if aid:
                from opai.core.sms_helpers import get_sms_api_key, sms_request_another, sms_wait_code

                api_key = get_sms_api_key("")
                if api_key:
                    self._append_log(job_id, "使用短信 API 自动等待支付 OTP")
                    try:
                        sms_request_another(api_key, aid)
                    except Exception:
                        log.debug("payment sms_request_another failed", exc_info=True)
                    code = sms_wait_code(api_key, aid, timeout=max(1, int(deadline - time.time())))
                    if code:
                        self._append_log(job_id, "支付 OTP 已由短信 API 获取，自动提交")
                        with self._lock:
                            job = self._jobs.get(job_id)
                            if job:
                                job["prompt"] = None
                                job["status"] = "running"
                                job["message"] = "支付 OTP 已提交"
                                job["updated_at"] = _now_iso()
                                self._save_state_locked()
                        return code
                    self._append_log(job_id, "短信 API 未取到支付 OTP，继续等待网页手动输入")
        except Exception:
            log.debug("payment auto sms otp failed", exc_info=True)

        with self._lock:
            job = self._jobs.get(job_id)
            cond = self._conds.get(job_id)
            if not job or not cond:
                return None
            while time.time() < deadline:
                if job.get("_cancel_requested"):
                    job["prompt"] = None
                    job["status"] = "failed"
                    job["message"] = "GoPay 支付已随主协议任务停止"
                    job["updated_at"] = _now_iso()
                    self._save_state_locked()
                    return None
                codes = job.setdefault("_otp", [])
                if codes:
                    code = str(codes.pop(0)).strip()
                    job["prompt"] = None
                    job["status"] = "running"
                    job["message"] = "支付 OTP 已提交"
                    job.setdefault("logs", []).append({"at": _now_iso(), "message": "支付 OTP 已提交，开始验证"})
                    job["updated_at"] = _now_iso()
                    self._save_state_locked()
                    return code
                cond.wait(timeout=min(1.0, max(0.1, deadline - time.time())))
            job["prompt"] = None
            job["status"] = "failed"
            job["message"] = "支付 OTP 输入超时"
            job["updated_at"] = _now_iso()
            self._save_state_locked()
            return None

    def _run(
        self,
        *,
        job_id: str,
        phone: str,
        local: str,
        pin: str,
        midtrans_url: str,
        inbox_job_id: str,
        proxy: str,
        payment_fingerprint: dict[str, Any] | None = None,
        midtrans_client_key: str = "",
    ) -> None:
        snap = _extract_midtrans_snap_token(midtrans_url)
        try:
            from opai.core.gopay_payment_protocol import GoPayFraudDenyError, GoPayPayment

            local_phone = local or _digits(phone)
            if local_phone.startswith("62"):
                local_phone = local_phone[2:]
            if payment_fingerprint is None:
                account, _idx = _find_gopay_account(phone)
                if account:
                    payment_fingerprint = ensure_account_payment_fingerprint(account)
            if payment_fingerprint:
                self._append_log(job_id, f"支付指纹 profile_id={payment_fingerprint.get('profile_id', '')}")
            probe = _preflight_gopay_proxy(proxy)
            if not probe.get("ok"):
                raise RuntimeError(_proxy_preflight_error(proxy, probe))
            self._append_log(job_id, f"支付前代理复检通过: 出口 IP {probe.get('ip') or '-'}")
            self._append_log(job_id, f"开始支付: {phone} -> {midtrans_url}")
            self._update_snap_state(snap, "linking", job_id=job_id)
            payment = GoPayPayment(proxy=proxy, payment_fingerprint=payment_fingerprint)
            result = payment.pay(
                midtrans_url=midtrans_url,
                phone=local_phone,
                country_code="62",
                pin=pin,
                wait_otp=lambda otp_phone, timeout: self._wait_otp(job_id, otp_phone, timeout),
                progress=lambda message: self._append_log(job_id, message),
                midtrans_client_key=midtrans_client_key,
            )
            with self._lock:
                job = self._jobs.get(job_id)
                if not job:
                    return
                job["result"] = result
                if result.get("success"):
                    job["status"] = "success"
                    job["message"] = result.get("detail") or "支付完成"
                    job.setdefault("logs", []).append({
                        "at": _now_iso(),
                        "message": f"支付成功: {job['message']}，交易状态 {result.get('transaction_status', '-')}",
                    })
                    self._update_snap_state(snap, "success", reason=job["message"], job_id=job_id)
                    _update_gopay_midtrans_binding_status(phone, "success", message=f"支付成功: {job['message']}")
                    if inbox_job_id:
                        self._store.set_status_if_pending(inbox_job_id, "paid")
                else:
                    label = _payment_failure_label(str(result.get("detail") or ""))
                    job["status"] = "failed"
                    job["message"] = label
                    result["failure_label"] = label
                    job.setdefault("logs", []).append({
                        "at": _now_iso(),
                        "message": f"支付失败: {label}，详情 {str(result.get('detail') or '')[:300]}",
                    })
                    self._update_snap_state(snap, "failed", reason=label, job_id=job_id)
                    _update_gopay_midtrans_binding_status(phone, "failed", message=label)
                    if inbox_job_id:
                        self._store.set_status_if_pending(inbox_job_id, "cancelled")
                job["prompt"] = None
                job["updated_at"] = _now_iso()
                self._save_state_locked()
        except GoPayFraudDenyError as exc:
            label = _payment_failure_label(str(exc))
            log.exception("web payment fraud denied: %s", job_id)
            self._update_snap_state(snap, "fraud_denied", reason=label, job_id=job_id)
            _update_gopay_midtrans_binding_status(phone, "failed", message=label)
            with self._lock:
                job = self._jobs.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["message"] = label
                    job["result"] = {"success": False, "detail": str(exc), "failure_label": label}
                    job.setdefault("logs", []).append({
                        "at": _now_iso(),
                        "message": f"支付失败: {label}，详情 {str(exc)[:300]}",
                    })
                    job["prompt"] = None
                    job["updated_at"] = _now_iso()
                    self._save_state_locked()
        except Exception as exc:
            log.exception("web payment job failed: %s", job_id)
            label = _payment_failure_label(str(exc))
            self._update_snap_state(snap, "failed", reason=label, job_id=job_id)
            _update_gopay_midtrans_binding_status(phone, "failed", message=label)
            with self._lock:
                job = self._jobs.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["message"] = label
                    job["result"] = {"success": False, "detail": str(exc), "failure_label": label}
                    job.setdefault("logs", []).append({
                        "at": _now_iso(),
                        "message": f"支付异常: {label}，详情 {str(exc)[:300]}",
                    })
                    job["prompt"] = None
                    job["updated_at"] = _now_iso()
                    self._save_state_locked()


class _AutoFlowManager:
    """GPT success account -> Midtrans setup link -> GoPay account -> payment."""

    def __init__(self, server: "_InboxServer", gpt_register: GptRegisterBridge, web_payment: _WebPaymentManager) -> None:
        self._server = server
        self._gpt_register = gpt_register
        self._web_payment = web_payment
        self._lock = threading.RLock()
        self._register_lock = threading.RLock()
        self._state_path = PROJECT_ROOT / "config" / "auto_flow_tasks.json"
        state = self._read_state()
        self._jobs: dict[str, dict[str, Any]] = {
            str(job.get("id")): job for job in state.get("jobs", []) if isinstance(job, dict) and job.get("id")
        }
        self._processed: set[str] = set(str(x).lower() for x in state.get("processed_emails", []) if str(x).strip())
        self._seen: set[str] = set(str(x).lower() for x in state.get("seen_emails", []) if str(x).strip())
        self._enabled = bool(state.get("enabled", False))
        self._pin = str(state.get("pin") or os.environ.get("OPAI_GOPAY_PIN") or "147258")
        self._proxy = str(state.get("proxy") or "")
        self._watcher_started = False

    def _read_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state_locked(self) -> None:
        rows = sorted(self._jobs.values(), key=lambda j: j.get("created_at", ""), reverse=True)[:500]
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps({
            "enabled": self._enabled,
            "pin": self._pin,
            "proxy": self._proxy,
            "processed_emails": sorted(self._processed),
            "seen_emails": sorted(self._seen),
            "jobs": rows,
            "updated_at": _now_iso(),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _append_log(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.setdefault("logs", []).append({"at": _now_iso(), "message": message})
            job["message"] = message
            job["updated_at"] = _now_iso()
            self._save_state_locked()

    def _update_job(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.update(fields)
            job["updated_at"] = _now_iso()
            self._save_state_locked()

    def _success_records(self) -> list[dict[str, str]]:
        pool = self._gpt_register.success_pool(limit=20000)
        records = []
        for line in pool.get("lines") or []:
            parts = str(line).split("----", 3)
            if len(parts) != 4:
                continue
            email, password, twofa, token = [p.strip() for p in parts]
            if email and token:
                records.append({"email": email, "password": password, "twofa": twofa, "access_token": token})
        return records

    def _record_for_email(self, email: str) -> dict[str, str] | None:
        target = (email or "").strip().lower()
        for row in self._success_records():
            if row["email"].lower() == target:
                return row
        return None

    def list(self) -> dict[str, Any]:
        with self._lock:
            jobs = [dict(job) for job in self._jobs.values()]
            for job in jobs:
                job.pop("access_token", None)
            jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
            return {
                "enabled": self._enabled,
                "pin": self._pin,
                "proxy": self._proxy,
                "processed": len(self._processed),
                "seen": len(self._seen),
                "jobs": jobs,
            }

    def configure(self, *, enabled: bool, pin: str = "", proxy: str = "", new_only: bool = True) -> dict[str, Any]:
        with self._lock:
            self._enabled = bool(enabled)
            if pin.strip():
                self._pin = pin.strip()
            self._proxy = proxy.strip()
            if self._enabled and new_only:
                self._seen = {row["email"].lower() for row in self._success_records()}
            self._save_state_locked()
        return self.list()

    def start_watcher(self) -> None:
        with self._lock:
            if self._watcher_started:
                return
            self._watcher_started = True
        self._resume_interrupted_jobs()
        threading.Thread(target=self._watch_loop, daemon=True, name="auto-flow-watch").start()

    def _resume_interrupted_jobs(self) -> None:
        resumable: list[tuple[str, dict[str, Any], str, str]] = []
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if str(job.get("status") or "") != "running":
                continue
            if str(job.get("stage") or "") not in {"gopay_account", "payment"}:
                continue
            if not str(job.get("midtrans_url") or ""):
                continue
            email = str(job.get("email") or "").strip()
            record = self._record_for_email(email)
            if not record:
                continue
            resumable.append((
                str(job.get("id") or ""),
                record,
                str(job.get("pin") or self._pin or "147258"),
                str(job.get("proxy") or self._proxy or ""),
            ))
        for job_id, record, pin, proxy in resumable:
            if not job_id:
                continue
            self._append_log(job_id, "服务重启后检测到未完成任务，复用已生成 Midtrans 链继续执行")
            threading.Thread(
                target=self._run,
                kwargs={"job_id": job_id, "record": record, "pin": pin, "proxy": proxy},
                daemon=True,
                name=f"auto-flow-resume-{job_id}",
            ).start()

    def _watch_loop(self) -> None:
        while True:
            try:
                with self._lock:
                    enabled = self._enabled
                    pin = self._pin
                    proxy = self._proxy
                if enabled:
                    for row in self._success_records():
                        key = row["email"].lower()
                        with self._lock:
                            if key in self._seen or key in self._processed:
                                continue
                            self._seen.add(key)
                            self._save_state_locked()
                        self.start(email=row["email"], pin=pin, proxy=proxy, force=False)
            except Exception:
                log.debug("auto flow watch loop failed", exc_info=True)
            time.sleep(5)

    def start(
        self,
        *,
        email: str,
        pin: str = "",
        proxy: str = "",
        force: bool = False,
        retry_count: int = 0,
    ) -> dict[str, Any]:
        record = self._record_for_email(email)
        if not record:
            raise ValueError(f"成功池里找不到账号: {email}")
        key = record["email"].lower()
        def reusable_existing_job(job: dict[str, Any]) -> bool:
            status = str(job.get("status") or "")
            message = str(job.get("message") or job.get("payment_message") or "")
            if status == "success":
                return True
            if status == "running":
                try:
                    updated = datetime.fromisoformat(str(job.get("updated_at") or "").replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - updated).total_seconds() > 900:
                        return False
                except Exception:
                    pass
                return True
            if not str(job.get("midtrans_url") or ""):
                return False
            lowered = message.lower()
            if "otp" in lowered or "expired" in lowered or "过期" in message:
                return False
            return "signature" in lowered

        with self._lock:
            matches = [j for j in self._jobs.values() if str(j.get("email", "")).lower() == key]
            matches.sort(key=lambda j: j.get("created_at", ""), reverse=True)
            existing = next((j for j in matches if reusable_existing_job(j)), None)
            if existing:
                status = str(existing.get("status") or "")
                if status == "running":
                    self._append_log(existing["id"], "已存在运行中的全流程任务，本次点击复用原任务，不重复生成 Midtrans")
                    return dict(existing)
                if status == "success":
                    return dict(existing)
                if existing.get("midtrans_url"):
                    existing["status"] = "running"
                    existing["stage"] = "gopay_account" if not existing.get("phone") else "payment"
                    existing["message"] = "复用已生成的 Midtrans 授权链继续执行"
                    existing["updated_at"] = _now_iso()
                    self._processed.add(key)
                    self._save_state_locked()
                    self._append_log(existing["id"], "复用已生成的 Midtrans 授权链继续执行，不再为同一账号生成新链接")
                    threading.Thread(
                        target=self._run,
                        kwargs={
                            "job_id": existing["id"],
                            "record": record,
                            "pin": (pin or self._pin or "147258").strip(),
                            "proxy": proxy.strip(),
                        },
                        daemon=True,
                        name=f"auto-flow-{existing['id']}",
                    ).start()
                    return dict(existing)
            if not force and key in self._processed and matches:
                return dict(matches[0])
            job_id = uuid.uuid4().hex[:12]
            now = _now_iso()
            job = {
                "id": job_id,
                "email": record["email"],
                "status": "running",
                "stage": "queued",
                "message": "全流程任务已排队",
                "retry_count": max(0, int(retry_count or 0)),
                "created_at": now,
                "updated_at": now,
                "logs": [{"at": now, "message": "排队: 准备生成 Midtrans 1 IDR 授权链"}],
            }
            self._jobs[job_id] = job
            self._processed.add(key)
            self._save_state_locked()
        threading.Thread(
            target=self._run,
            kwargs={
                "job_id": job_id,
                "record": record,
                "pin": (pin or self._pin or "147258").strip(),
                "proxy": proxy.strip(),
            },
            daemon=True,
            name=f"auto-flow-{job_id}",
        ).start()
        return dict(job)

    def _ensure_gopay_account(
        self,
        job_id: str,
        *,
        pin: str,
        proxy: str = "",
        order_id: str = "",
        midtrans_url: str = "",
        email: str = "",
    ) -> dict[str, Any]:
        min_balance = 1
        self._append_log(job_id, "检查 GoPay 账号池余额")
        try:
            _normalize_gopay_binding_history()
        except Exception:
            log.debug("auto flow normalize gopay binding failed", exc_info=True)
        for account in _load_gopay_accounts_raw():
            phone = str(account.get("phone") or "")
            if not phone:
                continue
            block_reason = _gopay_binding_block_reason(account)
            if block_reason:
                self._append_log(job_id, f"账号 {phone} {block_reason}，跳过")
                continue
            try:
                cached_balance = int(account.get("balance", 0) or 0)
            except Exception:
                cached_balance = 0
            if cached_balance < min_balance:
                self._append_log(job_id, f"账号 {phone} 当前余额 {cached_balance} Rp，不参与支付，跳过")
                continue
            with self._lock:
                used_job = next(
                    (
                        j for jid, j in self._jobs.items()
                        if jid != job_id
                        and str(j.get("phone") or "") == phone
                        and str(j.get("midtrans_url") or "")
                    ),
                    None,
                )
            if used_job:
                self._append_log(job_id, f"账号 {phone} 已用于任务 {used_job.get('id')}，一个 GoPay 号只绑一条 Midtrans，跳过")
                continue
            try:
                balance_info = _refresh_gopay_balance(phone)
                balance = int(balance_info.get("balance", 0) or 0)
                self._append_log(job_id, f"账号 {phone} 余额 {balance} Rp")
                if balance >= min_balance:
                    account, _ = _find_gopay_account(phone)
                    if account:
                        if not _gopay_payment_sms_active(account):
                            self._append_log(job_id, f"账号 {phone} 短信租号已释放，不能接支付 OTP，跳过")
                            continue
                        if not _reserve_gopay_midtrans_binding(
                            phone,
                            job_id=job_id,
                            order_id=order_id,
                            midtrans_url=midtrans_url,
                            email=email,
                        ):
                            self._append_log(job_id, f"账号 {phone} 已被其他 Midtrans 任务预占，跳过")
                            continue
                        return account
            except Exception as exc:
                self._append_log(job_id, f"账号 {phone} 余额刷新失败，跳过: {str(exc)[:160]}")

        with self._register_lock:
            self._append_log(job_id, "GoPay 池子没有可用余额账号，开始自动补号")
            from opai.core.gopay_protocol_worker import _get_envelope_did, _make_proxy, _register_one
            from opai.core.sms_helpers import get_sms_api_key, sms_done

            api_key = get_sms_api_key("")
            if not api_key:
                raise RuntimeError("短信 API key 未配置，无法自动补 GoPay 号")
            attempts = max(1, int(os.environ.get("OPAI_AUTO_FLOW_GOPAY_REGISTER_ATTEMPTS", "8") or "8"))
            signup_wait = max(10, int(os.environ.get("OPAI_AUTO_FLOW_SIGNUP_OTP_WAIT_SEC", "120") or "120"))
            pin_first_wait = max(10, int(os.environ.get("OPAI_AUTO_FLOW_PIN_OTP_FIRST_WAIT_SEC", "45") or "45"))
            pin_retry_wait = max(10, int(os.environ.get("OPAI_AUTO_FLOW_PIN_OTP_RETRY_WAIT_SEC", "60") or "60"))
            pin_attempts = max(1, int(os.environ.get("OPAI_AUTO_FLOW_PIN_OTP_ATTEMPTS", "2") or "2"))
            last_error = ""
            for attempt in range(1, attempts + 1):
                register_proxy = _normalize_proxy_url(proxy) or _make_proxy()
                self._append_log(job_id, f"GoPay 自动补号 {attempt}/{attempts}: 开始租号注册")
                try:
                    def register_note(message: str) -> None:
                        self._append_log(job_id, f"GoPay 注册 {attempt}/{attempts}: {message}")

                    result = _register_one(
                        api_key,
                        pin,
                        register_proxy,
                        _get_envelope_did(),
                        cancel_blocking=False,
                        status_cb=register_note,
                        signup_otp_timeout=signup_wait,
                        pin_first_otp_timeout=pin_first_wait,
                        pin_retry_otp_timeout=pin_retry_wait,
                        pin_otp_attempts=pin_attempts,
                    )
                except Exception as exc:
                    result = None
                    last_error = f"{type(exc).__name__}: {str(exc)[:220]}"
                    self._append_log(job_id, f"GoPay 自动补号 {attempt}/{attempts} 异常: {last_error}")
                if not result:
                    if not last_error:
                        last_error = "号码已注册/限流/OTP 失败或注册入口未返回账号"
                    self._append_log(job_id, f"GoPay 自动补号 {attempt}/{attempts} 未成功，直接取下一个号")
                    continue

                phone = str(result.get("phone") or "")
                aid = str(result.get("aid") or "")
                self._append_log(job_id, f"GoPay 补号成功: {phone}，开始刷新余额/钱包状态")

                balance = 0
                try:
                    reward = _claim_gopay_reward(phone)
                    balance = int(reward.get("balance", 0) or 0)
                    self._append_log(job_id, f"钱包初始化/余额刷新: {reward.get('message', '')}，余额 {balance} Rp")
                except Exception as exc:
                    self._append_log(job_id, f"钱包初始化失败，继续查余额: {str(exc)[:160]}")
                try:
                    if _list_gopay_envelopes().get("links"):
                        envelope = _claim_gopay_envelope(phone)
                        balance = int(envelope.get("balance", balance) or balance)
                        self._append_log(job_id, f"红包领取: {envelope.get('message', '')}，余额 {balance} Rp")
                except Exception as exc:
                    self._append_log(job_id, f"红包领取跳过/失败: {str(exc)[:160]}")

                wait_sec = max(0, int(os.environ.get("OPAI_AUTO_FLOW_BALANCE_WAIT_SEC", "180") or "180"))
                deadline = time.time() + wait_sec
                while True:
                    balance_info = _refresh_gopay_balance(phone)
                    balance = int(balance_info.get("balance", 0) or 0)
                    if balance >= min_balance:
                        account, _ = _find_gopay_account(phone)
                        if not account:
                            raise RuntimeError(f"GoPay 新号已注册但本地池未找到: {phone}")
                        if not _reserve_gopay_midtrans_binding(
                            phone,
                            job_id=job_id,
                            order_id=order_id,
                            midtrans_url=midtrans_url,
                            email=email,
                        ):
                            self._append_log(job_id, f"GoPay 新号 {phone} 已被其他 Midtrans 任务预占，继续补下一个号")
                            break
                        self._append_log(job_id, f"GoPay 账号可支付: {phone}，余额 {balance} Rp")
                        return account
                    if time.time() >= deadline:
                        break
                    self._append_log(job_id, f"GoPay 新号余额暂为 {balance} Rp，继续等待刷新")
                    time.sleep(10)
                last_error = f"GoPay 新号余额不足: {balance} Rp"
                self._append_log(job_id, f"{last_error}，继续补下一个号")
            raise RuntimeError(f"自动补 GoPay 号失败，已尝试 {attempts} 次: {last_error or '无可用号码'}")

    def _run(self, *, job_id: str, record: dict[str, str], pin: str, proxy: str) -> None:
        try:
            self._update_job(job_id, stage="midtrans")
            with self._lock:
                current = dict(self._jobs.get(job_id) or {})
            if current.get("midtrans_url"):
                inbox_job = {"id": str(current.get("inbox_job_id") or "")}
                midtrans = {
                    "midtrans_url": current.get("midtrans_url", ""),
                    "order_id": current.get("order_id", ""),
                    "egress_country": current.get("egress_country", ""),
                    "inbox_job": inbox_job,
                }
                self._update_job(job_id, stage="gopay_account")
                self._append_log(job_id, f"复用 Midtrans 授权链: {midtrans.get('order_id') or '-'}")
            else:
                self._append_log(job_id, "生成 Midtrans 1 IDR 授权链，使用 checkout 固定日本出口")
                midtrans = _create_openai_midtrans_job(self._server, {
                    "access_token": record["access_token"],
                    "account_email": record["email"],
                    "plan_kind": "plus",
                })
                inbox_job = midtrans.get("inbox_job") or {}
                self._update_job(
                    job_id,
                    midtrans_url=midtrans.get("midtrans_url", ""),
                    inbox_job_id=inbox_job.get("id", ""),
                    order_id=midtrans.get("order_id", ""),
                    egress_country=midtrans.get("egress_country", ""),
                    stage="gopay_account",
                )
                self._append_log(job_id, f"Midtrans 已生成: {midtrans.get('order_id', '-')}，出口 {midtrans.get('egress_country', '-')}")

            account = self._ensure_gopay_account(
                job_id,
                pin=pin,
                proxy=proxy,
                order_id=str(midtrans.get("order_id") or ""),
                midtrans_url=str(midtrans.get("midtrans_url") or ""),
                email=record["email"],
            )
            phone = str(account.get("phone") or "")
            self._update_job(job_id, phone=phone, stage="payment")
            self._append_log(job_id, f"启动 GoPay 自动支付: {phone}")
            payment = self._web_payment.start(
                phone=phone,
                pin=pin,
                midtrans_url=str(midtrans.get("midtrans_url") or ""),
                inbox_job_id=str(inbox_job.get("id") or ""),
                proxy=proxy,
            )
            payment_id = str(payment.get("id") or "")
            self._update_job(job_id, payment_task_id=payment_id)

            deadline = time.time() + int(os.environ.get("OPAI_AUTO_FLOW_PAYMENT_TIMEOUT_SEC", "900"))
            while time.time() < deadline:
                latest = self._web_payment.get(payment_id) if payment_id else None
                if latest:
                    status = str(latest.get("status") or "")
                    self._update_job(job_id, payment_status=status, payment_message=str(latest.get("message") or ""))
                    if status in {"success", "failed"}:
                        if status == "success":
                            self._update_job(job_id, status="success", stage="done", message="全流程完成，GoPay 授权支付成功")
                            self._append_log(job_id, "全流程完成，GoPay 授权支付成功")
                            _update_gopay_midtrans_binding_status(phone, "success", message="授权支付成功")
                            try:
                                account, _ = _find_gopay_account(phone)
                                if account:
                                    _mark_gopay_sms_done(account)
                            except Exception:
                                log.debug("auto flow mark sms done after payment failed", exc_info=True)
                        else:
                            _update_gopay_midtrans_binding_status(phone, "failed", message=str(latest.get("message") or "支付失败"))
                            raise RuntimeError(str(latest.get("message") or "支付失败"))
                        return
                time.sleep(2)
            raise TimeoutError("GoPay 支付任务超时")
        except Exception as exc:
            log.exception("auto flow failed: %s", job_id)
            reason = str(exc)
            with self._lock:
                failed_job = self._jobs.get(job_id) or {}
                inbox_job_id = str(failed_job.get("inbox_job_id") or "")
                email_key = str(failed_job.get("email") or "").strip().lower()
                retry_count = int(failed_job.get("retry_count") or 0)
                if email_key:
                    self._processed.discard(email_key)
                self._save_state_locked()
            try:
                if inbox_job_id and self._server.store is not None:
                    self._server.store.set_status_if_pending(inbox_job_id, "cancelled")
            except Exception:
                log.debug("auto flow cancel inbox job failed", exc_info=True)
            self._update_job(job_id, status="failed", stage="failed", message=reason[:500])
            self._append_log(job_id, f"全流程失败: {reason[:500]}")

            lowered = reason.lower()
            retryable = (
                "otp" in lowered
                or "token has expired" in lowered
                or "过期" in reason
                or "短信" in reason
                or "已释放" in reason
            )
            max_retries = max(0, int(os.environ.get("OPAI_AUTO_FLOW_FAILURE_RETRIES", "2") or "2"))
            if retryable and retry_count < max_retries:
                self._append_log(job_id, f"失败原因可自动重试，准备重新生成新链并补新号 ({retry_count + 1}/{max_retries})")

                def retry_later() -> None:
                    time.sleep(2)
                    try:
                        self.start(
                            email=record["email"],
                            pin=pin,
                            proxy=proxy,
                            force=True,
                            retry_count=retry_count + 1,
                        )
                    except Exception:
                        log.debug("auto flow retry start failed", exc_info=True)

                threading.Thread(target=retry_later, daemon=True, name=f"auto-flow-retry-{job_id}").start()


class _PinManager:
    """Browser-driven GoPay PIN updater for saved accounts."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def _public(self, job: dict[str, Any]) -> dict[str, Any]:
        clean = dict(job)
        clean.pop("old_pin", None)
        clean.pop("new_pin", None)
        return clean

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [self._public(j) for j in self._jobs.values()]
        jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return jobs

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._public(job) if job else None

    def start(self, *, phone: str, old_pin: str, new_pin: str, mode: str = "known", flow: str = "UPDATE_PIN") -> dict[str, Any]:
        mode = (mode or "known").strip()
        old_pin = (old_pin or "").strip()
        new_pin = (new_pin or "").strip()
        if not (new_pin.isdigit() and len(new_pin) == 6):
            raise ValueError("新 PIN 需要 6 位数字")
        if mode == "forgot":
            raise ValueError("不知道旧 PIN 时要走短信 OTP 重置；当前还缺 GoPay「忘记 PIN」真实抓包，先不要选这个。请在真机点忘记 PIN 抓一次包，我再接完整流程。")
        if not (old_pin.isdigit() and len(old_pin) == 6):
            raise ValueError("旧 PIN 需要 6 位数字")
        if old_pin == new_pin:
            raise ValueError("新 PIN 不能和旧 PIN 一样")
        account, _idx = _find_gopay_account(phone)
        if account is None:
            raise ValueError(f"账号不存在: {phone}")

        job_id = uuid.uuid4().hex[:12]
        now = _now_iso()
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "phone": account.get("phone", phone),
                "local": account.get("local", ""),
                "old_pin": old_pin,
                "new_pin": new_pin,
                "mode": mode,
                "flow": flow or "UPDATE_PIN",
                "status": "running",
                "message": "准备修改 PIN",
                "created_at": now,
                "updated_at": now,
                "logs": [],
            }
        t = threading.Thread(
            target=self._run,
            kwargs={"job_id": job_id, "phone": account.get("phone", phone), "old_pin": old_pin, "new_pin": new_pin, "flow": flow or "UPDATE_PIN"},
            daemon=True,
            name=f"pin-update-{job_id}",
        )
        t.start()
        return self.get(job_id) or {}

    def _append_log(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.setdefault("logs", []).append({"at": _now_iso(), "message": message})
            job["message"] = message
            job["updated_at"] = _now_iso()

    def _run(self, *, job_id: str, phone: str, old_pin: str, new_pin: str, flow: str) -> None:
        try:
            account, idx = _find_gopay_account(phone)
            if account is None:
                raise ValueError(f"账号不存在: {phone}")
            client = _gopay_client_from_account(account, phone)
            self._append_log(job_id, "刷新账号 token")
            try:
                refresh = client.refresh_token()
                if refresh.get("status") not in (200, 201):
                    self._append_log(job_id, f"Token refresh 返回 {refresh.get('status')}，继续尝试现有 token")
            except Exception:
                log.debug("pin update refresh token failed; trying existing token", exc_info=True)
                self._append_log(job_id, "Token refresh 异常，继续尝试现有 token")

            total_challenge_attempts = 1 + len(PIN_CHALLENGE_RETRY_DELAYS)
            challenge = {"status": 0, "body": {"error": "not_started"}}
            for attempt in range(1, total_challenge_attempts + 1):
                if attempt > 1:
                    delay = PIN_CHALLENGE_RETRY_DELAYS[attempt - 2]
                    self._append_log(job_id, f"GoPay 临时错误，等待 {delay}s 后进行第 {attempt}/{total_challenge_attempts} 次创建 PIN challenge")
                    time.sleep(delay)
                    try:
                        refresh = client.refresh_token()
                        if refresh.get("status") in (200, 201):
                            self._append_log(job_id, "重试前 token refresh 成功")
                        else:
                            self._append_log(job_id, f"重试前 token refresh 返回 {refresh.get('status')}，继续")
                    except Exception:
                        log.debug("pin challenge retry refresh failed; trying existing token", exc_info=True)
                        self._append_log(job_id, "重试前 token refresh 异常，继续")
                self._append_log(job_id, f"创建 PIN 修改 challenge，第 {attempt}/{total_challenge_attempts} 次")
                challenge = client.pin_create_challenge(flow=flow or "UPDATE_PIN")
                if challenge.get("status") in (200, 201):
                    self._append_log(job_id, "PIN challenge 创建成功")
                    break
                if not _is_transient_pin_challenge_error(challenge):
                    break
            if challenge.get("status") not in (200, 201):
                body_text = str(challenge.get("body", ""))[:300]
                if _is_transient_pin_challenge_error(challenge):
                    raise RuntimeError(f"创建 PIN challenge 仍是 GoPay 临时错误，请冷却几分钟后再试: {challenge.get('status')} {body_text}")
                raise RuntimeError(f"创建 PIN challenge 失败: {challenge.get('status')} {body_text}")

            self._append_log(job_id, "验证旧 PIN")
            verify = client.pin_verify(old_pin)
            if verify.get("status") not in (200, 201):
                raise RuntimeError(f"旧 PIN 验证失败: {verify.get('status')} {str(verify.get('body', ''))[:300]}")

            self._append_log(job_id, "提交新 PIN")
            update = client.pin_update_v3(new_pin)
            if update.get("status") not in (200, 201):
                raise RuntimeError(f"修改 PIN 失败: {update.get('status')} {str(update.get('body', ''))[:300]}")

            _save_gopay_account_tokens_and_pin(idx, client, new_pin)
            with self._lock:
                job = self._jobs.get(job_id)
                if job:
                    job["status"] = "success"
                    job["message"] = "PIN 修改成功，已更新本地账号记录"
                    job["updated_at"] = _now_iso()
        except Exception as exc:
            log.exception("pin update job failed: %s", job_id)
            with self._lock:
                job = self._jobs.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["message"] = str(exc)
                    job["updated_at"] = _now_iso()


class _InboxServer(ThreadingHTTPServer):
    # 每请求起独立线程：本地脚本 40 worker × 3s 轮询 = 13 QPS，靠多线程不阻塞
    store: InboxStore | None = None
    require_token: str = ""
    require_basic_auth: tuple[str, str] | None = None  # (user, pass) when set
    claim_ttl_sec: float = 60.0
    claim_behavior: str = "sort_bottom"  # "sort_bottom"（默认）/ "hide"（旧行为）
    otp_box: _OTPBox | None = None  # SMS/OTP 收发箱
    manual_register: _ManualRegisterManager | None = None
    web_payment: _WebPaymentManager | None = None
    pin_manager: _PinManager | None = None
    gpt_register: GptRegisterBridge | None = None
    auto_flow: _AutoFlowManager | None = None


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _nested_get(obj: Any, *path: str) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _pick_cookie_header(obj: dict[str, Any]) -> str:
    direct = _first_non_empty(
        obj.get("cookie_header"),
        obj.get("cookieHeader"),
        obj.get("cookie"),
        _nested_get(obj, "headers", "cookie"),
        _nested_get(obj, "headers", "Cookie"),
    )
    if direct:
        return direct
    cookie_list = obj.get("cookieList") if isinstance(obj.get("cookieList"), list) else obj.get("cookies")
    if not isinstance(cookie_list, list):
        return ""
    parts: list[str] = []
    for item in cookie_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name and value:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _looks_like_token_holder(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    return bool(_first_non_empty(
        obj.get("accessToken"),
        obj.get("access_token"),
        obj.get("token"),
        _nested_get(obj, "tokens", "accessToken"),
        _nested_get(obj, "tokens", "access_token"),
        _nested_get(obj, "token", "accessToken"),
        _nested_get(obj, "token", "access_token"),
        _nested_get(obj, "credentials", "accessToken"),
        _nested_get(obj, "credentials", "access_token"),
    ))


def _find_token_holder(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        if _looks_like_token_holder(obj):
            return obj
        for key, value in obj.items():
            if key in {"accessToken", "access_token", "sessionToken", "session_token"}:
                continue
            found = _find_token_holder(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_token_holder(value)
            if found:
                return found
    return None


def _parse_checkout_credentials(data: dict[str, Any]) -> dict[str, str]:
    raw = _first_non_empty(data.get("access_token"), data.get("accessToken"), data.get("token"))
    holder: dict[str, Any] = {}
    if raw.startswith("{") or raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            raise ValueError(f"AT JSON 解析失败: {exc}") from exc
        found = _find_token_holder(parsed)
        if not found:
            raise ValueError("AT JSON 里没有找到 accessToken/access_token")
        holder = found
    else:
        holder = data

    access_token = _first_non_empty(
        holder.get("accessToken"),
        holder.get("access_token"),
        holder.get("token"),
        _nested_get(holder, "tokens", "accessToken"),
        _nested_get(holder, "tokens", "access_token"),
        _nested_get(holder, "token", "accessToken"),
        _nested_get(holder, "token", "access_token"),
        _nested_get(holder, "credentials", "accessToken"),
        _nested_get(holder, "credentials", "access_token"),
        raw if not (raw.startswith("{") or raw.startswith("[")) else "",
    )
    session_token = _first_non_empty(
        holder.get("sessionToken"),
        holder.get("session_token"),
        _nested_get(holder, "tokens", "sessionToken"),
        _nested_get(holder, "tokens", "session_token"),
        _nested_get(holder, "token", "sessionToken"),
        _nested_get(holder, "token", "session_token"),
        _nested_get(holder, "credentials", "sessionToken"),
        _nested_get(holder, "credentials", "session_token"),
        data.get("session_token"),
    )
    cookie_header = _first_non_empty(data.get("cookie_header"), data.get("cookieHeader"), _pick_cookie_header(holder))
    email = _first_non_empty(
        data.get("account_email"),
        holder.get("email"),
        _nested_get(holder, "user", "email"),
        _nested_get(holder, "account", "email"),
        _nested_get(holder, "meta", "email"),
        _nested_get(holder, "providerSpecificData", "email"),
    )
    device_id = _first_non_empty(data.get("device_id"), data.get("deviceId"), holder.get("device_id"), holder.get("deviceId"))
    return {
        "access_token": access_token,
        "session_token": session_token,
        "cookie_header": cookie_header,
        "account_email": email,
        "device_id": device_id,
    }


def _read_checkout_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def _checkout_external_env() -> dict[str, str]:
    configured = _first_non_empty(os.environ.get("OPAI_OPENAI_CHECKOUT_ENV"))
    if not configured:
        return {}
    try:
        return _read_checkout_env_file(Path(configured).expanduser())
    except Exception as exc:
        log.warning("checkout env load failed: %s", exc)
        return {}


def _checkout_project_env() -> dict[str, str]:
    configured = _first_non_empty(
        os.environ.get("OPAI_OPENAI_CHECKOUT_PROJECT_ENV"),
        str(Path.cwd() / "config" / "checkout_proxy.env"),
    )
    try:
        return _read_checkout_env_file(Path(configured).expanduser())
    except Exception as exc:
        log.warning("checkout project env load failed: %s", exc)
        return {}


def _checkout_generation_config(data: dict[str, Any]) -> dict[str, str]:
    own = _checkout_project_env()
    ext = _checkout_external_env()
    # Checkout extraction is intentionally fixed to the local "Japan 3" proxy
    # config. Request-body proxy fields are ignored so the web UI cannot
    # accidentally generate a non-1Rp checkout from a different egress.
    proxy = _first_non_empty(
        own.get("OPAI_OPENAI_CHECKOUT_PROXY"),
        own.get("CHECKOUT_LOCAL_PROXY"),
        own.get("PAYMENT_PROXY"),
        own.get("OPEN_PRECHECK_PROXY"),
        os.environ.get("OPAI_OPENAI_CHECKOUT_PROXY"),
        os.environ.get("CHECKOUT_LOCAL_PROXY"),
        os.environ.get("PAYMENT_PROXY"),
        os.environ.get("OPEN_PRECHECK_PROXY"),
        ext.get("PAYMENT_PROXY"),
        ext.get("OPAI_OPENAI_CHECKOUT_PROXY"),
        ext.get("CHECKOUT_LOCAL_PROXY"),
        ext.get("OPEN_PRECHECK_PROXY"),
        "http://127.0.0.1:17892",
    )
    required_country = _first_non_empty(
        own.get("OPAI_OPENAI_CHECKOUT_REQUIRED_COUNTRY"),
        own.get("CHECKOUT_REQUIRED_COUNTRY"),
        own.get("OPEN_PRECHECK_REQUIRED_COUNTRY"),
        os.environ.get("OPEN_PRECHECK_REQUIRED_COUNTRY"),
        os.environ.get("OPAI_OPENAI_CHECKOUT_REQUIRED_COUNTRY"),
        os.environ.get("CHECKOUT_REQUIRED_COUNTRY"),
        ext.get("OPEN_PRECHECK_REQUIRED_COUNTRY"),
        ext.get("OPAI_OPENAI_CHECKOUT_REQUIRED_COUNTRY"),
        ext.get("CHECKOUT_REQUIRED_COUNTRY"),
        "JP",
    ).upper()
    return {"proxy": proxy, "required_country": required_country}


def _probe_checkout_egress(proxy: str, required_country: str = "") -> dict[str, str]:
    required = (required_country or "").strip().upper()
    if required and not proxy:
        raise RuntimeError(f"未配置 checkout 固定代理，当前要求出口国家 {required}")
    if not proxy:
        return {"country": "", "ok": "1"}

    import tls_client

    session = tls_client.Session(client_identifier="chrome130", random_tls_extension_order=True)
    session.proxies = {"http": proxy, "https": proxy}
    urls = ("https://ipinfo.io/json", "https://api.country.is/")
    last_error = ""
    for url in urls:
        try:
            resp = session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout_seconds=15,
            )
            data = resp.json()
            country = str(data.get("country") or "").strip().upper()
            if country:
                if required and country != required:
                    raise RuntimeError(f"checkout 代理出口不是 {required}，当前出口是 {country}")
                return {"country": country, "ok": "1"}
        except RuntimeError:
            raise
        except Exception as exc:
            last_error = str(exc)
    raise RuntimeError(f"checkout 代理出口检测失败: {last_error[:200]}")


def _extract_midtrans_snap_token(url: str) -> str:
    match = re.search(r"app\.midtrans\.com/snap/v[14]/redirection/([a-f0-9-]{36})", url or "")
    return match.group(1) if match else ""


def _is_setup_authorization_order_id(order_id: str) -> bool:
    """Recognize Midtrans setup/authorization order IDs across protocol versions.

    Older checkout responses used ``setatt_``.  Newer OpenAI/Midtrans
    responses use ``ufpi_`` (user-flow payment intent) for the same 1 IDR
    setup authorization.  The prefix is only one part of the gate: callers
    still require an exact 1 IDR amount and IDR currency.
    """
    value = str(order_id or "").strip().lower()
    return value.startswith(("setatt_", "ufpi_"))


def _midtrans_transaction_meta(
    midtrans_url: str,
    *,
    proxy: str = "",
    payment_fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snap = _extract_midtrans_snap_token(midtrans_url)
    if not snap:
        return {}
    import tls_client

    session = tls_client.Session(client_identifier="chrome130", random_tls_extension_order=True)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    resp = session.get(
        f"https://app.midtrans.com/snap/v1/transactions/{snap}",
        headers=payment_fingerprint_headers(payment_fingerprint),
        timeout_seconds=30,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"raw": getattr(resp, "text", "")}
    if resp.status_code != 200:
        raise RuntimeError(f"Midtrans 链接信息读取失败: {resp.status_code} {str(data)[:300]}")
    details = data.get("transaction_details") if isinstance(data.get("transaction_details"), dict) else {}
    merchant = data.get("merchant") if isinstance(data.get("merchant"), dict) else {}
    accounts = data.get("accounts") if isinstance(data.get("accounts"), dict) else {}
    gopay_account = accounts.get("gopay") if isinstance(accounts.get("gopay"), dict) else {}
    order_id = str(details.get("order_id") or data.get("order_id") or "").strip()
    gross_amount = str(details.get("gross_amount") or data.get("gross_amount") or "").strip()
    currency = str(details.get("currency") or data.get("currency") or "").strip().upper()
    return {
        "snap_token": snap,
        "order_id": order_id,
        "gross_amount": gross_amount,
        "currency": currency,
        "midtrans_client_key": str(merchant.get("client_key") or data.get("client_key") or "").strip(),
        "expiry_time": str(data.get("expiry_time") or "").strip(),
        "account_status": str(gopay_account.get("account_status") or "").strip(),
        "transaction_status": str(data.get("transaction_status") or "").strip(),
        "is_setup_authorization": _is_setup_authorization_order_id(order_id),
        "is_paid_invoice": order_id.startswith("payatt_"),
    }


def _enforce_setup_midtrans(meta: dict[str, Any]) -> None:
    amount = str(meta.get("gross_amount") or "").strip()
    currency = str(meta.get("currency") or "").strip().upper()
    order_id = str(meta.get("order_id") or "").strip()
    if not meta.get("is_setup_authorization") or amount != "1" or currency != "IDR":
        raise RuntimeError(
            f"生成的不是 1 IDR 授权链，已拦截：order_id={order_id or '-'} amount={amount or '-'} {currency or '-'}。"
            "请确认 Japan 固定代理后重新生成。"
        )


def _probe_checkout_promo_with_browser(checkout_url: str, *, proxy: str = "", required_country: str = "") -> dict[str, Any]:
    script = GPT_REGISTER_ROOT / "scripts" / "checkout_promo_probe.js"
    if not script.exists():
        return {"hasPromo": None, "promoStatus": "未知", "promoError": f"优惠探测脚本不存在: {script}"}
    env = os.environ.copy()
    if proxy:
        env["CHECKOUT_PROBE_PROXY"] = proxy
    if required_country:
        env["CHECKOUT_PROBE_REQUIRED_COUNTRY"] = required_country
    env.setdefault("CHECKOUT_PROBE_WAIT_MS", os.environ.get("OPAI_CHECKOUT_PROMO_PROBE_WAIT_MS", "7000"))
    try:
        proc = subprocess.run(
            ["node", str(script), checkout_url],
            cwd=str(GPT_REGISTER_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("OPAI_CHECKOUT_PROMO_PROBE_TIMEOUT_SEC", "60") or "60"),
            check=False,
        )
    except Exception as exc:
        return {"hasPromo": None, "promoStatus": "未知", "promoError": f"优惠探测异常: {type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"hasPromo": None, "promoStatus": "未知", "promoError": (proc.stderr or proc.stdout or f"probe rc={proc.returncode}")[:500]}
    try:
        data = json.loads((proc.stdout or "").strip() or "{}")
    except Exception as exc:
        return {"hasPromo": None, "promoStatus": "未知", "promoError": f"优惠探测 JSON 解析失败: {exc}: {(proc.stdout or '')[:220]}"}
    if not isinstance(data, dict):
        return {"hasPromo": None, "promoStatus": "未知", "promoError": "优惠探测返回格式异常"}
    return data


def _checkout_promo_status(client: Any, checkout: dict[str, str], *, proxy: str = "", required_country: str = "") -> dict[str, Any]:
    checkout_url = str(checkout.get("checkout_url") or "").strip()
    browser_result = _probe_checkout_promo_with_browser(checkout_url, proxy=proxy, required_country=required_country) if checkout_url else {
        "hasPromo": None,
        "promoStatus": "未知",
        "promoError": "checkout_url 为空",
    }
    if browser_result.get("hasPromo") in (True, False):
        return browser_result
    try:
        stripe_result = client.inspect_checkout_discount(checkout)
        if stripe_result.get("hasPromo") in (True, False):
            stripe_result["browserPromoProbe"] = browser_result
            return stripe_result
        stripe_result["browserPromoProbe"] = browser_result
        return stripe_result
    except Exception as exc:
        return {
            "hasPromo": None,
            "promoStatus": "未知",
            "promoError": f"{browser_result.get('promoError') or '页面优惠未知'}; Stripe 检测失败: {type(exc).__name__}: {exc}",
            "browserPromoProbe": browser_result,
        }


def _enforce_checkout_promo(promo: dict[str, Any]) -> None:
    if promo.get("hasPromo") is True:
        return
    status = str(promo.get("promoStatus") or "未知").strip()
    due = str(promo.get("totalDueToday") or promo.get("totalAmount") or "").strip()
    detail = str(promo.get("promoError") or promo.get("promoText") or "").strip()
    suffix = f"，今日应付 {due}" if due else ""
    if detail:
        detail = f": {detail[:220]}"
    raise RuntimeError(f"这条 checkout 没有检测到 Plus 优惠，已标记不可用：{status}{suffix}{detail}")


def _parse_midtrans_expiry(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _payment_failure_label(detail: str) -> str:
    text = (detail or "").lower()
    if "fraud denied" in text or "fraud_status" in text or "transaction_status\": \"deny" in text:
        return "Midtrans/GoPay 风控拒绝，这条链接不要重试"
    if "still linked" in text or "unfinished" in text or "406" in text or "未完成的 gopay 绑定状态" in text:
        return "Midtrans 链接绑定状态已污染，请重新生成新链接"
    if "429" in text or "rate limited" in text:
        return "请求过多被限流，请冷却后换新链接"
    if "insufficient" in text or "balance" in text or "createauth" in text:
        return "余额不足或钱包不满足扣款条件"
    if "otp" in text and ("invalid" in text or "failed" in text or "timeout" in text):
        return "支付 OTP 错误或超时"
    if "pin" in text and ("failed" in text or "verify" in text):
        return "支付 PIN 验证失败"
    if "expired" in text or "expire" in text:
        return "Midtrans 链接已过期，请重新生成"
    return detail or "支付失败"


def _validate_payment_midtrans_meta(meta: dict[str, Any], *, balance: int | None = None) -> None:
    order_id = str(meta.get("order_id") or "").strip()
    amount_text = str(meta.get("gross_amount") or "").strip()
    currency = str(meta.get("currency") or "").strip().upper()
    txn_status = str(meta.get("transaction_status") or "").strip().lower()
    account_status = str(meta.get("account_status") or "").strip().upper()
    expiry = _parse_midtrans_expiry(str(meta.get("expiry_time") or ""))
    if not order_id:
        raise RuntimeError("Midtrans 链接读取不到 order_id，先不要支付")
    if amount_text != "1" or currency != "IDR":
        raise RuntimeError(f"非 1 IDR 授权链已拦截: {order_id} {amount_text or '-'} {currency or '-'}")
    if not _is_setup_authorization_order_id(order_id):
        raise RuntimeError(f"订单类型不是 1 IDR 授权链，已拦截: {order_id} {amount_text} {currency}")
    if txn_status in {"deny", "cancel", "expire", "failure", "settlement", "capture"}:
        raise RuntimeError(f"Midtrans 链接状态不可支付: transaction_status={txn_status}")
    if account_status == "ENABLED":
        raise RuntimeError("这条 Midtrans 链接已经绑定过 GoPay，重新生成新链接再支付")
    if expiry and expiry <= datetime.now(expiry.tzinfo or timezone.utc):
        raise RuntimeError(f"Midtrans 链接已过期: {meta.get('expiry_time')}")
    if balance is not None:
        try:
            amount = int(float(amount_text))
        except Exception:
            amount = 0
        if amount > 0 and balance < amount:
            raise RuntimeError(f"账号余额不足: 当前 {balance} Rp，需要 {amount} Rp")


def _legacy_checkout_create(access_token: str, *, proxy: str = "", country: str = "ID", currency: str = "IDR") -> dict[str, str]:
    """Use the older local checkout-link-extractor TLS script as a fallback.

    The old tool has a slightly different TLS/browser warmup profile that worked
    for the user's AT JSON flow. Token is sent to the helper via stdin only.
    """
    script = Path(os.environ.get(
        "OPAI_LEGACY_CHECKOUT_TLS_SCRIPT",
        str(GPT_REGISTER_ROOT / "scripts" / "checkout_tls_direct.py"),
    )).expanduser()
    if not script.exists():
        raise RuntimeError(f"旧版 checkout 生成器不存在: {script}")

    preferred_python = Path(os.environ.get(
        "OPAI_LEGACY_CHECKOUT_PYTHON",
        "python3",
    )).expanduser()
    python_bin = str(preferred_python) if preferred_python.exists() else "python3"
    proxy_type = "socks5" if proxy.startswith("socks5://") else ("direct" if not proxy else "http")
    payload = {
        "access_token": access_token,
        "country": country,
        "currency": currency,
        "proxy_url": proxy,
        "proxy_type": proxy_type,
    }
    proc = subprocess.run(
        [python_bin, str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=90,
        cwd=str(script.parents[1]) if len(script.parents) > 1 else str(script.parent),
    )
    raw = (proc.stdout or "").strip()
    if not raw:
        raise RuntimeError((proc.stderr or "旧版 checkout 生成器无输出").strip()[:800])
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"旧版 checkout 输出不是 JSON: {raw[:800]}") from exc
    if not data.get("ok"):
        msg = data.get("message") or data.get("error") or data.get("checkoutResponse") or data
        raise RuntimeError(f"旧版 checkout 失败: {str(msg)[:800]}")

    cs_id = str(data.get("checkoutSessionId") or data.get("checkout_session_id") or "").strip()
    checkout_url = str(data.get("longUrl") or data.get("url") or data.get("checkout_url") or "").strip()
    if not cs_id:
        m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9_]+)", checkout_url)
        cs_id = m.group(1) if m else ""
    if not cs_id:
        raise RuntimeError(f"旧版 checkout 没有返回 session id: {str(data)[:800]}")
    return {
        "checkout_session_id": cs_id,
        "checkout_url": checkout_url,
        "processor_entity": str(data.get("processorEntity") or data.get("processor_entity") or "openai_llc"),
        "publishable_key": str(data.get("publishableKey") or data.get("publishable_key") or ""),
        "checkout_source": "legacy_tls",
    }


def _browser_checkout_create(
    *,
    access_token: str,
    session_token: str = "",
    cookie_header: str = "",
    proxy: str = "",
    country: str = "ID",
    currency: str = "IDR",
) -> dict[str, str]:
    script = Path(os.environ.get(
        "OPAI_BROWSER_CHECKOUT_SCRIPT",
        str(Path(__file__).resolve().parents[3] / "scripts" / "openai_checkout_browser_direct.js"),
    )).expanduser()
    if not script.exists():
        raise RuntimeError(f"浏览器 checkout 生成器不存在: {script}")
    payload = {
        "access_token": access_token,
        "session_token": session_token,
        "cookie_header": cookie_header,
        "proxy": proxy,
        "country": country,
        "currency": currency,
    }
    proc = subprocess.run(
        ["node", str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=150,
        cwd=str(script.parent),
    )
    raw = (proc.stdout or "").strip()
    if not raw:
        raise RuntimeError((proc.stderr or "浏览器 checkout 生成器无输出").strip()[:1000])
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"浏览器 checkout 输出不是 JSON: {raw[:1000]}") from exc
    if not data.get("ok"):
        msg = data.get("message") or data.get("error") or data.get("checkoutResponse") or data
        raise RuntimeError(f"浏览器 checkout 失败: {str(msg)[:1000]}")
    cs_id = str(data.get("checkoutSessionId") or data.get("checkout_session_id") or "").strip()
    checkout_url = str(data.get("longUrl") or data.get("url") or data.get("checkout_url") or "").strip()
    if not cs_id:
        m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9_]+)", checkout_url)
        cs_id = m.group(1) if m else ""
    if not cs_id:
        raise RuntimeError(f"浏览器 checkout 没有返回 session id: {str(data)[:800]}")
    return {
        "checkout_session_id": cs_id,
        "checkout_url": checkout_url,
        "processor_entity": str(data.get("processorEntity") or data.get("processor_entity") or "openai_llc"),
        "publishable_key": str(data.get("publishableKey") or data.get("publishable_key") or ""),
        "checkout_source": str(data.get("strategy") or "browser_warmup"),
    }


def _create_openai_midtrans_job(server: "_InboxServer", data: dict[str, Any]) -> dict[str, Any]:
    from opai.core.openai_checkout import OpenAICheckout

    creds = _parse_checkout_credentials(data)
    checkout_cfg = _checkout_generation_config(data)
    proxy = checkout_cfg["proxy"]
    egress = _probe_checkout_egress(proxy, checkout_cfg["required_country"])
    checkout = OpenAICheckout(
        access_token=creds["access_token"],
        cookie_header=creds["cookie_header"],
        session_token=creds["session_token"],
        device_id=creds["device_id"],
        proxy=proxy,
    )
    billing = data.get("billing") if isinstance(data.get("billing"), dict) else {}
    source = "native_tls"
    promo: dict[str, Any] = {}
    try:
        native_checkout = checkout.create_checkout()
        promo = _checkout_promo_status(checkout, native_checkout, proxy=proxy, required_country=checkout_cfg["required_country"])
        _enforce_checkout_promo(promo)
        result = checkout.generate_midtrans_url_from_checkout(native_checkout, billing=billing)
    except Exception as native_exc:
        if "没有检测到 Plus 优惠" in str(native_exc):
            raise
        log.info("native OpenAI checkout failed, trying legacy generator: %s", native_exc)
        try:
            legacy = _legacy_checkout_create(creds["access_token"], proxy=proxy, country="ID", currency="IDR")
        except Exception as legacy_exc:
            log.info("legacy TLS checkout failed, trying browser generator: %s", legacy_exc)
            legacy = _browser_checkout_create(
                access_token=creds["access_token"],
                session_token=creds["session_token"],
                cookie_header=creds["cookie_header"],
                proxy=proxy,
                country="ID",
                currency="IDR",
            )
        promo = _checkout_promo_status(checkout, legacy, proxy=proxy, required_country=checkout_cfg["required_country"])
        _enforce_checkout_promo(promo)
        result = checkout.generate_midtrans_url_from_checkout(legacy, billing=billing)
        result["native_error"] = str(native_exc)
        source = legacy.get("checkout_source") or "legacy_tls"
    result["promo"] = promo
    midtrans_meta = _midtrans_transaction_meta(str(result.get("midtrans_url") or ""), proxy=proxy)
    _enforce_setup_midtrans(midtrans_meta)
    store = server.store
    assert store is not None
    inbox_job = store.create(
        account_name=str(data.get("account_name") or "").strip(),
        account_email=str(creds.get("account_email") or data.get("account_email") or "").strip(),
        plan_kind=str(data.get("plan_kind") or "plus").strip().lower() or "plus",
        checkout_url=str(result.get("checkout_url") or "").strip(),
        paypal_url="",
        provider="gopay",
        provider_url=str(result.get("midtrans_url") or "").strip(),
        notes=(
            f"AT 自动生成 Midtrans({source}, {midtrans_meta.get('gross_amount')} "
            f"{midtrans_meta.get('currency')}, {midtrans_meta.get('order_id')}): "
            f"{result.get('checkout_session_id', '')}"
        ),
    )
    return {
        "midtrans_url": result.get("midtrans_url", ""),
        "checkout_url": result.get("checkout_url", ""),
        "checkout_session_id": result.get("checkout_session_id", ""),
        "processor_entity": result.get("processor_entity", ""),
        "snap_token": result.get("snap_token", "") or midtrans_meta.get("snap_token", ""),
        "order_id": midtrans_meta.get("order_id", ""),
        "gross_amount": midtrans_meta.get("gross_amount", ""),
        "currency": midtrans_meta.get("currency", ""),
        "is_setup_authorization": midtrans_meta.get("is_setup_authorization", False),
        "promo": promo,
        "has_promo": promo.get("hasPromo") is True,
        "promo_status": promo.get("promoStatus", ""),
        "egress_country": egress.get("country", ""),
        "source": source,
        "inbox_job": inbox_job,
    }


class _InboxHandler(BaseHTTPRequestHandler):
    server: _InboxServer  # type: ignore[assignment]

    def log_message(self, format, *args):  # noqa: A002
        log.debug("payment_inbox HTTP: " + format, *args)

    # ---- 鉴权 ----
    @staticmethod
    def _ct_eq(a: str, b: str) -> bool:
        try:
            return hmac.compare_digest(a, b)
        except Exception:
            return False

    def _check_basic_auth(self) -> bool:
        creds = self.server.require_basic_auth
        if not creds:
            return False
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:].strip()).decode("utf-8", errors="replace")
        except Exception:
            return False
        if ":" not in decoded:
            return False
        u, p = decoded.split(":", 1)
        return self._ct_eq(u, creds[0]) and self._ct_eq(p, creds[1])

    def _check_token(self) -> bool:
        require = self.server.require_token
        if not require:
            return False
        # Bearer
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            if self._ct_eq(auth[7:].strip(), require):
                return True
        # X-Auth-Token header
        x = (self.headers.get("X-Auth-Token") or "").strip()
        if x and self._ct_eq(x, require):
            return True
        # Cookie inbox_token
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            kv = part.strip().split("=", 1)
            if len(kv) == 2 and kv[0].strip() == "inbox_token" and self._ct_eq(kv[1].strip(), require):
                return True
        # ?token= 参数
        try:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            tok2 = (params.get("token") or [""])[0]
            if tok2 and self._ct_eq(tok2, require):
                return True
        except Exception:
            pass
        return False

    def _check_auth(self) -> bool:
        """两套认证任一通过就放行；都没配置则全开放。"""
        no_token = not self.server.require_token
        no_basic = not self.server.require_basic_auth
        if no_token and no_basic:
            return True
        if not no_basic and self._check_basic_auth():
            return True
        if not no_token and self._check_token():
            return True
        return False

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _send_json(self, code: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
            pass

    def _send_html(self, code: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._send_cors_headers()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
            pass

    def _send_text(self, code: int, msg: str) -> None:
        data = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._send_cors_headers()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
            pass

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def _send_unauthorized_html(self) -> None:
        """HTML 入口未通过鉴权：附 WWW-Authenticate: Basic 让浏览器弹登录框。"""
        body = b"<h1>401 Unauthorized</h1>"
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if self.server.require_basic_auth:
            self.send_header("WWW-Authenticate", 'Basic realm="OPAI Payment Inbox", charset="UTF-8"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
            pass

    def _send_unauthorized_json(self) -> None:
        body = json.dumps({"error": "unauthorized"}).encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if self.server.require_basic_auth:
            self.send_header("WWW-Authenticate", 'Basic realm="OPAI Payment Inbox", charset="UTF-8"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
            pass

    def _read_json_body(self) -> dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def handle(self):  # noqa: D401
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
            pass
        except OSError as exc:
            if getattr(exc, "winerror", None) in (10053, 10054, 10060):
                return
            raise

    def finish(self):
        """Close per-request thread's SQLite connection before the thread dies."""
        try:
            store = self.server.store
            if store is not None:
                store.close_thread_connection()
        except Exception:
            pass
        try:
            super().finish()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
            pass

    # ---- 路由 ----
    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/" or path == "/index.html":
            if not self._check_auth():
                self._send_unauthorized_html()
                return
            store = self.server.store
            if store is not None:
                try:
                    store.expire_overdue()
                except Exception:
                    log.debug("payment_inbox: expire_overdue 异常", exc_info=True)
            self._send_html(HTTPStatus.OK, _HTML_PAGE)
            return
        if path == "/api/jobs":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            status = (qs.get("status") or [""])[0] or None
            email = (qs.get("email") or [""])[0] or None
            plan_kind = (qs.get("plan_kind") or [""])[0] or None
            include_claimed = (qs.get("include_claimed") or ["0"])[0] in ("1", "true", "yes")
            order = (qs.get("order") or ["created_desc"])[0]
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except ValueError:
                limit = 50
            try:
                offset = int((qs.get("offset") or ["0"])[0])
            except ValueError:
                offset = 0
            limit = max(0, min(500, limit))  # 防过大单次返回
            offset = max(0, offset)

            store = self.server.store
            assert store is not None
            store.expire_overdue()

            if include_claimed:
                # 调用方明确要全集（脚本侧轮询用）：跳过 claim 过滤，store 内分页
                jobs, total = store.list(
                    status=status, email=email, plan_kind=plan_kind,
                    limit=limit if limit > 0 else None, offset=offset, order=order,
                )
            else:
                # 网页视角：根据 claim_behavior 决定是隐藏还是排序到底部
                jobs_full, _ = store.list(
                    status=status, email=email, plan_kind=plan_kind,
                    limit=None, offset=0, order=order,
                )
                behavior = getattr(self.server, "claim_behavior", "sort_bottom")
                if behavior == "hide":
                    # 旧行为：TTL 内的 claim 直接过滤掉
                    ttl = self.server.claim_ttl_sec
                    now = datetime.now(timezone.utc)
                    jobs_full = [j for j in jobs_full if not _is_job_actively_claimed(j, ttl, now)]
                else:
                    # 新默认 sort_bottom：claim 过的 pending 全部沉到列表底部
                    # 顶部：fresh + 非 pending（按 created_at 已经按 order 排好）
                    # 底部：claim 过的 pending（按 claimed_at 倒序，最近 claim 的先）
                    fresh, claimed = [], []
                    for j in jobs_full:
                        if _job_has_claim(j):
                            claimed.append(j)
                        else:
                            fresh.append(j)
                    claimed.sort(key=_claim_ts, reverse=True)
                    jobs_full = fresh + claimed
                total = len(jobs_full)
                if offset:
                    jobs_full = jobs_full[offset:]
                if limit:
                    jobs_full = jobs_full[:limit]
                jobs = jobs_full

            self._send_json(HTTPStatus.OK, {
                "jobs": jobs,
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": (offset + len(jobs)) < total,
            })
            return
        if path.startswith("/api/accounts/") and path.endswith("/sms-code"):
            phone = urllib.parse.unquote(path[len("/api/accounts/"):-len("/sms-code")].strip("/"))
            code = _poll_imported_sms_code(phone, set())
            self._send_json(HTTPStatus.OK, {"phone": phone, "code": code or ""})
            return
        if path == "/api/accounts":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            _normalize_gopay_binding_history()
            accounts = _load_gopay_accounts()
            balances = [int(a.get("balance", 0) or 0) for a in accounts]
            self._send_json(HTTPStatus.OK, {
                "accounts": accounts,
                "total": len(accounts),
                "summary": {"total": len(accounts), "one_rp": sum(b == 1 for b in balances), "zero": sum(b == 0 for b in balances)},
                "path": str(_gopay_accounts_path()),
            })
            return
        if path == "/api/envelopes":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            self._send_json(HTTPStatus.OK, _list_gopay_envelopes())
            return
        if path == "/api/payment-tasks":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            mgr = self.server.web_payment
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "web_payment not initialized"})
                return
            self._send_json(HTTPStatus.OK, {"jobs": mgr.list()})
            return
        if path == "/api/pin-tasks":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            mgr = self.server.pin_manager
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "pin_manager not initialized"})
                return
            self._send_json(HTTPStatus.OK, {"jobs": mgr.list()})
            return
        if path.startswith("/api/pin-tasks/"):
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            parts = path.strip("/").split("/")
            if len(parts) < 3:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            mgr = self.server.pin_manager
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "pin_manager not initialized"})
                return
            job = mgr.get(parts[2])
            if job is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, job)
            return
        if path.startswith("/api/payment-tasks/"):
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            parts = path.strip("/").split("/")
            if len(parts) < 3:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            mgr = self.server.web_payment
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "web_payment not initialized"})
                return
            job = mgr.get(parts[2])
            if job is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, job)
            return
        if path == "/api/manual-register":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            mgr = self.server.manual_register
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "manual_register not initialized"})
                return
            self._send_json(HTTPStatus.OK, {"jobs": mgr.list()})
            return
        if path.startswith("/api/manual-register/"):
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            parts = path.strip("/").split("/")
            if len(parts) < 3:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            mgr = self.server.manual_register
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "manual_register not initialized"})
                return
            job = mgr.get(parts[2])
            if job is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, job)
            return
        if path.startswith("/api/jobs/"):
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            jid = path.split("/", 3)[3]
            store = self.server.store
            assert store is not None
            j = store.get(jid)
            if j is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, j)
            return
        if path == "/api/gpt/summary":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            bridge = self.server.gpt_register
            if bridge is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "gpt_register not initialized"})
                return
            try:
                self._send_json(HTTPStatus.OK, bridge.summary())
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/gpt/emails":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            bridge = self.server.gpt_register
            if bridge is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "gpt_register not initialized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            status = (qs.get("status") or [""])[0].strip()
            limit = int((qs.get("limit") or ["500"])[0] or "500")
            try:
                self._send_json(HTTPStatus.OK, bridge.list_emails(status=status, limit=limit))
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/gpt/accounts":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            bridge = self.server.gpt_register
            if bridge is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "gpt_register not initialized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            limit = int((qs.get("limit") or ["500"])[0] or "500")
            try:
                self._send_json(HTTPStatus.OK, bridge.list_accounts(limit=limit))
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/gpt/success-pool":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            bridge = self.server.gpt_register
            if bridge is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "gpt_register not initialized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            limit = int((qs.get("limit") or ["5000"])[0] or "5000")
            try:
                self._send_json(HTTPStatus.OK, bridge.success_pool(limit=limit))
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/gpt/plus-pool":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            bridge = self.server.gpt_register
            if bridge is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "gpt_register not initialized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            limit = int((qs.get("limit") or ["5000"])[0] or "5000")
            try:
                self._send_json(HTTPStatus.OK, bridge.plus_pool(limit=limit))
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/gpt/register-tasks":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            bridge = self.server.gpt_register
            if bridge is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "gpt_register not initialized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            limit = int((qs.get("limit") or ["100"])[0] or "100")
            try:
                self._send_json(HTTPStatus.OK, bridge.list_registration_jobs(limit=limit))
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/auto-flow":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            mgr = self.server.auto_flow
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "auto_flow not initialized"})
                return
            self._send_json(HTTPStatus.OK, mgr.list())
            return
        if path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/api/sms-status":
            if not self._check_auth():
                self._send_unauthorized_json()
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            include_balance = (qs.get("balance") or ["0"])[0] in ("1", "true", "yes")
            self._send_json(HTTPStatus.OK, _sms_api_status(self.server.otp_box, include_balance=include_balance))
            return
        # /api/otp?phone=xxx[&after=timestamp] — GoPay 服务拉取 OTP
        if path == "/api/otp":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            phone = (qs.get("phone") or [""])[0].strip()
            after_ts = float((qs.get("after") or ["0"])[0])
            otp_box = self.server.otp_box
            if otp_box is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "otp_box not initialized"})
                return
            if not phone:
                # 列出所有待取的 OTP
                self._send_json(HTTPStatus.OK, otp_box.list_all())
                return
            code = otp_box.pop(phone, after_ts=after_ts)
            self._send_json(HTTPStatus.OK, {"phone": phone, "code": code})
            return
        self._send_text(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if not self._check_auth():
            self._send_unauthorized_json()
            return
        if path == "/api/jobs/claim_next":
            data = self._read_json_body()
            store = self.server.store
            assert store is not None
            try:
                ttl = float(data.get("ttl_sec") or self.server.claim_ttl_sec)
            except (TypeError, ValueError):
                ttl = self.server.claim_ttl_sec
            job = store.claim_next_pending(
                prefer_paypal_url=bool(data.get("prefer_paypal_url")),
                prefer_oldest=bool(data.get("prefer_oldest")),
                ttl_sec=ttl,
                provider=str(data.get("provider") or ""),
            )
            if job is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "no_pending_job"})
                return
            self._send_json(HTTPStatus.OK, job)
            return
        if path == "/api/jobs":
            data = self._read_json_body()
            try:
                store = self.server.store
                assert store is not None
                # provider / provider_url 是 v2 字段;老 client(只发 paypal_url)走 store.create()
                # 默认行为(provider='paypal',provider_url 兜底 paypal_url)。
                raw_provider = data.get("provider")
                raw_provider_url = data.get("provider_url")
                job = store.create(
                    account_name=str(data.get("account_name") or "").strip(),
                    account_email=str(data.get("account_email") or "").strip(),
                    plan_kind=str(data.get("plan_kind") or "team").strip().lower(),
                    checkout_url=str(data.get("checkout_url") or "").strip(),
                    paypal_url=str(data.get("paypal_url") or "").strip(),
                    provider=str(raw_provider or "paypal").strip().lower() or "paypal",
                    provider_url=(str(raw_provider_url).strip() if raw_provider_url is not None else None),
                    expires_at=str(data.get("expires_at") or "").strip(),
                    notes=str(data.get("notes") or ""),
                )
                self._send_json(HTTPStatus.CREATED, job)
            except Exception as exc:
                log.exception("payment_inbox: 创建 job 失败")
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/gpt/emails/import":
            data = self._read_json_body()
            bridge = self.server.gpt_register
            if bridge is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "gpt_register not initialized"})
                return
            try:
                result = bridge.import_emails(
                    text=str(data.get("text") or data.get("raw") or ""),
                    records=data.get("records") if isinstance(data.get("records"), list) else None,
                )
                self._send_json(HTTPStatus.CREATED, result)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/gpt/register-tasks":
            data = self._read_json_body()
            bridge = self.server.gpt_register
            if bridge is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "gpt_register not initialized"})
                return
            try:
                job = bridge.start_registration(
                    count=int(data.get("count") or 1),
                    workers=int(data.get("workers") or 1),
                    delay=float(data.get("delay") or 0),
                    continue_on_fail=bool(data.get("continue_on_fail", True)),
                    proxy=str(data.get("proxy") or "").strip(),
                    source=str(data.get("source") or "pool").strip(),
                    note=str(data.get("note") or "").strip(),
                )
                self._send_json(HTTPStatus.CREATED, job)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/auto-flow/config":
            data = self._read_json_body()
            mgr = self.server.auto_flow
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "auto_flow not initialized"})
                return
            try:
                result = mgr.configure(
                    enabled=bool(data.get("enabled")),
                    pin=str(data.get("pin") or "").strip(),
                    proxy=str(data.get("proxy") or "").strip(),
                    new_only=bool(data.get("new_only", True)),
                )
                self._send_json(HTTPStatus.OK, result)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/auto-flow/start":
            data = self._read_json_body()
            mgr = self.server.auto_flow
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "auto_flow not initialized"})
                return
            try:
                job = mgr.start(
                    email=str(data.get("email") or "").strip(),
                    pin=str(data.get("pin") or "").strip(),
                    proxy=str(data.get("proxy") or "").strip(),
                    force=bool(data.get("force")),
                )
                self._send_json(HTTPStatus.CREATED, job)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/gpt/accounts/refresh-status":
            data = self._read_json_body()
            bridge = self.server.gpt_register
            if bridge is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "gpt_register not initialized"})
                return
            try:
                limit = int(data.get("limit") or 100)
                self._send_json(HTTPStatus.OK, bridge.refresh_account_statuses(limit=limit))
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path.startswith("/api/gpt/accounts/") and path.endswith("/refresh-status"):
            bridge = self.server.gpt_register
            if bridge is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "gpt_register not initialized"})
                return
            parts = path.strip("/").split("/")
            if len(parts) < 5:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing_email"})
                return
            email = urllib.parse.unquote(parts[3])
            try:
                self._send_json(HTTPStatus.OK, bridge.refresh_account_status(email=email))
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/manual-register":
            data = self._read_json_body()
            mgr = self.server.manual_register
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "manual_register not initialized"})
                return
            try:
                job = mgr.start(
                    phone=str(data.get("phone") or "").strip(),
                    pin=str(data.get("pin") or "147258").strip(),
                    country_code=str(data.get("country_code") or "62").strip(),
                    signed_up_country=str(data.get("signed_up_country") or "ID").strip(),
                    force_live=bool(data.get("force_live")),
                    login_existing=bool(data.get("login_existing")),
                    relogin_after_register=bool(data.get("relogin_after_register")),
                    claim_envelope_after_register=bool(data.get("claim_envelope_after_register")),
                    proxy=str(data.get("proxy") or "").strip(),
                )
                self._send_json(HTTPStatus.CREATED, job)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/envelopes":
            data = self._read_json_body()
            try:
                result = _replace_gopay_envelope_url(str(data.get("url") or "").strip())
                self._send_json(HTTPStatus.OK, result)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/sms-config":
            data = self._read_json_body()
            try:
                result = _write_sms_config(data)
                self._send_json(HTTPStatus.OK, result)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/payment-tasks":
            data = self._read_json_body()
            mgr = self.server.web_payment
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "web_payment not initialized"})
                return
            try:
                job = mgr.start(
                    phone=str(data.get("phone") or "").strip(),
                    pin=str(data.get("pin") or "").strip(),
                    midtrans_url=str(data.get("midtrans_url") or "").strip(),
                    inbox_job_id=str(data.get("inbox_job_id") or "").strip(),
                    proxy=str(data.get("proxy") or "").strip(),
                )
                self._send_json(HTTPStatus.CREATED, job)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/openai-checkout/midtrans":
            data = self._read_json_body()
            try:
                self._send_json(HTTPStatus.CREATED, _create_openai_midtrans_job(self.server, data))
            except Exception as exc:
                log.exception("openai checkout generate midtrans failed")
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/openai-checkout/from-url":
            data = self._read_json_body()
            try:
                from opai.core.openai_checkout import OpenAICheckout

                creds = _parse_checkout_credentials(data)
                checkout_url = str(data.get("checkout_url") or data.get("url") or "").strip()
                match = re.search(r"(cs_(?:live|test)_[A-Za-z0-9_]+)", checkout_url)
                if not match:
                    raise ValueError("checkout 链接里没有找到 cs_live/cs_test session id")
                checkout = {
                    "checkout_session_id": match.group(1),
                    "checkout_url": checkout_url,
                    "processor_entity": str(data.get("processor_entity") or "openai_llc"),
                    "checkout_source": "bookmarklet_url",
                }
                checkout_cfg = _checkout_generation_config(data)
                egress = _probe_checkout_egress(checkout_cfg["proxy"], checkout_cfg["required_country"])
                client = OpenAICheckout(
                    access_token=creds["access_token"],
                    cookie_header=creds["cookie_header"],
                    session_token=creds["session_token"],
                    device_id=creds["device_id"],
                    proxy=checkout_cfg["proxy"],
                )
                promo = _checkout_promo_status(
                    client,
                    checkout,
                    proxy=checkout_cfg["proxy"],
                    required_country=checkout_cfg["required_country"],
                )
                _enforce_checkout_promo(promo)
                billing = data.get("billing") if isinstance(data.get("billing"), dict) else {}
                result = client.generate_midtrans_url_from_checkout(checkout, billing=billing)
                midtrans_meta = _midtrans_transaction_meta(str(result.get("midtrans_url") or ""), proxy=checkout_cfg["proxy"])
                _enforce_setup_midtrans(midtrans_meta)
                store = self.server.store
                assert store is not None
                inbox_job = store.create(
                    account_name=str(data.get("account_name") or "").strip(),
                    account_email=str(creds.get("account_email") or data.get("account_email") or "").strip(),
                    plan_kind=str(data.get("plan_kind") or "plus").strip().lower() or "plus",
                    checkout_url=checkout_url,
                    paypal_url="",
                    provider="gopay",
                    provider_url=str(result.get("midtrans_url") or "").strip(),
                    notes=(
                        f"bookmarklet checkout 转 Midtrans({midtrans_meta.get('gross_amount')} "
                        f"{midtrans_meta.get('currency')}, {midtrans_meta.get('order_id')}): "
                        f"{result.get('checkout_session_id', '')}"
                    ),
                )
                self._send_json(HTTPStatus.CREATED, {
                    "midtrans_url": result.get("midtrans_url", ""),
                    "checkout_url": checkout_url,
                    "checkout_session_id": result.get("checkout_session_id", ""),
                    "processor_entity": result.get("processor_entity", ""),
                    "snap_token": result.get("snap_token", "") or midtrans_meta.get("snap_token", ""),
                    "order_id": midtrans_meta.get("order_id", ""),
                    "gross_amount": midtrans_meta.get("gross_amount", ""),
                    "currency": midtrans_meta.get("currency", ""),
                    "is_setup_authorization": midtrans_meta.get("is_setup_authorization", False),
                    "promo": promo,
                    "has_promo": promo.get("hasPromo") is True,
                    "promo_status": promo.get("promoStatus", ""),
                    "egress_country": egress.get("country", ""),
                    "source": "bookmarklet_url",
                    "inbox_job": inbox_job,
                })
            except Exception as exc:
                log.exception("openai checkout url to midtrans failed")
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/payment-tasks/claim-next":
            data = self._read_json_body()
            mgr = self.server.web_payment
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "web_payment not initialized"})
                return
            try:
                job = mgr.claim_and_start(
                    phone=str(data.get("phone") or "").strip(),
                    pin=str(data.get("pin") or "").strip(),
                    proxy=str(data.get("proxy") or "").strip(),
                )
                self._send_json(HTTPStatus.CREATED, job)
            except LookupError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/pin-tasks":
            data = self._read_json_body()
            mgr = self.server.pin_manager
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "pin_manager not initialized"})
                return
            try:
                job = mgr.start(
                    phone=str(data.get("phone") or "").strip(),
                    old_pin=str(data.get("old_pin") or "").strip(),
                    new_pin=str(data.get("new_pin") or "").strip(),
                    mode=str(data.get("mode") or "known").strip(),
                    flow=str(data.get("flow") or "UPDATE_PIN").strip(),
                )
                self._send_json(HTTPStatus.CREATED, job)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path.startswith("/api/accounts/") and path.endswith("/balance"):
            phone = urllib.parse.unquote(path[len("/api/accounts/"):-len("/balance")].strip("/"))
            try:
                result = _refresh_gopay_balance(phone)
                self._send_json(HTTPStatus.OK, result)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path.startswith("/api/accounts/") and path.endswith("/reward"):
            phone = urllib.parse.unquote(path[len("/api/accounts/"):-len("/reward")].strip("/"))
            try:
                result = _claim_gopay_reward(phone)
                self._send_json(HTTPStatus.OK, result)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path.startswith("/api/accounts/") and path.endswith("/envelope"):
            phone = urllib.parse.unquote(path[len("/api/accounts/"):-len("/envelope")].strip("/"))
            try:
                result = _claim_gopay_envelope(phone)
                self._send_json(HTTPStatus.OK, result)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path.startswith("/api/payment-tasks/") and path.endswith("/otp"):
            parts = path.strip("/").split("/")
            if len(parts) < 4:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            data = self._read_json_body()
            mgr = self.server.web_payment
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "web_payment not initialized"})
                return
            job = mgr.submit_otp(parts[2], str(data.get("code") or "").strip())
            if job is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, job)
            return
        if path.startswith("/api/manual-register/") and path.endswith("/otp"):
            parts = path.strip("/").split("/")
            if len(parts) < 4:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            data = self._read_json_body()
            mgr = self.server.manual_register
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "manual_register not initialized"})
                return
            job = mgr.submit_otp(parts[2], str(data.get("code") or "").strip())
            if job is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, job)
            return
        # /api/otp — 推送 OTP 验证码（外部 WhatsApp bot / SMS 网关调用）
        if path == "/api/otp":
            data = self._read_json_body()
            phone = str(data.get("phone", "")).strip()
            code = str(data.get("code", "")).strip()
            if not phone or not code:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing phone or code"})
                return
            otp_box = self.server.otp_box
            if otp_box is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "otp_box not initialized"})
                return
            otp_box.push(phone, code)
            self._send_json(HTTPStatus.OK, {"ok": True, "phone": phone, "code": code})
            return
        self._send_text(HTTPStatus.NOT_FOUND, "not found")

    def do_PUT(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if not self._check_auth():
            self._send_unauthorized_json()
            return
        store = self.server.store
        assert store is not None
        # /api/jobs/<id>  → JSON body 更新可变字段（仅 paypal_url / checkout_url / notes / expires_at）
        if path.startswith("/api/jobs/") and "/" not in path[len("/api/jobs/"):]:
            jid = path[len("/api/jobs/"):]
            data = self._read_json_body()
            allowed = {
                "paypal_url", "provider", "provider_url",
                "checkout_url", "notes", "expires_at",
                "oauth_status",
            }
            updates = {k: str(v).strip() if isinstance(v, str) else v
                       for k, v in data.items() if k in allowed}
            if not updates:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "no allowed fields", "allowed": sorted(allowed)})
                return
            j = store.patch(jid, updates)
            if j is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, j)
            return
        # /api/jobs/<id>/claim — 网页用户点开支付链接前调用，TTL 内列表会隐藏此 job，
        # 避免多人浏览面板同时点同一条 job。返回新写入的 ``claimed_at`` 时间。
        if path.startswith("/api/jobs/") and path.endswith("/claim"):
            jid = path.split("/")[3]
            j = store.patch(jid, {"claimed_at": _now_iso()})
            if j is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, {"id": j["id"], "claimed_at": j.get("claimed_at"),
                                            "ttl_sec": self.server.claim_ttl_sec})
            return
        # /api/jobs/<id>/paid 或 /cancel —— 用 set_status_if_pending 走幂等 SQL
        if path.startswith("/api/jobs/") and (path.endswith("/paid") or path.endswith("/cancel")):
            parts = path.split("/")
            jid = parts[3]
            action = parts[4]
            new_status = "paid" if action == "paid" else "cancelled"
            j = store.set_status_if_pending(jid, new_status)
            if j is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, j)
            return
        if path.startswith("/api/accounts/") and path.endswith("/balance"):
            phone = urllib.parse.unquote(path[len("/api/accounts/"):-len("/balance")].strip("/"))
            try:
                result = _refresh_gopay_balance(phone)
                self._send_json(HTTPStatus.OK, result)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_text(HTTPStatus.NOT_FOUND, "not found")

    def do_DELETE(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if not self._check_auth():
            self._send_unauthorized_json()
            return
        if path.startswith("/api/jobs/"):
            jid = path.split("/", 3)[3]
            store = self.server.store
            assert store is not None
            ok = store.delete(jid)
            self._send_json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"deleted": ok})
            return
        self._send_text(HTTPStatus.NOT_FOUND, "not found")


def run_inbox_server(host: str = "0.0.0.0", port: int = 18130, store: InboxStore | None = None) -> None:
    """启 inbox HTTP 服务（阻塞，Ctrl+C 退出）。"""
    inbox_store = store or InboxStore()
    otp_box = _OTPBox()
    manual_register = _ManualRegisterManager()
    web_payment = _WebPaymentManager(inbox_store)
    pin_manager = _PinManager()
    gpt_register = GptRegisterBridge(GPT_REGISTER_ROOT)
    srv = _InboxServer((host, port), _InboxHandler)
    srv.store = inbox_store
    srv.otp_box = otp_box
    srv.manual_register = manual_register
    srv.web_payment = web_payment
    srv.pin_manager = pin_manager
    srv.gpt_register = gpt_register
    auto_flow = _AutoFlowManager(srv, gpt_register, web_payment)
    srv.auto_flow = auto_flow
    auto_flow.start_watcher()
    srv.require_token = _server_token()
    srv.require_basic_auth = _server_basic_auth()
    srv.claim_ttl_sec = _server_claim_ttl_sec()
    srv.claim_behavior = _server_claim_behavior()
    retention = _server_retention_sec()

    # 后台线程每小时跑一次 prune_old，删 ``created_at`` 早于 ``retention`` 的终态记录。
    # 启动时先跑一次扫存量，pending 始终保留不被删。retention=0 关闭自动清理。
    if retention > 0:
        def _retention_loop() -> None:
            try:
                first = inbox_store.prune_old(retention)
                if first:
                    log.info("payment_inbox: 启动时清理 %d 条 ≥%.1fd 终态记录", first, retention / 86400.0)
            except Exception:
                log.exception("payment_inbox: 启动时 prune_old 异常")
            sleep_sec = min(3600.0, retention / 24.0)  # 至少 1 小时一次，但不超过 retention/24
            while True:
                try:
                    time.sleep(sleep_sec)
                    n = inbox_store.prune_old(retention)
                    if n:
                        log.info("payment_inbox: 周期清理 %d 条 ≥%.1fd 终态记录", n, retention / 86400.0)
                except Exception:
                    log.debug("payment_inbox: prune_old 周期异常", exc_info=True)
        threading.Thread(target=_retention_loop, daemon=True, name="inbox-retention").start()
        log.info("payment_inbox: retention=%.1fd（每 %.0fs 清一次终态记录）",
                 retention / 86400.0, min(3600.0, retention / 24.0))
    else:
        log.info("payment_inbox: retention 自动清理 已关闭（OPAI_PAYMENT_INBOX_RETENTION_SEC=0）")
    log.info(
        "payment_inbox: serving on http://%s:%d  (storage=%s, token=%s, basic=%s)",
        host, port, inbox_store.path,
        "set" if srv.require_token else "none",
        f"user={srv.require_basic_auth[0]}" if srv.require_basic_auth else "none",
    )
    print(f"\nOPAI Payment Inbox 已启动: http://{host}:{port}")
    print(f"  存储: {inbox_store.path}")
    print(f"  Token: {'已设（OPAI_PAYMENT_INBOX_TOKEN）' if srv.require_token else '未设（开放访问）'}")
    if srv.require_basic_auth:
        print(f"  Basic Auth: 用户={srv.require_basic_auth[0]}（OPAI_PAYMENT_INBOX_BASIC_USER/PASS）")
    else:
        print("  Basic Auth: 未设")
    if retention > 0:
        print(f"  Retention: {retention / 86400.0:.1f} 天（终态记录自动清理；OPAI_PAYMENT_INBOX_RETENTION_SEC=0 关闭）")
    else:
        print("  Retention: 关闭（永远保留，需手动 DELETE 清理）")
    print("  GET /                  — HTML 视图")
    print("  GET /api/jobs          — 列出 (可选 ?status=pending)")
    print("  POST /api/jobs         — 创建（subscribe_team manual 模式自动调）")
    print("  PUT /api/jobs/<id>/paid    — 标记已付")
    print("  PUT /api/jobs/<id>/cancel  — 取消")
    print("  DELETE /api/jobs/<id>      — 删除")
    print("按 Ctrl+C 停止...")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PaymentInboxClient:
    """opai-team subscribe_team manual 模式的客户端。"""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        basic_auth: tuple[str, str] | None = None,
        timeout: float = 15.0,
    ):
        # 支持 ``http://user:pass@host:port`` 形式把 basic auth 塞进 URL，
        # 让本地脚本只用一条 env 变量就能完整连上远程 inbox。
        parsed = urllib.parse.urlsplit(base_url)
        url_user = urllib.parse.unquote(parsed.username) if parsed.username else ""
        url_pass = urllib.parse.unquote(parsed.password) if parsed.password else ""
        if url_user or url_pass:
            host = parsed.hostname or ""
            netloc = host + (f":{parsed.port}" if parsed.port else "")
            base_url = urllib.parse.urlunsplit(
                (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
            )
        self.base_url = base_url.rstrip("/")
        self.token = (token or os.environ.get("OPAI_PAYMENT_INBOX_TOKEN") or "").strip()
        self.basic_auth = basic_auth
        if self.basic_auth is None:
            if url_user and url_pass:
                self.basic_auth = (url_user, url_pass)
            else:
                env_u = (os.environ.get("OPAI_PAYMENT_INBOX_BASIC_USER") or "").strip()
                env_p = (os.environ.get("OPAI_PAYMENT_INBOX_BASIC_PASS") or "").strip()
                if env_u and env_p:
                    self.basic_auth = (env_u, env_p)
        self.timeout = timeout

    def _req(self, method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url + path
        body = None
        headers = {"Accept": "application/json"}
        if self.basic_auth is not None:
            cred = base64.b64encode(f"{self.basic_auth[0]}:{self.basic_auth[1]}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {cred}"
        elif self.token:
            headers["X-Auth-Token"] = self.token
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                txt = resp.read().decode("utf-8")
                code = resp.status
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8") if e.fp else ""
            code = e.code
        try:
            data_out = json.loads(txt) if txt else {}
        except Exception:
            data_out = {"raw": txt}
        if code >= 400:
            raise RuntimeError(f"{method} {url} → HTTP {code}: {txt[:200]}")
        return data_out

    def push_job(
        self,
        *,
        account_name: str,
        account_email: str,
        plan_kind: str,
        checkout_url: str,
        paypal_url: str | None = None,
        provider: str = "paypal",
        provider_url: str | None = None,
        expires_at: str | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "account_name": account_name,
            "account_email": account_email,
            "plan_kind": plan_kind,
            "checkout_url": checkout_url,
            "paypal_url": paypal_url or "",
            "expires_at": expires_at or "",
            "notes": notes,
        }
        # provider / provider_url 仅当显式给了或非默认时发送,保持与老 server(v1)兼容
        # —— v1 server 不识别这俩字段会忽略,只存 paypal_url。
        if provider and provider != "paypal":
            body["provider"] = provider
        if provider_url is not None:
            body["provider_url"] = provider_url or ""
        return self._req("POST", "/api/jobs", body)

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._req("GET", f"/api/jobs/{job_id}")

    def list_jobs(
        self,
        *,
        status: str = "",
        email: str = "",
        plan_kind: str = "",
        provider: str = "",
        limit: int = 200,
        include_claimed: bool = True,
        order: str = "created_desc",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit or 200), 500)),
            "order": order or "created_desc",
        }
        if status:
            params["status"] = status
        if email:
            params["email"] = email
        if plan_kind:
            params["plan_kind"] = plan_kind
        if include_claimed:
            params["include_claimed"] = "1"
        qs = urllib.parse.urlencode(params)
        resp = self._req("GET", f"/api/jobs?{qs}")
        jobs = resp.get("jobs") or []
        if not isinstance(jobs, list):
            return []
        if provider:
            target = provider.strip().lower()
            return [j for j in jobs if str(j.get("provider") or "").strip().lower() == target]
        return [j for j in jobs if isinstance(j, dict)]

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._req("PUT", f"/api/jobs/{job_id}/cancel")

    def mark_paid(self, job_id: str) -> dict[str, Any]:
        return self._req("PUT", f"/api/jobs/{job_id}/paid")

    def claim_job(self, job_id: str) -> dict[str, Any]:
        return self._req("PUT", f"/api/jobs/{job_id}/claim")

    def _claim_next_one(self, *, prefer_paypal_url: bool, prefer_oldest: bool) -> dict[str, Any] | None:
        """一次原子 claim;无 pending 返 None,真错(网络/HTTP 5xx 等)log warning + 返 None。

        ``_req`` 把所有 HTTP >=400 包成 ``RuntimeError("METHOD URL → HTTP CODE: BODY")``,
        我们解析其中的 ``HTTP <code>`` 来区分 404(no_pending,无活)与真错(需要让上层知道)。
        """
        try:
            return self._req("POST", "/api/jobs/claim_next", data={
                "prefer_paypal_url": bool(prefer_paypal_url),
                "prefer_oldest": bool(prefer_oldest),
            })
        except RuntimeError as exc:
            msg = str(exc)
            if "HTTP 404" in msg:
                return None
            log.warning("payment_inbox client: claim_next HTTP error: %s", msg)
            return None
        except urllib.error.URLError as exc:
            log.warning("payment_inbox client: claim_next 网络错误: %s", exc)
            return None
        except Exception:
            log.warning("payment_inbox client: claim_next 未知错误", exc_info=True)
            return None

    def pick_next_pending(
        self,
        *,
        prefer_paypal_url: bool = True,
        prefer_oldest: bool = False,
    ) -> dict[str, Any] | None:
        """从 inbox 拿一条 pending job 并**原子 claim**(server 端单条 SQL 完成)。

        ``prefer_paypal_url=True``(默认):**优先**拿带 paypal_url 的;严格优先选不上时**再调一次**
        不限 paypal_url(fallback),保留旧 client-side fallback 语义。
        ``prefer_oldest=True`` 用 ``created_asc`` 拿最早创建的优先;默认 ``False``(``created_desc``)。
        返回 ``None`` 表示当前没活。

        实现:POST ``/api/jobs/claim_next``(server 端 ``UPDATE ... RETURNING`` 单 SQL,
        多 worker 不会双 claim 到同一条;之前 GET ``/api/jobs?status=pending`` 客户端 pick
        会有竞争窗口,已废弃)。
        网络错误 / HTTP 5xx 等会 ``log.warning`` 但不抛 — 调用方仍当成"暂时没活"轮询下一轮。
        """
        # 第一遍:按 prefer_paypal_url 严格选
        out = self._claim_next_one(
            prefer_paypal_url=prefer_paypal_url, prefer_oldest=prefer_oldest,
        )
        if out is not None:
            return out
        # 第二遍 fallback:首选时(prefer_paypal_url=True)没选到 → 放宽到不限,把
        # checkout-only job 也带回来。等价旧 client-side fallback 语义。
        if prefer_paypal_url:
            return self._claim_next_one(
                prefer_paypal_url=False, prefer_oldest=prefer_oldest,
            )
        return None

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        """局部更新 job 可变字段。

        允许字段(server 端 ``allowed`` whitelist):
          - ``paypal_url``(老 PayPal 字段) / ``provider`` / ``provider_url``(v2 通用通道)
          - ``checkout_url`` / ``notes`` / ``expires_at``
          - ``oauth_status``(v3:``''`` / ``in_progress`` / ``completed`` / ``failed``)

        例:
          - PayPal 重提取:``client.update_job(jid, paypal_url="https://...")``
          - GoPay 重提取:``client.update_job(jid, provider="gopay", provider_url="https://app.midtrans.com/...")``
          - 标 OAuth 完成:``client.update_job(jid, oauth_status="completed")``
        """
        return self._req("PUT", f"/api/jobs/{job_id}", data=fields)

    def find_active_job_by_email(self, email: str) -> dict[str, Any] | None:
        """按 ``account_email`` 找当前 active 的 job(用于 subscribe_team 重启 resume)。

        优先返回 **pending**(待付款,worker 应续 poll);
        其次返回 **paid 但 oauth_status != 'completed'** 的最新一条
        (已付款但 OAuth 没跑完,worker 应续 OAuth);
        其它(全是 cancelled/expired/已 oauth_done)返 ``None`` — caller 走全新流程。

        网络/HTTP 异常一律返 ``None``,让 caller 当成"无活"继续走全新流程,避免
        inbox 暂时不可达就阻塞订阅。
        """
        e = (email or "").strip()
        if not e:
            return None
        try:
            qs = urllib.parse.urlencode({"email": e, "limit": 50})
            r = self._req("GET", f"/api/jobs?{qs}")
        except Exception:
            log.warning("payment_inbox client: find_active_job_by_email 网络/HTTP 失败", exc_info=True)
            return None
        jobs = r.get("jobs") or []
        if not isinstance(jobs, list):
            return None
        # 先挑 pending(任意一条都行,inbox 设计上同 email 同时只有 1 个 pending — 但容错处理)
        pending = [j for j in jobs if isinstance(j, dict) and j.get("status") == "pending"]
        if pending:
            # 拿 created_at 最新的(防止旧 pending job 被新覆盖时漏选)
            pending.sort(key=lambda j: str(j.get("created_at") or ""), reverse=True)
            return pending[0]
        # 再挑 paid + oauth 未完成
        unfinished_paid = [
            j for j in jobs
            if isinstance(j, dict)
            and j.get("status") == "paid"
            and (j.get("oauth_status") or "") != "completed"
        ]
        if unfinished_paid:
            unfinished_paid.sort(key=lambda j: str(j.get("paid_at") or j.get("created_at") or ""), reverse=True)
            return unfinished_paid[0]
        return None

    def count_pending(self) -> int:
        """返回当前 ``status=pending`` 的总数。``include_claimed=1`` 让 claim 过的也算进来——
        我们要的是「inbox 真实 pending 总量」（含已被点过但未付款），不是 web 视角下的可见数。

        网络异常返回 -1，调用方应当作「未知」放行（避免因 inbox 暂时不可达就死等）。
        """
        try:
            r = self._req("GET", "/api/jobs?status=pending&limit=1&include_claimed=1")
        except Exception:
            return -1
        try:
            return int(r.get("total") or 0)
        except (TypeError, ValueError):
            return -1

    def wait_for_paid(
        self,
        job_id: str,
        *,
        timeout_sec: float,
        poll_interval_sec: float = 10.0,
        progress_callback=None,
    ) -> dict[str, Any]:
        """轮询直到 ``status`` 为 ``paid`` / ``cancelled`` / ``expired`` 或超时。

        ``progress_callback(remaining_sec, job)`` 可选，每轮调一次（用于打日志）。
        返回最终的 job dict；超时抛 ``TimeoutError``。
        """
        deadline = time.monotonic() + timeout_sec
        while True:
            job = self.get_job(job_id)
            status = (job.get("status") or "").strip().lower()
            if status in ("paid", "cancelled", "expired"):
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"payment_inbox: 等待 paid 超时 (job={job_id})")
            if progress_callback is not None:
                try:
                    progress_callback(remaining, job)
                except Exception:
                    log.debug("payment_inbox: progress_callback 异常", exc_info=True)
            sleep_for = min(poll_interval_sec, max(1.0, remaining))
            time.sleep(sleep_for)


# 模块级辅助：让 subscribe_team / 其他调用方拿到客户端
def get_default_client() -> PaymentInboxClient | None:
    """如果设置了 ``OPAI_PAYMENT_INBOX_BASE_URL``，返回客户端；否则 None。"""
    base = (os.environ.get("OPAI_PAYMENT_INBOX_BASE_URL") or "").strip()
    if not base:
        return None
    return PaymentInboxClient(base)


# ---------------------------------------------------------------------------
# Standalone entrypoint：``python3 payment_inbox.py [--host H] [--port P]``
# 让本文件 scp 到远程后能直接独立运行（不依赖 opai 包）。
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse as _argparse

    _ap = _argparse.ArgumentParser(description="OPAI Payment Inbox standalone server")
    _ap.add_argument("--host", default=os.environ.get("OPAI_PAYMENT_INBOX_HOST") or "0.0.0.0")
    _ap.add_argument("--port", type=int, default=int(os.environ.get("OPAI_PAYMENT_INBOX_PORT") or "18130"))
    _ap.add_argument("--storage", default=os.environ.get("OPAI_PAYMENT_INBOX_PATH") or "")
    _args = _ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _store = InboxStore(Path(_args.storage).expanduser().resolve()) if _args.storage else InboxStore()
    run_inbox_server(host=_args.host, port=_args.port, store=_store)
