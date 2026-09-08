"""PROMETHEUS HTTP API.

These are PROMETHEUS routes. None of them is a Binance endpoint.

The x402 flow lives on one resource:

    GET /api/purchases/{id}/delivery
        without a valid PAYMENT-SIGNATURE -> 402 with a PAYMENT-REQUIRED challenge
        with a verified PAYMENT-SIGNATURE -> 200 with the protected artifact

so a generic x402 client can drive it with no PROMETHEUS-specific knowledge, and
the demo path and the production path are the same path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from ..config import get_config
from ..db import get_db
from ..market.agentos import AgentOSProvider
from ..market.binance import BinanceMarketData
from ..marketplace import Marketplace, PurchaseError
from ..model.registry import all_provider_status, build_provider
from ..payments.x402 import (
    HEADER_PAYMENT,
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    LEGACY_HEADER_PAYMENT,
    encode_payment_required,
    encode_settlement_response,
)
from ..provenance import Status, now_iso
from ..scheduler import Scheduler
from ..signals.engine import SignalEngine, passport, public_preview
from .. import reputation as reputation_mod

STATIC = Path(__file__).resolve().parent.parent / "static"


def _challenge_from_stored_requirement(requirement: dict[str, Any], error: str) -> dict[str, Any]:
    """Rebuild the x402 v2 PaymentRequired object from an invoice row.

    Older persisted invoices contain v1's `maxAmountRequired`; they are kept
    readable and are returned in their original shape instead of being silently
    relabelled as v2.
    """
    version = 1 if "maxAmountRequired" in requirement else 2
    if version == 1:
        return {"x402Version": 1, "error": error, "accepts": [requirement]}
    resource = requirement.get("resource")
    if not isinstance(resource, dict):
        resource = {"url": resource} if resource else {}
    accepts = {k: v for k, v in requirement.items() if k != "resource"}
    return {"x402Version": 2, "error": error, "resource": resource, "accepts": [accepts]}


def create_app() -> FastAPI:
    cfg = get_config()
    db = get_db()
    market = BinanceMarketData(cfg.binance_hosts, cfg.http_timeout_s)
    provider = build_provider(cfg)
    agentos = AgentOSProvider(
        cfg.agentos_binary,
        timeout_s=cfg.http_timeout_s * 3,
        api_env=cfg.agentos_api_env,
        wsl_distro=cfg.agentos_wsl_distro or None,
        wsl_user=cfg.agentos_wsl_user,
    )
    engine = SignalEngine(db, cfg, market, provider, agentos=agentos)
    marketplace = Marketplace(db, cfg, engine)
    scheduler = Scheduler(db, cfg, engine, market)

    app = FastAPI(
        title="PROMETHEUS — Autonomous Signal Economy",
        version="1.0.0",
        description=(
            "A machine-to-machine market intelligence economy. PROMETHEUS observes verified "
            "Binance spot markets, computes deterministic features, generates evidence-validated "
            "predictions, cryptographically freezes them before the outcome is knowable, sells them "
            "over x402, verifies settlement independently, and then scores itself against reality.\n\n"
            "Every route below is a PROMETHEUS route. None is a Binance endpoint."
        ),
    )
    app.state.cfg = cfg
    app.state.db = db
    app.state.market = market
    app.state.agentos = agentos
    app.state.engine = engine
    app.state.marketplace = marketplace
    app.state.scheduler = scheduler

    @app.on_event("startup")
    def _startup() -> None:
        scheduler.start()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        scheduler.stop()
        market.close()

    @app.exception_handler(PurchaseError)
    async def _purchase_error(_: Request, exc: PurchaseError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"error": str(exc), "code": exc.code})

    # ---------------------------------------------------------------- health
    @app.get("/health", tags=["status"])
    def health() -> dict[str, Any]:
        ping = market.ping()
        return {
            "service": "prometheus",
            "version": "1.0.0",
            "status": "ok" if ping["status"] == Status.VERIFIED_LIVE.value else "degraded",
            "checked_at": now_iso(),
            "environment": cfg.environment,
            "market_data": ping,
            "autopilot": cfg.autopilot,
            "scheduler": scheduler.stats,
            "counts": {
                "signals": db.query_one("SELECT COUNT(*) AS n FROM signals")["n"],
                "listed": db.query_one("SELECT COUNT(*) AS n FROM signals WHERE state='LISTED'")["n"],
                "rejections": db.query_one("SELECT COUNT(*) AS n FROM rejections")["n"],
                "purchases": db.query_one("SELECT COUNT(*) AS n FROM purchases")["n"],
                "paid": db.query_one("SELECT COUNT(*) AS n FROM purchases WHERE state='PAID'")["n"],
                "deliveries": db.query_one("SELECT COUNT(*) AS n FROM deliveries")["n"],
                "outcomes": db.query_one("SELECT COUNT(*) AS n FROM outcomes")["n"],
            },
        }

    @app.get("/provider-status", tags=["status"])
    def provider_status() -> dict[str, Any]:
        """Honest status of every external integration.

        Nothing here is described as working unless it has actually been contacted.
        """
        return {
            "checked_at": now_iso(),
            "environment": cfg.environment,
            "market_data": {
                "provider": "binance-spot-public",
                "hosts": cfg.binance_hosts,
                **market.ping(),
                "capabilities": ["market data (read-only)"],
                "not_implemented": [
                    "account data", "balances", "positions", "order placement", "withdrawals",
                ],
                "note": "no API key is held; no authenticated Binance capability exists in this build",
            },
            "model_providers": all_provider_status(cfg),
            "binance_agent_os": agentos.status(
                probe_symbol=cfg.assets[0] if cfg.assets else None
            ),
            "settlement": marketplace.verifier.describe(),
            "x402": {
                "version": cfg.x402_version,
                "scheme": cfg.x402_scheme,
                "network": cfg.x402_network,
                "chain_id": cfg.x402_chain_id,
                "asset": cfg.x402_asset,
                "pay_to": cfg.x402_pay_to,
                "status": Status.VERIFIED_LOCAL.value,
                "note": (
                    "wire protocol implemented from the official x402 v2 specification; "
                    "see DISCOVERY.md section 3"
                ),
            },
        }

    # ----------------------------------------------------------- marketplace
    @app.get("/api/marketplace/signals", tags=["marketplace"])
    def list_signals(
        asset: str | None = Query(None), limit: int = Query(50, ge=1, le=200)
    ) -> dict[str, Any]:
        """Public listings. Contains no protected intelligence."""
        items = marketplace.listings(asset=asset, limit=limit)
        return {
            "count": len(items),
            "generated_at": now_iso(),
            "environment": cfg.environment,
            "signals": items,
            "protected_fields": [
                "direction", "confidence", "thesis", "claims", "evidence", "invalidation",
            ],
            "note": "protected fields are released only after verified x402 settlement",
        }

    @app.get("/api/marketplace/signals/{signal_id}/preview", tags=["marketplace"])
    def preview(signal_id: str) -> dict[str, Any]:
        row = engine.get(signal_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"signal {signal_id} not found")
        return public_preview(row)

    @app.post("/api/marketplace/signals/{signal_id}/purchase", tags=["marketplace"], status_code=402)
    def purchase(
        signal_id: str,
        response: Response,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        """Open a purchase and receive x402 payment requirements.

        Responds 402 by design: nothing has been paid yet.
        """
        result = marketplace.create_purchase(signal_id, idempotency_key=idempotency_key)
        response.status_code = 402
        response.headers[HEADER_PAYMENT_REQUIRED] = encode_payment_required(result["x402"])
        return result

    @app.get("/api/purchases/{purchase_id}", tags=["purchases"])
    def purchase_status(purchase_id: str) -> dict[str, Any]:
        return marketplace.purchase_status(purchase_id)

    @app.get("/api/purchases/{purchase_id}/delivery", tags=["purchases"])
    def delivery(
        purchase_id: str,
        response: Response,
        x_payment: str | None = Header(None, alias=HEADER_PAYMENT),
        legacy_x_payment: str | None = Header(None, alias=LEGACY_HEADER_PAYMENT),
        x_transaction_hash: str | None = Header(None, alias="X-Transaction-Hash"),
    ):
        """The x402-protected resource.

        Without verified settlement this returns 402 and the payment requirements.
        With a verified PAYMENT-SIGNATURE it returns the frozen artifact.
        """
        status = marketplace.purchase_status(purchase_id)

        # Already paid and verified -- serve the artifact, no re-payment.
        if status["state"] == "PAID" and status["verification"] == "VERIFIED":
            artifact = marketplace.deliver(purchase_id)
            return JSONResponse(content=artifact)

        payment_header = x_payment or legacy_x_payment
        if payment_header:
            result = marketplace.submit_payment(
                purchase_id, payment_header, tx_hash=x_transaction_hash
            )
            settlement = result.get("settlement", {})
            if result["state"] == "PAID":
                artifact = marketplace.deliver(purchase_id)
                headers = {
                    HEADER_PAYMENT_RESPONSE: encode_settlement_response(
                        success=True,
                        network=status["network"],
                        payer=settlement.get("payer"),
                        transaction=settlement.get("transaction_hash") or "",
                    )
                }
                return JSONResponse(content=artifact, headers=headers)

            # Payment did not verify. Re-issue the requirements per the spec.
            body = json.loads(
                marketplace.db.query_one(
                    "SELECT requirements_json FROM purchases WHERE purchase_id = ?", (purchase_id,)
                )["requirements_json"]
            )
            challenge = _challenge_from_stored_requirement(
                body, f"Payment failed: {settlement.get('reason', 'verification failed')}"
            )
            return JSONResponse(
                status_code=402,
                content={
                    **challenge,
                    "purchase_state": result["state"],
                    "settlement": settlement,
                },
                headers={
                    HEADER_PAYMENT_REQUIRED: encode_payment_required(challenge),
                    HEADER_PAYMENT_RESPONSE: encode_settlement_response(
                        success=False,
                        network=status["network"],
                        payer=settlement.get("payer"),
                        error_reason=settlement.get("reason", "verification_failed")[:200],
                    )
                },
            )

        # No payment presented.
        body = json.loads(
            marketplace.db.query_one(
                "SELECT requirements_json FROM purchases WHERE purchase_id = ?", (purchase_id,)
            )["requirements_json"]
        )
        challenge = _challenge_from_stored_requirement(
            body, "Payment required to access this resource"
        )
        return JSONResponse(
            status_code=402,
            content={
                **challenge,
                "settlement": marketplace.verifier.describe(),
            },
            headers={HEADER_PAYMENT_REQUIRED: encode_payment_required(challenge)},
        )

    # ------------------------------------------------------------ reputation
    @app.get("/api/reputation", tags=["reputation"])
    def reputation() -> dict[str, Any]:
        return reputation_mod.compute(db, cfg)

    @app.get("/api/signals/{signal_id}/passport", tags=["audit"])
    def public_passport(signal_id: str) -> dict[str, Any]:
        """Full passport, released once the signal has matured.

        Before maturity this is protected: publishing the thesis for free would
        destroy the product. After maturity the prediction is public, which is what
        makes the track record auditable.
        """
        row = engine.get(signal_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"signal {signal_id} not found")
        if row["state"] not in ("MATURED", "SCORED", "VERIFIED", "EXPIRED", "UNRESOLVED"):
            raise HTTPException(
                status_code=402,
                detail=(
                    f"signal {signal_id} has not matured; the full passport is protected until "
                    "the horizon expires, or available immediately via purchase"
                ),
            )
        return passport(row, db)

    @app.get("/api/pipeline", tags=["status"])
    def pipeline() -> dict[str, Any]:
        """Live counts at each stage of the OBSERVE -> SCORE lifecycle.

        Rejections are a first-class stage, not an error bucket: refusing to
        publish is a product outcome and appears in the funnel beside the rest.
        """
        states = {
            r["state"]: r["n"]
            for r in db.query("SELECT state, COUNT(*) AS n FROM signals GROUP BY state")
        }
        rejects = {
            r["reason_code"]: r["n"]
            for r in db.query("SELECT reason_code, COUNT(*) AS n FROM rejections GROUP BY reason_code")
        }
        cor = {"AGREED": 0, "UNAVAILABLE": 0, "DISPUTED": rejects.get("CORROBORATION_FAILED", 0)}
        for row in db.query("SELECT snapshot_json FROM signals"):
            try:
                v = (json.loads(row["snapshot_json"]).get("corroboration") or {}).get("verdict")
            except (ValueError, TypeError):
                continue
            if v in cor:
                cor[v] += 1

        def n(*keys: str) -> int:
            return sum(states.get(k, 0) for k in keys)

        published = sum(states.values())
        return {
            "generated_at": now_iso(),
            "stages": [
                {"key": "observed", "label": "Observed",
                 "count": published + sum(rejects.values())},
                {"key": "rejected", "label": "Refused", "count": sum(rejects.values())},
                {"key": "frozen", "label": "Frozen", "count": published},
                {"key": "listed", "label": "Listed",
                 "count": n("LISTED", "PURCHASED", "DELIVERED")},
                {"key": "sold", "label": "Sold", "count": n("PURCHASED", "DELIVERED")},
                {"key": "delivered", "label": "Delivered", "count": n("DELIVERED")},
                {"key": "awaiting", "label": "Awaiting outcome", "count": n("AWAITING_OUTCOME")},
                {"key": "scored", "label": "Scored",
                 "count": n("MATURED", "SCORED", "VERIFIED")},
            ],
            "states": states,
            "rejections_by_code": rejects,
            "corroboration": cor,
        }

    @app.get("/api/outcomes", tags=["audit"])
    def outcomes(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        """Resolved outcomes joined to the predictions that earned them.

        Every row pairs what was predicted -- frozen and hashed beforehand -- with
        what the market actually did. Losses are listed alongside wins; there is no
        filter parameter that could hide them.
        """
        rows = db.query(
            "SELECT o.outcome_id, o.signal_id, o.resolved_at, o.entry_price, o.exit_price,"
            "       o.raw_return, o.directional, o.resolution_source, o.methodology,"
            "       s.asset, s.horizon, s.direction, s.confidence, s.model_provider,"
            "       s.model_version, s.signal_hash, s.state, s.environment"
            "  FROM outcomes o JOIN signals s ON s.signal_id = o.signal_id"
            " ORDER BY o.resolved_at DESC LIMIT ?",
            (limit,),
        )
        return {"count": len(rows), "outcomes": [dict(r) for r in rows]}

    @app.get("/api/journal", tags=["audit"])
    def journal(limit: int = Query(100, ge=1, le=1000), signal_id: str | None = None) -> dict[str, Any]:
        sql = "SELECT * FROM journal"
        params: list[Any] = []
        if signal_id:
            sql += " WHERE signal_id = ?"
            params.append(signal_id)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        return {
            "entries": [
                {**{k: r[k] for k in r.keys() if k != "payload"}, "payload": json.loads(r["payload"])}
                for r in db.query(sql, params)
            ]
        }

    @app.get("/api/rejections", tags=["audit"])
    def rejections(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        rows = db.query(
            "SELECT rejection_id, created_at, asset, horizon, model_provider, model_version,"
            " reason_code, detail FROM rejections ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return {"count": len(rows), "rejections": [dict(r) for r in rows]}

    @app.get("/api/treasury", tags=["audit"])
    def treasury() -> dict[str, Any]:
        rows = db.query("SELECT * FROM ledger ORDER BY created_at DESC LIMIT 200")
        total = db.query_one(
            "SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) AS t FROM ledger"
            " WHERE kind='SIGNAL_SALE' AND environment <> 'SIMULATION'"
        )["t"]
        simulated = db.query_one(
            "SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) AS t FROM ledger"
            " WHERE kind='SIGNAL_SALE' AND environment='SIMULATION'"
        )["t"]
        return {
            "currency": cfg.price_currency,
            "environment": cfg.environment,
            "gross_revenue": f"{total:.6f}",
            "simulated_authorization_value": f"{simulated:.6f}",
            "entries": [dict(r) for r in rows],
            "note": (
                "gross_revenue excludes SIMULATION authorizations because no funds moved; "
                "see simulated_authorization_value and the environment field for settlement status"
            ),
        }

    # ------------------------------------------------------------- operations
    @app.post("/api/admin/generate", tags=["operations"])
    def force_generate(asset: str = Query(...), horizon: str = Query(...)) -> dict[str, Any]:
        """Generate one signal immediately. Same code path as the scheduler."""
        from ..signals.engine import SignalRejected

        try:
            row = engine.generate(asset, horizon)
        except SignalRejected as exc:
            return {"generated": False, "rejected": True, "code": exc.code.value, "detail": exc.detail}
        return {"generated": True, "signal": public_preview(row)}

    @app.post("/api/admin/resolve", tags=["operations"])
    def force_resolve() -> dict[str, Any]:
        return {"resolved": scheduler.resolve_due()}

    # -------------------------------------------------------------- dashboard
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> HTMLResponse:
        path = STATIC / "dashboard.html"
        if not path.exists():
            return HTMLResponse("<h1>PROMETHEUS</h1><p>Dashboard asset missing.</p>", status_code=200)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    return app


app = create_app()
