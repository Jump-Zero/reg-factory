#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cliproxy 本地中转代理。

背景：cliproxy 动态住宅拒绝中国大陆来源 IP 直连（报
"forbidden ip=... not supported"），因此本机必须先经 Clash 出海，
再连 cliproxy。requests / Playwright / 指纹浏览器都只支持一跳代理，
无法自己完成「Clash -> cliproxy」两级链路，故由本进程代为串联：

    客户端(本工具口 127.0.0.1:8899)
      -> Clash(127.0.0.1:7897, CONNECT 隧道)
        -> cliproxy(us.cliproxy.io:3010, 客户端凭据原样透传)
          -> 目标网站(菲律宾等住宅出口)

对客户端而言本工具就是一个普通 HTTP 代理：CONNECT 与明文请求均
原样透传（含 Proxy-Authorization），cliproxy 的用户名密码仍由
调用方携带，本工具不存储任何凭据——除非显式提供兜底凭据：
Chromium 系浏览器（Playwright/指纹浏览器/CDP 接管场景）的 407
应答式代理认证经常无人应答（凭据应答器注册在启动进程，接管会话
不继承），因此支持注入兜底凭据：客户端未带凭据时自动补上，浏览
器首个 CONNECT 即成功。

用法：
    python tools/cliproxy_relay.py
可选环境变量：
    CLIPROXY_RELAY_LISTEN       默认 127.0.0.1:8899
    CLIPROXY_RELAY_CLASH        默认 127.0.0.1:7897
    CLIPROXY_RELAY_UPSTREAM     默认 us.cliproxy.io:3010
    CLIPROXY_RELAY_CREDENTIALS  兜底凭据 user:pass；缺省时自动从
                                项目 .env 的住宅代理解析
"""

import base64
import os
import socket
import select
import sys
import threading
import urllib.parse
from pathlib import Path

LISTEN = os.environ.get("CLIPROXY_RELAY_LISTEN", "127.0.0.1:8899")
CLASH = os.environ.get("CLIPROXY_RELAY_CLASH", "127.0.0.1:7897")
UPSTREAM = os.environ.get("CLIPROXY_RELAY_UPSTREAM", "us.cliproxy.io:3010")

HANDSHAKE_TIMEOUT = 30          # 建立 Clash/cliproxy 链路的超时
IDLE_TIMEOUT = 300              # 转发阶段闲置超时
BUFSIZE = 65536


def load_default_credentials():
    """解析兜底凭据 user:pass，返回可注入的 Proxy-Authorization 头字节。

    优先级：环境变量 CLIPROXY_RELAY_CREDENTIALS > 项目 .env 的
    REG_FACTORY_PROXY_POOL 第一条 > REG_FACTORY_PROXY。解析失败返回 None。
    """
    cred = os.environ.get("CLIPROXY_RELAY_CREDENTIALS", "").strip()
    if not cred:
        env_path = Path(__file__).resolve().parents[1] / ".env"
        try:
            text = env_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        candidates = []
        for key in ("REG_FACTORY_PROXY_POOL", "REG_FACTORY_PROXY"):
            for line in text.splitlines():
                line = line.strip()
                if line.startswith(f"{key}=") and not line.lstrip("_").startswith("#"):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    first = val.split(",", 1)[0].strip()
                    if first:
                        candidates.append(first)
                    break
        for cand in candidates:
            # http://user:pass@host:port -> user:pass
            if "@" in cand:
                scheme_split = cand.split("://", 1)
                cred = scheme_split[1].rsplit("@", 1)[0]
                break
    if not cred:
        return None
    cred = urllib.parse.unquote(cred)
    if ":" not in cred:
        return None
    b64 = base64.b64encode(cred.encode("utf-8")).decode("ascii")
    return f"Proxy-Authorization: Basic {b64}\r\n".encode("ascii")


DEFAULT_CRED_HEADER = load_default_credentials()


def _log(msg: str) -> None:
    print(f"[relay] {msg}", flush=True)


def _recv_head(sock: socket.socket) -> bytes:
    """读一段请求头（到 \\r\\n\\r\\n），返回完整缓冲。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(BUFSIZE)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 128 * 1024:
            raise ValueError("header too large")
    return buf


def _read_response_head(sock: socket.socket):
    """读一个 HTTP 响应头，返回 (head_bytes, residual_after_head)。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(BUFSIZE)
        if not chunk:
            break
        buf += chunk
    if b"\r\n\r\n" not in buf:
        return buf, b""
    head, residual = buf.split(b"\r\n\r\n", 1)
    return head + b"\r\n\r\n", residual


def _connect_via_clash() -> socket.socket:
    """建立 Clash -> cliproxy 的 CONNECT 隧道，返回隧道 socket。"""
    host, port = UPSTREAM.rsplit(":", 1)
    s = socket.create_connection((CLASH.split(":")[0], int(CLASH.rsplit(":", 1)[1])),
                                 timeout=HANDSHAKE_TIMEOUT)
    s.settimeout(HANDSHAKE_TIMEOUT)
    s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    head, _ = _read_response_head(s)
    first_line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
    if " 200" not in first_line:
        s.close()
        raise OSError(f"clash tunnel refused: {first_line}")
    return s


def _splice(a: socket.socket, b: socket.socket) -> None:
    """双向透传，任一侧关闭/超时即结束并关闭两侧。"""
    a.settimeout(None)
    b.settimeout(None)
    sockets = [a, b]
    try:
        while True:
            r, _, x = select.select(sockets, [], sockets, IDLE_TIMEOUT)
            if x:
                break
            if not r:
                _log("idle timeout, closing")
                break
            for src in r:
                dst = b if src is a else a
                data = src.recv(BUFSIZE)
                if not data:
                    return
                dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


def _handle(client: socket.socket) -> None:
    client.settimeout(HANDSHAKE_TIMEOUT)
    head = _recv_head(client)
    if not head:
        client.close()
        return
    # 兜底凭据：客户端未带 Proxy-Authorization 时自动补上，
    # 让 Chromium 系浏览器的首个 CONNECT 直接通过，绕开 407 应答流程。
    if (DEFAULT_CRED_HEADER
            and b"proxy-authorization" not in head.lower()
            and head.endswith(b"\r\n\r\n")):
        # 只去掉结尾空行（\r\n\r\n 的后 2 字节），保留上一行自身的 CRLF；
        # [:-4] 会把末行换行一起吃掉，导致注入头粘进上一行的值里（407）。
        head = head[:-2] + DEFAULT_CRED_HEADER + b"\r\n"
        _log("injected fallback Proxy-Authorization into client request")
    first_line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
    method = first_line.split(" ", 1)[0].upper()

    try:
        tunnel = _connect_via_clash()
    except Exception as e:
        _log(f"upstream tunnel failed: {e}")
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n"
                           b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        client.close()
        return

    if method == "CONNECT":
        # 客户端 CONNECT 原样送入 cliproxy（含 Proxy-Authorization）
        tunnel.sendall(head)
        try:
            resp_head, residual = _read_response_head(tunnel)
        except OSError as e:
            _log(f"upstream read failed: {e}")
            tunnel.close()
            client.close()
            return
        status = resp_head.split(b"\r\n", 1)[0].decode("latin1", "replace")
        if " 200" in status:
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if residual:
                client.sendall(residual)
            _splice(client, tunnel)
        else:
            _log(f"upstream denied: {status}")
            try:
                client.sendall(resp_head + residual)
            except OSError:
                pass
            tunnel.close()
            client.close()
    else:
        # 明文请求（GET http://...）：原样透传后进入双向转发，
        # keep-alive 的后续请求也在 splice 中继续透传。
        tunnel.sendall(head)
        _splice(client, tunnel)


def main() -> None:
    lhost, lport = LISTEN.rsplit(":", 1)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((lhost, int(lport)))
    server.listen(128)
    _log(f"listening on http://{LISTEN} -> clash {CLASH} -> {UPSTREAM}")
    _log("keep Clash running; point REG_FACTORY_PROXY at this port.")
    while True:
        try:
            client, addr = server.accept()
        except KeyboardInterrupt:
            _log("bye")
            break
        threading.Thread(target=_handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
