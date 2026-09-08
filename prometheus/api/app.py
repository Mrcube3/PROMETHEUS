"""PROMETHEUS HTTP API.

These are PROMETHEUS routes. None of them is a Binance endpoint.

The x402 flow lives on one resource:

    GET /api/purchases/{id}/delivery
        without a valid X-PAYMENT -> 402 with payment requirements
        with a verified X-PAYMENT -> 200 with the protected artifact

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
from ..market.binance import BinanceMarketData
from ..marketplace import Marketplace, PurchaseError
from ..model.registry import all_provider_status, build_provider
from ..payments.x402 import HEADER_PAYMENT, HEADER_PAYMENT_RESPONSE, encode_settlement_response
from ..provenance import Status, now_iso
from ..scheduler import Scheduler
from ..signals.engine import SignalEngine, passport, public_preview
from .. import reputation as reputation_mod

STATIC = Path(__file__).resolve().parent.parent / "static"


def create_app() -> FastAPI:
    cfg = get_config()
    db = get_db()
    market = BinanceMarketData(cfg.binance_hosts, cfg.http_timeout_s)
    provider = build_provider(cfg)
    engine = SignalEngine(db, cfg, market, provider)
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
                    "wire protocol implemented from the official x402 v1 specification; "
                    "see DISCOVERY.md section 3"
                ),
            },
            "binance_agent_os": {
                "status": Status.UNAVAILABLE.value,
                "note": (
                    "no Agent OS MCP server is mounted in this runtime and no credential is "
                    "present; no tool signatures are claimed"
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
        return result

    @app.get("/api/purchases/{purchase_id}", tags=["purchases"])
    def purchase_status(purchase_id: str) -> dict[str, Any]:
        return marketplace.purchase_status(purchase_id)

    @app.get("/api/purchases/{purchase_id}/delivery", tags=["purchases"])
    def delivery(
        purchase_id: str,
        response: Response,
        x_payment: str | None = Header(None, alias=HEADER_PAYMENT),
        x_transaction_hash: str | None = Header(None, alias="X-Transaction-Hash"),
    ):
        """The x402-protected resource.

        Without verified settlement this returns 402 and the payment requirements.
        With a verified X-PAYMENT it returns the frozen artifact.
        """
        status = marketplace.purchase_status(purchase_id)

        # Already paid and verified -- serve the artifact, no re-payment.
        if status["state"] == "PAID" and status["verification"] == "VERIFIED":
            artifact = marketplace.deliver(purchase_id)
            return JSONResponse(content=artifact)

        if x_payment:
            result = marketplace.submit_payment(
                purchase_id, x_payment, tx_hash=x_transaction_hash
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
            return JSONResponse(
                status_code=402,
                content={
                    "x402Version": cfg.x402_version,
                    "error": f"Payment failed: {settlement.get('reason', 'verification failed')}",
                    "accepts": [body],
                    "purchase_state": result["state"],
                    "settlement": settlement,
                },
                headers={
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
        return JSONResponse(
            status_code=402,
            content={
                "x402Version": cfg.x402_version,
                "error": "Payment required to access this resource",
                "accepts": [body],
                "settlement": marketplace.verifier.describe(),
            },
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
            "SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) AS t FROM ledger WHERE kind='SIGNAL_SALE'"
        )["t"]
        return {
            "currency": cfg.price_currency,
            "environment": cfg.environment,
            "gross_revenue": f"{total:.6f}",
            "entries": [dict(r) for r in rows],
            "note": (
                "revenue is booked only against settlement a verifier confirmed; see the "
                "environment field for whether that settlement was simulated or on-chain"
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
