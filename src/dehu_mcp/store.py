"""SQLite state: current items per source, an append-only event log, and sync runs.

Every synced object has a stable key (e.g. "dehu:notif:<sentReference>", "aeat:plazo:<acuerdo>:<n>").
`upsert` compares a content hash and returns what changed, so callers turn diffs into events.
`mark_missing` handles disappearance (e.g. a debt no longer listed = paid/cancelled).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  key TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
  data TEXT NOT NULL, hash TEXT NOT NULL,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, gone_at REAL
);
CREATE INDEX IF NOT EXISTS items_kind ON items(source, kind);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, source TEXT NOT NULL,
  type TEXT NOT NULL, key TEXT, summary TEXT NOT NULL, data TEXT, notified INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, started REAL NOT NULL,
  finished REAL, ok INTEGER, error TEXT, stats TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def _hash(d: dict) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")

    # -- items -------------------------------------------------------------
    def upsert(self, source: str, kind: str, key: str, data: dict) -> tuple[str, dict | None]:
        """Returns ("new"|"changed"|"same", previous_data)."""
        h, now = _hash(data), time.time()
        row = self.db.execute("SELECT data, hash, gone_at FROM items WHERE key=?", (key,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO items(key,source,kind,data,hash,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)",
                (key, source, kind, json.dumps(data, ensure_ascii=False), h, now, now),
            )
            return "new", None
        prev = json.loads(row["data"])
        self.db.execute(
            "UPDATE items SET data=?, hash=?, last_seen=?, gone_at=NULL WHERE key=?",
            (json.dumps(data, ensure_ascii=False), h, now, key),
        )
        return ("same" if row["hash"] == h and row["gone_at"] is None else "changed"), prev

    def mark_missing(self, source: str, kind: str, seen_keys: set[str]) -> list[dict]:
        """Items of (source, kind) not seen this run -> flagged gone; returns their last data."""
        gone = []
        for r in self.db.execute(
            "SELECT key, data FROM items WHERE source=? AND kind=? AND gone_at IS NULL", (source, kind)
        ).fetchall():
            if r["key"] not in seen_keys:
                self.db.execute("UPDATE items SET gone_at=? WHERE key=?", (time.time(), r["key"]))
                gone.append({"key": r["key"], **json.loads(r["data"])})
        return gone

    def items(self, source: str, kind: str, include_gone: bool = False) -> list[dict]:
        q = "SELECT key, data, first_seen, last_seen, gone_at FROM items WHERE source=? AND kind=?"
        if not include_gone:
            q += " AND gone_at IS NULL"
        out = []
        for r in self.db.execute(q, (source, kind)):
            d = json.loads(r["data"])
            d["_key"], d["_first_seen"], d["_gone_at"] = r["key"], r["first_seen"], r["gone_at"]
            out.append(d)
        return out

    def get(self, key: str) -> dict | None:
        r = self.db.execute("SELECT data FROM items WHERE key=?", (key,)).fetchone()
        return json.loads(r["data"]) if r else None

    # -- events ------------------------------------------------------------
    def event(self, source: str, type_: str, summary: str, key: str | None = None, data: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO events(ts,source,type,key,summary,data) VALUES(?,?,?,?,?,?)",
            (time.time(), source, type_, key, summary, json.dumps(data or {}, ensure_ascii=False)),
        )

    def events(self, source: str | None = None, since_ts: float = 0, limit: int = 200) -> list[dict]:
        q, args = "SELECT * FROM events WHERE ts>=?", [since_ts]
        if source:
            q += " AND source=?"
            args.append(source)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) | {"data": json.loads(r["data"] or "{}")} for r in self.db.execute(q, args)]

    def unnotified(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE notified=0 ORDER BY id")]

    def mark_notified(self, ids: list[int]) -> None:
        self.db.executemany("UPDATE events SET notified=1 WHERE id=?", [(i,) for i in ids])

    # -- runs / kv ---------------------------------------------------------
    def run_start(self, source: str) -> int:
        cur = self.db.execute("INSERT INTO runs(source,started) VALUES(?,?)", (source, time.time()))
        self.db.commit()
        return cur.lastrowid

    def run_end(self, run_id: int, ok: bool, error: str | None = None, stats: dict | None = None) -> None:
        self.db.execute(
            "UPDATE runs SET finished=?, ok=?, error=?, stats=? WHERE id=?",
            (time.time(), int(ok), error, json.dumps(stats or {}), run_id),
        )
        self.db.commit()

    def last_runs(self, source: str | None = None, limit: int = 10) -> list[dict]:
        q, args = "SELECT * FROM runs", []
        if source:
            q += " WHERE source=?"
            args.append(source)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(q, args)]

    def last_ok(self, source: str) -> float | None:
        r = self.db.execute("SELECT max(finished) f FROM runs WHERE source=? AND ok=1", (source,)).fetchone()
        return r["f"] if r else None

    def kv_get(self, k: str, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def kv_set(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.close()
