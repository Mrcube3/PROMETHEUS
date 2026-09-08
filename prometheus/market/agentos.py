"""Binance Agent OS integration via the official `binance-cli`.

This module wraps the Binance Agent OS command-line surface described by the
official Binance Skills Hub skill (`skills/binance/binance/SKILL.md`, version
2.0.0). It is used for exactly one purpose: **independent corroboration of market
data**.

Why corroboration rather than a second data feed
------------------------------------------------
PROMETHEUS sells frozen predictions and stakes its reputation on the market
snapshot it hashed. A snapshot is a factual claim about the world. If the only
witness to that claim is a single HTTP client, a bad response, a stale cache or a
hijacked host silently becomes "evidence" that gets sealed into a signal hash and
sold.

So a second, independent path to Binance -- the official Agent OS CLI, a separate
binary speaking its own transport -- is asked the same question. If the two
disagree beyond tolerance, the snapshot is **disputed** and no signal is published
from it. Disagreement is not averaged away and not silently preferred one way.

Deliberate limits
-----------------
* **Read-only.** Only unauthenticated Market endpoints are called. No account
  command, no trade command, no wallet command is invoked anywhere in this file.
* **No credentials.** This module never reads, writes, logs or passes
  `BINANCE_API_KEY` / `BINANCE_SECRET_KEY`, and never creates a CLI profile. The
  Market endpoints it uses require no authentication, so PROMETHEUS continues to
  hold no Binance API key at all.
* **No order path.** There is no function here that could place, amend or cancel
  an order, and none that could move funds. Not disabled -- absent.

The official skill mandates that production transactions require explicit user
confirmation. PROMETHEUS sidesteps that requirement entirely by never issuing a
transaction.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ..provenance import Status, now_iso

# Agent OS surface identifiers recorded on every corroborated snapshot.
AGENT_OS_SURFACE = "binance-agent-os:binance-cli"
SKILL_REFERENCE = "binance/binance-skills-hub:skills/binance/binance@2.0.0"

# Corroboration verdicts.
AGREED = "AGREED"
DISPUTED = "DISPUTED"
UNAVAILABLE = "UNAVAILABLE"

# Two honest sources for the same instant should agree far inside this band.
# Set generously so ordinary tick-timing skew is not called a dispute.
DEFAULT_TOLERANCE_BPS = Decimal("50")


class AgentOSUnavailable(RuntimeError):
    """The binance-cli surface could not be reached."""


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d > 0 else None


def _find_price(payload: Any) -> Decimal | None:
    """Pull a price out of a binance-cli JSON response.

    The CLI wraps the Binance REST API, so the documented response shapes are
    ``{"symbol": ..., "price": ...}`` and the bookTicker form. Some CLI versions
    nest the payload under a ``data`` key. All three are handled, and anything
    unrecognised returns None rather than a guess.
    """
    if payload is None:
        return None
    if isinstance(payload, list):
        for item in payload:
            found = _find_price(item)
            if found is not None:
                return found
        return None
    if not isinstance(payload, dict):
        return None

    for key in ("price", "lastPrice", "weightedAvgPrice"):
        found = _to_decimal(payload.get(key))
        if found is not None:
            return found

    # bookTicker shape: derive the mid so it is comparable to a last price.
    bid = _to_decimal(payload.get("bidPrice"))
    ask = _to_decimal(payload.get("askPrice"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2

    for nest in ("data", "result", "response"):
        if nest in payload:
            found = _find_price(payload[nest])
            if found is not None:
                return found
    return None


@dataclass
class AgentOSQuote:
    symbol: str
    price: Decimal
    surface: str
    command: str
    latency_ms: int
    retrieved_at: str
    cli_version: str | None


class AgentOSProvider:
    """Read-only Binance Agent OS market surface."""

    def __init__(
        self,
        binary: str = "binance-cli",
        *,
        timeout_s: float = 25.0,
        api_env: str = "prod",
        extra_paths: list[str] | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self.api_env = api_env
        self._version: str | None = None
        self._probe_error: str | None = None
        self._verified_call = False
        self.binary = self._resolve(binary, extra_paths or [])

    # -- discovery -----------------------------------------------------------
    @staticmethod
    def _resolve(binary: str, extra_paths: list[str]) -> str | None:
        found = shutil.which(binary)
        if found:
            return found
        home = os.path.expanduser("~")
        candidates = list(extra_paths) + [
            os.path.join(home, ".binance-cli", "bin"),
            os.path.join(home, ".local", "bin"),
            os.path.join(home, ".cargo", "bin"),
        ]
        for d in candidates:
            for name in (binary, f"{binary}.exe"):
                p = os.path.join(d, name)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    return p
        return None

    @property
    def installed(self) -> bool:
        return self.binary is not None

    # -- transport -----------------------------------------------------------
    def _run(self, args: list[str]) -> tuple[Any, str, int]:
        """Run binance-cli and parse stdout as JSON.

        Returns (payload, command_string, latency_ms).
        """
        if not self.binary:
            raise AgentOSUnavailable("binance-cli is not installed on this host")

        cmd = [self.binary, *args]
        printable = "binance-cli " + " ".join(args)

        # A deliberately minimal environment: the API credential variables are
        # stripped, so an unauthenticated call cannot accidentally become an
        # authenticated one, and no secret can leak into a subprocess.
        env = {k: v for k, v in os.environ.items()
               if k not in ("BINANCE_API_KEY", "BINANCE_SECRET_KEY")}
        env["BINANCE_API_ENV"] = self.api_env

        started = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout_s, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentOSUnavailable(f"{printable} timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise AgentOSUnavailable(f"{printable} failed to execute: {exc}") from exc
        latency = int((time.time() - started) * 1000)

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise AgentOSUnavailable(f"{printable} exited {proc.returncode}: {detail}")

        # The skill notes output may arrive on either stream.
        for stream in (proc.stdout, proc.stderr):
            text = (stream or "").strip()
            if not text:
                continue
            try:
                return json.loads(text), printable, latency
            except json.JSONDecodeError:
                start, end = text.find("{"), text.rfind("}")
                if start == -1:
                    start, end = text.find("["), text.rfind("]")
                if start != -1 and end > start:
                    try:
                        return json.loads(text[start : end + 1]), printable, latency
                    except json.JSONDecodeError:
                        continue
        raise AgentOSUnavailable(f"{printable} produced no parseable JSON")

    def version(self) -> str | None:
        if self._version is not None:
            return self._version
        if not self.binary:
            return None
        try:
            proc = subprocess.run(
                [self.binary, "--version"], capture_output=True, text=True, timeout=15
            )
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            self._version = out.splitlines()[0].strip() if out else None
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._probe_error = str(exc)
        return self._version

    # -- market surface (unauthenticated only) -------------------------------
    def ticker_price(self, symbol: str) -> AgentOSQuote:
        """Spot ticker price for ``symbol`` via the Agent OS CLI."""
        payload, cmd, latency = self._run(["spot", "ticker-price", "--symbol", symbol])
        price = _find_price(payload)
        if price is None:
            raise AgentOSUnavailable(
                f"{cmd} returned no recognisable price field: {json.dumps(payload)[:200]}"
            )
        self._verified_call = True
        return AgentOSQuote(
            symbol=symbol, price=price, surface=AGENT_OS_SURFACE, command=cmd,
            latency_ms=latency, retrieved_at=now_iso(), cli_version=self.version(),
        )

    # -- status --------------------------------------------------------------
    def status(self, probe_symbol: str | None = None) -> dict[str, Any]:
        """Honest Agent OS status using the project status vocabulary."""
        base: dict[str, Any] = {
            "provider": "binance-agent-os",
            "surface": AGENT_OS_SURFACE,
            "skill": SKILL_REFERENCE,
            "binary": self.binary,
            "api_env": self.api_env,
            "scope": "read-only market data",
            "authenticated": False,
            "capabilities": ["spot ticker-price (unauthenticated)"],
            "not_implemented": [
                "account data", "order placement", "wallet", "withdrawals",
            ],
            "note": (
                "used solely to corroborate the REST market snapshot; no credential is "
                "read or passed, and no order or transfer path exists in this module"
            ),
            "checked_at": now_iso(),
        }
        if not self.installed:
            base.update({
                "status": Status.UNAVAILABLE.value,
                "version": None,
                "error": (
                    "binance-cli is not installed on this host. Upstream ships no Windows "
                    "build for v2.x; install on Linux/macOS or build from source."
                ),
            })
            return base

        version = self.version()
        if probe_symbol:
            try:
                q = self.ticker_price(probe_symbol)
                base.update({
                    "status": Status.VERIFIED_LIVE.value,
                    "version": version,
                    "error": None,
                    "probe": {
                        "symbol": q.symbol, "price": str(q.price),
                        "command": q.command, "latency_ms": q.latency_ms,
                    },
                })
                return base
            except AgentOSUnavailable as exc:
                base.update({
                    "status": Status.BROKEN.value, "version": version, "error": str(exc)[:300],
                })
                return base

        base.update({
            "status": Status.VERIFIED_LOCAL.value if version else Status.ADAPTER_ONLY.value,
            "version": version,
            "error": self._probe_error,
        })
        return base


def corroborate(
    *,
    symbol: str,
    rest_price: Decimal | None,
    provider: AgentOSProvider | None,
    tolerance_bps: Decimal = DEFAULT_TOLERANCE_BPS,
) -> dict[str, Any]:
    """Cross-check a REST price against the Agent OS surface.

    Returns a record that is embedded in the market snapshot and therefore sealed
    into ``snapshot_hash``. A buyer can see which sources agreed, and by how much,
    at the instant the prediction was frozen.

    UNAVAILABLE is a distinct verdict from AGREED. An absent second witness never
    counts as confirmation.
    """
    record: dict[str, Any] = {
        "verdict": UNAVAILABLE,
        "surface": AGENT_OS_SURFACE,
        "skill": SKILL_REFERENCE,
        "tolerance_bps": str(tolerance_bps),
        "rest_price": None if rest_price is None else str(rest_price),
        "agent_os_price": None,
        "deviation_bps": None,
        "checked_at": now_iso(),
        "detail": "",
    }

    if provider is None or not provider.installed:
        record["detail"] = "binance-cli is not installed; snapshot has a single witness"
        return record
    if rest_price is None or rest_price <= 0:
        record["detail"] = "no REST price to corroborate"
        return record

    try:
        quote = provider.ticker_price(symbol)
    except AgentOSUnavailable as exc:
        record["detail"] = f"agent os surface unavailable: {exc}"[:300]
        return record

    deviation = abs(quote.price - rest_price) / rest_price * Decimal(10_000)
    record["agent_os_price"] = str(quote.price)
    record["deviation_bps"] = str(deviation.quantize(Decimal("0.0001")))
    record["command"] = quote.command
    record["cli_version"] = quote.cli_version
    record["latency_ms"] = quote.latency_ms

    if deviation <= tolerance_bps:
        record["verdict"] = AGREED
        record["detail"] = (
            f"REST and Binance Agent OS agree on {symbol} within "
            f"{deviation.quantize(Decimal('0.0001'))} bps"
        )
    else:
        record["verdict"] = DISPUTED
        record["detail"] = (
            f"REST reports {rest_price} but Binance Agent OS reports {quote.price} for "
            f"{symbol}: {deviation.quantize(Decimal('0.0001'))} bps apart, beyond the "
            f"{tolerance_bps} bps tolerance"
        )
    return record
