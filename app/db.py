"""
SQLite persistence for the Avalon Q Controller.

Schema:
  miners        - one row per managed Avalon Q
  pools         - pool config library, miner-scoped
  miner_pools   - assignment of pool to slot 1/2/3 on a miner
  rules         - schedule rules per miner
  miner_state   - per-miner singleton state (paused, override, schedule_tz, etc.)
  samples       - time-series of polled metrics (24h retention)
  events        - audit log

All write operations go through the DB single-writer pattern; reads can
happen concurrently. Tied to a single thread by virtue of running inside the
poll loop's executor and sync HTTP handlers.

Time-series retention: samples older than 24h are pruned on each write.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time as _time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RETENTION_SECONDS = 24 * 60 * 60           # samples
EVENT_RETENTION_DAYS = 30


# ----------------------------------------------------------------------
# data classes (lightweight; we hand-pack/unpack rows)
# ----------------------------------------------------------------------
@dataclass
class Miner:
    id: int
    name: str
    host: str
    port: int
    username: str
    password: str
    poll_seconds: int
    enabled: bool
    created_at: str

    def to_public_dict(self) -> dict:
        d = self.__dict__.copy()
        d["password"] = "***" if self.password else ""
        return d


@dataclass
class Pool:
    id: int
    miner_id: int
    name: str
    url: str
    worker: str
    worker_password: str
    notes: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# ----------------------------------------------------------------------
# DB connector
# ----------------------------------------------------------------------
class DB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            isolation_level=None,           # autocommit; we manage txn
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        cur = self._conn.execute("SELECT version FROM schema_version")
        row = cur.fetchone()
        current = row["version"] if row else 0

        if current < 1:
            self._conn.executescript(
                """
                CREATE TABLE miners (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT NOT NULL,
                    host        TEXT NOT NULL,
                    port        INTEGER NOT NULL DEFAULT 4028,
                    username    TEXT NOT NULL DEFAULT 'root',
                    password    TEXT NOT NULL DEFAULT 'admin',
                    poll_seconds INTEGER NOT NULL DEFAULT 30,
                    enabled     INTEGER NOT NULL DEFAULT 1,
                    created_at  TEXT NOT NULL,
                    UNIQUE(host, port)
                );

                CREATE TABLE pools (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    miner_id        INTEGER NOT NULL REFERENCES miners(id) ON DELETE CASCADE,
                    name            TEXT NOT NULL,
                    url             TEXT NOT NULL,
                    worker          TEXT NOT NULL,
                    worker_password TEXT NOT NULL DEFAULT 'x',
                    notes           TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX pools_miner_idx ON pools(miner_id);

                CREATE TABLE miner_state (
                    miner_id        INTEGER PRIMARY KEY REFERENCES miners(id) ON DELETE CASCADE,
                    paused          INTEGER NOT NULL DEFAULT 0,
                    override_json   TEXT,
                    schedule_json   TEXT NOT NULL,
                    last_pool_id    INTEGER REFERENCES pools(id) ON DELETE SET NULL,
                    last_status     TEXT
                );

                CREATE TABLE samples (
                    miner_id     INTEGER NOT NULL REFERENCES miners(id) ON DELETE CASCADE,
                    ts           INTEGER NOT NULL,
                    online       INTEGER NOT NULL,
                    workmode     INTEGER,
                    ths          REAL,
                    load_w       REAL,
                    temp_max     REAL,
                    temp_chassis REAL,
                    fan_pct      REAL,
                    accepted     INTEGER,
                    rejected     INTEGER,
                    hw_errors    INTEGER,
                    pool_url     TEXT,
                    PRIMARY KEY (miner_id, ts)
                );
                CREATE INDEX samples_ts_idx ON samples(ts);

                CREATE TABLE events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    miner_id    INTEGER REFERENCES miners(id) ON DELETE CASCADE,
                    ts          TEXT NOT NULL,
                    kind        TEXT NOT NULL,
                    data_json   TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX events_miner_ts_idx ON events(miner_id, id DESC);
                CREATE INDEX events_ts_idx ON events(ts);

                CREATE TABLE app_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                INSERT INTO schema_version(version) VALUES (1);
                """
            )
            log.info("Initialized DB schema v1")

    # ------------------------------------------------------------------
    # txn helper
    # ------------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # miners
    # ------------------------------------------------------------------
    def list_miners(self) -> list[Miner]:
        cur = self._conn.execute("SELECT * FROM miners ORDER BY id")
        return [_row_to_miner(r) for r in cur.fetchall()]

    def get_miner(self, miner_id: int) -> Miner | None:
        cur = self._conn.execute("SELECT * FROM miners WHERE id=?", (miner_id,))
        r = cur.fetchone()
        return _row_to_miner(r) if r else None

    def create_miner(
        self,
        name: str,
        host: str,
        port: int = 4028,
        username: str = "root",
        password: str = "admin",
        poll_seconds: int = 30,
    ) -> Miner:
        now = datetime.now(ZoneInfo("UTC")).isoformat()
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO miners(name,host,port,username,password,poll_seconds,enabled,created_at)
                   VALUES(?,?,?,?,?,?,1,?)""",
                (name, host, port, username, password, poll_seconds, now),
            )
            miner_id = cur.lastrowid
            # Default empty schedule for the new miner
            empty_sched = json.dumps({
                "timezone": "UTC",
                "enabled": True,
                "default_action": "none",
                "rules": [],
            })
            c.execute(
                """INSERT INTO miner_state(miner_id, paused, override_json, schedule_json)
                   VALUES(?, 0, NULL, ?)""",
                (miner_id, empty_sched),
            )
            self._log_event(c, miner_id, "miner_created", {"name": name, "host": host})
        return self.get_miner(miner_id)

    def update_miner(self, miner_id: int, **fields: Any) -> Miner | None:
        if not fields:
            return self.get_miner(miner_id)
        allowed = {"name", "host", "port", "username", "password", "poll_seconds", "enabled"}
        cols, vals = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                cols.append(f"{k}=?")
                if k == "enabled":
                    vals.append(1 if v else 0)
                else:
                    vals.append(v)
        if not cols:
            return self.get_miner(miner_id)
        vals.append(miner_id)
        with self.transaction() as c:
            c.execute(f"UPDATE miners SET {', '.join(cols)} WHERE id=?", vals)
            self._log_event(c, miner_id, "miner_updated", {"fields": list(fields.keys())})
        return self.get_miner(miner_id)

    def delete_miner(self, miner_id: int) -> bool:
        with self.transaction() as c:
            cur = c.execute("DELETE FROM miners WHERE id=?", (miner_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # pools
    # ------------------------------------------------------------------
    def list_pools(self, miner_id: int) -> list[Pool]:
        cur = self._conn.execute(
            "SELECT * FROM pools WHERE miner_id=? ORDER BY id", (miner_id,)
        )
        return [_row_to_pool(r) for r in cur.fetchall()]

    def get_pool(self, pool_id: int) -> Pool | None:
        cur = self._conn.execute("SELECT * FROM pools WHERE id=?", (pool_id,))
        r = cur.fetchone()
        return _row_to_pool(r) if r else None

    def create_pool(
        self,
        miner_id: int,
        name: str,
        url: str,
        worker: str,
        worker_password: str = "x",
        notes: str = "",
    ) -> Pool:
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO pools(miner_id,name,url,worker,worker_password,notes)
                   VALUES(?,?,?,?,?,?)""",
                (miner_id, name, url, worker, worker_password, notes),
            )
            pool_id = cur.lastrowid
            self._log_event(c, miner_id, "pool_created", {"id": pool_id, "name": name})
        return self.get_pool(pool_id)

    def update_pool(self, pool_id: int, **fields: Any) -> Pool | None:
        allowed = {"name", "url", "worker", "worker_password", "notes"}
        cols, vals = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                cols.append(f"{k}=?")
                vals.append(v)
        if not cols:
            return self.get_pool(pool_id)
        vals.append(pool_id)
        with self.transaction() as c:
            c.execute(f"UPDATE pools SET {', '.join(cols)} WHERE id=?", vals)
        return self.get_pool(pool_id)

    def delete_pool(self, pool_id: int) -> bool:
        with self.transaction() as c:
            cur = c.execute("DELETE FROM pools WHERE id=?", (pool_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # miner_state (paused, override, schedule, last applied pool)
    # ------------------------------------------------------------------
    def get_state(self, miner_id: int) -> dict:
        cur = self._conn.execute(
            "SELECT * FROM miner_state WHERE miner_id=?", (miner_id,)
        )
        r = cur.fetchone()
        if not r:
            return {
                "paused": False,
                "override": None,
                "schedule": {"timezone": "UTC", "enabled": True, "default_action": "none", "rules": []},
                "last_pool_id": None,
                "last_status": None,
            }
        return {
            "paused": bool(r["paused"]),
            "override": json.loads(r["override_json"]) if r["override_json"] else None,
            "schedule": json.loads(r["schedule_json"]),
            "last_pool_id": r["last_pool_id"],
            "last_status": json.loads(r["last_status"]) if r["last_status"] else None,
        }

    def set_paused(self, miner_id: int, paused: bool) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE miner_state SET paused=? WHERE miner_id=?",
                (1 if paused else 0, miner_id),
            )
            self._log_event(c, miner_id, "pause" if paused else "resume", {})

    def set_override(self, miner_id: int, override: dict | None) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE miner_state SET override_json=? WHERE miner_id=?",
                (json.dumps(override) if override else None, miner_id),
            )
            self._log_event(
                c,
                miner_id,
                "override_set" if override else "override_cleared",
                override or {},
            )

    def set_schedule(self, miner_id: int, schedule: dict) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE miner_state SET schedule_json=? WHERE miner_id=?",
                (json.dumps(schedule), miner_id),
            )
            self._log_event(
                c,
                miner_id,
                "schedule_updated",
                {"rules": len(schedule.get("rules", []))},
            )

    def set_last_pool(self, miner_id: int, pool_id: int | None) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE miner_state SET last_pool_id=? WHERE miner_id=?",
                (pool_id, miner_id),
            )

    def set_last_status(self, miner_id: int, status: dict) -> None:
        # Don't open a transaction for this - it happens every poll cycle
        # and is non-critical if it loses on race.
        self._conn.execute(
            "UPDATE miner_state SET last_status=? WHERE miner_id=?",
            (json.dumps(status), miner_id),
        )

    # ------------------------------------------------------------------
    # samples (time-series)
    # ------------------------------------------------------------------
    def insert_sample(self, miner_id: int, sample: dict) -> None:
        ts = sample.get("ts") or int(_time.time())
        self._conn.execute(
            """INSERT OR REPLACE INTO samples(
                miner_id, ts, online, workmode, ths, load_w, temp_max, temp_chassis,
                fan_pct, accepted, rejected, hw_errors, pool_url
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                miner_id,
                ts,
                1 if sample.get("online") else 0,
                sample.get("workmode"),
                sample.get("ths"),
                sample.get("load_w"),
                sample.get("temp_max"),
                sample.get("temp_chassis"),
                sample.get("fan_pct"),
                sample.get("accepted"),
                sample.get("rejected"),
                sample.get("hw_errors"),
                sample.get("pool_url"),
            ),
        )
        # Prune old samples (cheap; indexed on ts)
        cutoff = ts - RETENTION_SECONDS
        self._conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))

    def get_samples(self, miner_id: int, since_ts: int | None = None) -> list[dict]:
        if since_ts is None:
            since_ts = int(_time.time()) - RETENTION_SECONDS
        cur = self._conn.execute(
            """SELECT ts, online, workmode, ths, load_w, temp_max, temp_chassis,
                      fan_pct, accepted, rejected, hw_errors, pool_url
               FROM samples
               WHERE miner_id=? AND ts >= ?
               ORDER BY ts ASC""",
            (miner_id, since_ts),
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def _log_event(self, conn: sqlite3.Connection, miner_id: int | None, kind: str, data: dict) -> None:
        ts = datetime.now(ZoneInfo("UTC")).isoformat()
        conn.execute(
            "INSERT INTO events(miner_id, ts, kind, data_json) VALUES (?,?,?,?)",
            (miner_id, ts, kind, json.dumps(data)),
        )
        # Prune events older than retention
        cutoff = (datetime.now(ZoneInfo("UTC")) - timedelta(days=EVENT_RETENTION_DAYS)).isoformat()
        conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))

    def log_event(self, miner_id: int | None, kind: str, data: dict) -> None:
        with self.transaction() as c:
            self._log_event(c, miner_id, kind, data)

    def list_events(
        self,
        miner_id: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        if miner_id is None:
            cur = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM events WHERE miner_id=? ORDER BY id DESC LIMIT ?",
                (miner_id, limit),
            )
        return [
            {
                "id": r["id"],
                "miner_id": r["miner_id"],
                "ts": r["ts"],
                "kind": r["kind"],
                "data": json.loads(r["data_json"] or "{}"),
            }
            for r in cur.fetchall()
        ]

    # ------------------------------------------------------------------
    # app_settings (kv)
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        cur = self._conn.execute("SELECT value FROM app_settings WHERE key=?", (key,))
        r = cur.fetchone()
        if not r:
            return default
        try:
            return json.loads(r["value"])
        except json.JSONDecodeError:
            return r["value"]

    def set_setting(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO app_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


# ----------------------------------------------------------------------
# row helpers
# ----------------------------------------------------------------------
def _row_to_miner(r: sqlite3.Row) -> Miner:
    return Miner(
        id=r["id"],
        name=r["name"],
        host=r["host"],
        port=r["port"],
        username=r["username"],
        password=r["password"],
        poll_seconds=r["poll_seconds"],
        enabled=bool(r["enabled"]),
        created_at=r["created_at"],
    )


def _row_to_pool(r: sqlite3.Row) -> Pool:
    return Pool(
        id=r["id"],
        miner_id=r["miner_id"],
        name=r["name"],
        url=r["url"],
        worker=r["worker"],
        worker_password=r["worker_password"],
        notes=r["notes"] or "",
    )
