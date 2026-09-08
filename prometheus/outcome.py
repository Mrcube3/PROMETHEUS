"""Outcome resolution.

Predictions face reality here. The resolution rule is fixed before any signal is
published and applied mechanically:

    resolution price = close of the first 1-minute Binance candle whose close time
                       is at or after the horizon expiry

The rule never looks at highs or lows and never chooses among candidates, so there
is nothing to cherry-pick after seeing the result. Entry is the frozen
``entry_reference``, recorded before the outcome was knowable.

If the price cannot be retrieved, the signal becomes UNRESOLVED. It is never guessed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .market.binance import BinanceMarketData, MarketDataUnavailable
from .provenance import now_iso

METHODOLOGY_VERSION = "outcome-1.0.0"

# A move smaller than this is treated as FLAT rather than a directional win. It is
# fixed in advance so it cannot be tuned to flatter the record.
FLAT_THRESHOLD = Decimal("0.0002")  # 2 basis points


class OutcomeResult:
    def __init__(
        self,
        *,
        resolved: bool,
        directional: str,
        entry: Decimal,
        exit_price: Decimal | None,
        raw_return: Decimal | None,
        source: str,
        provenance: dict[str, Any],
    ) -> None:
        self.resolved = resolved
        self.directional = directional
        self.entry = entry
        self.exit_price = exit_price
        self.raw_return = raw_return
        self.source = source
        self.provenance = provenance


def _iso_to_ms(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp() * 1000)


def resolve(
    market: BinanceMarketData,
    *,
    asset: str,
    direction: str,
    entry_reference: Decimal,
    matures_at: str,
) -> OutcomeResult:
    """Resolve one matured signal against Binance."""
    target_ms = _iso_to_ms(matures_at)

    try:
        found = market.price_at_or_after(asset, target_ms)
    except MarketDataUnavailable as exc:
        return OutcomeResult(
            resolved=False, directional="UNRESOLVED", entry=entry_reference, exit_price=None,
            raw_return=None, source="unavailable",
            provenance={
                "methodology_version": METHODOLOGY_VERSION,
                "reason": f"market data unavailable at resolution: {exc}",
                "target_ms": target_ms,
                "resolved_at": now_iso(),
            },
        )

    if found is None:
        return OutcomeResult(
            resolved=False, directional="UNRESOLVED", entry=entry_reference, exit_price=None,
            raw_return=None, source="unavailable",
            provenance={
                "methodology_version": METHODOLOGY_VERSION,
                "reason": "no 1m candle closing at or after the horizon expiry was returned",
                "target_ms": target_ms,
                "resolved_at": now_iso(),
            },
        )

    exit_price: Decimal = found["price"]
    if entry_reference <= 0:
        return OutcomeResult(
            resolved=False, directional="UNRESOLVED", entry=entry_reference, exit_price=exit_price,
            raw_return=None, source=found["source"],
            provenance={
                "methodology_version": METHODOLOGY_VERSION,
                "reason": "entry_reference is not positive; return is undefined",
                "resolved_at": now_iso(),
            },
        )

    # Directional return, per the published formulas.
    if direction == "LONG":
        raw = (exit_price - entry_reference) / entry_reference
    elif direction == "SHORT":
        raw = (entry_reference - exit_price) / entry_reference
    else:
        # NEUTRAL and STAND_DOWN express no directional exposure. The price move is
        # still recorded for transparency, but the signal is not scored right or
        # wrong -- it never claimed a direction.
        raw = (exit_price - entry_reference) / entry_reference

    raw = raw.quantize(Decimal("0.00000001"))

    if direction in ("NEUTRAL", "STAND_DOWN"):
        directional = "NOT_SCORED"
    elif abs(raw) < FLAT_THRESHOLD:
        directional = "FLAT"
    elif raw > 0:
        directional = "CORRECT"
    else:
        directional = "INCORRECT"

    return OutcomeResult(
        resolved=True,
        directional=directional,
        entry=entry_reference,
        exit_price=exit_price,
        raw_return=raw,
        source=found["source"],
        provenance={
            "methodology_version": METHODOLOGY_VERSION,
            "rule": found["rule"],
            "flat_threshold": str(FLAT_THRESHOLD),
            "formula": (
                "LONG: (exit - entry) / entry; SHORT: (entry - exit) / entry; "
                "NEUTRAL and STAND_DOWN are recorded but NOT_SCORED"
            ),
            "entry_reference": str(entry_reference),
            "exit_price": str(exit_price),
            "resolution_candle_open_ms": found["open_time_ms"],
            "resolution_candle_close_ms": found["close_time_ms"],
            "horizon_expiry_ms": target_ms,
            "source": found["source"],
            "host": found["host"],
            "classification": "BINANCE_REPORTED",
            "resolved_at": now_iso(),
        },
    )
