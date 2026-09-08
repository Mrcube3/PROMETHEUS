"""Signal generation, validation, cryptographic freeze and listing.

The order of operations is the product:

    observe -> compute -> generate -> validate -> freeze -> price -> list

Freeze happens before pricing and before listing, so the artifact whose hash a
buyer verifies is fixed before anybody could know its price, let alone its outcome.

The protected/public split is enforced by construction: ``public_preview`` is built
from an explicit allowlist of keys, so a new field added to the frozen artifact
cannot leak into the marketplace by default.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from ..canonical import sha256_hex
from ..config import Config
from ..db import Database
from ..market.agentos import AgentOSProvider, DISPUTED, corroborate
from ..market.binance import BinanceMarketData, MarketDataUnavailable
from ..model.base import ModelProvider, ModelRequest, ProviderError
from ..provenance import iso, now_iso, utcnow
from ..quant import features as quant
from .. import pricing as pricing_mod
from .. import reputation as reputation_mod
from .schema import (
    Direction,
    HORIZONS,
    ModelSignal,
    SCHEMA_VERSION,
    SignalState,
    assert_transition,
)
from .validator import RejectionCode, ValidationResult, validate_evidence

# The only fields a non-buyer may ever see. Anything not on this list is protected.
PUBLIC_PREVIEW_KEYS = (
    "signal_id", "asset", "venue", "horizon", "horizon_seconds",
    "created_at", "listed_at", "matures_at", "state",
    "model_provider", "model_version", "prompt_version", "schema_version",
    "price", "currency", "pricing_version", "pricing_classification",
    "snapshot_hash", "quant_hash", "signal_hash", "environment",
    "freshness", "evidence_key_count", "claim_count",
    # Corroboration describes the quality of the observation, not the prediction
    # made from it, so it is safe to publish and useful to a buyer deciding
    # whether the snapshot behind a signal had one witness or two.
    "corroboration_verdict", "corroboration_deviation_bps", "witnesses",
)


class GenerationError(RuntimeError):
    pass


class SignalRejected(RuntimeError):
    def __init__(self, code: RejectionCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def new_signal_id() -> str:
    return "sig_" + secrets.token_hex(12)


class SignalEngine:
    def __init__(
        self,
        db: Database,
        cfg: Config,
        market: BinanceMarketData,
        provider: ModelProvider,
        agentos: AgentOSProvider | None = None,
    ) -> None:
        self.db = db
        self.cfg = cfg
        self.market = market
        self.provider = provider
        self.agentos = agentos

    # -- generation ----------------------------------------------------------
    def generate(self, asset: str, horizon: str) -> dict[str, Any]:
        """Run one full generate-validate-freeze-price-list cycle.

        Returns the stored record, or raises. A rejection is persisted to
        ``rejections`` and re-raised as ``SignalRejected``.
        """
        if asset not in self.cfg.assets:
            raise SignalRejected(RejectionCode.UNSUPPORTED_ASSET, f"{asset} is not a configured asset")
        if horizon not in HORIZONS:
            raise SignalRejected(RejectionCode.UNSUPPORTED_HORIZON, f"{horizon} is not a supported horizon")

        # 1. OBSERVE
        try:
            snapshot = self.market.snapshot(asset, venue=self.cfg.venue)
        except MarketDataUnavailable as exc:
            raise GenerationError(f"market data unavailable for {asset}: {exc}") from exc

        entry_field = snapshot.get("last_price")
        if not entry_field.usable:
            self._record_rejection(
                asset, horizon, RejectionCode.STALE_MARKET_DATA,
                f"entry reference is {entry_field.freshness.value}", None,
            )
            raise SignalRejected(
                RejectionCode.STALE_MARKET_DATA,
                f"entry reference unusable: freshness={entry_field.freshness.value}",
            )
        entry_reference: Decimal = Decimal(str(entry_field.value))

        # 1b. CORROBORATE -- ask the Binance Agent OS surface the same question.
        # This runs before hashing so the verdict is sealed into snapshot_hash: a
        # buyer can see which independent sources agreed at the moment of freeze.
        snapshot.corroboration = corroborate(
            symbol=asset,
            rest_price=entry_reference,
            provider=self.agentos if self.cfg.agentos_enabled else None,
            tolerance_bps=self.cfg.agentos_tolerance_bps,
        )
        if (
            self.cfg.agentos_require_agreement
            and snapshot.corroboration["verdict"] == DISPUTED
        ):
            # Two independent witnesses disagree about the price. Publishing would
            # mean sealing a contested fact into a signal and selling it.
            self._record_rejection(
                asset, horizon, RejectionCode.CORROBORATION_FAILED,
                snapshot.corroboration["detail"], {"corroboration": snapshot.corroboration},
            )
            raise SignalRejected(
                RejectionCode.CORROBORATION_FAILED, snapshot.corroboration["detail"]
            )

        snapshot_dict = snapshot.to_dict()
        snapshot_hash = sha256_hex(snapshot_dict)

        # 2. ANALYZE -- deterministic, in code
        packet = quant.compute(snapshot, snapshot_hash)
        quant_dict = packet.to_dict()
        quant_hash = sha256_hex(quant_dict)

        # 3. PREDICT
        request = ModelRequest(
            asset=asset, venue=self.cfg.venue, horizon=horizon,
            horizon_seconds=HORIZONS[horizon], entry_reference=str(entry_reference),
            features=packet.summary(), available_keys=sorted(packet.available_keys()),
            quant_hash=quant_hash, snapshot_hash=snapshot_hash,
        )
        try:
            response = self.provider.generate(request)
        except (ProviderError, ValidationError) as exc:
            self._record_rejection(
                asset, horizon, RejectionCode.PROVIDER_ERROR, str(exc)[:500], None,
                provider=self.provider.name, model=self.provider.model_version(),
            )
            raise SignalRejected(RejectionCode.PROVIDER_ERROR, str(exc)) from exc

        model_signal = response.signal

        # 4. VALIDATE -- evidence must exist and be available
        result = validate_evidence(model_signal, packet, entry_reference=entry_reference)
        if not result.ok:
            self._record_rejection(
                asset, horizon, result.code or RejectionCode.SCHEMA_INVALID, result.detail,
                {"model_signal": model_signal.model_dump(mode="json"), "validation": result.to_dict()},
                provider=response.provider, model=response.model_version,
            )
            raise SignalRejected(result.code or RejectionCode.SCHEMA_INVALID, result.detail)

        # 5. FREEZE
        return self._freeze_and_list(
            asset=asset, horizon=horizon, entry_reference=entry_reference,
            snapshot_dict=snapshot_dict, snapshot_hash=snapshot_hash,
            quant_dict=quant_dict, quant_hash=quant_hash,
            model_signal=model_signal, response=response, validation=result,
            worst_freshness=snapshot.worst_freshness,
        )

    def _freeze_and_list(
        self, *, asset: str, horizon: str, entry_reference: Decimal,
        snapshot_dict: dict[str, Any], snapshot_hash: str,
        quant_dict: dict[str, Any], quant_hash: str,
        model_signal: ModelSignal, response, validation: ValidationResult,
        worst_freshness: str,
    ) -> dict[str, Any]:
        signal_id = new_signal_id()
        created = utcnow()
        matures = created + timedelta(seconds=HORIZONS[horizon])

        # The immutable core. This exact object is what `signal_hash` commits to.
        frozen: dict[str, Any] = {
            "signal_id": signal_id,
            "schema_version": SCHEMA_VERSION,
            "created_at": iso(created),
            "asset": asset,
            "venue": self.cfg.venue,
            "horizon": horizon,
            "horizon_seconds": HORIZONS[horizon],
            "matures_at": iso(matures),
            "direction": model_signal.direction.value,
            "confidence": str(model_signal.confidence),
            "entry_reference": str(entry_reference),
            "thesis": model_signal.thesis,
            "claims": [
                {"statement": c.statement, "evidence_keys": sorted(c.evidence_keys)}
                for c in model_signal.claims
            ],
            "risk_factors": list(model_signal.risk_factors),
            "invalidation": {
                "condition": model_signal.invalidation.condition,
                "reference_price": str(model_signal.invalidation.reference_price),
                "evidence_keys": sorted(model_signal.invalidation.evidence_keys),
            },
            "evidence_keys": sorted(validation.cited_keys),
            "model_provider": response.provider,
            "model_version": response.model_version,
            "prompt_version": response.prompt_version,
            "snapshot_hash": snapshot_hash,
            "quant_hash": quant_hash,
            "environment": self.cfg.environment,
        }
        signal_hash = sha256_hex(frozen)

        # 6. PRICE -- only after the prediction is immutable
        rep = reputation_mod.compute(self.db, self.cfg)
        demand = self._demand()
        quote = pricing_mod.quote(
            cfg=self.cfg, confidence=model_signal.confidence, horizon=horizon, asset=asset,
            freshness=worst_freshness, reputation=rep, demand=demand,
        )
        price = Decimal(quote["final_price"])

        # 7. LIST
        # A STAND_DOWN expresses no directional exposure, so it is not offered for
        # sale -- charging for "no edge here" while the preview hides the direction
        # would be selling a buyer something they did not agree to. It is still
        # frozen, hashed and journaled, so declines remain part of the public record.
        sellable = model_signal.direction != Direction.STAND_DOWN

        assert_transition(SignalState.DRAFT, SignalState.VALIDATING)
        assert_transition(SignalState.VALIDATING, SignalState.FROZEN)
        if sellable:
            assert_transition(SignalState.FROZEN, SignalState.LISTED)

        import json as _json

        state = SignalState.LISTED if sellable else SignalState.FROZEN
        listed_at = now_iso() if sellable else None
        with self.db.transaction() as tx:
            tx.execute(
                "INSERT INTO signals("
                " signal_id, created_at, listed_at, asset, venue, horizon, horizon_seconds,"
                " matures_at, state, direction, confidence, model_provider, model_version,"
                " prompt_version, entry_reference, price, currency, pricing_version, environment,"
                " snapshot_hash, quant_hash, signal_hash, frozen_json, snapshot_json, quant_json,"
                " pricing_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    signal_id, iso(created), listed_at, asset, self.cfg.venue, horizon,
                    HORIZONS[horizon], iso(matures), state.value,
                    model_signal.direction.value, str(model_signal.confidence),
                    response.provider, response.model_version, response.prompt_version,
                    str(entry_reference), str(price), self.cfg.price_currency,
                    quote["pricing_version"], self.cfg.environment,
                    snapshot_hash, quant_hash, signal_hash,
                    _json.dumps(frozen, sort_keys=True),
                    _json.dumps(snapshot_dict, sort_keys=True),
                    _json.dumps(quant_dict, sort_keys=True),
                    _json.dumps(quote, sort_keys=True),
                ),
            )
        self.db.journal(
            "SIGNAL_LISTED" if sellable else "SIGNAL_FROZEN_NOT_LISTED",
            {
                "signal_hash": signal_hash, "price": str(price), "direction": model_signal.direction.value,
                "pricing_classification": quote["classification"], "validation": validation.to_dict(),
                "model_latency_ms": response.latency_ms,
            },
            signal_id=signal_id,
        )
        return self.get(signal_id)  # type: ignore[return-value]

    def _demand(self) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM purchases"
            " WHERE state = 'PAID' AND created_at >= datetime('now', '-1 day')"
        )
        return {"sold_last_24h": row["n"] if row else 0}

    def _record_rejection(
        self, asset: str, horizon: str, code: RejectionCode, detail: str,
        raw: dict[str, Any] | None, *, provider: str = "", model: str = "",
    ) -> None:
        import json as _json

        rid = "rej_" + secrets.token_hex(10)
        self.db.execute(
            "INSERT INTO rejections(rejection_id, created_at, asset, horizon, model_provider,"
            " model_version, reason_code, detail, raw_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                rid, now_iso(), asset, horizon, provider or self.provider.name,
                model or self.provider.model_version(), code.value, detail[:1000],
                None if raw is None else _json.dumps(raw, sort_keys=True, default=str),
            ),
        )
        self.db.journal(
            "SIGNAL_REJECTED",
            {"rejection_id": rid, "asset": asset, "horizon": horizon,
             "code": code.value, "detail": detail[:500]},
        )

    # -- access --------------------------------------------------------------
    def get(self, signal_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM signals WHERE signal_id = ?", (signal_id,))
        return None if row is None else dict(row)

    def transition(self, signal_id: str, target: SignalState) -> None:
        row = self.db.query_one("SELECT state FROM signals WHERE signal_id = ?", (signal_id,))
        if row is None:
            raise KeyError(signal_id)
        current = SignalState(row["state"])
        if current == target:
            return
        assert_transition(current, target)
        self.db.execute("UPDATE signals SET state = ? WHERE signal_id = ?", (target.value, signal_id))
        self.db.journal(
            "STATE_TRANSITION", {"from": current.value, "to": target.value}, signal_id=signal_id
        )


# -- projections -------------------------------------------------------------
def public_preview(row: dict[str, Any]) -> dict[str, Any]:
    """The unpaid view.

    Built from an allowlist. Direction, confidence, thesis, claims, evidence and
    invalidation are absent by construction, not by omission.
    """
    import json as _json

    frozen = _json.loads(row["frozen_json"])
    pricing = _json.loads(row["pricing_json"]) if row.get("pricing_json") else {}
    snapshot = _json.loads(row["snapshot_json"])

    freshness = "UNKNOWN"
    lp = snapshot.get("fields", {}).get("last_price")
    if isinstance(lp, dict):
        freshness = lp.get("freshness", "UNKNOWN")

    cor = snapshot.get("corroboration") or {}
    verdict = cor.get("verdict", "UNAVAILABLE")

    full = {
        "signal_id": row["signal_id"],
        "asset": row["asset"],
        "venue": row["venue"],
        "horizon": row["horizon"],
        "horizon_seconds": row["horizon_seconds"],
        "created_at": row["created_at"],
        "listed_at": row["listed_at"],
        "matures_at": row["matures_at"],
        "state": row["state"],
        "model_provider": row["model_provider"],
        "model_version": row["model_version"],
        "prompt_version": row["prompt_version"],
        "schema_version": frozen.get("schema_version"),
        "price": row["price"],
        "currency": row["currency"],
        "pricing_version": row["pricing_version"],
        "pricing_classification": pricing.get("classification"),
        "snapshot_hash": row["snapshot_hash"],
        "quant_hash": row["quant_hash"],
        "signal_hash": row["signal_hash"],
        "environment": row["environment"],
        "freshness": freshness,
        "evidence_key_count": len(frozen.get("evidence_keys", [])),
        "claim_count": len(frozen.get("claims", [])),
        "corroboration_verdict": verdict,
        "corroboration_deviation_bps": cor.get("deviation_bps"),
        "witnesses": 2 if verdict == "AGREED" else 1,
    }
    return {k: full[k] for k in PUBLIC_PREVIEW_KEYS if k in full}


def passport(row: dict[str, Any], db: Database) -> dict[str, Any]:
    """The full Signal Passport -- the purchased artifact.

    Assembled from the frozen record plus separately stored payment, delivery and
    outcome references. The prediction section is byte-identical to what was hashed.
    """
    import json as _json

    frozen = _json.loads(row["frozen_json"])
    outcome_row = db.query_one("SELECT * FROM outcomes WHERE signal_id = ?", (row["signal_id"],))
    purchases = db.query(
        "SELECT purchase_id, state, amount, currency, network, verifier, verification,"
        " settlement_status, tx_hash, payer, environment, verified_at"
        " FROM purchases WHERE signal_id = ? ORDER BY created_at",
        (row["signal_id"],),
    )
    deliveries = db.query(
        "SELECT delivery_id, purchase_id, delivered_at, signal_hash, receipt_hash"
        " FROM deliveries WHERE signal_id = ? ORDER BY delivered_at",
        (row["signal_id"],),
    )

    return {
        "passport_version": "passport-1.0.0",
        "signal_id": row["signal_id"],
        "state": row["state"],
        # --- immutable, hashed -------------------------------------------
        "prediction": frozen,
        "hashes": {
            "signal_hash": row["signal_hash"],
            "snapshot_hash": row["snapshot_hash"],
            "quant_hash": row["quant_hash"],
            "algorithm": "SHA-256 over canonical JSON (sorted keys, no whitespace, UTF-8)",
            "canonical_version": "canonical-json-1.0.0",
            "verify": (
                "recompute sha256 of the canonical serialisation of `prediction` and compare "
                "to signal_hash"
            ),
        },
        # --- evidence -----------------------------------------------------
        "market_snapshot": _json.loads(row["snapshot_json"]),
        "quant_packet": _json.loads(row["quant_json"]),
        # --- commercial ---------------------------------------------------
        "pricing": _json.loads(row["pricing_json"]) if row.get("pricing_json") else None,
        "listed_price": row["price"],
        "currency": row["currency"],
        "purchases": [dict(p) for p in purchases],
        "deliveries": [dict(d) for d in deliveries],
        # --- reality ------------------------------------------------------
        "outcome": (
            None
            if outcome_row is None
            else {
                **{k: outcome_row[k] for k in outcome_row.keys() if k != "provenance_json"},
                "provenance": _json.loads(outcome_row["provenance_json"]),
            }
        ),
        "environment": row["environment"],
    }
