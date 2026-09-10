"""SQLite-backed persistent shared state for crawler safeguards.

Provides domain visit timestamps, daily counters, domain exclusions/pauses,
consecutive 5xx counters, 429 backoff state, emergency stop, and active-visit
lease tracking.  All state survives process restarts.

In-process asyncio primitives (Semaphore, Lock) provide real-time concurrency
control within a single event loop.  SQLite provides persistence and
cross-process visibility (e.g., script.py → cli.py).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from safeguard_config import (
    BACKOFF_JITTER_MAX_SECONDS,
    BACKOFF_JITTER_MIN_SECONDS,
    DOMAIN_VISIT_INTERVAL_SECONDS,
    MAX_429_BACKOFF_SECONDS,
    MAX_ACTIVE_CRAWLERS_PER_DOMAIN,
    MAX_CONSECUTIVE_DOMAIN_5XX,
    MAX_DOMAIN_VISITS_PER_UTC_DAY,
    MAX_SIMULTANEOUS_CRAWLERS,
    SAFEGUARD_STATE_DB_PATH,
)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS domain_visits (
    domain TEXT NOT NULL,
    visit_started_at_utc REAL NOT NULL,
    visit_id TEXT NOT NULL,
    PRIMARY KEY (domain, visit_started_at_utc)
);

CREATE TABLE IF NOT EXISTS domain_daily_counts (
    domain TEXT NOT NULL,
    utc_date TEXT NOT NULL,
    visit_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (domain, utc_date)
);

CREATE TABLE IF NOT EXISTS domain_exclusions (
    domain TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    excluded_at_utc TEXT NOT NULL,
    evidence TEXT
);

CREATE TABLE IF NOT EXISTS domain_pauses (
    domain TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    paused_at_utc TEXT NOT NULL,
    resume_after_utc TEXT
);

CREATE TABLE IF NOT EXISTS domain_manual_review (
    domain TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    added_at_utc TEXT NOT NULL,
    evidence TEXT
);

CREATE TABLE IF NOT EXISTS domain_5xx_counters (
    domain TEXT PRIMARY KEY,
    consecutive_count INTEGER NOT NULL DEFAULT 0,
    last_status INTEGER,
    last_timestamp_utc TEXT,
    status_history TEXT
);

CREATE TABLE IF NOT EXISTS domain_backoff (
    domain TEXT PRIMARY KEY,
    retry_number INTEGER NOT NULL DEFAULT 0,
    next_allowed_utc REAL NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS emergency_stop (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    is_active INTEGER NOT NULL DEFAULT 0,
    activated_by TEXT,
    activated_at_utc TEXT,
    reason TEXT,
    cleared_by TEXT,
    cleared_at_utc TEXT,
    clear_reason TEXT
);

CREATE TABLE IF NOT EXISTS active_visits (
    visit_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    started_at_utc REAL NOT NULL,
    heartbeat_utc REAL NOT NULL
);

-- Ensure emergency_stop row exists
INSERT OR IGNORE INTO emergency_stop (id, is_active) VALUES (1, 0);
"""


class SafeguardState:
    """Persistent safeguard state backed by SQLite with in-process concurrency primitives."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path else SAFEGUARD_STATE_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

        # In-process concurrency primitives
        self._global_semaphore = asyncio.Semaphore(MAX_SIMULTANEOUS_CRAWLERS)
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._domain_locks_mutex = asyncio.Lock()
        self._db_lock = asyncio.Lock()

        # Clean up stale active visits from prior crashed processes (leases > 5 min old)
        self._cleanup_stale_visits()

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=30.0)
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA busy_timeout = 30000")
        return self._conn

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    @contextmanager
    def _cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        """Yield a cursor within a transaction with auto-reconnect on closed database."""
        conn = self._ensure_conn()
        cur = None
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        except sqlite3.ProgrammingError as pe:
            if "closed database" in str(pe).lower():
                self._conn = None
                conn = self._ensure_conn()
                cur = conn.cursor()
                yield cur
                conn.commit()
            else:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass

    def _cleanup_stale_visits(self, max_age_seconds: float = 300.0) -> None:
        """Remove active_visits entries whose heartbeat is older than max_age_seconds."""
        cutoff = time.time() - max_age_seconds
        with self._cursor() as cur:
            cur.execute("DELETE FROM active_visits WHERE heartbeat_utc < ?", (cutoff,))

    # -------------------------------------------------------------------
    # Emergency Stop
    # -------------------------------------------------------------------
    def is_emergency_stop_active(self) -> bool:
        with self._cursor() as cur:
            cur.execute("SELECT is_active FROM emergency_stop WHERE id = 1")
            row = cur.fetchone()
            return bool(row and row[0])

    def activate_emergency_stop(self, researcher: str, reason: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(
                """UPDATE emergency_stop SET
                    is_active = 1, activated_by = ?, activated_at_utc = ?, reason = ?,
                    cleared_by = NULL, cleared_at_utc = NULL, clear_reason = NULL
                WHERE id = 1""",
                (researcher, now_iso, reason),
            )

    def clear_emergency_stop(self, researcher: str, reason: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(
                """UPDATE emergency_stop SET
                    is_active = 0, cleared_by = ?, cleared_at_utc = ?, clear_reason = ?
                WHERE id = 1""",
                (researcher, now_iso, reason),
            )

    def get_emergency_stop_info(self) -> dict:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM emergency_stop WHERE id = 1")
            row = cur.fetchone()
            if not row:
                return {"is_active": False}
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    # -------------------------------------------------------------------
    # Domain Exclusion / Pause / Manual Review
    # -------------------------------------------------------------------
    def is_domain_excluded(self, domain: str) -> bool:
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM domain_exclusions WHERE domain = ?", (domain,))
            return cur.fetchone() is not None

    def exclude_domain(self, domain: str, reason: str, evidence: str = "") -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO domain_exclusions (domain, reason, excluded_at_utc, evidence) VALUES (?, ?, ?, ?)",
                (domain, reason, now_iso, evidence),
            )

    def is_domain_paused(self, domain: str) -> bool:
        with self._cursor() as cur:
            cur.execute("SELECT resume_after_utc FROM domain_pauses WHERE domain = ?", (domain,))
            row = cur.fetchone()
            if not row:
                return False
            resume = row[0]
            if resume:
                now = time.time()
                try:
                    resume_ts = float(resume)
                except (ValueError, TypeError):
                    return True
                if now >= resume_ts:
                    cur.execute("DELETE FROM domain_pauses WHERE domain = ?", (domain,))
                    return False
            return True

    def pause_domain(self, domain: str, reason: str, resume_after_utc: float | None = None) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO domain_pauses (domain, reason, paused_at_utc, resume_after_utc) VALUES (?, ?, ?, ?)",
                (domain, reason, now_iso, str(resume_after_utc) if resume_after_utc else None),
            )

    def is_domain_in_manual_review(self, domain: str) -> bool:
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM domain_manual_review WHERE domain = ?", (domain,))
            return cur.fetchone() is not None

    def add_domain_to_manual_review(self, domain: str, reason: str, evidence: str = "") -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO domain_manual_review (domain, reason, added_at_utc, evidence) VALUES (?, ?, ?, ?)",
                (domain, reason, now_iso, evidence),
            )

    def is_domain_stopped(self, domain: str) -> tuple[bool, str]:
        """Return (stopped, reason_code) for any exclusion, pause, or manual-review state."""
        if self.is_domain_excluded(domain):
            return True, "DOMAIN_EXCLUDED"
        if self.is_domain_paused(domain):
            return True, "DOMAIN_PAUSED"
        if self.is_domain_in_manual_review(domain):
            return True, "DOMAIN_MANUAL_REVIEW"
        return False, ""

    # -------------------------------------------------------------------
    # Daily Domain Counters
    # -------------------------------------------------------------------
    def get_daily_count(self, domain: str) -> int:
        utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._cursor() as cur:
            cur.execute(
                "SELECT visit_count FROM domain_daily_counts WHERE domain = ? AND utc_date = ?",
                (domain, utc_date),
            )
            row = cur.fetchone()
            return row[0] if row else 0

    def increment_daily_count(self, domain: str) -> int:
        """Atomically increment and return the new daily count."""
        utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO domain_daily_counts (domain, utc_date, visit_count)
                VALUES (?, ?, 1)
                ON CONFLICT(domain, utc_date) DO UPDATE SET visit_count = visit_count + 1""",
                (domain, utc_date),
            )
            cur.execute(
                "SELECT visit_count FROM domain_daily_counts WHERE domain = ? AND utc_date = ?",
                (domain, utc_date),
            )
            return cur.fetchone()[0]

    def is_daily_limit_reached(self, domain: str) -> bool:
        return self.get_daily_count(domain) >= MAX_DOMAIN_VISITS_PER_UTC_DAY

    # -------------------------------------------------------------------
    # Domain Visit Interval (rolling 60s)
    # -------------------------------------------------------------------
    def get_last_visit_time(self, domain: str) -> float | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT MAX(visit_started_at_utc) FROM domain_visits WHERE domain = ?",
                (domain,),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None

    def record_visit_start(self, domain: str, visit_id: str) -> None:
        now = time.time()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO domain_visits (domain, visit_started_at_utc, visit_id) VALUES (?, ?, ?)",
                (domain, now, visit_id),
            )

    def seconds_until_next_allowed(self, domain: str) -> float:
        """Return seconds to wait before the next visit is allowed, or 0.0 if allowed now."""
        last = self.get_last_visit_time(domain)
        if last is None:
            return 0.0
        elapsed = time.time() - last
        remaining = DOMAIN_VISIT_INTERVAL_SECONDS - elapsed
        return max(0.0, remaining)

    # -------------------------------------------------------------------
    # Consecutive 5xx Counter
    # -------------------------------------------------------------------
    def record_5xx(self, domain: str, status: int) -> int:
        """Record a 5xx response and return the new consecutive count."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute("SELECT consecutive_count, status_history FROM domain_5xx_counters WHERE domain = ?", (domain,))
            row = cur.fetchone()
            if row:
                count = row[0] + 1
                history = row[1] or ""
                history += f",{status}@{now_iso}"
            else:
                count = 1
                history = f"{status}@{now_iso}"
            cur.execute(
                """INSERT OR REPLACE INTO domain_5xx_counters
                (domain, consecutive_count, last_status, last_timestamp_utc, status_history)
                VALUES (?, ?, ?, ?, ?)""",
                (domain, count, status, now_iso, history),
            )
            return count

    def reset_5xx_counter(self, domain: str) -> None:
        """Reset after a successful (non-5xx) response."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM domain_5xx_counters WHERE domain = ?", (domain,))

    def get_5xx_count(self, domain: str) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT consecutive_count FROM domain_5xx_counters WHERE domain = ?", (domain,))
            row = cur.fetchone()
            return row[0] if row else 0

    def is_5xx_limit_reached(self, domain: str) -> bool:
        return self.get_5xx_count(domain) >= MAX_CONSECUTIVE_DOMAIN_5XX

    # -------------------------------------------------------------------
    # 429 Backoff State
    # -------------------------------------------------------------------
    def is_domain_in_backoff(self, domain: str) -> bool:
        with self._cursor() as cur:
            cur.execute("SELECT next_allowed_utc FROM domain_backoff WHERE domain = ?", (domain,))
            row = cur.fetchone()
            if not row:
                return False
            if time.time() >= row[0]:
                cur.execute("DELETE FROM domain_backoff WHERE domain = ?", (domain,))
                return False
            return True

    def get_backoff_remaining(self, domain: str) -> float:
        """Return seconds until backoff expires, or 0.0."""
        with self._cursor() as cur:
            cur.execute("SELECT next_allowed_utc FROM domain_backoff WHERE domain = ?", (domain,))
            row = cur.fetchone()
            if not row:
                return 0.0
            remaining = row[0] - time.time()
            return max(0.0, remaining)

    def set_backoff(self, domain: str, delay_seconds: float, retry_number: int, reason: str = "429") -> None:
        next_allowed = time.time() + delay_seconds
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO domain_backoff (domain, retry_number, next_allowed_utc, reason) VALUES (?, ?, ?, ?)",
                (domain, retry_number, next_allowed, reason),
            )

    def get_backoff_retry_number(self, domain: str) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT retry_number FROM domain_backoff WHERE domain = ?", (domain,))
            row = cur.fetchone()
            return row[0] if row else 0

    def clear_backoff(self, domain: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM domain_backoff WHERE domain = ?", (domain,))

    # -------------------------------------------------------------------
    # Active Visit Leases
    # -------------------------------------------------------------------
    def register_active_visit(self, visit_id: str, domain: str, worker_id: str) -> None:
        now = time.time()
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO active_visits (visit_id, domain, worker_id, started_at_utc, heartbeat_utc) VALUES (?, ?, ?, ?, ?)",
                (visit_id, domain, worker_id, now, now),
            )

    def heartbeat_visit(self, visit_id: str) -> None:
        now = time.time()
        with self._cursor() as cur:
            cur.execute("UPDATE active_visits SET heartbeat_utc = ? WHERE visit_id = ?", (now, visit_id))

    def release_active_visit(self, visit_id: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM active_visits WHERE visit_id = ?", (visit_id,))

    def get_active_global_count(self) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM active_visits")
            return cur.fetchone()[0]

    def get_active_domain_count(self, domain: str) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM active_visits WHERE domain = ?", (domain,))
            return cur.fetchone()[0]

    # -------------------------------------------------------------------
    # In-process concurrency helpers
    # -------------------------------------------------------------------
    async def acquire_global_slot(self) -> None:
        await self._global_semaphore.acquire()

    def release_global_slot(self) -> None:
        self._global_semaphore.release()

    async def get_domain_lock(self, domain: str) -> asyncio.Lock:
        async with self._domain_locks_mutex:
            if domain not in self._domain_locks:
                self._domain_locks[domain] = asyncio.Lock()
            return self._domain_locks[domain]

    def reset_all_state(self) -> None:
        """Clear all domain visit history, daily counts, pauses, exclusions, 5xx counters, backoffs, and active visits."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM domain_visits;")
            cur.execute("DELETE FROM domain_daily_counts;")
            cur.execute("DELETE FROM domain_exclusions;")
            cur.execute("DELETE FROM domain_pauses;")
            cur.execute("DELETE FROM domain_manual_review;")
            cur.execute("DELETE FROM domain_5xx_counters;")
            cur.execute("DELETE FROM domain_backoff;")
            cur.execute("DELETE FROM active_visits;")
            cur.execute("UPDATE emergency_stop SET is_active = 0 WHERE id = 1;")

    def reset_domain(self, domain: str) -> None:
        """Clear all safeguard limits, counts, pauses, exclusions, and backoffs for a specific domain."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM domain_visits WHERE domain = ?;", (domain,))
            cur.execute("DELETE FROM domain_daily_counts WHERE domain = ?;", (domain,))
            cur.execute("DELETE FROM domain_exclusions WHERE domain = ?;", (domain,))
            cur.execute("DELETE FROM domain_pauses WHERE domain = ?;", (domain,))
            cur.execute("DELETE FROM domain_manual_review WHERE domain = ?;", (domain,))
            cur.execute("DELETE FROM domain_5xx_counters WHERE domain = ?;", (domain,))
            cur.execute("DELETE FROM domain_backoff WHERE domain = ?;", (domain,))
            cur.execute("DELETE FROM active_visits WHERE domain = ?;", (domain,))


