"""Binance Agent OS (binance-cli) corroboration tests.

The CLI is driven through a stub binary so the corroboration decision logic is
proven deterministically on any host. The stub is a MOCK and is labelled as one:
it proves the decision logic, not that binance-cli is installed.

The property under test is the one that matters for the product: a snapshot with
two disagreeing witnesses must never become a sellable signal, and a snapshot with
no second witness must never be reported as corroborated.
"""

from __future__ import annotations

import stat
import sys
from decimal import Decimal

import pytest

from prometheus.market.agentos import (
    AGREED,
    DISPUTED,
    UNAVAILABLE,
    AgentOSProvider,
    corroborate,
)


def _make_stub(tmp_path, body: str, exit_code: int = 0) -> str:
    """Write a fake binance-cli that runs ``body`` (a Python snippet)."""
    script = tmp_path / "fake_cli_impl.py"
    script.write_text(body + f"\nimport sys as _s; _s.exit({exit_code})\n", encoding="utf-8")

    if sys.platform == "win32":
        launcher = tmp_path / "stub_cli.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = tmp_path / "stub_cli"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return str(launcher)


def _printing_stub(tmp_path, payload: str, exit_code: int = 0) -> AgentOSProvider:
    body = f"import sys\nsys.stdout.write({payload!r})"
    return AgentOSProvider(_make_stub(tmp_path, body, exit_code))


class TestCorroborationVerdicts:
    def test_agreeing_sources_produce_AGREED(self, tmp_path):
        p = _printing_stub(tmp_path, '{"symbol":"BTCUSDT","price":"79000.00"}')
        rec = corroborate(symbol="BTCUSDT", rest_price=Decimal("79000.00"), provider=p)
        assert rec["verdict"] == AGREED
        assert rec["agent_os_price"] == "79000.00"
        assert Decimal(rec["deviation_bps"]) == 0

    def test_small_tick_skew_is_tolerated(self, tmp_path):
        # ~1.27 bps apart: two honest sources sampled milliseconds apart.
        p = _printing_stub(tmp_path, '{"symbol":"BTCUSDT","price":"79010.00"}')
        rec = corroborate(symbol="BTCUSDT", rest_price=Decimal("79000.00"), provider=p)
        assert rec["verdict"] == AGREED

    def test_material_disagreement_produces_DISPUTED(self, tmp_path):
        p = _printing_stub(tmp_path, '{"symbol":"BTCUSDT","price":"71000.00"}')
        rec = corroborate(symbol="BTCUSDT", rest_price=Decimal("79000.00"), provider=p)
        assert rec["verdict"] == DISPUTED
        assert Decimal(rec["deviation_bps"]) > Decimal("50")

    def test_tolerance_boundary_is_respected(self, tmp_path):
        p = _printing_stub(tmp_path, '{"symbol":"BTCUSDT","price":"79400.00"}')
        # ~50.6 bps -> outside a 50 bps tolerance, inside a 100 bps one.
        assert corroborate(symbol="BTCUSDT", rest_price=Decimal("79000"),
                           provider=p)["verdict"] == DISPUTED
        assert corroborate(symbol="BTCUSDT", rest_price=Decimal("79000"), provider=p,
                           tolerance_bps=Decimal("100"))["verdict"] == AGREED


class TestAbsenceIsNotConfirmation:
    """The single most important property in this module."""

    def test_missing_cli_is_UNAVAILABLE_not_AGREED(self):
        p = AgentOSProvider("definitely-not-a-real-binary-xyz")
        rec = corroborate(symbol="BTCUSDT", rest_price=Decimal("79000"), provider=p)
        assert rec["verdict"] == UNAVAILABLE
        assert rec["verdict"] != AGREED

    def test_provider_none_is_UNAVAILABLE(self):
        rec = corroborate(symbol="BTCUSDT", rest_price=Decimal("79000"), provider=None)
        assert rec["verdict"] == UNAVAILABLE

    def test_cli_failure_is_UNAVAILABLE(self, tmp_path):
        p = _printing_stub(tmp_path, "boom", exit_code=1)
        rec = corroborate(symbol="BTCUSDT", rest_price=Decimal("79000"), provider=p)
        assert rec["verdict"] == UNAVAILABLE

    def test_unparseable_output_is_UNAVAILABLE(self, tmp_path):
        p = _printing_stub(tmp_path, "not json at all")
        rec = corroborate(symbol="BTCUSDT", rest_price=Decimal("79000"), provider=p)
        assert rec["verdict"] == UNAVAILABLE

    def test_no_rest_price_is_UNAVAILABLE(self, tmp_path):
        p = _printing_stub(tmp_path, '{"price":"79000.00"}')
        rec = corroborate(symbol="BTCUSDT", rest_price=None, provider=p)
        assert rec["verdict"] == UNAVAILABLE


class TestResponseShapes:
    def test_book_ticker_shape(self, tmp_path):
        p = _printing_stub(tmp_path, '{"bidPrice":"78999.00","askPrice":"79001.00"}')
        assert corroborate(symbol="BTCUSDT", rest_price=Decimal("79000.00"),
                           provider=p)["verdict"] == AGREED

    def test_nested_data_shape(self, tmp_path):
        p = _printing_stub(tmp_path, '{"data":{"symbol":"BTCUSDT","price":"79000.00"}}')
        assert corroborate(symbol="BTCUSDT", rest_price=Decimal("79000.00"),
                           provider=p)["verdict"] == AGREED

    def test_list_shape(self, tmp_path):
        p = _printing_stub(tmp_path, '[{"symbol":"BTCUSDT","price":"79000.00"}]')
        assert corroborate(symbol="BTCUSDT", rest_price=Decimal("79000.00"),
                           provider=p)["verdict"] == AGREED

    def test_zero_or_negative_price_is_not_accepted(self, tmp_path):
        p = _printing_stub(tmp_path, '{"symbol":"BTCUSDT","price":"0"}')
        rec = corroborate(symbol="BTCUSDT", rest_price=Decimal("79000.00"), provider=p)
        assert rec["verdict"] == UNAVAILABLE


class TestReadOnlyByConstruction:
    def test_no_trading_or_account_surface_exists(self):
        import inspect

        from prometheus.market import agentos

        public = [
            n for n, _ in inspect.getmembers(agentos.AgentOSProvider, inspect.isfunction)
            if not n.startswith("_")
        ]
        assert set(public) <= {"ticker_price", "status", "version"}, public

    def test_only_unauthenticated_market_command_is_issued(self):
        import inspect

        from prometheus.market import agentos

        src = inspect.getsource(agentos.AgentOSProvider.ticker_price)
        assert '"spot", "ticker-price"' in src
        for forbidden in ("new-order", "get-account", "withdraw", "transfer", "--signed"):
            assert forbidden not in src

    def test_credentials_are_stripped_from_subprocess_env(self, tmp_path, monkeypatch):
        """A key set in this process must not reach the CLI."""
        monkeypatch.setenv("BINANCE_API_KEY", "SHOULD_NOT_LEAK")
        monkeypatch.setenv("BINANCE_SECRET_KEY", "SHOULD_NOT_LEAK")
        body = (
            "import os, sys, json\n"
            "leaked = [k for k in ('BINANCE_API_KEY','BINANCE_SECRET_KEY') if k in os.environ]\n"
            "sys.stdout.write(json.dumps({'symbol':'BTCUSDT','price':'79000.00','leaked':leaked}))"
        )
        p = AgentOSProvider(_make_stub(tmp_path, body))
        payload, _cmd, _lat = p._run(["spot", "ticker-price", "--symbol", "BTCUSDT"])
        assert payload["leaked"] == [], f"credentials leaked to CLI: {payload['leaked']}"

    def test_status_is_honest_when_not_installed(self):
        st = AgentOSProvider("definitely-not-a-real-binary-xyz").status()
        assert st["status"] == "UNAVAILABLE"
        assert st["authenticated"] is False
        assert "order placement" in st["not_implemented"]
        assert "withdrawals" in st["not_implemented"]


class TestDisputeBlocksPublication:
    """A disputed snapshot must not become a sellable signal."""

    def test_disputed_verdict_rejects_and_records(self, tmp_path):
        from prometheus.config import Config
        from prometheus.db import Database
        from prometheus.market.binance import BinanceMarketData
        from prometheus.model.heuristic import HeuristicProvider
        from prometheus.signals.engine import SignalEngine, SignalRejected
        from prometheus.signals.validator import RejectionCode

        stub = _printing_stub(tmp_path, '{"symbol":"BTCUSDT","price":"1.00"}')
        cfg = Config()
        cfg.agentos_enabled = True
        cfg.agentos_require_agreement = True
        db = Database(str(tmp_path / "d.db"))
        market = BinanceMarketData(cfg.binance_hosts, cfg.http_timeout_s)
        engine = SignalEngine(db, cfg, market, HeuristicProvider(), agentos=stub)
        try:
            with pytest.raises(SignalRejected) as exc:
                engine.generate("BTCUSDT", "10M")
            assert exc.value.code == RejectionCode.CORROBORATION_FAILED
            # The refusal is on the public record, and nothing was published.
            assert db.query_one("SELECT reason_code FROM rejections")["reason_code"] == (
                "CORROBORATION_FAILED"
            )
            assert db.query_one("SELECT COUNT(*) AS n FROM signals")["n"] == 0
        finally:
            market.close()

    def test_missing_cli_does_not_block_publication(self, tmp_path):
        """Absence of a second witness must degrade, not halt, the product."""
        from prometheus.config import Config
        from prometheus.db import Database
        from prometheus.market.binance import BinanceMarketData
        from prometheus.model.heuristic import HeuristicProvider
        from prometheus.signals.engine import SignalEngine, SignalRejected

        cfg = Config()
        cfg.agentos_enabled = True
        cfg.agentos_require_agreement = True
        db = Database(str(tmp_path / "d2.db"))
        market = BinanceMarketData(cfg.binance_hosts, cfg.http_timeout_s)
        engine = SignalEngine(
            db, cfg, market, HeuristicProvider(),
            agentos=AgentOSProvider("definitely-not-a-real-binary-xyz"),
        )
        try:
            try:
                engine.generate("BTCUSDT", "10M")
            except SignalRejected as exc:
                # Any rejection is acceptable except one caused by corroboration.
                assert exc.code.value != "CORROBORATION_FAILED"
            row = db.query_one(
                "SELECT COUNT(*) AS n FROM rejections WHERE reason_code = 'CORROBORATION_FAILED'"
            )
            assert row["n"] == 0
        finally:
            market.close()
