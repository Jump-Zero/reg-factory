"""Concurrency planning with explicit network-isolation rules."""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from typing import Mapping

from common import direct_proxy, proxy_switch
from common.task_context import WorkerContext


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(value, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class WorkerPlan:
    platform: str
    count: int
    requested_concurrency: int
    effective_concurrency: int
    workers: tuple[WorkerContext, ...]
    isolation: str
    warnings: tuple[str, ...] = ()

    def worker(self, index: int) -> WorkerContext:
        return self.workers[index - 1]

    def log(self) -> None:
        print(
            f"  [concurrency] requested={self.requested_concurrency} "
            f"effective={self.effective_concurrency} network={self.isolation}"
        )
        for warning in self.warnings:
            print(f"  [concurrency] {warning}")


def _ordered_pool(environ: Mapping[str, str]) -> list[str]:
    pool = direct_proxy.proxy_pool(environ)
    if not pool:
        configured = direct_proxy.configured_proxy(environ=environ)
        return [configured.url] if configured else []
    active = direct_proxy.configured_proxy(environ=environ)
    urls = [item.url for item in pool]
    if active and active.url in urls:
        offset = urls.index(active.url)
        urls = urls[offset:] + urls[:offset]
    return urls


def build_worker_plan(
    platform: str,
    count: int,
    requested_concurrency: int,
    environ: Mapping[str, str] | None = None,
) -> WorkerPlan:
    """Build task contexts without changing global proxy state.

    ``clash_auto`` owns one mutable selector and therefore cannot safely serve
    independent workers.  Residential pools get one lane per concurrent slot.
    A fixed Clash node is stable and may be shared, but is reported as shared
    egress so operators do not mistake profile isolation for IP isolation.
    """
    env = dict(os.environ if environ is None else environ)
    normalized_platform = str(platform or "registration").strip().lower()
    total = max(1, int(count or 1))
    requested = max(1, min(total, int(requested_concurrency or 1)))
    maximum = _positive_int(env.get("REG_FACTORY_MAX_CONCURRENCY"), 10)
    effective = min(requested, maximum)
    mode = proxy_switch.proxy_mode(env)
    allow_shared = _truthy(env.get("REG_FACTORY_ALLOW_SHARED_EGRESS"))
    warnings: list[str] = []
    proxy_pool: list[str] = []
    isolation = "shared"

    if requested > maximum:
        warnings.append(f"并发数受 REG_FACTORY_MAX_CONCURRENCY={maximum} 限制")

    if mode == "residential":
        proxy_pool = _ordered_pool(env)
        if not proxy_pool:
            effective = 1
            warnings.append("住宅代理模式未解析到有效端点，已降为单并发")
            isolation = "missing-proxy"
        elif len(proxy_pool) >= effective:
            isolation = "dedicated-proxy"
        elif allow_shared:
            isolation = "shared-proxy"
            warnings.append(
                f"代理池仅 {len(proxy_pool)} 个端点，{effective} 个并发槽会共享出口"
            )
        else:
            effective = max(1, len(proxy_pool))
            isolation = "dedicated-proxy"
            warnings.append(
                f"代理池仅 {len(proxy_pool)} 个端点，为保持账号出口隔离，并发降为 {effective}"
            )
    elif mode == "clash_auto":
        isolation = "global-clash"
        if effective > 1:
            effective = 1
            warnings.append("Clash 自动节点是全局可变出口，为避免任务中途换 IP，并发降为 1")
    elif mode == "clash_fixed":
        isolation = "shared-fixed-clash" if effective > 1 else "fixed-clash"
        if effective > 1:
            warnings.append("浏览器 Profile 独立，但并发任务共享同一固定 Clash 公网 IP")
    else:
        isolation = "shared-direct"
        if effective > 1 and not allow_shared:
            effective = 1
            warnings.append("直连只有一个公网出口；如确认可共享，可设置 REG_FACTORY_ALLOW_SHARED_EGRESS=1")

    run_id = uuid.uuid4().hex
    workers = []
    for index in range(1, total + 1):
        slot = (index - 1) % effective
        candidates: tuple[str, ...] = ()
        shared_egress = effective > 1 and isolation.startswith("shared")
        if proxy_pool:
            if len(proxy_pool) >= effective:
                lane = proxy_pool[slot::effective]
            else:
                lane = [proxy_pool[slot % len(proxy_pool)]]
            candidates = tuple(lane)
            shared_egress = effective > len(proxy_pool)
        seed_input = f"{run_id}:{normalized_platform}:{index}:{slot}"
        seed = hashlib.sha256(seed_input.encode("ascii")).hexdigest()
        workers.append(WorkerContext(
            platform=normalized_platform,
            index=index,
            slot=slot + 1,
            concurrency=effective,
            proxy_mode=mode,
            proxy_candidates=candidates,
            shared_egress=shared_egress,
            fingerprint_seed=seed,
        ))

    return WorkerPlan(
        platform=normalized_platform,
        count=total,
        requested_concurrency=requested,
        effective_concurrency=effective,
        workers=tuple(workers),
        isolation=isolation,
        warnings=tuple(warnings),
    )
