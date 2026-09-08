"""Binance spot market data client.

Only public, unauthenticated endpoints are used. Field names and array offsets in
this module were verified against live responses during discovery; see DISCOVERY.md
section 1. Nothing here is inferred from documentation alone.

No account, balance, order or withdrawal capability exists in this client. There is
no code path that can move funds, because none is implemented.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from ..provenance import (
    Classification,
    Field,
    Status,
    binance_field,
    now_iso,
    unavailable_field,
)

# Verified kline array offsets.
K_OPEN_TIME, K_OPEN, K_HIGH, K_LOW, K_CLOSE, K_VOLUME, K_CLOSE_TIME = 0, 1, 2, 3, 4, 5, 6
K_QUOTE_VOLUME, K_TRADES, K_TAKER_BASE, K_TAKER_QUOTE = 7, 8, 9, 10


class MarketDataUnavailable(RuntimeError):
    """Raised when no configured Binance host could answer."""


@dataclass
class Snapshot:
    """A provenance-tagged point-in-time view of one market."""

    asset: str
    venue: str
    retrieved_at: str
    retrieved_ms: int
    fields: dict[str, Field]
    klines: list[list[Any]]
    kline_interval: str
    host: str
    server_time_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "venue": self.venue,
            "retrieved_at": self.retrieved_at,
            "kline_interval": self.kline_interval,
            "kline_count": len(self.klines),
            "source_host": self.host,
            "server_time": None if self.server_time_ms is None else self.server_time_ms,
            "fields": {name: f.to_dict() for name, f in sorted(self.fields.items())},
            # Klines are carried as the exact strings Binance returned so the
            # snapshot hash commits to the venue's own representation.
            "klines": [[str(c) for c in k] for k in self.klines],
        }

    def get(self, name: str) -> Field:
        return self.fields.get(name) or unavailable_field(source=f"binance:{name}")

    def decimal(self, name: str) -> Decimal | None:
        f = self.fields.get(name)
        if f is None or f.value is None:
            return None
        return Decimal(str(f.value))

    @property
    def worst_freshness(self) -> str:
        order = ["FRESH", "AGING", "STALE", "EXPIRED", "UNAVAILABLE"]
        worst = "FRESH"
        for f in self.fields.values():
            if order.index(f.freshness.value) > order.index(worst):
                worst = f.freshness.value
        return worst


class BinanceMarketData:
    """Reads Binance public spot market data with host failover."""

    def __init__(self, hosts: list[str], timeout_s: float = 10.0) -> None:
        if not hosts:
            raise ValueError("at least one Binance host is required")
        self.hosts = list(hosts)
        self._client = httpx.Client(
            timeout=timeout_s, headers={"User-Agent": "PROMETHEUS/1.0 (+market-data)"}
        )
        self.last_host: str | None = None
        self.last_error: str | None = None

    def close(self) -> None:
        self._client.close()

    # -- transport -----------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, str]:
        """GET ``path`` from the first host that answers. Returns (json, host)."""
        errors: list[str] = []
        for host in self.hosts:
            url = f"{host}{path}"
            try:
                resp = self._client.get(url, params=params)
                if resp.status_code != 200:
                    errors.append(f"{host} -> HTTP {resp.status_code}: {resp.text[:160]}")
                    continue
                self.last_host = host
                self.last_error = None
                return resp.json(), host
            except Exception as exc:  # network, TLS, JSON
                errors.append(f"{host} -> {type(exc).__name__}: {exc}")
        self.last_error = " | ".join(errors)
        raise MarketDataUnavailable(f"all Binance hosts failed for {path}: {self.last_error}")

    # -- endpoints -----------------------------------------------------------
    def ping(self) -> dict[str, Any]:
        started = time.time()
        try:
            payload, host = self._get("/api/v3/time")
            return {
                "status": Status.VERIFIED_LIVE.value,
                "host": host,
                "server_time": payload.get("serverTime"),
                "latency_ms": int((time.time() - started) * 1000),
                "checked_at": now_iso(),
                "error": None,
            }
        except MarketDataUnavailable as exc:
            return {
                "status": Status.BROKEN.value,
                "host": None,
                "server_time": None,
                "latency_ms": int((time.time() - started) * 1000),
                "checked_at": now_iso(),
                "error": str(exc),
            }

    def snapshot(self, asset: str, *, venue: str, kline_interval: str = "1m", kline_limit: int = 120) -> Snapshot:
        """Build a full provenance-tagged snapshot for ``asset``.

        A partial failure degrades individual fields to UNAVAILABLE rather than
        substituting a value. Book ticker and klines are mandatory; without them
        there is nothing honest to predict from and the caller must stand down.
        """
        retrieved_ms = int(time.time() * 1000)
        fields: dict[str, Field] = {}
        server_time_ms: int | None = None

        # Book ticker (mandatory). bookTicker carries no timestamp of its own, so
        # the retrieval instant is the best-attested event time available.
        book, host = self._get("/api/v3/ticker/bookTicker", {"symbol": asset})
        book_ms = int(time.time() * 1000)
        for src_key, name in (("bidPrice", "bid"), ("askPrice", "ask"),
                              ("bidQty", "bid_qty"), ("askQty", "ask_qty")):
            raw = book.get(src_key)
            fields[name] = binance_field(
                None if raw is None else Decimal(str(raw)),
                source=f"binance:GET /api/v3/ticker/bookTicker[{src_key}]",
                event_ms=book_ms,
                retrieved_ms=retrieved_ms,
            )

        # Klines (mandatory).
        klines, _ = self._get(
            "/api/v3/klines", {"symbol": asset, "interval": kline_interval, "limit": kline_limit}
        )
        if not isinstance(klines, list) or len(klines) < 2:
            raise MarketDataUnavailable(f"insufficient klines for {asset}: got {len(klines) if isinstance(klines, list) else 'non-list'}")
        last_closed = klines[-2]  # the final element is the still-forming candle
        fields["last_close"] = binance_field(
            Decimal(str(last_closed[K_CLOSE])),
            source=f"binance:GET /api/v3/klines[{kline_interval}][-2][close]",
            event_ms=int(last_closed[K_CLOSE_TIME]),
            retrieved_ms=retrieved_ms,
        )
        fields["last_volume"] = binance_field(
            Decimal(str(last_closed[K_VOLUME])),
            source=f"binance:GET /api/v3/klines[{kline_interval}][-2][volume]",
            event_ms=int(last_closed[K_CLOSE_TIME]),
            retrieved_ms=retrieved_ms,
        )

        # Last traded price (mandatory -- this is the entry reference).
        price, _ = self._get("/api/v3/ticker/price", {"symbol": asset})
        fields["last_price"] = binance_field(
            Decimal(str(price["price"])),
            source="binance:GET /api/v3/ticker/price[price]",
            event_ms=int(time.time() * 1000),
            retrieved_ms=retrieved_ms,
        )

        # Order book depth (optional -- degrades cleanly).
        try:
            depth, _ = self._get("/api/v3/depth", {"symbol": asset, "limit": 20})
            depth_ms = int(time.time() * 1000)
            bid_vol = sum(Decimal(str(lvl[1])) for lvl in depth.get("bids", []))
            ask_vol = sum(Decimal(str(lvl[1])) for lvl in depth.get("asks", []))
            fields["depth_bid_volume"] = binance_field(
                bid_vol, source="binance:GET /api/v3/depth[bids]", event_ms=depth_ms, retrieved_ms=retrieved_ms
            )
            fields["depth_ask_volume"] = binance_field(
                ask_vol, source="binance:GET /api/v3/depth[asks]", event_ms=depth_ms, retrieved_ms=retrieved_ms
            )
            fields["depth_levels"] = binance_field(
                len(depth.get("bids", [])), source="binance:GET /api/v3/depth", event_ms=depth_ms, retrieved_ms=retrieved_ms
            )
        except MarketDataUnavailable as exc:
            for name in ("depth_bid_volume", "depth_ask_volume", "depth_levels"):
                fields[name] = unavailable_field(source="binance:GET /api/v3/depth", reason=str(exc)[:80])

        # 24h statistics (optional).
        try:
            stats, _ = self._get("/api/v3/ticker/24hr", {"symbol": asset})
            close_ms = int(stats["closeTime"])
            for src_key, name in (
                ("priceChangePercent", "change_pct_24h"),
                ("weightedAvgPrice", "vwap_24h"),
                ("highPrice", "high_24h"),
                ("lowPrice", "low_24h"),
                ("volume", "volume_24h"),
                ("quoteVolume", "quote_volume_24h"),
            ):
                raw = stats.get(src_key)
                fields[name] = binance_field(
                    None if raw is None else Decimal(str(raw)),
                    source=f"binance:GET /api/v3/ticker/24hr[{src_key}]",
                    event_ms=close_ms,
                    retrieved_ms=retrieved_ms,
                )
            fields["trade_count_24h"] = binance_field(
                int(stats["count"]),
                source="binance:GET /api/v3/ticker/24hr[count]",
                event_ms=close_ms,
                retrieved_ms=retrieved_ms,
            )
        except (MarketDataUnavailable, KeyError) as exc:
            for name in ("change_pct_24h", "vwap_24h", "high_24h", "low_24h",
                         "volume_24h", "quote_volume_24h", "trade_count_24h"):
                fields[name] = unavailable_field(source="binance:GET /api/v3/ticker/24hr", reason=str(exc)[:80])

        try:
            t, _ = self._get("/api/v3/time")
            server_time_ms = int(t["serverTime"])
        except Exception:
            server_time_ms = None

        return Snapshot(
            asset=asset,
            venue=venue,
            retrieved_at=now_iso(),
            retrieved_ms=retrieved_ms,
            fields=fields,
            klines=klines,
            kline_interval=kline_interval,
            host=host,
            server_time_ms=server_time_ms,
        )

    def price_at_or_after(self, asset: str, target_ms: int) -> dict[str, Any] | None:
        """Resolution price: the close of the first 1m candle that closes at or
        after ``target_ms``.

        This rule is fixed in advance and applied mechanically. It cannot be
        gamed after the fact because it never inspects highs or lows, and it never
        chooses among candidates -- there is exactly one qualifying candle.
        """
        # Ask for candles starting one minute before the target so the first
        # qualifying close is inside the window.
        klines, host = self._get(
            "/api/v3/klines",
            {"symbol": asset, "interval": "1m", "startTime": max(0, target_ms - 60_000), "limit": 10},
        )
        if not isinstance(klines, list):
            return None
        for k in klines:
            close_time = int(k[K_CLOSE_TIME])
            if close_time >= target_ms:
                return {
                    "price": Decimal(str(k[K_CLOSE])),
                    "close_time_ms": close_time,
                    "open_time_ms": int(k[K_OPEN_TIME]),
                    "source": "binance:GET /api/v3/klines[1m][close]",
                    "host": host,
                    "rule": "first 1m candle closing at or after horizon expiry",
                }
        return None
