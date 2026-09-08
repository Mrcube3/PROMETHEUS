"""Sample-composition caveats.

A reputation figure can clear the size threshold and still be misleading. These
tests pin the property that matters: a headline accuracy is only presented
unqualified when the sample that produced it is actually capable of supporting it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from prometheus.config import Config
from prometheus.db import Database
from prometheus.reputation import (
    CAVEAT_NARROW_ASSETS,
    CAVEAT_SINGLE_REGIME,
    CAVEAT_SMALL_SAMPLE,
    INSUFFICIENT,
    compute,
)


def _seed(db: Database, rows: list[tuple[str, str, str, str]]) -> None:
    """rows = [(signal_id, asset, direction, directional_result)]"""
    for i, (sid, asset, direction, result) in enumerate(rows):
        db.execute(
            "INSERT INTO signals(signal_id, created_at, asset, venue, horizon, horizon_seconds,"
            " matures_at, state, direction, confidence, model_provider, model_version,"
            " prompt_version, entry_reference, environment, snapshot_hash, quant_hash,"
            " signal_hash, frozen_json, snapshot_json, quant_json)"
            " VALUES (?,'t',?,'V','10M',600,'t','VERIFIED',?,'0.6','p','m','pv','100',"
            " 'SIMULATION','h1','h2','h3','{}','{}','{}')",
            (sid, asset, direction),
        )
        db.execute(
            "INSERT INTO outcomes(outcome_id, signal_id, resolved_at, entry_price, exit_price,"
            " raw_return, directional, resolution_source, methodology, provenance_json)"
            " VALUES (?,?,'t','100','101','0.01',?,'src','m','{}')",
            (f"o{i}", sid, result),
        )


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "rep.db"))


@pytest.fixture
def cfg():
    c = Config()
    c.min_reputation_sample = 20
    return c


class TestSingleDirectionSample:
    def test_all_one_direction_is_flagged_even_with_a_large_sample(self, db, cfg):
        """The real case this was built for: 27 SHORT calls in one downtrend."""
        _seed(db, [(f"s{i}", ["BTCUSDT", "ETHUSDT", "SOLUSDT"][i % 3], "SHORT",
                    "CORRECT" if i % 9 else "INCORRECT") for i in range(27)])
        r = compute(db, cfg)

        assert r["n"] == 27
        assert r["sample_sufficient"] is True          # size threshold cleared
        assert r["sample_trustworthy"] is False        # but not trustworthy
        assert r["direction_concentration"] == "1.0000"
        codes = [c["code"] for c in r["sample_caveats"]]
        assert CAVEAT_SINGLE_REGIME in codes
        # The headline must not stand bare.
        assert "qualified" in r["directional_accuracy_display"]

    def test_balanced_large_sample_is_trustworthy(self, db, cfg):
        rows = []
        for i in range(24):
            rows.append((f"s{i}", ["BTCUSDT", "ETHUSDT", "SOLUSDT"][i % 3],
                         "LONG" if i % 2 else "SHORT", "CORRECT" if i % 3 else "INCORRECT"))
        _seed(db, rows)
        r = compute(db, cfg)

        assert r["sample_trustworthy"] is True
        assert r["sample_caveats"] == []
        assert "qualified" not in r["directional_accuracy_display"]
        assert r["directional_accuracy_display"].endswith("%")

    def test_concentration_just_under_the_limit_is_accepted(self, db, cfg):
        # 15 LONG / 6 SHORT = 71.4% concentration, below the 80% limit.
        rows = [(f"s{i}", ["BTCUSDT", "ETHUSDT"][i % 2],
                 "LONG" if i < 15 else "SHORT", "CORRECT") for i in range(21)]
        _seed(db, rows)
        r = compute(db, cfg)
        assert Decimal(r["direction_concentration"]) < Decimal("0.80")
        assert CAVEAT_SINGLE_REGIME not in [c["code"] for c in r["sample_caveats"]]

    def test_concentration_at_the_limit_is_flagged(self, db, cfg):
        # 16 LONG / 4 SHORT = exactly 80%.
        rows = [(f"s{i}", ["BTCUSDT", "ETHUSDT"][i % 2],
                 "LONG" if i < 16 else "SHORT", "CORRECT") for i in range(20)]
        _seed(db, rows)
        r = compute(db, cfg)
        assert r["direction_concentration"] == "0.8000"
        assert CAVEAT_SINGLE_REGIME in [c["code"] for c in r["sample_caveats"]]


class TestOtherCaveats:
    def test_small_sample_still_withholds_entirely(self, db, cfg):
        _seed(db, [(f"s{i}", "BTCUSDT", "LONG" if i % 2 else "SHORT", "CORRECT")
                   for i in range(3)])
        r = compute(db, cfg)
        assert r["directional_accuracy_display"] == INSUFFICIENT
        assert CAVEAT_SMALL_SAMPLE in [c["code"] for c in r["sample_caveats"]]

    def test_single_asset_sample_is_flagged(self, db, cfg):
        rows = [(f"s{i}", "BTCUSDT", "LONG" if i % 2 else "SHORT", "CORRECT")
                for i in range(22)]
        _seed(db, rows)
        r = compute(db, cfg)
        assert list(r["asset_mix"]) == ["BTCUSDT"]
        assert CAVEAT_NARROW_ASSETS in [c["code"] for c in r["sample_caveats"]]

    def test_empty_record_reports_no_concentration(self, db, cfg):
        r = compute(db, cfg)
        assert r["n"] == 0
        assert r["direction_concentration"] is None
        assert r["directional_accuracy_display"] == INSUFFICIENT

    def test_stand_downs_are_excluded_from_composition(self, db, cfg):
        """Declining is not a directional call and must not skew the mix."""
        rows = [(f"s{i}", "BTCUSDT", "LONG" if i % 2 else "SHORT", "CORRECT")
                for i in range(10)]
        rows += [(f"sd{i}", "BTCUSDT", "STAND_DOWN", "NOT_SCORED") for i in range(10)]
        _seed(db, rows)
        r = compute(db, cfg)
        assert set(r["direction_mix"]) == {"LONG", "SHORT"}
        assert r["n"] == 10

    def test_raw_accuracy_is_always_still_available(self, db, cfg):
        """Qualifying the headline must not hide the underlying number."""
        _seed(db, [(f"s{i}", "BTCUSDT", "SHORT", "CORRECT") for i in range(25)])
        r = compute(db, cfg)
        assert r["directional_accuracy"] == "1.0000"      # the real figure, unhidden
        assert r["sample_trustworthy"] is False           # but flagged
        assert r["sample_caveats"]
