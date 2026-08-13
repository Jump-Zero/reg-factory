"""Task-local state for concurrent registration workers.

Registration scripts are asyncio/thread based, so mutating ``os.environ`` for
each worker would leak proxy state across accounts.  A context variable keeps
the active network lease and fingerprint seed local to one logical task while
still propagating through ``asyncio.to_thread``.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
from typing import Iterator, Mapping


@dataclass
class WorkerContext:
    platform: str
    index: int
    slot: int
    concurrency: int
    proxy_mode: str
    proxy_candidates: tuple[str, ...] = ()
    shared_egress: bool = False
    fingerprint_seed: str = ""
    _proxy_index: int = field(default=0, init=False, repr=False)

    @property
    def worker_id(self) -> str:
        return f"{self.platform}-{self.index}"

    @property
    def proxy_url(self) -> str:
        if not self.proxy_candidates:
            return ""
        return self.proxy_candidates[self._proxy_index % len(self.proxy_candidates)]

    def rotate_proxy(self) -> tuple[str, bool]:
        """Advance this worker's private proxy lane.

        The bool reports whether the endpoint changed.  Callers must create a
        fresh browser/session after a change; an existing profile remains bound
        to its original endpoint.
        """
        before = self.proxy_url
        if len(self.proxy_candidates) > 1:
            self._proxy_index = (self._proxy_index + 1) % len(self.proxy_candidates)
        return self.proxy_url, self.proxy_url != before

    def environment_overlay(self) -> dict[str, str]:
        overlay = {
            "REG_FACTORY_WORKER_ID": self.worker_id,
            "REG_FACTORY_WORKER_INDEX": str(self.index),
            "REG_FACTORY_WORKER_SLOT": str(self.slot),
            "REG_FACTORY_FINGERPRINT_SEED": self.fingerprint_seed,
        }
        if self.proxy_mode == "residential" and self.proxy_url:
            overlay.update({
                "PROXY_MODE": "residential",
                "REG_FACTORY_PROXY_MODE": "residential",
                "REG_FACTORY_PROXY": self.proxy_url,
                "RESIDENTIAL_PROXY": self.proxy_url,
                # The lane is already allocated.  Hide the shared pool and
                # provider-wide rotate URL from task-local reads.
                "REG_FACTORY_PROXY_POOL": "",
                "RESIDENTIAL_PROXY_POOL": "",
                "REG_FACTORY_PROXY_ROTATE_URL": "",
                "RESIDENTIAL_PROXY_ROTATE_URL": "",
                "HTTP_PROXY": self.proxy_url,
                "HTTPS_PROXY": self.proxy_url,
                "http_proxy": self.proxy_url,
                "https_proxy": self.proxy_url,
                "NO_PROXY": "127.0.0.1,localhost,::1",
                "no_proxy": "127.0.0.1,localhost,::1",
            })
        return overlay

    def merged_environment(self, base: Mapping[str, str]) -> dict[str, str]:
        merged = dict(base)
        merged.update(self.environment_overlay())
        return merged


_ACTIVE_WORKER: contextvars.ContextVar[WorkerContext | None] = contextvars.ContextVar(
    "reg_factory_worker", default=None
)


def active_worker() -> WorkerContext | None:
    return _ACTIVE_WORKER.get()


def task_environment(base: Mapping[str, str]) -> Mapping[str, str]:
    worker = active_worker()
    return worker.merged_environment(base) if worker else base


@contextlib.contextmanager
def activate_worker(worker: WorkerContext) -> Iterator[WorkerContext]:
    token = _ACTIVE_WORKER.set(worker)
    try:
        yield worker
    finally:
        _ACTIVE_WORKER.reset(token)
