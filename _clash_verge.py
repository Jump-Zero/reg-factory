"""Clash Verge / mihomo RESTful controller helper.

Used by _batch_register.py to rotate proxy nodes between registration attempts,
reducing the Cloudflare / Replit risk-control rate that hits a single IP.

Default Clash Verge controller is http://127.0.0.1:9097 (no secret). The port
can be checked / changed in Settings -> Verge Settings -> External Controller.
mihomo / Clash core default is 9090.

Public API (all standalone-runnable):

    >>> python _clash_verge.py ping
    >>> python _clash_verge.py groups
    >>> python _clash_verge.py nodes --group "🚀 节点选择"
    >>> python _clash_verge.py current --group "🚀 节点选择"
    >>> python _clash_verge.py switch --group "🚀 节点选择" --node "Tokyo-01"
    >>> python _clash_verge.py rotate --group "🚀 节点选择" --strategy round_robin
    >>> python _clash_verge.py ip

Designed to be imported as a module too:

    from _clash_verge import ClashClient
    c = ClashClient("http://127.0.0.1:9097")
    c.rotate("Proxy", strategy="random")
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------
# stdout utf-8 hardening (Windows console)
# ------------------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ------------------------------------------------------------------
# defaults
# ------------------------------------------------------------------
DEFAULT_API = os.environ.get("CLASH_API", "http://127.0.0.1:9097")
DEFAULT_SECRET = os.environ.get("CLASH_SECRET", "")
DEFAULT_GROUP = os.environ.get("CLASH_GROUP", "")
DEFAULT_TEST_URL = "http://www.gstatic.com/generate_204"

# Ports we auto-probe when --clash-api is not set. 9097 = Clash Verge default,
# 9090 = mihomo / clash core default, 9091/9099 = common alternates.
AUTO_PROBE_PORTS = (9097, 9090, 9091, 9099, 9095, 9898)

# Names that should never be picked as a "node" (they are special tokens / groups).
SPECIAL_NAMES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}

# Many Chinese paid subscriptions stuff "info pseudo-nodes" into the proxy list
# (showing remaining traffic, expiry date, etc). Switching to them silently kills
# the connection. Any node name containing these substrings is filtered out.
FAKE_NODE_HINTS = (
    "剩余流量", "剩余", "到期", "重置", "距离", "套餐", "官网", "客服",
    "更新", "公告", "网址", "通知", "Expire", "Traffic", "Reset",
    "expire", "traffic", "reset", "流量", "网站", "订阅", "返佣", "邀请",
    "有问题", "获取订阅", "机场",
)


def is_fake_node(name: str) -> bool:
    """Heuristic: true for subscription-info pseudo-nodes that aren't real proxies."""
    if not str(name or "").strip():
        return True
    name = str(name)
    for h in FAKE_NODE_HINTS:
        if h in name:
            return True
    return False

# Preferred group names — searched in order when no explicit --clash-group given.
GROUP_NAME_PREFERENCE = (
    "Proxy", "PROXY", "🚀 节点选择", "节点选择", "Manual Select",
    "手动选择", "Select", "全部节点", "All",
)

# Rotation state — persisted per group between invocations, so round_robin
# survives across subprocess boundaries (each register attempt is a new process).
STATE_DIR = Path(os.environ.get("CLASH_STATE_DIR", str(Path.home() / ".cache" / "clash_rotate")))


# ------------------------------------------------------------------
# HTTP layer
# ------------------------------------------------------------------
class ClashError(RuntimeError):
    pass


class ClashClient:
    def __init__(self, api: str = DEFAULT_API, secret: str = DEFAULT_SECRET, timeout: int = 8):
        self.api = api.rstrip("/")
        self.secret = secret or ""
        self.timeout = timeout

    def _req(self, method: str, path: str, body: Any | None = None) -> Any:
        url = f"{self.api}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                if not raw:
                    return None
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body_txt = ""
            try:
                body_txt = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise ClashError(f"HTTP {e.code} {method} {path}: {body_txt}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ClashError(f"connect failed {method} {path}: {e}") from e

    # ---- read --------------------------------------------------------------
    def version(self) -> dict:
        return self._req("GET", "/version")

    def proxies(self) -> dict:
        """Returns {'proxies': {name: {type, now?, all?, history, ...}, ...}}"""
        return self._req("GET", "/proxies")

    def group(self, name: str) -> dict:
        return self._req("GET", f"/proxies/{urllib.parse.quote(name, safe='')}")

    def list_groups(self) -> list[str]:
        all_p = self.proxies().get("proxies", {})
        out = []
        for name, info in all_p.items():
            t = (info.get("type") or "").lower()
            if t in ("selector", "fallback", "urltest", "loadbalance"):
                out.append(name)
        return sorted(out)

    def list_nodes(self, group: str, exclude_special: bool = True,
                   exclude_fake: bool = True) -> list[str]:
        info = self.group(group)
        all_n = info.get("all") or []
        if exclude_special:
            all_n = [n for n in all_n if n not in SPECIAL_NAMES]
        if exclude_fake:
            all_n = [n for n in all_n if not is_fake_node(n)]
        return list(all_n)

    def current(self, group: str) -> str | None:
        return self.group(group).get("now")

    # ---- delay / health ----------------------------------------------------
    def delay(self, node: str, url: str = DEFAULT_TEST_URL, timeout_ms: int = 3000) -> int | None:
        """Returns latency in ms, or None on timeout/error."""
        q = urllib.parse.urlencode({"url": url, "timeout": timeout_ms})
        try:
            r = self._req("GET", f"/proxies/{urllib.parse.quote(node, safe='')}/delay?{q}")
            if isinstance(r, dict) and "delay" in r:
                return int(r["delay"])
        except ClashError as e:
            # 408/504 = timeout
            return None
        return None

    # ---- write -------------------------------------------------------------
    def switch(self, group: str, node: str) -> None:
        self._req("PUT", f"/proxies/{urllib.parse.quote(group, safe='')}", body={"name": node})

    def close_connections(self) -> int:
        """Drop all currently-open TCP connections so they re-route via the new node."""
        try:
            self._req("DELETE", "/connections")
            return 0
        except ClashError as e:
            print(f"[clash] close_connections warning: {e}", file=sys.stderr)
            return -1


# ------------------------------------------------------------------
# rotation strategies
# ------------------------------------------------------------------
def _state_file(api: str, group: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    key = urllib.parse.quote(f"{api}#{group}", safe="")
    return STATE_DIR / f"{key}.json"


def _load_state(api: str, group: str) -> dict:
    f = _state_file(api, group)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(api: str, group: str, st: dict) -> None:
    _state_file(api, group).write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def _probe_timeout_ms(max_latency_ms: int | None) -> int:
    """Clash /delay `timeout` is how long the core waits for the TCP probe (not RTT cap)."""
    if max_latency_ms is None:
        return 10000
    return max(10000, int(max_latency_ms) + 3000)


def pick_node(
    client: ClashClient,
    group: str,
    strategy: str = "round_robin",
    excluded: set[str] | None = None,
    max_latency_ms: int | None = None,
    probe_url: str = DEFAULT_TEST_URL,
) -> str | None:
    """Pick the next node based on strategy.

    Strategies:
      round_robin: cycle alphabetically through nodes, persisted in state file
      random:      pick a random node
      sequential:  same as round_robin but resets to first on missing state
      lowest:      probe latency for all nodes, pick the lowest (slow, only on demand)

    If `max_latency_ms` is set, only nodes with a successful /delay and RTT <= cap are
    returned. If none pass after a full pass, returns None (caller should skip rotate).
    """
    excluded = excluded or set()
    nodes = [n for n in client.list_nodes(group) if n not in excluded]
    if not nodes:
        return None
    nodes_sorted = sorted(nodes)
    tmo = _probe_timeout_ms(max_latency_ms)

    def ok_latency(d: int | None) -> bool:
        if d is None:
            return False
        if max_latency_ms is None:
            return True
        return d <= max_latency_ms

    if strategy in ("random",):
        if max_latency_ms is None:
            return random.choice(nodes_sorted)
        order = nodes_sorted[:]
        random.shuffle(order)
        for candidate in order:
            d = client.delay(candidate, probe_url, tmo)
            if ok_latency(d):
                return candidate
        return None

    if strategy in ("round_robin", "sequential"):
        st = _load_state(client.api, group)
        last = st.get("last")
        if last in nodes_sorted:
            idx = (nodes_sorted.index(last) + 1) % len(nodes_sorted)
        else:
            idx = 0
        candidate = nodes_sorted[idx]
        if max_latency_ms is not None:
            tried = 0
            while tried < len(nodes_sorted):
                d = client.delay(candidate, probe_url, tmo)
                if ok_latency(d):
                    return candidate
                idx = (idx + 1) % len(nodes_sorted)
                candidate = nodes_sorted[idx]
                tried += 1
            return None
        return candidate

    if strategy == "lowest":
        best, best_d = None, 1 << 30
        for n in nodes_sorted:
            d = client.delay(n, probe_url, tmo)
            if d is None:
                continue
            if max_latency_ms is not None and d > max_latency_ms:
                continue
            if d < best_d:
                best, best_d = n, d
        return best

    raise ValueError(f"unknown strategy: {strategy!r}")


def rotate(
    client: ClashClient,
    group: str,
    strategy: str = "round_robin",
    excluded: set[str] | None = None,
    drop_conns: bool = True,
    verbose: bool = True,
    max_latency_ms: int | None = None,
    probe_url: str = DEFAULT_TEST_URL,
) -> dict:
    """Switch the named group to the next node per `strategy`.

    Returns a dict {ok, prev, next, group, latency_ms}.
    """
    prev = client.current(group)
    candidate = pick_node(client, group, strategy=strategy, excluded=excluded,
                          max_latency_ms=max_latency_ms, probe_url=probe_url)
    if not candidate:
        if verbose:
            print(f"[clash] rotate: no usable node in group {group!r}")
        return {"ok": False, "prev": prev, "next": None, "group": group}

    client.switch(group, candidate)
    if drop_conns:
        client.close_connections()

    # remember for next round_robin
    st = _load_state(client.api, group)
    st["last"] = candidate
    st["ts"] = time.time()
    _save_state(client.api, group, st)

    latency = client.delay(candidate, probe_url, _probe_timeout_ms(max_latency_ms))
    if verbose:
        print(f"[clash] rotated {group!r}: {prev!r} -> {candidate!r} "
              f"(latency={latency}ms strategy={strategy})")
    return {"ok": True, "prev": prev, "next": candidate, "group": group,
            "latency_ms": latency, "strategy": strategy}


# ------------------------------------------------------------------
# external-IP probe (sanity check after switching)
# ------------------------------------------------------------------
def auto_detect_api(secret: str = "", timeout: int = 2,
                    ports=AUTO_PROBE_PORTS, verbose: bool = True) -> str | None:
    """Probe localhost for a Clash controller. Returns the first responding URL
    or None.

    Tries each port with the supplied secret first; on HTTP 401 it also tries
    without (and vice-versa) so a wrong-secret config still gets found. Logs a
    hint when 401 is seen so the user knows to pass --clash-secret.
    """
    # Build secret-attempt order: try the configured one first, then the other
    secret_candidates = [secret] if secret else [""]
    if secret and "" not in secret_candidates:
        secret_candidates.append("")  # try without secret as fallback
    if not secret:
        # no secret given — only try empty, but remember if we hit 401
        secret_candidates = [""]

    seen_401 = []
    for port in ports:
        url = f"http://127.0.0.1:{port}"
        for sec in secret_candidates:
            c = ClashClient(url, sec, timeout=timeout)
            try:
                v = c.version()
                if isinstance(v, dict) and ("version" in v or "premium" in v or "meta" in v):
                    if verbose and sec and sec != secret:
                        print(f"[clash] auto-detect: found {url} via fallback (no-secret)")
                    return url
            except ClashError as e:
                msg = str(e)
                if "HTTP 401" in msg:
                    seen_401.append((url, sec))
                continue
            except Exception:
                continue

    if seen_401 and verbose:
        # Saw at least one controller demanding auth but our secret didn't match
        urls = sorted({u for u, _ in seen_401})
        print(f"[clash] auto-detect: 401 Unauthorized at {urls} "
              f"— Clash IS running but needs a secret. "
              f"Set CLASH_SECRET=<your secret> and retry.")
    return None


def _seed_probe_connection(client: ClashClient, hosts: tuple[str, ...]) -> None:
    """Provoke at least one Clash connection for each `hosts` so that the
    subsequent /connections snapshot has data to reason about.

    Why: in TUN mode at startup there are usually 0 live connections, which
    makes detect_active_groups() useless. We poke through Clash's own
    /proxies/<node>/delay endpoint, which causes Clash core to open a real
    connection to the URL — exactly what we want to observe.
    """
    if not hosts:
        return
    try:
        prox = client.proxies().get("proxies", {})
    except ClashError:
        return
    # any reachable proxy will do; we just need Clash to evaluate rules for url.
    probe_node = None
    for name, info in prox.items():
        if name in SPECIAL_NAMES:
            continue
        t = (info.get("type") or "").lower()
        if t in ("shadowsocks", "vmess", "trojan", "vless", "hysteria",
                 "hysteria2", "snell", "ss", "wireguard", "tuic"):
            probe_node = name
            break
    if not probe_node:
        # Fallback: any selector's first member
        for info in prox.values():
            for n in (info.get("all") or []):
                if n not in SPECIAL_NAMES and not is_fake_node(n):
                    probe_node = n
                    break
            if probe_node:
                break
    if not probe_node:
        return
    for host in hosts:
        url = f"https://{host}/"
        try:
            q = urllib.parse.urlencode({"url": url, "timeout": 800})
            client._req("GET", f"/proxies/{urllib.parse.quote(probe_node, safe='')}/delay?{q}")
        except ClashError:
            continue
        except Exception:
            continue


def detect_active_groups(client: ClashClient, target_hosts: tuple[str, ...] = (),
                         seed_probe: bool = True) -> list[str]:
    """Inspect live /connections and infer which selector groups are actually
    used by the current ruleset. Returns groups ordered by usage frequency.

    If target_hosts is provided, prefers groups serving those hostnames.
    If seed_probe is True, fires a quick delay-probe to each target_host first
    so /connections has data even when called early in a session.
    """
    if seed_probe and target_hosts:
        _seed_probe_connection(client, target_hosts)
        time.sleep(0.4)  # let Clash actually open the connection
    try:
        data = client._req("GET", "/connections")
    except ClashError:
        return []
    conns = (data or {}).get("connections") or []
    if not conns:
        return []
    try:
        all_p = client.proxies().get("proxies", {})
    except ClashError:
        return []

    def is_group(name: str) -> bool:
        t = (all_p.get(name, {}).get("type") or "").lower()
        return t in ("selector", "fallback", "urltest", "loadbalance")

    from collections import Counter
    targeted = Counter()
    overall = Counter()
    for c in conns:
        host = ((c.get("metadata") or {}).get("host") or "").lower()
        chain = list(c.get("chains") or [])
        # chains[0] = leaf node, chains[-1] = top-level (GLOBAL); selector groups
        # are anything in between (or chains[-1] if it IS a selector).
        groups_in_chain = [n for n in chain if is_group(n)]
        if not groups_in_chain:
            continue
        # Heuristic: pick the *outermost* group that has > 1 leaf node — that's the
        # one whose switch will actually change egress for this connection.
        # If only GLOBAL is present, accept it (TUN/global mode).
        for grp in reversed(groups_in_chain):
            overall[grp] += 1
            if target_hosts and any(h in host for h in target_hosts):
                targeted[grp] += 1
            break

    chosen = targeted if targeted else overall
    return [g for g, _ in chosen.most_common()]


def auto_pick_group(client: ClashClient, min_nodes: int = 2,
                    prefer_active: bool = True,
                    target_hosts: tuple[str, ...] = ()) -> str | None:
    """Pick the most sensible selector group for rotation.

    Order:
      0. (if prefer_active) groups OBSERVED to currently serve traffic for
         target_hosts via /connections — TUN / global setups land here.
      1. groups whose name contains any GROUP_NAME_PREFERENCE entry (in order).
      2. selector / fallback / urltest groups with the most non-special nodes,
         deprioritising the catch-all "GLOBAL" unless it's the only candidate.
    Returns the group name or None if nothing usable.
    """
    try:
        all_p = client.proxies().get("proxies", {})
    except ClashError:
        return None

    def viable(name: str) -> bool:
        info = all_p.get(name) or {}
        t = (info.get("type") or "").lower()
        if t not in ("selector", "fallback", "urltest", "loadbalance"):
            return False
        nodes = [n for n in (info.get("all") or [])
                 if n not in SPECIAL_NAMES and not is_fake_node(n)]
        # Allow other groups OR real proxies, just need >= min_nodes choices.
        return len(nodes) >= min_nodes

    # 0. live-traffic match (highest priority for TUN / global routes)
    if prefer_active:
        active = detect_active_groups(client, target_hosts=target_hosts)
        for grp in active:
            if viable(grp):
                return grp

    # 1 & 2. previous heuristic, but allow GLOBAL as a normal candidate now.
    selectors: list[tuple[str, int, str]] = []
    for name, info in all_p.items():
        t = (info.get("type") or "").lower()
        if t not in ("selector", "fallback", "urltest", "loadbalance"):
            continue
        nodes = [n for n in (info.get("all") or [])
                 if n not in SPECIAL_NAMES and not is_fake_node(n)]
        leaf_nodes = [n for n in nodes if all_p.get(n, {}).get("type", "").lower()
                      not in ("selector", "fallback", "urltest", "loadbalance")]
        if len(leaf_nodes) < min_nodes:
            continue
        selectors.append((name, len(leaf_nodes), t))

    if not selectors:
        return None

    # Preference by name substring
    for pref in GROUP_NAME_PREFERENCE:
        for name, _, _ in selectors:
            if pref in name:
                return name

    # Sort: deprioritise GLOBAL unless it's all we have; prefer selector type.
    type_rank = {"selector": 0, "urltest": 1, "loadbalance": 2, "fallback": 3}
    def sort_key(it):
        name, n, t = it
        is_global = 1 if name == "GLOBAL" else 0
        return (is_global, -n, type_rank.get(t, 99), name)
    selectors.sort(key=sort_key)
    return selectors[0][0]


def public_ip(timeout: int = 5, mixed_port: int | None = None) -> str | None:
    """Return the current outbound IP as the proxy sees it.

    `mixed_port` is the Clash mixed-port HTTP listener (typically 7897). If
    given, we force the IP-probe HTTP requests through it. This is critical
    for TUN-mode setups where the OS system proxy is OFF — without forcing,
    `urlopen` would go through the OS network stack and report the wrong IP
    (or get blocked by TUN routing). By going through the mixed port we are
    guaranteed to see exactly what egress IP Clash is producing right now.

    If `mixed_port` is None and HTTP_PROXY env is set (e.g. the caller already
    pre-configured a Clash route), we use that. Last resort: direct urlopen.
    """
    if mixed_port is not None:
        proxy = f"http://127.0.0.1:{mixed_port}"
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
        fetcher = opener.open
    elif (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")):
        # Already configured globally; use whatever opener was installed.
        fetcher = urllib.request.urlopen
    else:
        fetcher = urllib.request.urlopen
    urls = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    ]
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "curl/8"})
            with fetcher(req, timeout=timeout) as r:
                ip = r.read().decode("utf-8", errors="replace").strip()
                if ip and len(ip) <= 64 and ip.count(".") in (3, 0):
                    return ip
        except Exception:
            continue
    return None


# ------------------------------------------------------------------
# Nested-group traversal helpers
# ------------------------------------------------------------------
_GROUP_TYPES = ("selector", "urltest", "fallback", "loadbalance")


def find_nested_selector_groups(client: ClashClient, root: str,
                                max_depth: int = 4) -> list[str]:
    """BFS-walk the currently-selected node of `root` down through nested
    selector/urltest/fallback/loadbalance groups, collecting every group along
    the way. Returns groups from outer to inner (excluding `root` itself).

    This is the core fix for: switching the OUTER selector to a nested group
    (e.g. "🔰 节点选择" -> "♻️ 自动选择") doesn't actually change egress IP,
    because the urltest underneath is still picking the same fast node.
    The caller can rotate every group in this list and finally see IP change.
    """
    try:
        all_p = client.proxies().get("proxies", {})
    except ClashError:
        return []
    chain: list[str] = []
    visited = {root}
    cur_name = root
    for _ in range(max_depth):
        info = all_p.get(cur_name) or {}
        cur = info.get("now")  # currently-selected child
        if not cur or cur in visited:
            break
        child = all_p.get(cur) or {}
        if (child.get("type") or "").lower() not in _GROUP_TYPES:
            break  # reached a terminal node (vmess/ss/...)
        chain.append(cur)
        visited.add(cur)
        cur_name = cur
    return chain


def find_terminal_node(client: ClashClient, root: str,
                       max_depth: int = 6) -> str | None:
    """Walk `root`'s now-selected child down until we hit a non-group node."""
    try:
        all_p = client.proxies().get("proxies", {})
    except ClashError:
        return None
    cur_name = root
    for _ in range(max_depth):
        info = all_p.get(cur_name) or {}
        t = (info.get("type") or "").lower()
        if t not in _GROUP_TYPES:
            return cur_name
        nxt = info.get("now")
        if not nxt or nxt == cur_name:
            return None
        cur_name = nxt
    return None


# ------------------------------------------------------------------
# rotate_with_verify: top-level rotate + IP verification + nested fallback
# ------------------------------------------------------------------
def rotate_with_verify(
    client: ClashClient,
    group: str,
    *,
    strategy: str = "round_robin",
    excluded: set[str] | None = None,
    max_latency_ms: int | None = None,
    mixed_port: int | None = None,
    settle_sec: float = 1.5,
    probe_url: str = DEFAULT_TEST_URL,
    verbose: bool = True,
) -> dict:
    """Rotate `group`, then verify egress IP actually changed.

    If IP did NOT change (which happens when `group` selects a nested
    urltest/selector that still uses the same underlying node), we descend
    into the nested groups and rotate each one until egress IP changes or we
    run out of options.

    Returns a dict with: ok, group, prev, next, ip_before, ip_after,
    ip_changed (bool), nested_rotations (list), terminal_node, settle_sec.
    """
    ip_before = public_ip(timeout=5, mixed_port=mixed_port)
    base = rotate(client, group, strategy=strategy, excluded=excluded,
                  max_latency_ms=max_latency_ms, drop_conns=True,
                  verbose=verbose, probe_url=probe_url)
    if not base.get("ok"):
        base["ip_before"] = ip_before
        base["ip_after"] = ip_before
        base["ip_changed"] = False
        return base

    time.sleep(settle_sec)
    ip_after = public_ip(timeout=5, mixed_port=mixed_port)
    result = dict(base)
    result["ip_before"] = ip_before
    result["ip_after"] = ip_after
    result["ip_changed"] = bool(ip_before and ip_after and ip_before != ip_after)

    if result["ip_changed"]:
        if verbose:
            print(f"[clash] IP egress changed: {ip_before} -> {ip_after}")
        return result

    # IP didn't move. Are we selecting a nested group?
    if verbose:
        print(f"[clash] outer rotate did NOT change IP ({ip_before}); "
              f"diving into nested groups...")
    nested = find_nested_selector_groups(client, group)
    if not nested:
        # No nested groups; the next node IS terminal — IP just happens to match.
        # That can be a real coincidence on a tiny pool. Caller can re-rotate.
        if verbose:
            print(f"[clash] no nested groups under {group!r}; egress IP unchanged.")
        result["nested_rotations"] = []
        return result

    rotations: list[dict] = []
    for inner in nested:
        try:
            r = rotate(client, inner, strategy=strategy, excluded=excluded,
                       max_latency_ms=max_latency_ms, drop_conns=True,
                       verbose=verbose, probe_url=probe_url)
            rotations.append({"group": inner, **r})
        except Exception as e:
            rotations.append({"group": inner, "ok": False, "error": str(e)})
        time.sleep(0.6)
        ip_check = public_ip(timeout=5, mixed_port=mixed_port)
        if ip_check and ip_before and ip_check != ip_before:
            result["ip_after"] = ip_check
            result["ip_changed"] = True
            result["broke_at_group"] = inner
            result["nested_rotations"] = rotations
            if verbose:
                print(f"[clash] IP egress changed after rotating nested {inner!r}: "
                      f"{ip_before} -> {ip_check}")
            return result

    # Last-ditch: probe terminal node identity (debug aid)
    result["nested_rotations"] = rotations
    result["terminal_node"] = find_terminal_node(client, group)
    if verbose:
        print(f"[clash] after rotating {len(nested)} nested groups, IP still "
              f"{ip_before} (terminal={result['terminal_node']!r}). "
              f"Provider may have a small unique-IP pool or sticky upstream.")
    return result


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def _add_common(p):
    p.add_argument("--api", default=DEFAULT_API, help=f"clash controller URL (default {DEFAULT_API})")
    p.add_argument("--secret", default=DEFAULT_SECRET, help="optional bearer token")
    p.add_argument("--group", default=DEFAULT_GROUP, help="proxy group name")
    p.add_argument("--exclude", default="", help="comma-separated node names to exclude")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ping", help="check that clash controller is reachable"); _add_common(p)
    p = sub.add_parser("groups", help="list selector groups"); _add_common(p)
    p = sub.add_parser("nodes", help="list nodes in --group"); _add_common(p)
    p = sub.add_parser("current", help="show currently-selected node in --group"); _add_common(p)
    p = sub.add_parser("switch", help="switch --group to --node"); _add_common(p)
    p.add_argument("--node", required=True)
    p = sub.add_parser("rotate", help="rotate --group per --strategy"); _add_common(p)
    p.add_argument("--strategy", default="round_robin",
                   choices=["round_robin", "random", "sequential", "lowest"])
    p.add_argument("--max-latency-ms", type=int, default=None,
                   help="skip nodes whose probe latency > this (None = don't probe)")
    p.add_argument("--no-drop", action="store_true", help="don't close existing connections")
    p = sub.add_parser("ip", help="print current outbound IP via Clash")
    p.add_argument("--mixed-port", type=int, default=7897,
                   help="Clash HTTP/SOCKS5 mixed port to route the IP probe through "
                        "(default 7897; set 0 to disable and use OS network)")
    p = sub.add_parser("rotate-verify",
                       help="rotate then verify egress IP changed; recurse into nested "
                            "selector/urltest groups if it didn't")
    _add_common(p)
    p.add_argument("--strategy", default="round_robin",
                   choices=["round_robin", "random", "sequential", "lowest"])
    p.add_argument("--max-latency-ms", type=int, default=None)
    p.add_argument("--mixed-port", type=int, default=7897,
                   help="Clash mixed port to verify egress IP through (default 7897)")
    p.add_argument("--settle-sec", type=float, default=1.5,
                   help="seconds to wait after rotate before measuring new IP")
    p = sub.add_parser("auto", help="auto-detect controller URL and best group")

    args = ap.parse_args()
    if args.cmd == "ip":
        mp = args.mixed_port if args.mixed_port > 0 else None
        ip = public_ip(mixed_port=mp)
        print(ip or "(unknown)")
        return

    if args.cmd == "rotate-verify":
        c = ClashClient(args.api, args.secret)
        excluded = {x.strip() for x in (args.exclude or "").split(",") if x.strip()}
        mp = args.mixed_port if args.mixed_port > 0 else None
        if args.group in ("", "auto"):
            grp = auto_pick_group(c)
            if not grp:
                print("FAIL: no viable group", file=sys.stderr); sys.exit(2)
        else:
            grp = args.group
        r = rotate_with_verify(c, grp, strategy=args.strategy, excluded=excluded,
                               max_latency_ms=args.max_latency_ms, mixed_port=mp,
                               settle_sec=args.settle_sec)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        sys.exit(0 if r.get("ip_changed") else 1)

    if args.cmd == "auto":
        url = auto_detect_api()
        if not url:
            print("no Clash controller found on common ports", file=sys.stderr)
            sys.exit(2)
        c = ClashClient(url)
        grp = auto_pick_group(c)
        out = {"api": url, "group": grp, "version": None}
        try:
            out["version"] = c.version()
        except Exception:
            pass
        if grp:
            try:
                out["current_node"] = c.current(grp)
                out["node_count"] = len(c.list_nodes(grp))
            except Exception:
                pass
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    c = ClashClient(args.api, args.secret)
    if args.cmd == "ping":
        try:
            v = c.version()
            print(json.dumps(v, ensure_ascii=False, indent=2))
        except ClashError as e:
            print(f"FAIL: {e}", file=sys.stderr); sys.exit(2)
        return

    if args.cmd == "groups":
        for g in c.list_groups():
            try:
                cur = c.current(g)
                n_count = len(c.list_nodes(g))
            except Exception:
                cur, n_count = "?", "?"
            print(f"{g}  (now={cur}, nodes={n_count})")
        return

    if not args.group:
        print("--group is required for this command", file=sys.stderr); sys.exit(2)
    excluded = {x.strip() for x in (args.exclude or "").split(",") if x.strip()}

    if args.cmd == "nodes":
        for n in c.list_nodes(args.group):
            if n in excluded:
                continue
            d = c.delay(n)
            print(f"{n:40} {('%dms' % d) if d is not None else 'TIMEOUT'}")
        return

    if args.cmd == "current":
        print(c.current(args.group))
        return

    if args.cmd == "switch":
        prev = c.current(args.group)
        c.switch(args.group, args.node)
        c.close_connections()
        print(f"switched {args.group!r}: {prev!r} -> {args.node!r}")
        return

    if args.cmd == "rotate":
        r = rotate(c, args.group, strategy=args.strategy,
                   excluded=excluded, drop_conns=not args.no_drop,
                   max_latency_ms=args.max_latency_ms)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return


if __name__ == "__main__":
    main()
