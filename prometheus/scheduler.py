"""Autonomous loop.

Two independent jobs:

  * ``generate`` -- keeps the marketplace stocked, respecting MAX_OPEN_LISTINGS.
  * ``resolve``  -- matures expired signals against Binance and updates reputation.

Resolution is idempotent per signal: the outcomes table has a UNIQUE constraint on
signal_id and an immutability trigger, so a duplicate run cannot score twice or
rewrite a score.
"""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .config import Config
from .db import Database
from .market.binance import BinanceMarketData
from .outcome import resolve as resolve_outcome
from .provenance import now_iso
from .signals.engine import GenerationError, SignalEngine, SignalRejected
from .signals.schema import SignalState


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


class Scheduler:
    def __init__(self, db: Database, cfg: Config, engine: SignalEngine, market: BinanceMarketData) -> None:
        self.db = db
        self.cfg = cfg
        self.engine = engine
        self.market = market
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._cycle = itertools.cycle(
            [(a, h) for a in cfg.assets for h in cfg.horizons]
        ) if cfg.assets and cfg.horizons else itertools.cycle([])
        self.stats: dict[str, Any] = {
            "generated": 0, "rejected": 0, "errors": 0,
            "resolved": 0, "unresolved": 0,
            "last_generate_at": None, "last_resolve_at": None, "last_error": None,
        }

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if not self.cfg.autopilot:
            return
        for name, fn, interval in (
            ("prometheus-generate", self.generate_once, self.cfg.generate_interval_s),
            ("prometheus-resolve", self.resolve_due, self.cfg.resolve_interval_s),
        ):
            t = threading.Thread(target=self._loop, args=(fn, interval), name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)

    def _loop(self, fn, interval: int) -> None:
        # Stagger startup so generation and resolution do not collide on the first tick.
        self._stop.wait(2)
        while not self._stop.is_set():
            try:
                fn()
            except Exception as exc:
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{type(exc).__name__}: {exc}"
                self.db.journal(
                    "SCHEDULER_ERROR",
                    {"error": str(exc)[:500], "traceback": traceback.format_exc()[-1500:]},
                )
            self._stop.wait(interval)

    # -- jobs ----------------------------------------------------------------
    def generate_once(self) -> dict[str, Any] | None:
        """Produce one signal, if there is room on the shelf."""
        open_count = self.db.query_one(
            "SELECT COUNT(*) AS n FROM signals WHERE state IN ('LISTED','PURCHASED') AND matures_at > ?",
            (now_iso(),),
        )["n"]
        if open_count >= self.cfg.max_open_listings:
            return None

        asset, horizon = next(self._cycle)
        self.stats["last_generate_at"] = now_iso()
        try:
            row = self.engine.generate(asset, horizon)
            self.stats["generated"] += 1
            return row
        except SignalRejected as exc:
            # A rejection is a healthy outcome, not an error. It is already recorded.
            self.stats["rejected"] += 1
            self.stats["last_error"] = f"rejected {asset}/{horizon}: {exc.code.value}"
            return None
        except GenerationError as exc:
            self.stats["errors"] += 1
            self.stats["last_error"] = str(exc)
            return None

    def resolve_due(self) -> int:
        """Resolve every signal whose horizon has expired."""
        now = now_iso()
        due = self.db.query(
            "SELECT * FROM signals"
            " WHERE matures_at <= ?"
            "   AND state IN ('LISTED','PURCHASED','DELIVERED','AWAITING_OUTCOME','FROZEN')"
            " ORDER BY matures_at LIMIT 25",
            (now,),
        )
        count = 0
        for row in due:
            if self._resolve_one(dict(row)):
                count += 1
        if due:
            self.stats["last_resolve_at"] = now_iso()
        return count

    def _resolve_one(self, row: dict[str, Any]) -> bool:
        signal_id = row["signal_id"]

        # Already scored? The unique constraint would reject a second row anyway.
        if self.db.query_one("SELECT 1 FROM outcomes WHERE signal_id = ?", (signal_id,)):
            return False

        # Walk the state machine to AWAITING_OUTCOME through legal transitions.
        state = SignalState(row["state"])
        if state == SignalState.FROZEN:
            self.engine.transition(signal_id, SignalState.EXPIRED)
            return False
        if state in (SignalState.LISTED, SignalState.PURCHASED, SignalState.DELIVERED):
            self.engine.transition(signal_id, SignalState.AWAITING_OUTCOME)

        result = resolve_outcome(
            self.market,
            asset=row["asset"],
            direction=row["direction"],
            entry_reference=Decimal(row["entry_reference"]),
            matures_at=row["matures_at"],
        )

        import json as _json
        import secrets

        if not result.resolved:
            self.stats["unresolved"] += 1
            self.engine.transition(signal_id, SignalState.UNRESOLVED)
            self.db.journal(
                "OUTCOME_UNRESOLVED", {"reason": result.provenance.get("reason")}, signal_id=signal_id
            )
            return False

        try:
            self.db.execute(
                "INSERT INTO outcomes(outcome_id, signal_id, resolved_at, entry_price, exit_price,"
                " raw_return, directional, resolution_source, methodology, provenance_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "out_" + secrets.token_hex(10), signal_id, now_iso(), str(result.entry),
                    None if result.exit_price is None else str(result.exit_price),
                    None if result.raw_return is None else str(result.raw_return),
                    result.directional, result.source,
                    result.provenance["methodology_version"],
                    _json.dumps(result.provenance, sort_keys=True, default=str),
                ),
            )
        except Exception:
            # Concurrent resolution won. Nothing to do -- and nothing was rewritten.
            return False

        self.engine.transition(signal_id, SignalState.MATURED)
        self.engine.transition(signal_id, SignalState.SCORED)
        self.engine.transition(signal_id, SignalState.VERIFIED)
        self.stats["resolved"] += 1
        self.db.journal(
            "OUTCOME_RECORDED",
            {
                "directional": result.directional,
                "raw_return": None if result.raw_return is None else str(result.raw_return),
                "entry": str(result.entry),
                "exit": None if result.exit_price is None else str(result.exit_price),
            },
            signal_id=signal_id,
        )
        return True
