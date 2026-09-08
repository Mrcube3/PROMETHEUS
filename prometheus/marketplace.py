"""Marketplace: purchase lifecycle, settlement verification and delivery.

Security posture, stated plainly:

  * The protected artifact is released by exactly one function, ``deliver``, and it
    refuses unless the purchase row in the database says PAID and VERIFIED. Those
    columns are written only by ``submit_payment`` after a verifier returned
    ``verified=True``.
   * A client-supplied claim of payment is inert. The only client input that matters
     is the PAYMENT-SIGNATURE header, and its contents are checked cryptographically against
    the invoice this server issued.
  * Every mutating operation is idempotent, keyed so that a network retry replays
    the first answer instead of charging, delivering or scoring twice.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .canonical import sha256_hex
from .config import Config
from .db import Database
from .payments.verifier import SettlementVerifier
from .payments.x402 import (
    MalformedPayment,
    PaymentPayload,
    PaymentRequirements,
    PaymentState,
    assert_payment_transition,
    decode_payment_header,
    payment_required_body,
)
from .provenance import iso, now_iso, utcnow
from .signals.engine import SignalEngine, passport
from .signals.schema import SignalState


class PurchaseError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class PaymentRequired(RuntimeError):
    """Carries the 402 body the caller must serve."""

    def __init__(self, body: dict[str, Any], purchase_id: str) -> None:
        super().__init__("payment required")
        self.body = body
        self.purchase_id = purchase_id


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


class Marketplace:
    def __init__(self, db: Database, cfg: Config, engine: SignalEngine) -> None:
        self.db = db
        self.cfg = cfg
        self.engine = engine
        self.verifier = SettlementVerifier(cfg)

    # -- listing -------------------------------------------------------------
    def listings(self, *, asset: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM signals WHERE state IN ('LISTED','PURCHASED','DELIVERED')"
            " AND matures_at > ?"
        )
        params: list[Any] = [now_iso()]
        if asset:
            sql += " AND asset = ?"
            params.append(asset)
        sql += " ORDER BY listed_at DESC LIMIT ?"
        params.append(limit)
        from .signals.engine import public_preview

        return [public_preview(dict(r)) for r in self.db.query(sql, params)]

    # -- invoice -------------------------------------------------------------
    def _requirements(self, signal_row: dict[str, Any], purchase_id: str) -> PaymentRequirements:
        amount = Decimal(signal_row["price"])
        atomic = self.cfg.atomic_units(amount)
        resource = f"{self.cfg.base_url()}/api/purchases/{purchase_id}/delivery"
        return PaymentRequirements(
            scheme=self.cfg.x402_scheme,
            network=self.cfg.x402_network,
            maxAmountRequired=str(atomic),
            asset=self.cfg.x402_asset,
            payTo=self.cfg.x402_pay_to,
            resource=resource,
            description=(
                f"PROMETHEUS signal {signal_row['signal_id']} -- {signal_row['asset']} "
                f"{signal_row['horizon']} (signal_hash {signal_row['signal_hash'][:23]}...)"
            ),
            mimeType="application/json",
            outputSchema=None,
            maxTimeoutSeconds=self.cfg.x402_timeout_seconds,
            extra={
                "assetTransferMethod": self.cfg.x402_asset_transfer_method,
                "name": self.cfg.x402_asset_name,
                "version": self.cfg.x402_asset_version,
                # Non-normative context so a buyer knows what it is being asked to
                # trust before it signs anything.
                "prometheusSignalId": signal_row["signal_id"],
                "prometheusSignalHash": signal_row["signal_hash"],
                "prometheusEnvironment": self.cfg.environment,
                "prometheusSettlementVerifier": self.cfg.settlement_verifier,
            },
        )

    def create_purchase(self, signal_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        """Create a purchase and return the 402 payment requirements."""
        scope = "purchase.create"
        key = idempotency_key or f"auto:{signal_id}:{secrets.token_hex(8)}"
        existing = self.db.idempotent_get(scope, key)
        if existing is not None:
            return existing

        row = self.engine.get(signal_id)
        if row is None:
            raise PurchaseError(f"signal {signal_id} not found", status=404, code="NOT_FOUND")
        if row["state"] not in ("LISTED", "PURCHASED", "DELIVERED"):
            raise PurchaseError(
                f"signal {signal_id} is in state {row['state']} and is not purchasable",
                status=409, code="NOT_PURCHASABLE",
            )
        if _parse_iso(row["matures_at"]) <= utcnow():
            raise PurchaseError(
                f"signal {signal_id} has already matured and is no longer for sale",
                status=409, code="MATURED",
            )

        purchase_id = "pur_" + secrets.token_hex(12)
        requirements = self._requirements(row, purchase_id)
        expires = utcnow() + timedelta(seconds=self.cfg.x402_timeout_seconds)

        assert_payment_transition(PaymentState.CREATED, PaymentState.PAYMENT_REQUIRED)
        self.db.execute(
            "INSERT INTO purchases(purchase_id, signal_id, created_at, updated_at, state, amount,"
            " currency, atomic_amount, network, scheme, asset_address, pay_to, environment,"
            " requirements_json, expires_at, verification)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                purchase_id, signal_id, now_iso(), now_iso(), PaymentState.PAYMENT_REQUIRED.value,
                row["price"], row["currency"], requirements.maxAmountRequired,
                requirements.network, requirements.scheme, requirements.asset, requirements.payTo,
                self.cfg.environment,
                # `resource` is top-level in x402 v2 and is retained here so a
                # later retry can reconstruct the exact challenge from the DB.
                json.dumps(
                    {
                        **requirements.to_dict(version=self.cfg.x402_version),
                        "resource": requirements.resource,
                    },
                    sort_keys=True,
                ),
                iso(expires), "UNVERIFIED",
            ),
        )
        self.db.journal(
            "PURCHASE_CREATED",
            {"amount": row["price"], "currency": row["currency"],
             "atomic": requirements.maxAmountRequired, "network": requirements.network},
            signal_id=signal_id, purchase_id=purchase_id,
        )

        body = payment_required_body(
            [requirements],
            error=f"Payment required for PROMETHEUS signal {signal_id}",
            version=self.cfg.x402_version,
        )
        response = {
            "purchase_id": purchase_id,
            "signal_id": signal_id,
            "state": PaymentState.PAYMENT_REQUIRED.value,
            "expires_at": iso(expires),
            "x402": body,
            "settlement": self.verifier.describe(),
            "delivery_url": requirements.resource,
        }
        return self.db.idempotent_put(scope, key, response)

    # -- payment -------------------------------------------------------------
    def submit_payment(
        self, purchase_id: str, header_value: str, *, tx_hash: str | None = None
    ) -> dict[str, Any]:
        """Verify a PAYMENT-SIGNATURE header against this purchase's invoice.

        Idempotent on the purchase: once PAID, resubmission replays the stored
        result rather than re-verifying or re-charging.
        """
        scope = "purchase.pay"
        cached = self.db.idempotent_get(scope, purchase_id)
        if cached is not None:
            return cached

        row = self.db.query_one("SELECT * FROM purchases WHERE purchase_id = ?", (purchase_id,))
        if row is None:
            raise PurchaseError(f"purchase {purchase_id} not found", status=404, code="NOT_FOUND")
        purchase = dict(row)
        state = PaymentState(purchase["state"])

        if state == PaymentState.PAID:
            return {"purchase_id": purchase_id, "state": state.value,
                    "verification": purchase["verification"], "replayed": True}

        if _parse_iso(purchase["expires_at"]) <= utcnow():
            self._set_state(purchase_id, state, PaymentState.EXPIRED, reason="invoice expired")
            raise PurchaseError("payment window expired", status=409, code="EXPIRED")

        requirements = PaymentRequirements.from_dict(json.loads(purchase["requirements_json"]))

        # 1. Decode. Malformed input fails here and never reaches a verifier.
        try:
            payload: PaymentPayload = decode_payment_header(header_value)
        except MalformedPayment as exc:
            # Malformed input is a client error, not a dead invoice: the buyer may
            # correct it and retry while the window is open. Per the x402 error
            # table this is HTTP 400, not 402.
            self.db.journal(
                "PAYMENT_MALFORMED", {"reason": str(exc)[:400]},
                signal_id=purchase["signal_id"], purchase_id=purchase_id,
            )
            raise PurchaseError(str(exc), status=400, code="MALFORMED_PAYMENT") from exc

        # Replay protection runs BEFORE the nonce is recorded, so a replayed
        # authorization is refused cleanly instead of tripping the unique index.
        # A signed authorization may buy exactly once, ever.
        clash = self.db.query_one(
            "SELECT purchase_id FROM purchases WHERE nonce = ? AND purchase_id != ?",
            (payload.authorization.nonce, purchase_id),
        )
        if clash is not None:
            self._set_state(
                purchase_id, state, PaymentState.FAILED,
                reason=f"authorization nonce already used by {clash['purchase_id']}",
            )
            raise PurchaseError(
                "this payment authorization nonce has already been used",
                status=409, code="NONCE_REPLAY",
            )

        assert_payment_transition(state, PaymentState.PAYMENT_SUBMITTED)
        try:
            self._set_state(purchase_id, state, PaymentState.PAYMENT_SUBMITTED,
                            reason="PAYMENT-SIGNATURE received", payer=payload.authorization.from_,
                            nonce=payload.authorization.nonce,
                            payment_json=json.dumps(payload.to_dict(), sort_keys=True))
        except sqlite3.IntegrityError as exc:
            # Lost a race to another request carrying the same nonce.
            raise PurchaseError(
                "this payment authorization nonce has already been used",
                status=409, code="NONCE_REPLAY",
            ) from exc

        self._set_state(purchase_id, PaymentState.PAYMENT_SUBMITTED, PaymentState.VERIFYING,
                        reason="verifying settlement")

        # 2. Verify. The server decides; the buyer's opinion is not consulted.
        result = self.verifier.verify(payload, requirements, tx_hash=tx_hash)

        if not result.verified:
            # PENDING means the chain has not confirmed yet -- that is UNKNOWN, and
            # UNKNOWN is never PAID. Anything else returns the invoice to
            # PAYMENT_REQUIRED so the buyer can retry within the window.
            if result.settlement_status == "PENDING":
                target, verification = PaymentState.UNKNOWN, "UNKNOWN"
            else:
                target, verification = PaymentState.PAYMENT_REQUIRED, "REJECTED"
            self._set_state(
                purchase_id, PaymentState.VERIFYING, target, reason=result.reason,
                verifier=result.verifier, verification=verification,
                settlement_status=result.settlement_status, tx_hash=result.tx_hash,
                evidence_json=json.dumps(result.to_dict(), sort_keys=True, default=str),
            )
            # A failure is never cached: the buyer may legitimately retry with a
            # corrected payload or a mined transaction.
            return {
                "purchase_id": purchase_id,
                "state": target.value,
                "verification": verification,
                "settlement": result.to_dict(),
            }

        self._set_state(
            purchase_id, PaymentState.VERIFYING, PaymentState.PAID, reason=result.reason,
            verifier=result.verifier, verification="VERIFIED", payer=result.payer,
            settlement_status=result.settlement_status, tx_hash=result.tx_hash,
            evidence_json=json.dumps(result.to_dict(), sort_keys=True, default=str),
            verified_at=now_iso(),
        )

        # Treasury: revenue is booked only against verified settlement.
        self.db.execute(
            "INSERT INTO ledger(entry_id, created_at, kind, signal_id, purchase_id, amount,"
            " currency, environment, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "led_" + secrets.token_hex(10), now_iso(), "SIGNAL_SALE", purchase["signal_id"],
                purchase_id, purchase["amount"], purchase["currency"], self.cfg.environment,
                f"verifier={result.verifier}; settlement={result.settlement_status}",
            ),
        )

        # The signal is now sold. LISTED -> PURCHASED; a second buyer of the same
        # signal leaves it in PURCHASED, which is legal and expected.
        srow = self.db.query_one("SELECT state FROM signals WHERE signal_id = ?", (purchase["signal_id"],))
        if srow and srow["state"] == SignalState.LISTED.value:
            self.engine.transition(purchase["signal_id"], SignalState.PURCHASED)

        self.db.journal(
            "PAYMENT_VERIFIED",
            {"verifier": result.verifier, "settlement_status": result.settlement_status,
             "payer": result.payer, "transaction_hash": result.tx_hash,
             "environment": result.environment},
            signal_id=purchase["signal_id"], purchase_id=purchase_id,
        )

        response = {
            "purchase_id": purchase_id,
            "state": PaymentState.PAID.value,
            "verification": "VERIFIED",
            "settlement": result.to_dict(),
        }
        return self.db.idempotent_put(scope, purchase_id, response)

    def _set_state(
        self, purchase_id: str, current: PaymentState, target: PaymentState, *, reason: str, **fields: Any
    ) -> None:
        if current != target:
            assert_payment_transition(current, target)
        sets = ["state = ?", "updated_at = ?"]
        params: list[Any] = [target.value, now_iso()]
        for col, val in fields.items():
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        params.append(purchase_id)
        self.db.execute(f"UPDATE purchases SET {', '.join(sets)} WHERE purchase_id = ?", params)
        self.db.journal(
            "PAYMENT_STATE", {"from": current.value, "to": target.value, "reason": reason[:400]},
            purchase_id=purchase_id,
        )

    # -- delivery ------------------------------------------------------------
    def deliver(self, purchase_id: str) -> dict[str, Any]:
        """Release the protected artifact.

        The single gate: the stored purchase must be PAID and VERIFIED. This is read
        from the database, not from the request.
        """
        row = self.db.query_one("SELECT * FROM purchases WHERE purchase_id = ?", (purchase_id,))
        if row is None:
            raise PurchaseError(f"purchase {purchase_id} not found", status=404, code="NOT_FOUND")
        purchase = dict(row)

        if purchase["state"] != PaymentState.PAID.value or purchase["verification"] != "VERIFIED":
            raise PurchaseError(
                (
                    f"purchase is {purchase['state']} / {purchase['verification']}; the protected "
                    "signal is released only after verified settlement"
                ),
                status=402, code="PAYMENT_REQUIRED",
            )

        existing = self.db.query_one(
            "SELECT * FROM deliveries WHERE purchase_id = ?", (purchase_id,)
        )
        if existing is not None:
            # Idempotent: redelivery returns the same bytes that were first sold.
            return json.loads(existing["artifact_json"])

        srow = self.engine.get(purchase["signal_id"])
        if srow is None:
            raise PurchaseError("signal record missing", status=500, code="INTERNAL")

        # The artifact is assembled from the frozen record. Nothing is regenerated.
        artifact = passport(srow, self.db)
        artifact["delivery"] = {
            "purchase_id": purchase_id,
            "delivered_at": now_iso(),
            "settlement_verifier": purchase["verifier"],
            "settlement_status": purchase["settlement_status"],
            "environment": purchase["environment"],
            "payer": purchase["payer"],
            "transaction_hash": purchase["tx_hash"],
            "amount": purchase["amount"],
            "currency": purchase["currency"],
            "notice": (
                "This artifact was frozen and hashed before purchase. Recompute the SHA-256 of "
                "the canonical JSON of `prediction` and compare it to hashes.signal_hash."
            ),
        }
        receipt_hash = sha256_hex(
            {
                "purchase_id": purchase_id,
                "signal_hash": srow["signal_hash"],
                "amount": purchase["amount"],
                "currency": purchase["currency"],
                "payer": purchase["payer"],
                "settlement_verifier": purchase["verifier"],
                "settlement_status": purchase["settlement_status"],
                "environment": purchase["environment"],
            }
        )
        artifact["delivery"]["receipt_hash"] = receipt_hash

        delivery_id = "dlv_" + secrets.token_hex(10)
        try:
            self.db.execute(
                "INSERT INTO deliveries(delivery_id, purchase_id, signal_id, delivered_at,"
                " signal_hash, artifact_json, receipt_hash) VALUES (?,?,?,?,?,?,?)",
                (
                    delivery_id, purchase_id, purchase["signal_id"], now_iso(),
                    srow["signal_hash"], json.dumps(artifact, sort_keys=True, default=str), receipt_hash,
                ),
            )
        except Exception:
            # Lost a race with a concurrent delivery: return the winner's artifact.
            won = self.db.query_one("SELECT artifact_json FROM deliveries WHERE purchase_id = ?", (purchase_id,))
            if won is not None:
                return json.loads(won["artifact_json"])
            raise

        if srow["state"] == SignalState.PURCHASED.value:
            self.engine.transition(purchase["signal_id"], SignalState.DELIVERED)

        self.db.journal(
            "SIGNAL_DELIVERED",
            {"delivery_id": delivery_id, "signal_hash": srow["signal_hash"], "receipt_hash": receipt_hash},
            signal_id=purchase["signal_id"], purchase_id=purchase_id,
        )
        return artifact

    def purchase_status(self, purchase_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM purchases WHERE purchase_id = ?", (purchase_id,))
        if row is None:
            raise PurchaseError(f"purchase {purchase_id} not found", status=404, code="NOT_FOUND")
        p = dict(row)
        delivered = self.db.query_one(
            "SELECT delivery_id, delivered_at FROM deliveries WHERE purchase_id = ?", (purchase_id,)
        )
        return {
            "purchase_id": p["purchase_id"],
            "signal_id": p["signal_id"],
            "state": p["state"],
            "verification": p["verification"],
            "settlement_status": p["settlement_status"],
            "settlement_verifier": p["verifier"],
            "environment": p["environment"],
            "amount": p["amount"],
            "currency": p["currency"],
            "atomic_amount": p["atomic_amount"],
            "network": p["network"],
            "asset": p["asset_address"],
            "pay_to": p["pay_to"],
            "payer": p["payer"],
            "transaction_hash": p["tx_hash"],
            "created_at": p["created_at"],
            "expires_at": p["expires_at"],
            "verified_at": p["verified_at"],
            "delivered": delivered is not None,
            "delivered_at": delivered["delivered_at"] if delivered else None,
            "settlement_evidence": json.loads(p["evidence_json"]) if p["evidence_json"] else None,
        }
