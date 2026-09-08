"""PROMETHEUS test suite.

Run: python -m pytest tests/ -v

Tests marked ``live`` contact real Binance public endpoints. Everything else is
hermetic. Nothing here mocks the thing it is trying to prove: the evidence
validator is tested by feeding it a genuinely fabricated key, and the paywall is
tested by actually requesting the protected resource.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from decimal import Decimal

import pytest

from prometheus.canonical import NonCanonicalValue, canonical_json, sha256_hex
from prometheus.config import Config, ConfigError, VERIFIER_ONCHAIN
from prometheus.db import Database
from prometheus.payments.x402 import (
    MalformedPayment,
    PaymentState,
    assert_payment_transition,
    decode_payment_header,
    IllegalPaymentTransition,
)
from prometheus.provenance import Classification, Freshness, classify_freshness, unavailable_field
from prometheus.quant.features import QuantPacket
from prometheus.signals.schema import (
    Claim,
    Direction,
    Invalidation,
    ModelSignal,
    SignalState,
    assert_transition,
    IllegalTransition,
)
from prometheus.signals.validator import RejectionCode, validate_evidence


# ============================================================ canonical hashing
class TestCanonical:
    def test_key_order_does_not_change_hash(self):
        assert sha256_hex({"b": 1, "a": 2}) == sha256_hex({"a": 2, "b": 1})

    def test_nested_key_order_does_not_change_hash(self):
        a = {"x": {"p": 1, "q": [{"m": 1, "n": 2}]}}
        b = {"x": {"q": [{"n": 2, "m": 1}], "p": 1}}
        assert sha256_hex(a) == sha256_hex(b)

    def test_floats_are_rejected(self):
        # A float that cannot be reproduced byte-for-byte in another language would
        # make every published hash unverifiable.
        with pytest.raises(NonCanonicalValue):
            canonical_json({"price": 1.1})

    def test_decimals_normalise(self):
        assert sha256_hex({"p": Decimal("1.50")}) == sha256_hex({"p": Decimal("1.5")})

    def test_any_change_changes_the_hash(self):
        base = {"direction": "LONG", "confidence": "0.6"}
        assert sha256_hex(base) != sha256_hex({**base, "direction": "SHORT"})

    def test_hash_is_reproducible_by_a_third_party(self):
        """A buyer recomputing with plain stdlib json must get the same digest."""
        import hashlib

        artifact = {"signal_id": "sig_1", "direction": "LONG", "claims": [{"k": ["a", "b"]}]}
        theirs = "sha256:" + hashlib.sha256(
            json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        assert sha256_hex(artifact) == theirs


# ========================================================== EVIDENCE VALIDATOR
def _packet(available: dict[str, str], unavailable: tuple[str, ...] = ()) -> QuantPacket:
    pkt = QuantPacket("BTCUSDT", "BINANCE_SPOT", "sha256:" + "0" * 64)
    for k, v in available.items():
        pkt.add(k, Decimal(v), inputs=["test"], formula="test", basis_ms=1, retrieved_ms=2)
    for k in unavailable:
        pkt.add(k, None, inputs=["test"], formula="test", basis_ms=1, retrieved_ms=2)
    return pkt


def _signal(evidence_keys: list[str], *, direction=Direction.LONG, inv="99") -> ModelSignal:
    return ModelSignal(
        direction=direction,
        confidence=Decimal("0.6"),
        thesis="A sufficiently long thesis string for the schema validator to accept it.",
        claims=[Claim(statement="Momentum is positive right now.", evidence_keys=evidence_keys)],
        risk_factors=["Volatility could invalidate this within the horizon."],
        invalidation=Invalidation(
            condition="Price trades through the reference level.", reference_price=Decimal(inv)
        ),
    )


class TestEvidenceValidator:
    """The flagship acceptance test from the build directive."""

    GOOD = {"return_15m": "0.001", "ema_fast": "100", "ema_slow": "99", "rsi_14": "55"}

    def test_five_consecutive_valid_signals_are_accepted(self):
        pkt = _packet(self.GOOD)
        for i in range(5):
            sig = _signal(["return_15m", "ema_fast"])
            result = validate_evidence(sig, pkt, entry_reference=Decimal("100"))
            assert result.ok, f"valid signal {i} was rejected: {result.detail}"

    def test_injected_nonexistent_evidence_key_is_rejected(self):
        """Inject a key the quant engine has never computed. It MUST be rejected."""
        pkt = _packet(self.GOOD)
        sig = _signal(["return_15m", "ema_200"])  # ema_200 does not exist
        result = validate_evidence(sig, pkt, entry_reference=Decimal("100"))
        assert result.ok is False
        assert result.code == RejectionCode.UNSUPPORTED_CLAIM
        assert "ema_200" in result.unsupported_keys

    def test_key_that_exists_but_is_unavailable_is_rejected(self):
        """A concept the engine knows but could not compute is still unusable."""
        pkt = _packet(self.GOOD, unavailable=("atr_14",))
        sig = _signal(["atr_14"])
        result = validate_evidence(sig, pkt, entry_reference=Decimal("100"))
        assert result.ok is False
        assert result.code == RejectionCode.UNAVAILABLE_EVIDENCE
        assert "atr_14" in result.unavailable_keys

    def test_unsupported_claim_is_not_repaired(self):
        """The validator must reject, never silently drop the bad key and continue."""
        pkt = _packet(self.GOOD)
        sig = _signal(["return_15m", "totally_made_up"])
        result = validate_evidence(sig, pkt, entry_reference=Decimal("100"))
        assert result.ok is False
        assert "return_15m" in result.cited_keys  # the good key is reported, not used to rescue

    def test_long_invalidation_above_entry_is_incoherent(self):
        pkt = _packet(self.GOOD)
        sig = _signal(["return_15m"], direction=Direction.LONG, inv="105")
        result = validate_evidence(sig, pkt, entry_reference=Decimal("100"))
        assert result.ok is False
        assert result.code == RejectionCode.INCOHERENT_INVALIDATION

    def test_short_invalidation_below_entry_is_incoherent(self):
        pkt = _packet(self.GOOD)
        sig = _signal(["return_15m"], direction=Direction.SHORT, inv="95")
        result = validate_evidence(sig, pkt, entry_reference=Decimal("100"))
        assert result.ok is False
        assert result.code == RejectionCode.INCOHERENT_INVALIDATION

    def test_absurdly_distant_invalidation_is_rejected(self):
        pkt = _packet(self.GOOD)
        sig = _signal(["return_15m"], direction=Direction.LONG, inv="1")
        result = validate_evidence(sig, pkt, entry_reference=Decimal("100"))
        assert result.ok is False


# ================================================================ SIGNAL SCHEMA
class TestSignalSchema:
    def test_confidence_above_one_is_rejected(self):
        with pytest.raises(Exception):
            _signal(["a"]).model_copy(update={"confidence": Decimal("1.5")})
        with pytest.raises(Exception):
            ModelSignal(
                direction=Direction.LONG, confidence=Decimal("1.5"),
                thesis="x" * 40, claims=[Claim(statement="y" * 20, evidence_keys=["a"])],
                risk_factors=["z" * 20],
                invalidation=Invalidation(condition="c" * 20, reference_price=Decimal("1")),
            )

    def test_negative_confidence_is_rejected(self):
        with pytest.raises(Exception):
            ModelSignal(
                direction=Direction.LONG, confidence=Decimal("-0.1"),
                thesis="x" * 40, claims=[Claim(statement="y" * 20, evidence_keys=["a"])],
                risk_factors=["z" * 20],
                invalidation=Invalidation(condition="c" * 20, reference_price=Decimal("1")),
            )

    def test_missing_invalidation_is_rejected(self):
        with pytest.raises(Exception):
            ModelSignal(
                direction=Direction.LONG, confidence=Decimal("0.5"),
                thesis="x" * 40, claims=[Claim(statement="y" * 20, evidence_keys=["a"])],
                risk_factors=["z" * 20],
            )

    def test_extra_fields_are_rejected(self):
        with pytest.raises(Exception):
            ModelSignal(
                direction=Direction.LONG, confidence=Decimal("0.5"), price="999",
                thesis="x" * 40, claims=[Claim(statement="y" * 20, evidence_keys=["a"])],
                risk_factors=["z" * 20],
                invalidation=Invalidation(condition="c" * 20, reference_price=Decimal("1")),
            )

    def test_model_cannot_express_a_price(self):
        """The model has no field through which to influence pricing."""
        assert "price" not in ModelSignal.model_fields
        assert "reputation" not in ModelSignal.model_fields
        assert "outcome" not in ModelSignal.model_fields

    def test_malformed_evidence_key_is_rejected(self):
        with pytest.raises(Exception):
            Claim(statement="a" * 20, evidence_keys=["Bad-Key!"])


# ================================================================ STATE MACHINE
class TestStateMachines:
    def test_legal_signal_path(self):
        path = [
            SignalState.DRAFT, SignalState.VALIDATING, SignalState.FROZEN, SignalState.LISTED,
            SignalState.PURCHASED, SignalState.DELIVERED, SignalState.AWAITING_OUTCOME,
            SignalState.MATURED, SignalState.SCORED, SignalState.VERIFIED,
        ]
        for a, b in zip(path, path[1:]):
            assert_transition(a, b)

    def test_cannot_skip_from_listed_to_verified(self):
        with pytest.raises(IllegalTransition):
            assert_transition(SignalState.LISTED, SignalState.VERIFIED)

    def test_rejected_is_terminal(self):
        with pytest.raises(IllegalTransition):
            assert_transition(SignalState.REJECTED, SignalState.LISTED)

    def test_payment_cannot_jump_to_paid(self):
        with pytest.raises(IllegalPaymentTransition):
            assert_payment_transition(PaymentState.PAYMENT_REQUIRED, PaymentState.PAID)

    def test_unknown_is_never_paid(self):
        assert PaymentState.PAID not in {PaymentState.UNKNOWN}
        with pytest.raises(IllegalPaymentTransition):
            assert_payment_transition(PaymentState.UNKNOWN, PaymentState.PAID)

    def test_failed_is_terminal(self):
        with pytest.raises(IllegalPaymentTransition):
            assert_payment_transition(PaymentState.FAILED, PaymentState.PAID)


# ================================================================= x402 DECODING
def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


VALID_PAYLOAD = {
    "x402Version": 1, "scheme": "exact", "network": "bsc-testnet",
    "payload": {
        "signature": "0x" + "ab" * 65,
        "authorization": {
            "from": "0x" + "11" * 20, "to": "0x" + "22" * 20, "value": "1000",
            "validAfter": "1", "validBefore": "99999999999", "nonce": "0x" + "cd" * 32,
        },
    },
}


class TestX402Decoding:
    def test_valid_payload_decodes(self):
        p = decode_payment_header(_b64(VALID_PAYLOAD))
        assert p.authorization.value == "1000"
        assert p.scheme == "exact"

    def test_empty_header_rejected(self):
        with pytest.raises(MalformedPayment):
            decode_payment_header("")

    def test_non_base64_rejected(self):
        with pytest.raises(MalformedPayment):
            decode_payment_header("!!!not base64!!!")

    def test_wrong_version_rejected(self):
        with pytest.raises(MalformedPayment):
            decode_payment_header(_b64({**VALID_PAYLOAD, "x402Version": 99}))

    def test_unsupported_scheme_rejected(self):
        with pytest.raises(MalformedPayment):
            decode_payment_header(_b64({**VALID_PAYLOAD, "scheme": "free"}))

    def test_short_signature_rejected(self):
        bad = json.loads(json.dumps(VALID_PAYLOAD))
        bad["payload"]["signature"] = "0xdeadbeef"
        with pytest.raises(MalformedPayment):
            decode_payment_header(_b64(bad))

    def test_negative_value_rejected(self):
        bad = json.loads(json.dumps(VALID_PAYLOAD))
        bad["payload"]["authorization"]["value"] = "-5"
        with pytest.raises(MalformedPayment):
            decode_payment_header(_b64(bad))

    def test_paid_true_is_not_a_payment(self):
        """The canonical forged payment. It must not decode into anything."""
        with pytest.raises(MalformedPayment):
            decode_payment_header(_b64({"paid": True}))
        with pytest.raises(MalformedPayment):
            decode_payment_header(_b64({"x402Version": 1, "scheme": "exact",
                                        "network": "bsc-testnet", "paid": True}))


# ================================================================= SIGNATURES
class TestSignatureVerification:
    """Real secp256k1. No stub, no mock."""

    def _sign(self, account, requirements, chain_id=97, value=None, to=None):
        import time as _t

        from eth_account.messages import encode_typed_data

        now = int(_t.time())
        auth = {
            "from": account.address,
            "to": to or requirements["payTo"],
            "value": value or requirements["maxAmountRequired"],
            "validAfter": str(now - 60), "validBefore": str(now + 600),
            "nonce": "0x" + secrets.token_hex(32),
        }
        typed = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"},
                ],
                "TransferWithAuthorization": [
                    {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"},
                ],
            },
            "primaryType": "TransferWithAuthorization",
            "domain": {"name": "Tether USD", "version": "1", "chainId": chain_id,
                       "verifyingContract": requirements["asset"]},
            "message": {
                "from": auth["from"], "to": auth["to"], "value": int(auth["value"]),
                "validAfter": int(auth["validAfter"]), "validBefore": int(auth["validBefore"]),
                "nonce": bytes.fromhex(auth["nonce"][2:]),
            },
        }
        sig = account.sign_message(encode_typed_data(full_message=typed)).signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig
        return {"x402Version": 1, "scheme": "exact", "network": "bsc-testnet",
                "payload": {"signature": sig, "authorization": auth}}

    REQ = {
        "scheme": "exact", "network": "bsc-testnet", "maxAmountRequired": "250000000000000000",
        "asset": "0x66E972502A34A625828C544a1914E8D8cc2A9dE5",
        "payTo": "0x" + "33" * 20, "resource": "http://x/y", "description": "d",
        "mimeType": "application/json", "outputSchema": None, "maxTimeoutSeconds": 300,
        "extra": {"name": "Tether USD", "version": "1"},
    }

    def test_genuine_signature_recovers_to_signer(self):
        from eth_account import Account

        from prometheus.payments.verifier import recover_signer
        from prometheus.payments.x402 import PaymentRequirements

        acct = Account.from_key("0x" + secrets.token_hex(32))
        payload = decode_payment_header(_b64(self._sign(acct, self.REQ)))
        recovered = recover_signer(
            payload, chain_id=97, token_name="Tether USD", token_version="1",
            verifying_contract=self.REQ["asset"],
        )
        assert recovered.lower() == acct.address.lower()

    def test_tampered_amount_breaks_recovery(self):
        """Changing the amount after signing must not recover to the signer."""
        from eth_account import Account

        from prometheus.payments.verifier import recover_signer

        acct = Account.from_key("0x" + secrets.token_hex(32))
        raw = self._sign(acct, self.REQ)
        raw["payload"]["authorization"]["value"] = "1"  # tamper
        payload = decode_payment_header(_b64(raw))
        recovered = recover_signer(
            payload, chain_id=97, token_name="Tether USD", token_version="1",
            verifying_contract=self.REQ["asset"],
        )
        assert recovered.lower() != acct.address.lower()

    def test_verifier_rejects_wrong_amount(self):
        from eth_account import Account

        from prometheus.config import Config
        from prometheus.payments.verifier import SettlementVerifier
        from prometheus.payments.x402 import PaymentRequirements

        acct = Account.from_key("0x" + secrets.token_hex(32))
        payload = decode_payment_header(_b64(self._sign(acct, self.REQ, value="1")))
        cfg = Config()
        cfg.settlement_verifier = "signature_only"
        cfg.x402_chain_id = 97
        result = SettlementVerifier(cfg).verify(payload, PaymentRequirements.from_dict(self.REQ))
        assert result.verified is False
        assert "does not equal the required" in result.reason

    def test_verifier_rejects_wrong_recipient(self):
        from eth_account import Account

        from prometheus.config import Config
        from prometheus.payments.verifier import SettlementVerifier
        from prometheus.payments.x402 import PaymentRequirements

        acct = Account.from_key("0x" + secrets.token_hex(32))
        payload = decode_payment_header(
            _b64(self._sign(acct, self.REQ, to="0x" + "99" * 20))
        )
        cfg = Config()
        cfg.settlement_verifier = "signature_only"
        result = SettlementVerifier(cfg).verify(payload, PaymentRequirements.from_dict(self.REQ))
        assert result.verified is False
        assert "requires" in result.reason

    def test_simulation_never_reports_a_transaction_hash(self):
        from eth_account import Account

        from prometheus.config import Config
        from prometheus.payments.verifier import SettlementVerifier
        from prometheus.payments.x402 import PaymentRequirements

        acct = Account.from_key("0x" + secrets.token_hex(32))
        payload = decode_payment_header(_b64(self._sign(acct, self.REQ)))
        cfg = Config()
        cfg.settlement_verifier = "signature_only"
        result = SettlementVerifier(cfg).verify(payload, PaymentRequirements.from_dict(self.REQ))
        assert result.verified is True
        assert result.tx_hash is None, "a simulated settlement must never invent a tx hash"
        assert result.environment == "SIMULATION"
        assert result.status.value == "SIMULATION"


# ==================================================================== PRICING
class TestPricing:
    def _cfg(self) -> Config:
        c = Config()
        c.base_price = Decimal("0.25")
        c.min_price = Decimal("0.05")
        c.max_price = Decimal("5.00")
        c.min_reputation_sample = 20
        return c

    def test_cold_start_is_exactly_base_price(self):
        from prometheus.pricing import CLASS_BASE_INSUFFICIENT, quote

        q = quote(cfg=self._cfg(), confidence=Decimal("0.95"), horizon="10M", asset="BTCUSDT",
                  freshness="FRESH", reputation={"resolved": 3})
        assert q["final_price"] == "0.250000"
        assert q["classification"] == CLASS_BASE_INSUFFICIENT

    def test_high_confidence_cannot_inflate_price_at_cold_start(self):
        from prometheus.pricing import quote

        cfg = self._cfg()
        low = quote(cfg=cfg, confidence=Decimal("0.05"), horizon="10M", asset="BTCUSDT",
                    freshness="FRESH", reputation={"resolved": 0})
        high = quote(cfg=cfg, confidence=Decimal("1.0"), horizon="10M", asset="BTCUSDT",
                     freshness="FRESH", reputation={"resolved": 0})
        assert low["final_price"] == high["final_price"]

    def test_price_is_always_bounded(self):
        from prometheus.pricing import quote

        cfg = self._cfg()
        q = quote(cfg=cfg, confidence=Decimal("1.0"), horizon="10M", asset="BTCUSDT",
                  freshness="FRESH",
                  reputation={"resolved": 500, "directional_accuracy": "1.0",
                              "by_asset": {}, "by_horizon": {}},
                  demand={"sold_last_24h": 9999})
        assert Decimal(q["final_price"]) <= cfg.max_price
        assert Decimal(q["final_price"]) >= cfg.min_price

    def test_quote_is_fully_explained(self):
        from prometheus.pricing import quote

        q = quote(cfg=self._cfg(), confidence=Decimal("0.6"), horizon="10M", asset="BTCUSDT",
                  freshness="FRESH", reputation={"resolved": 0})
        for key in ("base_price", "inputs", "multipliers", "final_price",
                    "min_price", "max_price", "pricing_version", "quoted_at"):
            assert key in q

    def test_cohort_below_min_sample_is_neutral(self):
        from prometheus.pricing import quote

        cfg = self._cfg()
        rep = {"resolved": 50, "directional_accuracy": "0.5",
               "by_asset": {"BTCUSDT": {"resolved": 2, "directional_accuracy": "1.0"}},
               "by_horizon": {}}
        q = quote(cfg=cfg, confidence=Decimal("0.5"), horizon="10M", asset="BTCUSDT",
                  freshness="FRESH", reputation=rep)
        cohort = [m for m in q["multipliers"] if m["name"] == "cohort_by_asset"][0]
        assert cohort["value"] == "1.0"


# ================================================================== PROVENANCE
class TestProvenance:
    def test_missing_data_stays_missing(self):
        f = unavailable_field(source="binance:test")
        assert f.value is None
        assert f.classification == Classification.UNAVAILABLE
        assert f.usable is False

    def test_freshness_thresholds(self):
        assert classify_freshness(0) == Freshness.FRESH
        assert classify_freshness(10_000) == Freshness.AGING
        assert classify_freshness(30_000) == Freshness.STALE
        assert classify_freshness(999_999) == Freshness.EXPIRED
        assert classify_freshness(None) == Freshness.UNAVAILABLE

    def test_expired_field_is_not_usable(self):
        from prometheus.provenance import binance_field

        f = binance_field(Decimal("1"), source="s", event_ms=0, retrieved_ms=10_000_000)
        assert f.freshness == Freshness.EXPIRED
        assert f.usable is False


# ==================================================================== OUTCOMES
class TestOutcome:
    def test_long_return_formula(self):
        entry, exit_ = Decimal("100"), Decimal("110")
        assert (exit_ - entry) / entry == Decimal("0.1")

    def test_short_return_formula(self):
        entry, exit_ = Decimal("100"), Decimal("90")
        assert (entry - exit_) / entry == Decimal("0.1")

    def test_short_profits_when_price_falls(self):
        from prometheus.outcome import FLAT_THRESHOLD

        entry, exit_ = Decimal("100"), Decimal("95")
        raw = (entry - exit_) / entry
        assert raw > FLAT_THRESHOLD  # CORRECT

    def test_tiny_move_is_flat_not_a_win(self):
        from prometheus.outcome import FLAT_THRESHOLD

        raw = (Decimal("100.001") - Decimal("100")) / Decimal("100")
        assert abs(raw) < FLAT_THRESHOLD


# ====================================================== DATABASE & IDEMPOTENCY
class TestDatabase:
    @pytest.fixture
    def db(self, tmp_path):
        return Database(str(tmp_path / "t.db"))

    def test_idempotency_replays_first_response(self, db):
        a = db.idempotent_put("scope", "k", {"v": 1})
        b = db.idempotent_put("scope", "k", {"v": 2})
        assert a == {"v": 1}
        assert b == {"v": 1}, "a retry must replay the first answer, not overwrite it"

    def test_journal_cannot_be_updated(self, db):
        db.journal("E", {"a": 1})
        with pytest.raises(Exception):
            db.execute("UPDATE journal SET event = 'X'")

    def test_journal_cannot_be_deleted(self, db):
        db.journal("E", {"a": 1})
        with pytest.raises(Exception):
            db.execute("DELETE FROM journal")

    def _insert_signal(self, db, state="LISTED"):
        db.execute(
            "INSERT INTO signals(signal_id, created_at, asset, venue, horizon, horizon_seconds,"
            " matures_at, state, direction, confidence, model_provider, model_version,"
            " prompt_version, entry_reference, environment, snapshot_hash, quant_hash,"
            " signal_hash, frozen_json, snapshot_json, quant_json)"
            " VALUES ('s1','t','BTCUSDT','V','10M',600,'t',?, 'LONG','0.5','p','m','pv','100',"
            " 'SIMULATION','h1','h2','h3','{}','{}','{}')",
            (state,),
        )

    def test_frozen_signal_is_immutable(self, db):
        self._insert_signal(db)
        with pytest.raises(Exception):
            db.execute("UPDATE signals SET direction = 'SHORT' WHERE signal_id = 's1'")
        with pytest.raises(Exception):
            db.execute("UPDATE signals SET signal_hash = 'forged' WHERE signal_id = 's1'")
        with pytest.raises(Exception):
            db.execute("UPDATE signals SET frozen_json = '{\"x\":1}' WHERE signal_id = 's1'")

    def test_state_may_still_advance_on_a_frozen_signal(self, db):
        self._insert_signal(db)
        db.execute("UPDATE signals SET state = 'PURCHASED' WHERE signal_id = 's1'")
        assert db.query_one("SELECT state FROM signals")["state"] == "PURCHASED"

    def test_outcomes_are_immutable(self, db):
        self._insert_signal(db)
        db.execute(
            "INSERT INTO outcomes(outcome_id, signal_id, resolved_at, entry_price, exit_price,"
            " raw_return, directional, resolution_source, methodology, provenance_json)"
            " VALUES ('o1','s1','t','100','110','0.1','CORRECT','src','m','{}')"
        )
        with pytest.raises(Exception):
            db.execute("UPDATE outcomes SET directional = 'INCORRECT'")

    def test_one_outcome_per_signal(self, db):
        self._insert_signal(db)
        for i in (1, 2):
            stmt = (
                "INSERT INTO outcomes(outcome_id, signal_id, resolved_at, entry_price, exit_price,"
                " raw_return, directional, resolution_source, methodology, provenance_json)"
                f" VALUES ('o{i}','s1','t','100','110','0.1','CORRECT','src','m','{{}}')"
            )
            if i == 1:
                db.execute(stmt)
            else:
                with pytest.raises(Exception):
                    db.execute(stmt)


# ==================================================================== CONFIG
class TestConfigGuards:
    def test_onchain_without_pay_to_is_refused(self):
        c = Config()
        c.settlement_verifier = VERIFIER_ONCHAIN
        c.x402_pay_to = "0x" + "00" * 20
        with pytest.raises(ConfigError):
            c.validate()

    def test_facilitator_without_url_is_refused(self):
        c = Config()
        c.settlement_verifier = "facilitator"
        c.x402_facilitator_url = ""
        with pytest.raises(ConfigError):
            c.validate()

    def test_signature_only_labels_environment_simulation(self):
        c = Config()
        c.settlement_verifier = "signature_only"
        assert c.environment == "SIMULATION"

    def test_onchain_chain_97_is_testnet_not_live(self):
        c = Config()
        c.settlement_verifier = VERIFIER_ONCHAIN
        c.x402_chain_id = 97
        assert c.environment == "TESTNET"


# ============================================================ REPUTATION HONESTY
class TestReputationHonesty:
    def test_small_sample_reports_insufficient(self, tmp_path):
        from prometheus.reputation import INSUFFICIENT, compute

        db = Database(str(tmp_path / "r.db"))
        cfg = Config()
        cfg.min_reputation_sample = 20
        for i in range(3):
            db.execute(
                "INSERT INTO signals(signal_id, created_at, asset, venue, horizon, horizon_seconds,"
                " matures_at, state, direction, confidence, model_provider, model_version,"
                " prompt_version, entry_reference, environment, snapshot_hash, quant_hash,"
                " signal_hash, frozen_json, snapshot_json, quant_json)"
                f" VALUES ('s{i}','t','BTCUSDT','V','10M',600,'t','VERIFIED','LONG','0.9','p','m',"
                " 'pv','100','SIMULATION','h1','h2','h3','{}','{}','{}')"
            )
            db.execute(
                "INSERT INTO outcomes(outcome_id, signal_id, resolved_at, entry_price, exit_price,"
                " raw_return, directional, resolution_source, methodology, provenance_json)"
                f" VALUES ('o{i}','s{i}','t','100','110','0.1','CORRECT','src','m','{{}}')"
            )
        rep = compute(db, cfg)
        # 3 for 3 is a 100% record. It must not be advertised as one.
        assert rep["correct"] == 3
        assert rep["directional_accuracy_display"] == INSUFFICIENT
        assert rep["sample_sufficient"] is False
        assert rep["n"] == 3
        assert rep["calibration"]["brier_score"] is None

    def test_reputation_never_counts_unresolved_signals(self, tmp_path):
        from prometheus.reputation import compute

        db = Database(str(tmp_path / "r2.db"))
        cfg = Config()
        db.execute(
            "INSERT INTO signals(signal_id, created_at, asset, venue, horizon, horizon_seconds,"
            " matures_at, state, direction, confidence, model_provider, model_version,"
            " prompt_version, entry_reference, environment, snapshot_hash, quant_hash,"
            " signal_hash, frozen_json, snapshot_json, quant_json)"
            " VALUES ('s9','t','BTCUSDT','V','10M',600,'t','UNRESOLVED','LONG','0.9','p','m',"
            " 'pv','100','SIMULATION','h1','h2','h3','{}','{}','{}')"
        )
        rep = compute(db, cfg)
        assert rep["resolved"] == 0
        assert rep["unresolved"] == 1


# ======================================================== LIVE (real Binance)
@pytest.mark.skipif(os.environ.get("PROM_SKIP_LIVE") == "1", reason="live tests disabled")
class TestLiveBinance:
    def test_ping(self):
        from prometheus.market.binance import BinanceMarketData

        m = BinanceMarketData(Config().binance_hosts)
        try:
            assert m.ping()["status"] == "VERIFIED_LIVE"
        finally:
            m.close()

    def test_snapshot_is_provenance_tagged(self):
        from prometheus.market.binance import BinanceMarketData

        m = BinanceMarketData(Config().binance_hosts)
        try:
            snap = m.snapshot("BTCUSDT", venue="BINANCE_SPOT")
            assert snap.get("last_price").classification == Classification.BINANCE_REPORTED
            assert snap.get("last_price").usable
            assert len(snap.klines) > 50
        finally:
            m.close()

    def test_quant_features_are_estimates_not_binance_reported(self):
        from prometheus.market.binance import BinanceMarketData
        from prometheus.quant.features import compute

        m = BinanceMarketData(Config().binance_hosts)
        try:
            snap = m.snapshot("BTCUSDT", venue="BINANCE_SPOT")
            pkt = compute(snap, sha256_hex(snap.to_dict()))
            assert pkt.features["rsi_14"].classification == Classification.PROMETHEUS_ESTIMATE
            assert "rsi_14" in pkt.available_keys()
            for k in pkt.derivations.values():
                assert k["formula"] and k["formula_version"]
        finally:
            m.close()

    def test_host_failover(self):
        """A dead primary must fail over, not fail."""
        from prometheus.market.binance import BinanceMarketData

        m = BinanceMarketData(
            ["https://127.0.0.1:1", "https://api.binance.com"], timeout_s=3
        )
        try:
            assert m.ping()["status"] == "VERIFIED_LIVE"
            assert m.last_host == "https://api.binance.com"
        finally:
            m.close()
