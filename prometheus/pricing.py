"""Deterministic bounded pricing.

The model cannot set a price and cannot influence one, because nothing it emits is
an input here except `confidence`, which is bounded and capped in its contribution.

Cold start is explicit: below MIN_REPUTATION_SAMPLE resolved signals, the price is
exactly BASE_PRICE and is classified BASE_PRICE_INSUFFICIENT_HISTORY. There is no
quiet interpolation from a two-sample win rate.

Every quote records its base, each multiplier, the formula version and the bounds
that were applied, so any listed price can be recomputed from the record.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .config import Config
from .provenance import now_iso

PRICING_VERSION = "pricing-1.0.0"

CLASS_BASE_INSUFFICIENT = "BASE_PRICE_INSUFFICIENT_HISTORY"
CLASS_REPUTATION_ADJUSTED = "REPUTATION_ADJUSTED"

# Every multiplier is clamped into this band before being applied.
_MULT_MIN = Decimal("0.5")
_MULT_MAX = Decimal("2.0")


def _clamp(v: Decimal, lo: Decimal = _MULT_MIN, hi: Decimal = _MULT_MAX) -> Decimal:
    return max(lo, min(hi, v))


def quote(
    *,
    cfg: Config,
    confidence: Decimal,
    horizon: str,
    asset: str,
    freshness: str,
    reputation: dict[str, Any],
    demand: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a bounded, fully-explained price quote."""
    base = cfg.base_price
    multipliers: list[dict[str, Any]] = []
    inputs: dict[str, Any] = {
        "base_price": str(base),
        "confidence": str(confidence),
        "horizon": horizon,
        "asset": asset,
        "freshness": freshness,
        "resolved_signal_count": reputation.get("resolved", 0),
        "min_reputation_sample": cfg.min_reputation_sample,
    }

    resolved = int(reputation.get("resolved", 0) or 0)
    cold_start = resolved < cfg.min_reputation_sample

    # --- confidence: always applied, tightly bounded ------------------------
    # 0.30 confidence -> 0.90x, 0.80 confidence -> 1.15x.
    conf_mult = _clamp(Decimal("0.75") + confidence / Decimal(2), Decimal("0.75"), Decimal("1.25"))
    multipliers.append({
        "name": "confidence",
        "value": str(conf_mult),
        "formula": "clamp(0.75 + confidence / 2, 0.75, 1.25)",
    })

    # --- freshness: stale intelligence is worth less -------------------------
    fresh_mult = {
        "FRESH": Decimal("1.00"),
        "AGING": Decimal("0.92"),
        "STALE": Decimal("0.75"),
        "EXPIRED": Decimal("0.50"),
        "UNAVAILABLE": Decimal("0.50"),
    }.get(freshness, Decimal("0.75"))
    multipliers.append({
        "name": "freshness",
        "value": str(fresh_mult),
        "formula": "lookup on worst field freshness across the snapshot",
    })

    if cold_start:
        classification = CLASS_BASE_INSUFFICIENT
        # Cold start means exactly BASE_PRICE. No reputation, no demand, and the
        # confidence and freshness factors are recorded but not applied, so the
        # first prices this agent ever charges are not a function of a two-sample
        # accuracy figure.
        for m in multipliers:
            m["applied"] = False
        final = base
        note = (
            f"only {resolved} resolved signal(s); {cfg.min_reputation_sample} required before "
            "measured performance may influence price"
        )
    else:
        classification = CLASS_REPUTATION_ADJUSTED
        for m in multipliers:
            m["applied"] = True

        # --- accuracy: centred on 0.5, bounded ------------------------------
        accuracy = Decimal(str(reputation.get("directional_accuracy") or "0.5"))
        acc_mult = _clamp(Decimal("0.7") + accuracy * Decimal("0.8"))
        multipliers.append({
            "name": "directional_accuracy",
            "value": str(acc_mult),
            "formula": "clamp(0.7 + accuracy * 0.8, 0.5, 2.0)",
            "applied": True,
            "sample": resolved,
        })

        # --- cohort quality, only where the cohort itself is large enough ----
        for cohort_key, cohort_name in (("by_asset", asset), ("by_horizon", horizon)):
            cohort = (reputation.get(cohort_key) or {}).get(cohort_name) or {}
            n = int(cohort.get("resolved", 0) or 0)
            if n >= cfg.min_reputation_sample:
                cacc = Decimal(str(cohort.get("directional_accuracy") or "0.5"))
                cmult = _clamp(Decimal("0.85") + cacc * Decimal("0.3"), Decimal("0.85"), Decimal("1.15"))
                multipliers.append({
                    "name": f"cohort_{cohort_key}",
                    "value": str(cmult),
                    "formula": "clamp(0.85 + cohort_accuracy * 0.3, 0.85, 1.15)",
                    "applied": True,
                    "sample": n,
                })
            else:
                multipliers.append({
                    "name": f"cohort_{cohort_key}",
                    "value": "1.0",
                    "formula": "neutral: cohort sample below minimum",
                    "applied": True,
                    "sample": n,
                })

        # --- demand, strictly bounded ---------------------------------------
        if demand:
            sold = int(demand.get("sold_last_24h", 0) or 0)
            dmult = _clamp(Decimal(1) + Decimal(min(sold, 10)) * Decimal("0.02"),
                           Decimal("1.0"), Decimal("1.2"))
            multipliers.append({
                "name": "demand",
                "value": str(dmult),
                "formula": "clamp(1 + min(sold_last_24h, 10) * 0.02, 1.0, 1.2)",
                "applied": True,
                "sold_last_24h": sold,
            })

        final = base
        for m in multipliers:
            if m.get("applied"):
                final *= Decimal(m["value"])
        note = f"priced from {resolved} resolved signal(s)"

    # --- bounds -------------------------------------------------------------
    unbounded = final
    final = max(cfg.min_price, min(cfg.max_price, final))
    bound_hit = (
        "MIN_PRICE" if unbounded < cfg.min_price
        else "MAX_PRICE" if unbounded > cfg.max_price
        else None
    )

    # Quantise to something the settlement asset can actually represent.
    step = Decimal(1).scaleb(-min(cfg.price_decimals, 6))
    final = final.quantize(step, rounding=ROUND_HALF_UP)

    return {
        "pricing_version": PRICING_VERSION,
        "classification": classification,
        "base_price": str(base),
        "inputs": inputs,
        "multipliers": multipliers,
        "unbounded_price": str(unbounded),
        "final_price": str(final),
        "currency": cfg.price_currency,
        "min_price": str(cfg.min_price),
        "max_price": str(cfg.max_price),
        "bound_applied": bound_hit,
        "note": note,
        "quoted_at": now_iso(),
    }
