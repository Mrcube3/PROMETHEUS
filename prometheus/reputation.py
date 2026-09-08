"""Reputation from resolved outcomes only.

Nothing here reads a prediction's confidence to decide whether it was right. The
only inputs are outcomes the outcome engine actually resolved against Binance data.

Sample size is reported everywhere and never hidden. Below the configured minimum,
the headline figure is the literal string INSUFFICIENT_SAMPLE rather than a
percentage computed from three data points.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .config import Config
from .db import Database
from .provenance import now_iso

REPUTATION_VERSION = "reputation-1.1.0"
INSUFFICIENT = "INSUFFICIENT_SAMPLE"

# A sample can clear the size threshold and still be worthless as evidence of
# skill. If nearly every scored call points the same way, the record measures the
# market's trend during the window, not the agent's judgement. That caveat travels
# with the accuracy figure rather than being left for the reader to notice.
CONCENTRATION_LIMIT = Decimal("0.80")
CAVEAT_SINGLE_REGIME = "SINGLE_DIRECTION_SAMPLE"
CAVEAT_SMALL_SAMPLE = "INSUFFICIENT_SAMPLE"
CAVEAT_NARROW_ASSETS = "SINGLE_ASSET_SAMPLE"

# Only directional predictions are scored for accuracy. Standing down is tracked
# separately -- it is a decision, not a wrong answer.
_SCORED_DIRECTIONS = ("LONG", "SHORT")


def _acc(correct: int, resolved: int) -> Decimal | None:
    if resolved <= 0:
        return None
    return (Decimal(correct) / Decimal(resolved)).quantize(Decimal("0.0001"))


def _cohort(rows: list[dict[str, Any]], min_sample: int) -> dict[str, Any]:
    resolved = len(rows)
    correct = sum(1 for r in rows if r["directional"] == "CORRECT")
    incorrect = sum(1 for r in rows if r["directional"] == "INCORRECT")
    flat = sum(1 for r in rows if r["directional"] == "FLAT")
    returns = [Decimal(r["raw_return"]) for r in rows if r["raw_return"] is not None]
    scored = correct + incorrect
    accuracy = _acc(correct, scored)
    return {
        "resolved": resolved,
        "correct": correct,
        "incorrect": incorrect,
        "flat": flat,
        "n": resolved,
        "sample_sufficient": resolved >= min_sample,
        "directional_accuracy": None if accuracy is None else str(accuracy),
        "directional_accuracy_display": (
            INSUFFICIENT if resolved < min_sample or accuracy is None
            else f"{accuracy * 100:.2f}%"
        ),
        "mean_return": (
            None if not returns
            else str((sum(returns) / Decimal(len(returns))).quantize(Decimal("0.00000001")))
        ),
        "cumulative_return": (
            None if not returns else str(sum(returns).quantize(Decimal("0.00000001")))
        ),
    }


def compute(db: Database, cfg: Config) -> dict[str, Any]:
    """Full reputation report."""
    min_sample = cfg.min_reputation_sample

    published = db.query_one(
        "SELECT COUNT(*) AS n FROM signals WHERE state NOT IN ('DRAFT','VALIDATING','REJECTED')"
    )["n"]
    rejected = db.query_one("SELECT COUNT(*) AS n FROM rejections")["n"]
    unresolved = db.query_one("SELECT COUNT(*) AS n FROM signals WHERE state = 'UNRESOLVED'")["n"]
    awaiting = db.query_one(
        "SELECT COUNT(*) AS n FROM signals WHERE state IN ('LISTED','PURCHASED','DELIVERED','AWAITING_OUTCOME','FROZEN')"
    )["n"]
    stood_down = db.query_one(
        "SELECT COUNT(*) AS n FROM signals WHERE direction IN ('STAND_DOWN','NEUTRAL')"
    )["n"]

    rows = [
        dict(r)
        for r in db.query(
            "SELECT o.directional, o.raw_return, s.asset, s.horizon, s.model_version, s.direction, s.confidence"
            "  FROM outcomes o JOIN signals s ON s.signal_id = o.signal_id"
        )
    ]
    scored_rows = [r for r in rows if r["direction"] in _SCORED_DIRECTIONS]

    overall = _cohort(scored_rows, min_sample)

    # --- sample composition -------------------------------------------------
    dir_mix: dict[str, int] = {}
    asset_mix: dict[str, int] = {}
    for r in scored_rows:
        dir_mix[r["direction"]] = dir_mix.get(r["direction"], 0) + 1
        asset_mix[r["asset"]] = asset_mix.get(r["asset"], 0) + 1

    total_scored = len(scored_rows)
    caveats: list[dict[str, Any]] = []
    concentration = None
    if total_scored:
        top_dir, top_n = max(dir_mix.items(), key=lambda kv: kv[1])
        concentration = (Decimal(top_n) / Decimal(total_scored)).quantize(Decimal("0.0001"))
        if concentration >= CONCENTRATION_LIMIT:
            caveats.append({
                "code": CAVEAT_SINGLE_REGIME,
                "detail": (
                    f"{top_n} of {total_scored} scored signals were {top_dir}. An accuracy "
                    "figure drawn almost entirely from one direction measures the market's "
                    "trend over this window, not the agent's judgement."
                ),
            })
        top_asset, top_an = max(asset_mix.items(), key=lambda kv: kv[1])
        if len(asset_mix) == 1 and total_scored >= 5:
            caveats.append({
                "code": CAVEAT_NARROW_ASSETS,
                "detail": f"every scored signal was on {top_asset}; the record is single-asset.",
            })
    if total_scored < min_sample:
        caveats.append({
            "code": CAVEAT_SMALL_SAMPLE,
            "detail": f"n={total_scored}, below the configured minimum of {min_sample}.",
        })

    # The headline is withheld whenever any caveat would make it misleading, not
    # only when the sample is small.
    trustworthy = not caveats
    if not trustworthy:
        overall["directional_accuracy_display"] = INSUFFICIENT if total_scored < min_sample else (
            f"{overall['directional_accuracy_display']} (qualified)"
            if overall["directional_accuracy"] else INSUFFICIENT
        )

    def group(key: str) -> dict[str, Any]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for r in scored_rows:
            buckets.setdefault(r[key], []).append(r)
        return {k: _cohort(v, min_sample) for k, v in sorted(buckets.items())}

    # --- economics ----------------------------------------------------------
    # A signature-only authorization is deliberately not revenue: it proves a
    # payer signed the invoice, but no funds moved. Count it separately so demand
    # evidence is visible without turning simulation into settlement.
    rev = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(CAST(amount AS REAL)), 0) AS total, currency"
        "  FROM purchases WHERE state = 'PAID' AND environment <> 'SIMULATION'"
    )
    authorized = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(CAST(amount AS REAL)), 0) AS total"
        "  FROM purchases WHERE state = 'PAID' AND environment = 'SIMULATION'"
    )
    buyers = db.query(
        "SELECT payer, COUNT(*) AS n FROM purchases WHERE state = 'PAID'"
        " AND environment <> 'SIMULATION' AND payer IS NOT NULL GROUP BY payer"
    )
    simulated_buyers = db.query(
        "SELECT payer, COUNT(*) AS n FROM purchases WHERE state = 'PAID'"
        " AND environment = 'SIMULATION' AND payer IS NOT NULL GROUP BY payer"
    )
    repeat = sum(1 for b in buyers if b["n"] > 1)

    return {
        "reputation_version": REPUTATION_VERSION,
        "generated_at": now_iso(),
        "min_reputation_sample": min_sample,
        "published_signals": published,
        "rejected_drafts": rejected,
        "awaiting_outcome": awaiting,
        "unresolved": unresolved,
        "stood_down": stood_down,
        "scored_signals": len(scored_rows),
        **overall,
        "sample_trustworthy": trustworthy,
        "sample_caveats": caveats,
        "direction_mix": dict(sorted(dir_mix.items())),
        "asset_mix": dict(sorted(asset_mix.items())),
        "direction_concentration": None if concentration is None else str(concentration),
        "by_asset": group("asset"),
        "by_horizon": group("horizon"),
        "by_model_version": group("model_version"),
        "calibration": calibration(scored_rows, min_sample),
        "economics": {
            "paid_purchases": rev["n"] or 0,
            "gross_revenue": f"{rev['total']:.6f}" if rev["n"] else "0.000000",
            "currency": rev["currency"] or cfg.price_currency,
            "unique_buyers": len(buyers),
            "repeat_buyers": repeat,
            "simulated_authorizations": authorized["n"] or 0,
            "simulated_authorization_value": f"{authorized['total']:.6f}",
            "simulated_buyers": len(simulated_buyers),
            "revenue_note": "SIMULATION authorizations are excluded because no funds moved",
        },
    }


def calibration(rows: list[dict[str, Any]], min_sample: int) -> dict[str, Any]:
    """Confidence calibration in fixed decile buckets.

    Buckets whose sample is too small report INSUFFICIENT_SAMPLE. A calibration
    curve drawn from one observation per bucket is worse than no curve, because it
    looks like evidence.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        c = Decimal(r["confidence"])
        lo = (int(c * 10) if c < 1 else 9) / 10
        buckets.setdefault(f"{lo:.1f}-{lo + 0.1:.1f}", []).append(r)

    out = []
    for label in sorted(buckets):
        b = buckets[label]
        correct = sum(1 for r in b if r["directional"] == "CORRECT")
        incorrect = sum(1 for r in b if r["directional"] == "INCORRECT")
        scored = correct + incorrect
        mean_conf = sum(Decimal(r["confidence"]) for r in b) / Decimal(len(b))
        observed = _acc(correct, scored)
        sufficient = len(b) >= min_sample
        out.append({
            "bucket": label,
            "n": len(b),
            "sample_sufficient": sufficient,
            "mean_confidence": str(mean_conf.quantize(Decimal("0.0001"))),
            "observed_accuracy": None if observed is None else str(observed),
            "display": (
                INSUFFICIENT if not sufficient or observed is None
                else f"stated {mean_conf * 100:.1f}% vs observed {observed * 100:.1f}%"
            ),
        })

    total = sum(b["n"] for b in out)
    return {
        "buckets": out,
        "total_scored": total,
        "sample_sufficient": total >= min_sample,
        "brier_score": _brier(rows) if total >= min_sample else None,
        "note": (
            INSUFFICIENT
            if total < min_sample
            else "calibration computed over resolved directional signals only"
        ),
    }


def _brier(rows: list[dict[str, Any]]) -> str | None:
    """Brier score over directional signals, treating CORRECT as outcome 1."""
    scored = [r for r in rows if r["directional"] in ("CORRECT", "INCORRECT")]
    if not scored:
        return None
    total = sum(
        (Decimal(r["confidence"]) - (Decimal(1) if r["directional"] == "CORRECT" else Decimal(0))) ** 2
        for r in scored
    )
    return str((total / Decimal(len(scored))).quantize(Decimal("0.000001")))
