"""DuckDB-backed time series store for Pulse.

Single file, columnar, excellent for time-range scans. We store samples wide
as (ts, service, metric, value, status, labels_json, message). Retention is a
simple DELETE WHERE ts < now - retention_days run once per poll.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Iterable

import duckdb

from ..probes import Sample

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts         TIMESTAMP   NOT NULL,
    service    VARCHAR     NOT NULL,
    metric     VARCHAR     NOT NULL,
    value      DOUBLE      NOT NULL,
    status     VARCHAR     NOT NULL,
    labels     VARCHAR,
    message    VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_samples_ts       ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_samples_service  ON samples(service, ts);
"""


class Storage:
    def __init__(self, path: str, retention_days: int = 90) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._path = path
        self._retention_days = retention_days
        # DuckDB connections are not thread-safe — serialize access with a lock.
        self._lock = threading.Lock()
        # R657 (OpusLogic Block 219, 2026-09-06). DuckDB sizes its pool at 80 %
        # of the HOST's RAM and ignores the container's cgroup limit; on the
        # 7.9 GB VPS that was 6.1 GiB, and on 2026-07-27 an allocation inside
        # that pool failed (`Out of Memory Error … 6.1 GiB/6.1 GiB used`) and
        # took the scheduler with it. Bound the pool explicitly and let big
        # queries spill next to the database instead. The bound is passed AT
        # CONNECT, not SET afterwards: opening the file replays the WAL, and an
        # 8 MB WAL replayed unbounded peaked at 1,532 MiB — past a 1.5 GiB
        # container cap, which killed the collector three times in a minute.
        limit = os.getenv("PULSE_DUCKDB_MEMORY_LIMIT", "").strip()
        config = {"memory_limit": limit, "temp_directory": f"{path}.tmp"} if limit else {}
        self._conn = duckdb.connect(path, config=config)
        self._conn.execute(_SCHEMA)

    def write_samples(self, samples: Iterable[Sample]) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            (
                now,
                s.service,
                s.metric,
                s.value,
                s.status,
                json.dumps(s.labels) if s.labels else None,
                s.message or None,
            )
            for s in samples
        ]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)", rows
            )

    def prune(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        with self._lock:
            cur = self._conn.execute("DELETE FROM samples WHERE ts < ?", [cutoff])
            return cur.fetchall() and 0 or 0  # duckdb delete returns no rowcount reliably

    def latest_by_service(self) -> list[dict]:
        """Most recent sample per (service, metric, labels) — powers Overview tab."""
        sql = """
            SELECT service, metric, value, status, labels, message, ts
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY service, metric, COALESCE(labels, '')
                    ORDER BY ts DESC
                ) AS rn
                FROM samples
            ) WHERE rn = 1
            ORDER BY service, metric
        """
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [
            {
                "service": r[0],
                "metric": r[1],
                "value": r[2],
                "status": r[3],
                "labels": json.loads(r[4]) if r[4] else {},
                "message": r[5] or "",
                "ts": r[6].isoformat() if r[6] else None,
            }
            for r in rows
        ]

    def range(
        self,
        service: str | None,
        since: datetime,
        until: datetime | None = None,
    ) -> list[dict]:
        """Time-range query for the timeline scrubber."""
        until = until or datetime.now(timezone.utc)
        params: list = [since, until]
        where = "ts BETWEEN ? AND ?"
        if service:
            where += " AND service = ?"
            params.append(service)
        sql = f"SELECT ts, service, metric, value, status, labels FROM samples WHERE {where} ORDER BY ts"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "ts": r[0].isoformat(),
                "service": r[1],
                "metric": r[2],
                "value": r[3],
                "status": r[4],
                "labels": json.loads(r[5]) if r[5] else {},
            }
            for r in rows
        ]

    async def write_samples_async(self, samples: Iterable[Sample]) -> None:
        await asyncio.to_thread(self.write_samples, samples)
