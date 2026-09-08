"""Deterministic feature computation.

Every number the model is allowed to reason about is computed here, in code, from
snapshot inputs. The model never calculates anything authoritative; it only reads
these outputs and cites them by key.

Each feature records the inputs it consumed, the formula, and the formula version,
so a buyer holding a passport can recompute it. Features whose inputs are missing
are emitted as UNAVAILABLE rather than defaulted -- a missing RSI is not RSI 50.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ..market.binance import K_CLOSE, K_CLOSE_TIME, K_HIGH, K_LOW, K_VOLUME, Snapshot
from ..provenance import Classification, Field, Status, estimate_field, now_iso, unavailable_field

FORMULA_VERSION = "quant-1.0.0"

# Decimal helpers -----------------------------------------------------------
ZERO = Decimal(0)


def _d(x: Any) -> Decimal:
    return Decimal(str(x))


def _safe_div(num: Decimal, den: Decimal) -> Decimal | None:
    if den == 0:
        return None
    try:
        return num / den
    except (InvalidOperation, ZeroDivisionError):
        return None


def _sqrt(x: Decimal) -> Decimal:
    if x <= 0:
        return ZERO
    return x.sqrt()


class QuantPacket:
    """A named bundle of deterministic features plus their derivations."""

    def __init__(self, asset: str, venue: str, snapshot_hash: str) -> None:
        self.asset = asset
        self.venue = venue
        self.snapshot_hash = snapshot_hash
        self.computed_at = now_iso()
        self.features: dict[str, Field] = {}
        self.derivations: dict[str, dict[str, Any]] = {}

    def add(
        self,
        name: str,
        value: Decimal | int | None,
        *,
        inputs: list[str],
        formula: str,
        basis_ms: int | None,
        retrieved_ms: int,
        unavailable_reason: str | None = None,
    ) -> None:
        if value is None:
            self.features[name] = unavailable_field(
                source=f"prometheus:quant:{name}",
                classification=Classification.PROMETHEUS_ESTIMATE,
                reason=unavailable_reason or "inputs unavailable",
            )
        else:
            self.features[name] = estimate_field(
                value, source=f"prometheus:quant:{name}", basis_ms=basis_ms, retrieved_ms=retrieved_ms
            )
        self.derivations[name] = {
            "inputs": sorted(inputs),
            "formula": formula,
            "formula_version": FORMULA_VERSION,
            "computed_at": self.computed_at,
            "available": value is not None,
        }

    # -- access --------------------------------------------------------------
    def value(self, name: str) -> Decimal | None:
        f = self.features.get(name)
        if f is None or f.value is None:
            return None
        return _d(f.value)

    def available_keys(self) -> set[str]:
        """Keys a model is permitted to cite: present AND actually computed."""
        return {k for k, f in self.features.items() if f.value is not None}

    def all_keys(self) -> set[str]:
        return set(self.features.keys())

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "venue": self.venue,
            "snapshot_hash": self.snapshot_hash,
            "formula_version": FORMULA_VERSION,
            "computed_at": self.computed_at,
            "features": {k: f.to_dict() for k, f in sorted(self.features.items())},
            "derivations": {k: v for k, v in sorted(self.derivations.items())},
        }

    def summary(self) -> dict[str, str | None]:
        """Compact key -> value view handed to the model provider."""
        out: dict[str, str | None] = {}
        for k, f in sorted(self.features.items()):
            out[k] = None if f.value is None else str(f.value)
        return out


def _closes(snapshot: Snapshot) -> list[Decimal]:
    """Closed candles only. The last kline is still forming and is excluded."""
    return [_d(k[K_CLOSE]) for k in snapshot.klines[:-1]]


def _volumes(snapshot: Snapshot) -> list[Decimal]:
    return [_d(k[K_VOLUME]) for k in snapshot.klines[:-1]]


def compute(snapshot: Snapshot, snapshot_hash: str) -> QuantPacket:
    """Compute the full deterministic feature set for ``snapshot``."""
    pkt = QuantPacket(snapshot.asset, snapshot.venue, snapshot_hash)
    r_ms = snapshot.retrieved_ms
    closes = _closes(snapshot)
    vols = _volumes(snapshot)
    basis_ms = int(snapshot.klines[-2][K_CLOSE_TIME]) if len(snapshot.klines) >= 2 else None

    bid = snapshot.decimal("bid")
    ask = snapshot.decimal("ask")
    last = snapshot.decimal("last_price")

    # --- microstructure ----------------------------------------------------
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    pkt.add("mid", mid, inputs=["snapshot.bid", "snapshot.ask"],
            formula="(bid + ask) / 2", basis_ms=r_ms, retrieved_ms=r_ms)

    spread = (ask - bid) if bid is not None and ask is not None else None
    pkt.add("spread", spread, inputs=["snapshot.bid", "snapshot.ask"],
            formula="ask - bid", basis_ms=r_ms, retrieved_ms=r_ms)

    spread_bps = None
    if spread is not None and mid is not None:
        q = _safe_div(spread * Decimal(10_000), mid)
        spread_bps = None if q is None else q.quantize(Decimal("0.0001"))
    pkt.add("spread_bps", spread_bps, inputs=["spread", "mid"],
            formula="spread / mid * 10000", basis_ms=r_ms, retrieved_ms=r_ms)

    pkt.add("last_price", last, inputs=["snapshot.last_price"],
            formula="passthrough of Binance last traded price", basis_ms=r_ms, retrieved_ms=r_ms)

    # --- order book imbalance ---------------------------------------------
    bidv = snapshot.decimal("depth_bid_volume")
    askv = snapshot.decimal("depth_ask_volume")
    imbalance = None
    if bidv is not None and askv is not None and (bidv + askv) > 0:
        imbalance = ((bidv - askv) / (bidv + askv)).quantize(Decimal("0.000001"))
    pkt.add("book_imbalance", imbalance,
            inputs=["snapshot.depth_bid_volume", "snapshot.depth_ask_volume"],
            formula="(bid_vol - ask_vol) / (bid_vol + ask_vol) over 20 levels",
            basis_ms=r_ms, retrieved_ms=r_ms,
            unavailable_reason="order book depth unavailable")

    # --- returns -----------------------------------------------------------
    def ret_over(n: int) -> Decimal | None:
        if len(closes) < n + 1 or closes[-(n + 1)] == 0:
            return None
        q = _safe_div(closes[-1] - closes[-(n + 1)], closes[-(n + 1)])
        return None if q is None else q.quantize(Decimal("0.00000001"))

    for n, name in ((1, "return_1m"), (5, "return_5m"), (15, "return_15m"),
                    (30, "return_30m"), (60, "return_60m")):
        pkt.add(name, ret_over(n), inputs=[f"klines[1m][-{n+1}..-1][close]"],
                formula=f"(close[t] - close[t-{n}]) / close[t-{n}]",
                basis_ms=basis_ms, retrieved_ms=r_ms,
                unavailable_reason=f"fewer than {n+1} closed candles")

    # --- moving averages ---------------------------------------------------
    def sma(n: int) -> Decimal | None:
        if len(closes) < n:
            return None
        return (sum(closes[-n:]) / Decimal(n)).quantize(Decimal("0.00000001"))

    for n, name in ((9, "sma_9"), (21, "sma_21"), (50, "sma_50")):
        pkt.add(name, sma(n), inputs=[f"klines[1m][-{n}..-1][close]"],
                formula=f"mean of last {n} closed 1m closes",
                basis_ms=basis_ms, retrieved_ms=r_ms,
                unavailable_reason=f"fewer than {n} closed candles")

    def ema(n: int) -> Decimal | None:
        if len(closes) < n:
            return None
        k = Decimal(2) / Decimal(n + 1)
        val = sum(closes[:n]) / Decimal(n)
        for c in closes[n:]:
            val = c * k + val * (1 - k)
        return val.quantize(Decimal("0.00000001"))

    ema_fast, ema_slow = ema(9), ema(21)
    pkt.add("ema_fast", ema_fast, inputs=["klines[1m][close]"],
            formula="EMA(9) seeded with SMA(9), k = 2/(9+1)",
            basis_ms=basis_ms, retrieved_ms=r_ms, unavailable_reason="fewer than 9 closed candles")
    pkt.add("ema_slow", ema_slow, inputs=["klines[1m][close]"],
            formula="EMA(21) seeded with SMA(21), k = 2/(21+1)",
            basis_ms=basis_ms, retrieved_ms=r_ms, unavailable_reason="fewer than 21 closed candles")

    ema_gap_bps = None
    if ema_fast is not None and ema_slow is not None and ema_slow != 0:
        q = _safe_div((ema_fast - ema_slow) * Decimal(10_000), ema_slow)
        ema_gap_bps = None if q is None else q.quantize(Decimal("0.0001"))
    pkt.add("ema_gap_bps", ema_gap_bps, inputs=["ema_fast", "ema_slow"],
            formula="(ema_fast - ema_slow) / ema_slow * 10000",
            basis_ms=basis_ms, retrieved_ms=r_ms)

    # --- realised volatility ----------------------------------------------
    def realized_vol(n: int) -> Decimal | None:
        if len(closes) < n + 1:
            return None
        rets = []
        for i in range(-n, 0):
            prev = closes[i - 1]
            if prev == 0:
                return None
            rets.append((closes[i] - prev) / prev)
        mean = sum(rets) / Decimal(len(rets))
        var = sum((r - mean) ** 2 for r in rets) / Decimal(len(rets))
        return _sqrt(var).quantize(Decimal("0.00000001"))

    rv30 = realized_vol(30)
    pkt.add("realized_vol_30m", rv30, inputs=["klines[1m][close]"],
            formula="population stdev of last 30 simple 1m returns",
            basis_ms=basis_ms, retrieved_ms=r_ms, unavailable_reason="fewer than 31 closed candles")

    rv_bps = None if rv30 is None else (rv30 * Decimal(10_000)).quantize(Decimal("0.0001"))
    pkt.add("realized_vol_30m_bps", rv_bps, inputs=["realized_vol_30m"],
            formula="realized_vol_30m * 10000", basis_ms=basis_ms, retrieved_ms=r_ms)

    # --- ATR ---------------------------------------------------------------
    def atr(n: int) -> Decimal | None:
        candles = snapshot.klines[:-1]
        if len(candles) < n + 1:
            return None
        trs = []
        for i in range(-n, 0):
            high, low = _d(candles[i][K_HIGH]), _d(candles[i][K_LOW])
            prev_close = _d(candles[i - 1][K_CLOSE])
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        return (sum(trs) / Decimal(n)).quantize(Decimal("0.00000001"))

    atr14 = atr(14)
    pkt.add("atr_14", atr14, inputs=["klines[1m][high]", "klines[1m][low]", "klines[1m][close]"],
            formula="mean true range over last 14 closed 1m candles",
            basis_ms=basis_ms, retrieved_ms=r_ms, unavailable_reason="fewer than 15 closed candles")

    atr_bps = None
    if atr14 is not None and last is not None and last != 0:
        q = _safe_div(atr14 * Decimal(10_000), last)
        atr_bps = None if q is None else q.quantize(Decimal("0.0001"))
    pkt.add("atr_14_bps", atr_bps, inputs=["atr_14", "last_price"],
            formula="atr_14 / last_price * 10000", basis_ms=basis_ms, retrieved_ms=r_ms)

    # --- RSI (Wilder) ------------------------------------------------------
    def rsi(n: int) -> Decimal | None:
        if len(closes) < n + 1:
            return None
        gains, losses = [], []
        for i in range(1, len(closes)):
            ch = closes[i] - closes[i - 1]
            gains.append(ch if ch > 0 else ZERO)
            losses.append(-ch if ch < 0 else ZERO)
        avg_gain = sum(gains[:n]) / Decimal(n)
        avg_loss = sum(losses[:n]) / Decimal(n)
        for i in range(n, len(gains)):
            avg_gain = (avg_gain * (n - 1) + gains[i]) / Decimal(n)
            avg_loss = (avg_loss * (n - 1) + losses[i]) / Decimal(n)
        if avg_loss == 0:
            return Decimal(100) if avg_gain > 0 else Decimal(50)
        rs = avg_gain / avg_loss
        return (Decimal(100) - Decimal(100) / (Decimal(1) + rs)).quantize(Decimal("0.0001"))

    pkt.add("rsi_14", rsi(14), inputs=["klines[1m][close]"],
            formula="Wilder RSI, period 14, on closed 1m closes",
            basis_ms=basis_ms, retrieved_ms=r_ms, unavailable_reason="fewer than 15 closed candles")

    # --- momentum & volume -------------------------------------------------
    momentum = None
    if last is not None:
        s21 = pkt.value("sma_21")
        if s21 is not None and s21 != 0:
            q = _safe_div((last - s21) * Decimal(10_000), s21)
            momentum = None if q is None else q.quantize(Decimal("0.0001"))
    pkt.add("momentum_bps", momentum, inputs=["last_price", "sma_21"],
            formula="(last_price - sma_21) / sma_21 * 10000", basis_ms=basis_ms, retrieved_ms=r_ms)

    vol_change = None
    if len(vols) >= 30:
        recent = sum(vols[-5:]) / Decimal(5)
        baseline = sum(vols[-30:]) / Decimal(30)
        if baseline > 0:
            q = _safe_div(recent - baseline, baseline)
            vol_change = None if q is None else q.quantize(Decimal("0.000001"))
    pkt.add("volume_change_ratio", vol_change, inputs=["klines[1m][volume]"],
            formula="(mean vol last 5 - mean vol last 30) / mean vol last 30",
            basis_ms=basis_ms, retrieved_ms=r_ms, unavailable_reason="fewer than 30 closed candles")

    # --- 24h context -------------------------------------------------------
    pkt.add("change_pct_24h", snapshot.decimal("change_pct_24h"),
            inputs=["snapshot.change_pct_24h"],
            formula="passthrough of Binance 24h priceChangePercent",
            basis_ms=r_ms, retrieved_ms=r_ms, unavailable_reason="24h ticker unavailable")

    range_pos = None
    hi, lo = snapshot.decimal("high_24h"), snapshot.decimal("low_24h")
    if hi is not None and lo is not None and last is not None and hi > lo:
        range_pos = ((last - lo) / (hi - lo)).quantize(Decimal("0.000001"))
    pkt.add("range_position_24h", range_pos,
            inputs=["snapshot.high_24h", "snapshot.low_24h", "last_price"],
            formula="(last - low_24h) / (high_24h - low_24h)",
            basis_ms=r_ms, retrieved_ms=r_ms, unavailable_reason="24h high/low unavailable")

    pkt.add("closed_candles", Decimal(len(closes)), inputs=["klines"],
            formula="count of closed 1m candles in snapshot", basis_ms=basis_ms, retrieved_ms=r_ms)

    return pkt
