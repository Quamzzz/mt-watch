"""Postgres access. One pool, one schema, created on first use.

Two tables, and the distinction between them is the whole point of this service:

  beats    one row per (host, service). `last_beat_at` is when MT says the service last
           completed a cycle; `received_at` is when *we* heard about it. Those answer two
           different questions — "is the scanner wedged?" and "is the machine alive?" —
           and only the second one can be answered from outside the machine.

  alerts   what we have already said, so a dead box produces one message rather than one
           every time the cron service runs.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS beats (
    host            TEXT        NOT NULL,
    service         TEXT        NOT NULL,
    last_beat_at    TIMESTAMPTZ,            -- when MT says the service last completed a cycle
    received_at     TIMESTAMPTZ NOT NULL,   -- when this service received the report
    stale           BOOLEAN     NOT NULL DEFAULT FALSE,
    PRIMARY KEY (host, service)
);

CREATE TABLE IF NOT EXISTS hosts (
    host            TEXT PRIMARY KEY,
    received_at     TIMESTAMPTZ NOT NULL,
    healthy         BOOLEAN     NOT NULL,
    disk_pct        INTEGER,
    report          TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL PRIMARY KEY,
    host            TEXT        NOT NULL,
    kind            TEXT        NOT NULL,   -- silent | recovered
    sent_at         TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_host_time ON alerts(host, sent_at DESC);
"""

_pool: ConnectionPool | None = None


def now() -> datetime:
    return datetime.now(timezone.utc)


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set. On Railway, add a Postgres service and set this "
                "variable to the reference ${{Postgres.DATABASE_URL}}."
            )
        # min_size=0 so a cold start does not fail when Postgres is still waking up; the
        # traffic here is one request every fifteen minutes, so a pool of 2 is generous.
        _pool = ConnectionPool(url, min_size=0, max_size=2, kwargs={"row_factory": dict_row})
    return _pool


@contextmanager
def cursor() -> Iterator[Any]:
    with pool().connection() as conn, conn.cursor() as cur:
        yield cur


def init() -> None:
    with cursor() as cur:
        cur.execute(SCHEMA)


def record(host: str, healthy: bool, disk_pct: int | None, report: str | None,
           services: dict[str, datetime | None]) -> None:
    """Store one report from one MT box."""
    t = now()
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO hosts (host, received_at, healthy, disk_pct, report)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (host) DO UPDATE SET
                received_at = EXCLUDED.received_at,
                healthy     = EXCLUDED.healthy,
                disk_pct    = EXCLUDED.disk_pct,
                report      = EXCLUDED.report
            """,
            (host, t, healthy, disk_pct, report),
        )
        for service, last_beat in services.items():
            cur.execute(
                """
                INSERT INTO beats (host, service, last_beat_at, received_at, stale)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (host, service) DO UPDATE SET
                    last_beat_at = EXCLUDED.last_beat_at,
                    received_at  = EXCLUDED.received_at,
                    stale        = EXCLUDED.stale
                """,
                (host, service, last_beat, t, last_beat is None),
            )


def overview() -> list[dict[str, Any]]:
    """Every host we have ever heard from, with its services. Newest silence first."""
    with cursor() as cur:
        cur.execute("SELECT * FROM hosts ORDER BY received_at ASC")
        hosts = cur.fetchall()
        cur.execute("SELECT * FROM beats ORDER BY service")
        beats = cur.fetchall()
    by_host: dict[str, list[dict[str, Any]]] = {}
    for b in beats:
        by_host.setdefault(b["host"], []).append(b)
    for h in hosts:
        h["services"] = by_host.get(h["host"], [])
    return hosts


def last_alert(host: str, kind: str) -> datetime | None:
    with cursor() as cur:
        cur.execute(
            "SELECT sent_at FROM alerts WHERE host = %s AND kind = %s "
            "ORDER BY sent_at DESC LIMIT 1",
            (host, kind),
        )
        row = cur.fetchone()
    return row["sent_at"] if row else None


def note_alert(host: str, kind: str) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO alerts (host, kind, sent_at) VALUES (%s, %s, %s)",
            (host, kind, now()),
        )
