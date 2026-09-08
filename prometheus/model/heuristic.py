"""Deterministic rule-based provider.

This is NOT a language model and is never presented as one. It is a transparent,
versioned rule engine that reads the same deterministic features an LLM provider
would read and emits the same validated structure.

It exists so the product's economically meaningful machinery -- evidence
validation, freeze, pricing, x402 settlement, delivery, outcome resolution and
reputation -- runs on real market data with no API key and no network dependency
beyond Binance itself. Swapping in an LLM provider changes only this box.

Its published identity is ``heuristic / prometheus-rules-1.0.0``.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from ..provenance import Status
from ..signals.schema import Claim, Direction, Invalidation, ModelSignal
from .base import ModelProvider, ModelRequest, ModelResponse, ProviderError, PROMPT_VERSION

RULES_VERSION = "prometheus-rules-1.0.0"


def _dec(v: str | None) -> Decimal | None:
    return None if v is None else Decimal(v)


class HeuristicProvider(ModelProvider):
    name = "heuristic"

    def model_version(self) -> str:
        return RULES_VERSION

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model": RULES_VERSION,
            "status": Status.VERIFIED_LOCAL.value,
            "is_llm": False,
            "note": "deterministic rule engine; runs locally with no external dependency",
        }

    def generate(self, request: ModelRequest) -> ModelResponse:
        started = time.time()
        f = request.features
        entry = Decimal(request.entry_reference)
        if entry <= 0:
            raise ProviderError("entry_reference must be positive")

        # Only cite what is genuinely available. This engine physically cannot
        # fabricate an evidence key, which is exactly why the validator is tested
        # separately with a deliberately malformed payload.
        avail = set(request.available_keys)

        def has(*keys: str) -> bool:
            return all(k in avail for k in keys)

        ema_gap = _dec(f.get("ema_gap_bps")) if has("ema_gap_bps") else None
        r15 = _dec(f.get("return_15m")) if has("return_15m") else None
        r5 = _dec(f.get("return_5m")) if has("return_5m") else None
        rsi = _dec(f.get("rsi_14")) if has("rsi_14") else None
        imbalance = _dec(f.get("book_imbalance")) if has("book_imbalance") else None
        atr_bps = _dec(f.get("atr_14_bps")) if has("atr_14_bps") else None
        rv_bps = _dec(f.get("realized_vol_30m_bps")) if has("realized_vol_30m_bps") else None
        spread_bps = _dec(f.get("spread_bps")) if has("spread_bps") else None
        mom = _dec(f.get("momentum_bps")) if has("momentum_bps") else None

        # Without trend and volatility context there is nothing defensible to say.
        if ema_gap is None or atr_bps is None or r15 is None:
            return self._stand_down(
                request, entry, started,
                reason="core trend or volatility features are unavailable in this snapshot",
                cite=[k for k in ("closed_candles", "last_price", "spread_bps") if k in avail],
            )

        # A market whose spread is wide relative to its own volatility cannot be
        # predicted profitably at this horizon; declining is the correct output.
        if spread_bps is not None and atr_bps is not None and atr_bps > 0:
            if spread_bps > atr_bps / 2:
                return self._stand_down(
                    request, entry, started,
                    reason=(
                        f"spread of {spread_bps} bps is more than half of ATR ({atr_bps} bps); "
                        "edge does not survive microstructure cost"
                    ),
                    cite=[k for k in ("spread_bps", "atr_14_bps") if k in avail],
                )

        # --- directional score, bounded contributions ------------------------
        score = Decimal(0)
        drivers: list[tuple[str, list[str], Decimal]] = []

        trend = max(Decimal("-2"), min(Decimal("2"), ema_gap / Decimal(5)))
        score += trend
        drivers.append((
            f"the 9/21 EMA gap is {ema_gap} bps, indicating "
            f"{'upward' if ema_gap > 0 else 'downward' if ema_gap < 0 else 'flat'} short-term trend",
            ["ema_gap_bps", "ema_fast", "ema_slow"], trend,
        ))

        mo = max(Decimal("-2"), min(Decimal("2"), r15 * Decimal(2000)))
        score += mo
        drivers.append((
            f"15-minute return is {r15}, so recent price action is "
            f"{'positive' if r15 > 0 else 'negative' if r15 < 0 else 'flat'}",
            ["return_15m"], mo,
        ))

        if r5 is not None:
            acc = max(Decimal("-1"), min(Decimal("1"), r5 * Decimal(3000)))
            score += acc
            drivers.append((
                f"5-minute return of {r5} shows the move is "
                f"{'still extending' if (r5 > 0) == (r15 > 0) else 'losing impulse'}",
                ["return_5m", "return_15m"], acc,
            ))

        if rsi is not None:
            # Mean reversion at the extremes, mild trend confirmation in between.
            if rsi >= Decimal("70"):
                rc = Decimal("-1.2")
                txt = f"RSI(14) at {rsi} is in overbought territory, raising reversal risk"
            elif rsi <= Decimal("30"):
                rc = Decimal("1.2")
                txt = f"RSI(14) at {rsi} is in oversold territory, favouring a bounce"
            else:
                rc = (rsi - Decimal(50)) / Decimal(50)
                txt = f"RSI(14) at {rsi} is mid-range and mildly confirms the prevailing trend"
            score += rc
            drivers.append((txt, ["rsi_14"], rc))

        if imbalance is not None:
            ic = max(Decimal("-1"), min(Decimal("1"), imbalance * Decimal("1.5")))
            score += ic
            drivers.append((
                f"order book imbalance is {imbalance} across 20 levels, showing resting "
                f"{'bid' if imbalance > 0 else 'ask'} pressure",
                ["book_imbalance"], ic,
            ))

        if mom is not None:
            mc = max(Decimal("-1"), min(Decimal("1"), mom / Decimal(20)))
            score += mc
            drivers.append((
                f"price sits {mom} bps from the 21-period mean, indicating "
                f"{'extension above' if mom > 0 else 'discount below'} fair value",
                ["momentum_bps", "sma_21"], mc,
            ))

        # --- direction & confidence ------------------------------------------
        abs_score = abs(score)
        threshold = Decimal("1.0")
        if abs_score < threshold:
            return self._stand_down(
                request, entry, started,
                reason=(
                    f"aggregate directional score of {score.quantize(Decimal('0.001'))} is inside the "
                    f"neutral band of +/-{threshold}; no side has an edge worth selling"
                ),
                cite=[k for k in ("ema_gap_bps", "return_15m", "atr_14_bps") if k in avail],
            )

        direction = Direction.LONG if score > 0 else Direction.SHORT

        # Confidence is a bounded function of score strength, damped when realised
        # volatility is high relative to the trend signal.
        confidence = min(Decimal("0.82"), Decimal("0.30") + (abs_score - threshold) * Decimal("0.09"))
        if rv_bps is not None and rv_bps > Decimal("25"):
            confidence *= Decimal("0.85")
        confidence = max(Decimal("0.05"), confidence).quantize(Decimal("0.0001"))

        # --- invalidation -----------------------------------------------------
        # Placed at 1.5x ATR from entry, so it is derived from measured volatility
        # rather than picked to look good.
        stop_bps = max(Decimal("8"), min(Decimal("600"), atr_bps * Decimal("1.5")))
        offset = entry * stop_bps / Decimal(10_000)
        inv_price = (entry - offset) if direction == Direction.LONG else (entry + offset)
        inv_price = inv_price.quantize(Decimal("0.00000001"))

        # --- claims -----------------------------------------------------------
        ranked = sorted(drivers, key=lambda d: abs(d[2]), reverse=True)
        claims: list[Claim] = []
        for text, keys, _weight in ranked[:4]:
            keys = [k for k in keys if k in avail]
            if keys:
                claims.append(Claim(statement=text, evidence_keys=keys))
        if not claims:
            raise ProviderError("no citable drivers survived availability filtering")

        risks = [
            f"Realised 30m volatility of {rv_bps} bps can overwhelm the signal within the horizon."
            if rv_bps is not None
            else "Volatility context is unavailable, so horizon risk is not fully characterised.",
            f"Quoted spread of {spread_bps} bps is a direct cost against any move."
            if spread_bps is not None
            else "Spread context is unavailable, so execution cost is not characterised.",
            "Exogenous news or a large liquidation can invalidate microstructure-driven inference.",
        ]

        thesis = (
            f"{direction.value} {request.asset} over {request.horizon} from a reference of {entry}. "
            f"Aggregate directional score {score.quantize(Decimal('0.001'))} built from trend, "
            f"momentum and order book evidence. Invalidation at {inv_price}, placed 1.5x ATR "
            f"({stop_bps.quantize(Decimal('0.01'))} bps) away so it scales with measured volatility "
            f"rather than a fixed guess."
        )

        signal = ModelSignal(
            direction=direction,
            confidence=confidence,
            thesis=thesis,
            claims=claims,
            risk_factors=risks,
            invalidation=Invalidation(
                condition=(
                    f"{request.asset} trades through {inv_price} before the {request.horizon} "
                    f"horizon expires"
                ),
                reference_price=inv_price,
                evidence_keys=[k for k in ("atr_14_bps", "atr_14") if k in avail],
            ),
        )
        return ModelResponse(
            signal=signal,
            provider=self.name,
            model_version=RULES_VERSION,
            prompt_version=PROMPT_VERSION,
            raw={
                "score": str(score),
                "drivers": [{"text": t, "keys": k, "weight": str(w)} for t, k, w in ranked],
                "stop_bps": str(stop_bps),
            },
            latency_ms=int((time.time() - started) * 1000),
        )

    def _stand_down(
        self, request: ModelRequest, entry: Decimal, started: float, *, reason: str, cite: list[str]
    ) -> ModelResponse:
        """Decline to predict. This is a first-class product outcome, not a failure."""
        keys = cite or ["closed_candles"]
        keys = [k for k in keys if k in set(request.available_keys)] or ["closed_candles"]
        signal = ModelSignal(
            direction=Direction.STAND_DOWN,
            confidence=Decimal("0.0"),
            thesis=(
                f"Standing down on {request.asset} over {request.horizon}. {reason}. "
                "Publishing a directional call here would be selling noise."
            ),
            claims=[Claim(statement=f"Conditions do not support a directional call: {reason}", evidence_keys=keys)],
            risk_factors=["Declining to predict forgoes any upside if the market does trend."],
            invalidation=Invalidation(
                condition="Not applicable: no directional exposure is expressed.",
                reference_price=entry,
                evidence_keys=[],
            ),
        )
        return ModelResponse(
            signal=signal,
            provider=self.name,
            model_version=RULES_VERSION,
            prompt_version=PROMPT_VERSION,
            raw={"stand_down_reason": reason},
            latency_ms=int((time.time() - started) * 1000),
        )
