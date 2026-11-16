"""M3 事件去重持久层。

按服务保存采信契约版本、批次导入进度与已入账事件。SQLite 写入在模块级
锁内串行提交，保证契约切换与导入并发时的版本裁决可复现。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from app.errors import DomainError
from app.reasons import reason

LEGACY = "legacy"
INSTANCE = "instance"
KINDS = (LEGACY, INSTANCE)

INTERRUPTED = "INTERRUPTED"
DONE = "DONE"


class EventStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS m3_contracts (
                    service TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    note TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (service, version)
                );
                CREATE TABLE IF NOT EXISTS m3_contract_current (
                    service TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS m3_batches (
                    batch_id TEXT PRIMARY KEY,
                    service TEXT NOT NULL,
                    contract_version INTEGER NOT NULL,
                    contract_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    accepted INTEGER NOT NULL DEFAULT 0,
                    duplicate INTEGER NOT NULL DEFAULT 0,
                    conflict INTEGER NOT NULL DEFAULT 0,
                    quarantined INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS m3_ledger (
                    service TEXT NOT NULL,
                    identity_key TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    source_line INTEGER,
                    PRIMARY KEY (service, identity_key)
                );
                CREATE TABLE IF NOT EXISTS m3_findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    source_line INTEGER,
                    reason_code TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS m3_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    accepted INTEGER NOT NULL DEFAULT 0,
                    duplicate INTEGER NOT NULL DEFAULT 0,
                    conflict INTEGER NOT NULL DEFAULT 0,
                    quarantined INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (batch_id, attempt_no)
                );
                """
            )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    # ---------- 契约 ----------
    def get_contract(self, db: sqlite3.Connection, service: str) -> dict:
        row = db.execute(
            "SELECT c.service, c.version, c.kind, c.note FROM m3_contract_current t "
            "JOIN m3_contracts c ON c.service=t.service AND c.version=t.version "
            "WHERE t.service=?",
            (service,),
        ).fetchone()
        if row is None:
            return {"service": service, "version": 0, "kind": LEGACY, "note": "默认实例内编号契约"}
        return dict(row)

    def list_contracts(self, db: sqlite3.Connection) -> list[dict]:
        rows = db.execute(
            "SELECT c.service, c.version, c.kind, c.note, c.created_at FROM m3_contract_current t "
            "JOIN m3_contracts c ON c.service=t.service AND c.version=t.version "
            "ORDER BY c.service"
        ).fetchall()
        return [dict(r) for r in rows]

    def history(self, db: sqlite3.Connection, service: str) -> list[dict]:
        rows = db.execute(
            "SELECT service, version, kind, note, created_at FROM m3_contracts "
            "WHERE service=? ORDER BY version",
            (service,),
        ).fetchall()
        return [dict(r) for r in rows]

    def switch_contract(
        self, service: str, kind: str, expected_version: int | None, note: str
    ) -> dict:
        if kind not in KINDS:
            raise DomainError("INVALID_VALUE", f"未知契约类型: {kind}")
        with self._lock, self.connect() as db:
            current = self.get_contract(db, service)
            if expected_version is not None and int(expected_version) != current["version"]:
                raise DomainError(
                    "STALE_VERSION",
                    f"{reason('STALE_VERSION')}: 期望版本 {expected_version}，当前版本 {current['version']}",
                )
            new_version = current["version"] + 1
            db.execute(
                "INSERT INTO m3_contracts(service, version, kind, note) VALUES(?,?,?,?)",
                (service, new_version, kind, note),
            )
            db.execute(
                "INSERT INTO m3_contract_current(service, version) VALUES(?,?) "
                "ON CONFLICT(service) DO UPDATE SET version=excluded.version",
                (service, new_version),
            )
            return self.get_contract(db, service)

    def rollback_contract(
        self, service: str, target_version: int | None, expected_version: int | None
    ) -> dict:
        """回退只移动当前指针并追加新版本行，从不改写历史版本与已接收批次身份。"""
        with self._lock, self.connect() as db:
            current = self.get_contract(db, service)
            if expected_version is not None and int(expected_version) != current["version"]:
                raise DomainError(
                    "STALE_VERSION",
                    f"{reason('STALE_VERSION')}: 期望版本 {expected_version}，当前版本 {current['version']}",
                )
            history = self.history(db, service)
            versions = {row["version"]: row for row in history}
            if target_version is None:
                target_version = 0
            target = versions.get(int(target_version))
            if target is None and int(target_version) != 0:
                raise DomainError("INVALID_VALUE", f"契约版本不存在: {target_version}")
            kind = target["kind"] if target else LEGACY
            note = f"回退到版本 {target_version} 的 {kind} 契约"
            new_version = current["version"] + 1
            db.execute(
                "INSERT INTO m3_contracts(service, version, kind, note) VALUES(?,?,?,?)",
                (service, new_version, kind, note),
            )
            db.execute(
                "INSERT INTO m3_contract_current(service, version) VALUES(?,?) "
                "ON CONFLICT(service) DO UPDATE SET version=excluded.version",
                (service, new_version),
            )
            return self.get_contract(db, service)

    # ---------- 批次 ----------
    def get_batch(self, db: sqlite3.Connection, batch_id: str) -> dict | None:
        row = db.execute("SELECT * FROM m3_batches WHERE batch_id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def create_batch(
        self,
        db: sqlite3.Connection,
        batch_id: str,
        service: str,
        contract: dict,
        total: int,
    ) -> dict:
        db.execute(
            "INSERT INTO m3_batches(batch_id, service, contract_version, contract_kind, status, total) "
            "VALUES(?,?,?,?,?,?)",
            (batch_id, service, contract["version"], contract["kind"], INTERRUPTED, total),
        )
        return self.get_batch(db, batch_id)

    def touch_batch(
        self,
        db: sqlite3.Connection,
        batch_id: str,
        position: int,
        status: str,
        counters: dict | None = None,
    ) -> None:
        counters = counters or {}
        db.execute(
            "UPDATE m3_batches SET position=?, status=?, "
            "accepted=COALESCE(?, accepted), duplicate=COALESCE(?, duplicate), "
            "conflict=COALESCE(?, conflict), quarantined=COALESCE(?, quarantined), "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE batch_id=?",
            (
                position,
                status,
                counters.get("accepted"),
                counters.get("duplicate"),
                counters.get("conflict"),
                counters.get("quarantined"),
                batch_id,
            ),
        )

    def start_attempt(self, db: sqlite3.Connection, batch_id: str) -> int:
        row = db.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM m3_attempts WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        attempt_no = row[0]
        db.execute(
            "INSERT INTO m3_attempts(batch_id, attempt_no) VALUES(?,?)",
            (batch_id, attempt_no),
        )
        return attempt_no

    def finish_attempt(
        self, db: sqlite3.Connection, batch_id: str, attempt_no: int, counters: dict
    ) -> None:
        db.execute(
            "UPDATE m3_attempts SET accepted=?, duplicate=?, conflict=?, quarantined=? "
            "WHERE batch_id=? AND attempt_no=?",
            (
                counters["accepted"],
                counters["duplicate"],
                counters["conflict"],
                counters["quarantined"],
                batch_id,
                attempt_no,
            ),
        )

    def get_attempt(self, db: sqlite3.Connection, batch_id: str, attempt_no: int) -> dict:
        row = db.execute(
            "SELECT * FROM m3_attempts WHERE batch_id=? AND attempt_no=?",
            (batch_id, attempt_no),
        ).fetchone()
        return dict(row) if row else {}

    def attempt_count(self, db: sqlite3.Connection, batch_id: str) -> int:
        return db.execute(
            "SELECT COUNT(*) FROM m3_attempts WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]

    def list_batches(self, service: str | None = None) -> list[dict]:
        with self._lock, self.connect() as db:
            if service:
                rows = db.execute(
                    "SELECT * FROM m3_batches WHERE service=? ORDER BY created_at", (service,)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM m3_batches ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]

    def findings(self, batch_id: str, db: sqlite3.Connection | None = None) -> list[dict]:
        def _query(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT source_line, reason_code, detail FROM m3_findings "
                "WHERE batch_id=? ORDER BY id",
                (batch_id,),
            ).fetchall()
            return [
                {
                    "source_line": r["source_line"],
                    "reason": r["reason_code"],
                    "reason_text": reason(r["reason_code"]),
                    "detail": json.loads(r["detail"]),
                }
                for r in rows
            ]
        if db is not None:
            return _query(db)
        with self._lock, self.connect() as conn:
            return _query(conn)

    def ledger_count(self, service: str | None = None) -> int:
        with self._lock, self.connect() as db:
            if service:
                return db.execute(
                    "SELECT COUNT(*) FROM m3_ledger WHERE service=?", (service,)
                ).fetchone()[0]
            return db.execute("SELECT COUNT(*) FROM m3_ledger").fetchone()[0]
