"""SQLite persistence.

Two invariants are enforced here rather than by convention:

1. **Published signals are immutable.** ``signals`` rows carrying a state at or past
   FROZEN are protected by an UPDATE trigger that rejects any change to the frozen
   payload or its hashes. Outcomes, payments and deliveries are separate tables.
2. **Idempotency is a table, not a hope.** ``idempotency`` stores the response for a
   (scope, key) pair so a retry replays the first answer instead of acting twice.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Iterable

from .provenance import now_iso

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The frozen prediction. `frozen_json` is the exact canonical artifact that was
-- hashed; it is never rewritten after freeze.
CREATE TABLE IF NOT EXISTS signals (
    signal_id       TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    listed_at       TEXT,
    asset           TEXT NOT NULL,
    venue           TEXT NOT NULL,
    horizon         TEXT NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    matures_at      TEXT NOT NULL,
    state           TEXT NOT NULL,
    direction       TEXT NOT NULL,
    confidence      TEXT NOT NULL,
    model_provider  TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    entry_reference TEXT NOT NULL,
    price           TEXT,
    currency        TEXT,
    pricing_version TEXT,
    environment     TEXT NOT NULL,
    snapshot_hash   TEXT NOT NULL,
    quant_hash      TEXT NOT NULL,
    signal_hash     TEXT NOT NULL,
    frozen_json     TEXT NOT NULL,
    snapshot_json   TEXT NOT NULL,
    quant_json      TEXT NOT NULL,
    pricing_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_state ON signals(state);
CREATE INDEX IF NOT EXISTS idx_signals_matures ON signals(matures_at);
CREATE INDEX IF NOT EXISTS idx_signals_asset ON signals(asset);

-- Rejected drafts are kept: a marketplace that hides its rejects is not auditable.
CREATE TABLE IF NOT EXISTS rejections (
    rejection_id   TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    asset          TEXT NOT NULL,
    horizon        TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    reason_code    TEXT NOT NULL,
    detail         TEXT NOT NULL,
    raw_json       TEXT
);

CREATE TABLE IF NOT EXISTS purchases (
    purchase_id       TEXT PRIMARY KEY,
    signal_id         TEXT NOT NULL REFERENCES signals(signal_id),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    state             TEXT NOT NULL,
    amount            TEXT NOT NULL,
    currency          TEXT NOT NULL,
    atomic_amount     TEXT NOT NULL,
    network           TEXT NOT NULL,
    scheme            TEXT NOT NULL,
    asset_address     TEXT NOT NULL,
    pay_to            TEXT NOT NULL,
    nonce             TEXT,
    payer             TEXT,
    tx_hash           TEXT,
    verifier          TEXT,
    verification      TEXT NOT NULL DEFAULT 'UNVERIFIED',
    settlement_status TEXT,
    environment       TEXT NOT NULL,
    requirements_json TEXT NOT NULL,
    payment_json      TEXT,
    evidence_json     TEXT,
    expires_at        TEXT NOT NULL,
    verified_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_purchases_signal ON purchases(signal_id);
CREATE INDEX IF NOT EXISTS idx_purchases_state ON purchases(state);
-- One settled payment per authorization nonce: replay of a signed authorization
-- cannot buy a second artifact.
CREATE UNIQUE INDEX IF NOT EXISTS idx_purchases_nonce ON purchases(nonce) WHERE nonce IS NOT NULL;

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id   TEXT PRIMARY KEY,
    purchase_id   TEXT NOT NULL UNIQUE REFERENCES purchases(purchase_id),
    signal_id     TEXT NOT NULL REFERENCES signals(signal_id),
    delivered_at  TEXT NOT NULL,
    signal_hash   TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    receipt_hash  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id       TEXT PRIMARY KEY,
    signal_id        TEXT NOT NULL UNIQUE REFERENCES signals(signal_id),
    resolved_at      TEXT NOT NULL,
    entry_price      TEXT NOT NULL,
    exit_price       TEXT,
    raw_return       TEXT,
    directional      TEXT NOT NULL,
    resolution_source TEXT NOT NULL,
    methodology      TEXT NOT NULL,
    provenance_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    entry_id    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    signal_id   TEXT,
    purchase_id TEXT,
    amount      TEXT NOT NULL,
    currency    TEXT NOT NULL,
    environment TEXT NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event      TEXT NOT NULL,
    signal_id  TEXT,
    purchase_id TEXT,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_signal ON journal(signal_id);

CREATE TABLE IF NOT EXISTS idempotency (
    scope       TEXT NOT NULL,
    key         TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    response    TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

-- Immutability guard. Any attempt to rewrite the frozen artifact or its hashes
-- after freeze aborts the transaction.
CREATE TRIGGER IF NOT EXISTS signals_frozen_immutable
BEFORE UPDATE ON signals
FOR EACH ROW
WHEN OLD.state NOT IN ('DRAFT', 'VALIDATING')
  AND (
      NEW.frozen_json   IS NOT OLD.frozen_json
   OR NEW.signal_hash   IS NOT OLD.signal_hash
   OR NEW.snapshot_hash IS NOT OLD.snapshot_hash
   OR NEW.quant_hash    IS NOT OLD.quant_hash
   OR NEW.direction     IS NOT OLD.direction
   OR NEW.confidence    IS NOT OLD.confidence
   OR NEW.entry_reference IS NOT OLD.entry_reference
   OR NEW.matures_at    IS NOT OLD.matures_at
  )
BEGIN
    SELECT RAISE(ABORT, 'frozen signal is immutable');
END;

-- History is append-only.
CREATE TRIGGER IF NOT EXISTS journal_append_only
BEFORE UPDATE ON journal
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only');
END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete
BEFORE DELETE ON journal
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only');
END;
CREATE TRIGGER IF NOT EXISTS outcomes_immutable
BEFORE UPDATE ON outcomes
BEGIN
    SELECT RAISE(ABORT, 'outcomes are immutable');
END;
"""


class Database:
    """Thread-safe SQLite wrapper.

    A single connection guarded by a re-entrant lock. The workload is a handful of
    writes per minute, so contention is irrelevant and this removes a whole class
    of cross-connection WAL visibility bugs.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # -- primitives ----------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def transaction(self):
        """Context manager giving an exclusive write transaction."""
        return _Transaction(self)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- journal -------------------------------------------------------------
    def journal(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        signal_id: str | None = None,
        purchase_id: str | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO journal(created_at, event, signal_id, purchase_id, payload)"
            " VALUES (?, ?, ?, ?, ?)",
            (now_iso(), event, signal_id, purchase_id, json.dumps(payload, sort_keys=True)),
        )

    # -- idempotency ---------------------------------------------------------
    def idempotent_get(self, scope: str, key: str) -> dict[str, Any] | None:
        row = self.query_one(
            "SELECT response FROM idempotency WHERE scope = ? AND key = ?", (scope, key)
        )
        return None if row is None else json.loads(row["response"])

    def idempotent_put(self, scope: str, key: str, response: dict[str, Any]) -> dict[str, Any]:
        """Store ``response`` for (scope, key); if one already exists, return that.

        The INSERT is the concurrency primitive -- whoever wins the unique index
        defines the answer everybody else replays.
        """
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO idempotency(scope, key, created_at, response) VALUES (?, ?, ?, ?)",
                    (scope, key, now_iso(), json.dumps(response, sort_keys=True)),
                )
                return response
            except sqlite3.IntegrityError:
                existing = self.idempotent_get(scope, key)
                return existing if existing is not None else response


class _Transaction:
    def __init__(self, db: Database) -> None:
        self._db = db

    def __enter__(self) -> Database:
        self._db._lock.acquire()
        self._db._conn.execute("BEGIN IMMEDIATE")
        return self._db

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._db._conn.execute("COMMIT")
            else:
                self._db._conn.execute("ROLLBACK")
        finally:
            self._db._lock.release()
        return False


_db: Database | None = None


def get_db(path: str | None = None) -> Database:
    global _db
    if _db is None:
        from .config import get_config

        _db = Database(path or get_config().db_path)
    return _db


def set_db(db: Database | None) -> None:
    """Test hook."""
    global _db
    _db = db
