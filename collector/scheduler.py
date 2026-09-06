"""Scheduler — runs every registered probe on a fixed interval, writes samples
to storage, and hands them to the hub for WebSocket broadcast.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol

from .config import settings
from .probes import Sample
from .storage import Storage

log = logging.getLogger("pulse.scheduler")


class _ProbeModule(Protocol):
    name: str
    async def collect(self) -> list[Sample]: ...  # noqa: E704


def _load_probes() -> list[_ProbeModule]:
    mods = []
    for name in settings.enabled_probes:
        try:
            mod = importlib.import_module(f"collector.probes.{name}")
        except Exception as e:
            log.error("probe %s failed to import: %s", name, e)
            continue
        if not hasattr(mod, "collect"):
            log.warning("probe %s has no collect() — skipped", name)
            continue
        mods.append(mod)
    return mods


class Scheduler:
    def __init__(
        self,
        storage: Storage,
        broadcast: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._storage = storage
        self._broadcast = broadcast
        self._probes = _load_probes()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="pulse-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        log.info("scheduler: %d probes loaded, interval=%ss", len(self._probes), settings.poll_interval_seconds)
        tick = 0
        while not self._stopping.is_set():
            start = datetime.now(timezone.utc)
            # R657 (OpusLogic Block 219). One tick's failure must not end the
            # loop: on 2026-07-27 00:19 a DuckDB out-of-memory error escaped
            # this body, the task died with the exception never retrieved (the
            # reference in self._task keeps asyncio from logging it), and the
            # collector answered /api/health `ok` for 41 days while writing
            # nothing. Log it, name the tick, carry on.
            try:
                samples = await self._collect_all()
                await self._storage.write_samples_async(samples)
                await self._broadcast(
                    {
                        "type": "samples",
                        "ts": start.isoformat(),
                        "samples": [_serialize(s) for s in samples],
                    }
                )
                # Prune every 100 ticks (~15 min at 10s interval)
                tick += 1
                if tick % 100 == 0:
                    t0 = datetime.now(timezone.utc)
                    await asyncio.to_thread(self._storage.prune)
                    log.info("scheduler: prune done in %.1fs (tick %d)",
                             (datetime.now(timezone.utc) - t0).total_seconds(), tick)
            except Exception:
                log.exception("scheduler: tick %d failed; the loop continues", tick)

            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=settings.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _collect_all(self) -> list[Sample]:
        results = await asyncio.gather(
            *(self._safe_collect(p) for p in self._probes), return_exceptions=False
        )
        out: list[Sample] = []
        for r in results:
            out.extend(r)
        return out

    async def _safe_collect(self, probe: _ProbeModule) -> list[Sample]:
        try:
            return await probe.collect()
        except Exception as e:
            log.exception("probe %s raised", probe.name)
            return [Sample(probe.name, "probe_error", 0.0, "red", message=str(e)[:200])]


def _serialize(s: Sample) -> dict:
    d = asdict(s)
    return d
