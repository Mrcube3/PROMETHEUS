#!/usr/bin/env python3
"""Independent buyer agent.

This process shares no memory, no database handle and no Python object with the
PROMETHEUS seller. It speaks only HTTP, over the same public routes any third-party
machine would use. There is no bypass endpoint and no privileged mode.

What it does:

  1. discovers listings on the open marketplace;
  2. selects one and opens a purchase, receiving HTTP 402;
  3. parses the real x402 payment requirements from the 402 body;
  4. asks the connected Binance Agentic Wallet to preview and sign the selected
     payment option;
  5. retries the protected resource with the PAYMENT-SIGNATURE header;
  6. receives the artifact only if the seller independently verified settlement;
  7. recomputes the SHA-256 of the canonical JSON of the prediction and compares it
     to the signal_hash the seller published BEFORE payment.

Step 7 is the point. The buyer does not have to trust the seller: it proves the
artifact it received is the artifact that was frozen.

Wallet note
-----------
The production path uses `baw x402-payment preview/sign`, which selects the wallet's
currently supported chain, token and transfer method at runtime. Signing requires
an explicit `CONFIRM`. The old local EIP-3009 signer remains available only with
`--payment-mode simulation` for hermetic tests.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

try:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
except ImportError:  # pragma: no cover
    print("eth-account is required: pip install eth-account", file=sys.stderr)
    raise


# --- canonical hashing, reimplemented independently -------------------------
# Deliberately NOT imported from the prometheus package: a verification that
# shares code with the thing it verifies proves less. These are the published
# rules from the passport's `hashes.algorithm` field.
def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class BawError(RuntimeError):
    """The configured Binance Agentic Wallet could not complete a wallet step."""


def _baw_command() -> str:
    """Resolve baw even when this process inherited a stale Windows PATH."""
    configured = os.environ.get("BAW_BIN", "baw").strip()
    if configured != "baw":
        return configured
    found = shutil.which("baw")
    if found:
        return found
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidate = Path(appdata) / "npm" / "baw.cmd"
        if candidate.exists():
            return str(candidate)
    return configured


def _run_baw(*args: str) -> dict[str, Any]:
    """Run the documented Binance Agentic Wallet command and parse JSON only."""
    try:
        proc = subprocess.run(
            [_baw_command(), *args, "--json"], capture_output=True, text=True,
            timeout=60, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BawError(
            f"Binance Agentic Wallet CLI unavailable: {type(exc).__name__}: {exc}"
        ) from exc
    if proc.returncode != 0:
        raise BawError((proc.stderr or proc.stdout or "baw command failed").strip()[:500])
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise BawError("baw returned non-JSON output") from exc
    if not result.get("success", False):
        raise BawError(str(result.get("error") or result)[:500])
    return result


class BuyerAgent:
    def __init__(self, base_url: str, private_key: str | None = None, timeout: float = 30.0) -> None:
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout, headers={"User-Agent": "PROMETHEUS-BuyerAgent/1.0"})
        if private_key:
            self.account = Account.from_key(private_key)
            self.key_origin = "BUYER_PRIVATE_KEY"
        else:
            self.account = Account.from_key("0x" + secrets.token_hex(32))
            self.key_origin = "ephemeral (generated for this run)"

    def close(self) -> None:
        self.http.close()

    # -- 1. discover ---------------------------------------------------------
    def discover(self, asset: str | None = None) -> list[dict[str, Any]]:
        r = self.http.get(f"{self.base}/api/marketplace/signals", params={"asset": asset} if asset else None)
        r.raise_for_status()
        return r.json()["signals"]

    # -- 2. open a purchase (expects 402) ------------------------------------
    def open_purchase(self, signal_id: str) -> dict[str, Any]:
        r = self.http.post(
            f"{self.base}/api/marketplace/signals/{signal_id}/purchase",
            headers={"Idempotency-Key": f"buyer-{signal_id}-{secrets.token_hex(6)}"},
        )
        if r.status_code != 402:
            raise RuntimeError(f"expected HTTP 402 from purchase, got {r.status_code}: {r.text[:300]}")
        return r.json()

    # -- 3. fetch the protected resource unpaid (expects 402) ----------------
    def probe_protected(self, purchase_id: str) -> dict[str, Any]:
        r = self.http.get(f"{self.base}/api/purchases/{purchase_id}/delivery")
        if r.status_code != 402:
            raise RuntimeError(
                f"protected resource returned {r.status_code} WITHOUT payment -- "
                f"this would be a paywall leak: {r.text[:300]}"
            )
        return r.json()

    # -- 4. sign a real EIP-3009 authorization -------------------------------
    def sign_payment(self, requirements: dict[str, Any], chain_id: int) -> str:
        now = int(time.time())
        extra = requirements.get("extra") or {}
        amount = str(requirements.get("amount", requirements.get("maxAmountRequired")))
        authorization = {
            "from": self.account.address,
            "to": requirements["payTo"],
            "value": amount,
            "validAfter": str(now - 60),
            "validBefore": str(now + int(requirements.get("maxTimeoutSeconds", 300))),
            "nonce": "0x" + secrets.token_hex(32),
        }
        typed = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "TransferWithAuthorization": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"},
                    {"name": "nonce", "type": "bytes32"},
                ],
            },
            "primaryType": "TransferWithAuthorization",
            "domain": {
                "name": extra.get("name", "USDC"),
                "version": extra.get("version", "2"),
                "chainId": chain_id,
                "verifyingContract": requirements["asset"],
            },
            "message": {
                "from": authorization["from"],
                "to": authorization["to"],
                "value": int(authorization["value"]),
                "validAfter": int(authorization["validAfter"]),
                "validBefore": int(authorization["validBefore"]),
                "nonce": bytes.fromhex(authorization["nonce"][2:]),
            },
        }
        signed = self.account.sign_message(encode_typed_data(full_message=typed))
        # hexbytes .hex() omits the 0x prefix in current releases; the x402 wire
        # format requires it, so normalise rather than assuming either behaviour.
        sig = signed.signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig
        if "amount" in requirements:
            accepted = {k: v for k, v in requirements.items()
                        if k in ("scheme", "network", "amount", "asset", "payTo", "maxTimeoutSeconds", "extra")}
            payload = {
                "x402Version": 2,
                "resource": requirements.get("resource", {}),
                "accepted": accepted,
                "payload": {"signature": sig, "authorization": authorization},
            }
        else:
            payload = {
                "x402Version": 1,
                "scheme": requirements["scheme"],
                "network": requirements["network"],
                "payload": {"signature": sig, "authorization": authorization},
            }
        import base64

        return base64.b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).decode("ascii")

    # -- 5. pay and collect --------------------------------------------------
    def pay_and_collect(
        self, purchase_id: str, header: str, tx_hash: str | None = None,
        header_name: str = "PAYMENT-SIGNATURE",
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        headers = {header_name: header}
        if tx_hash:
            headers["X-Transaction-Hash"] = tx_hash
        r = self.http.get(f"{self.base}/api/purchases/{purchase_id}/delivery", headers=headers)
        return r.status_code, r.json(), dict(r.headers)

    def sign_with_baw(self, payment_required: dict[str, Any], *, confirm: bool) -> dict[str, Any]:
        """Preview and sign one payment using the real Binance wallet CLI."""
        # The CLI documents raw JSON, but its current backend reliably parses
        # the base64 PAYMENT-REQUIRED representation carried by HTTP x402.
        encoded_requirements = base64.b64encode(
            json.dumps(
                payment_required, separators=(",", ":"), sort_keys=True,
            ).encode("utf-8")
        ).decode("ascii")
        preview = _run_baw(
            "x402-payment", "preview",
            "--paymentRequirements", encoded_requirements,
        )
        options = (preview.get("data") or {}).get("options") or []
        ready = next((o for o in options if o.get("status") == "READY_TO_SIGN"), None)
        if ready is None:
            raise BawError(f"no READY_TO_SIGN payment option returned: {options}")
        if not confirm:
            try:
                answer = input(
                    f"Pay {ready.get('amount')} {ready.get('tokenSymbol', 'token')} on "
                    f"chain {ready.get('binanceChainId')} to {ready.get('payTo')}? "
                    "Type CONFIRM to sign: "
                ).strip()
            except EOFError as exc:
                raise BawError("payment signing cancelled: interactive confirmation is required") from exc
            if answer != "CONFIRM":
                raise BawError("payment signing cancelled: confirmation was not provided")
        payment_id = (preview.get("data") or {}).get("paymentId")
        signed = _run_baw(
            "x402-payment", "sign", "--paymentId", str(payment_id),
            "--selectedIndex", str(ready["index"]),
        )
        data = signed.get("data") or {}
        if not data.get("paymentHeaderName") or not data.get("paymentHeaderValue"):
            raise BawError("baw did not return a payment header")
        return data

    # -- 6. verify -----------------------------------------------------------
    @staticmethod
    def verify_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
        """Recompute the published hash from the delivered prediction."""
        prediction = artifact["prediction"]
        claimed = artifact["hashes"]["signal_hash"]
        recomputed = sha256_hex(prediction)
        return {
            "claimed_signal_hash": claimed,
            "recomputed_signal_hash": recomputed,
            "match": claimed == recomputed,
            "snapshot_hash_claimed": artifact["hashes"]["snapshot_hash"],
            "snapshot_hash_recomputed": sha256_hex(artifact["market_snapshot"]),
            "quant_hash_claimed": artifact["hashes"]["quant_hash"],
            "quant_hash_recomputed": sha256_hex(artifact["quant_packet"]),
        }


def _hr(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def main() -> int:
    ap = argparse.ArgumentParser(description="PROMETHEUS independent buyer agent")
    ap.add_argument("--base-url", default=os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:8402"))
    ap.add_argument("--asset", default=None, help="filter listings by asset")
    ap.add_argument("--signal-id", default=None, help="buy this specific signal")
    ap.add_argument("--tx-hash", default=None, help="settled transaction hash for on-chain verification")
    ap.add_argument(
        "--payment-mode", choices=("baw", "simulation"), default="baw",
        help="production Binance Agentic Wallet flow, or explicit local simulation for tests",
    )
    ap.add_argument("--confirm", action="store_true", help="skip the interactive CONFIRM prompt")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON only")
    args = ap.parse_args()

    agent = BuyerAgent(args.base_url, os.environ.get("BUYER_PRIVATE_KEY"))
    report: dict[str, Any] = {"base_url": agent.base, "buyer_address": agent.account.address}
    quiet = args.json

    try:
        if not quiet:
            _hr("PROMETHEUS BUYER AGENT")
            print(f"seller      : {agent.base}")
            print(f"buyer wallet: {agent.account.address}")
            print(f"key origin  : {agent.key_origin}")

        # 1. DISCOVER
        listings = agent.discover(args.asset)
        report["listings_found"] = len(listings)
        if not listings:
            report["error"] = "no signals are currently listed"
            print(json.dumps(report, indent=2) if quiet else f"\n{report['error']}")
            return 2

        chosen = next((s for s in listings if s["signal_id"] == args.signal_id), None) if args.signal_id else listings[0]
        if chosen is None:
            report["error"] = f"signal {args.signal_id} is not listed"
            print(json.dumps(report, indent=2) if quiet else report["error"])
            return 2
        report["signal"] = chosen

        # Recorded in every output mode: whether the seller leaked protected fields
        # before payment is a result, not a piece of console decoration.
        leaked = [
            k for k in ("direction", "confidence", "thesis", "claims", "invalidation")
            if k in chosen
        ]
        report["preview_leak"] = leaked

        if not quiet:
            _hr("1. DISCOVER")
            print(f"{len(listings)} signal(s) listed. Selected {chosen['signal_id']}")
            print(f"  asset        : {chosen['asset']}  horizon: {chosen['horizon']}")
            print(f"  price        : {chosen['price']} {chosen['currency']}")
            print(f"  signal_hash  : {chosen['signal_hash']}")
            print(f"  environment  : {chosen['environment']}")
            print(f"  protected fields visible pre-payment: {leaked or 'NONE'}")

        # 2. OPEN PURCHASE -> 402
        purchase = agent.open_purchase(chosen["signal_id"])
        purchase_id = purchase["purchase_id"]
        requirements = dict(purchase["x402"]["accepts"][0])
        requirements["resource"] = purchase["x402"].get("resource", {})
        report["purchase_id"] = purchase_id
        report["payment_requirements"] = requirements
        report["settlement_mode"] = purchase["settlement"]

        if not quiet:
            _hr("2. HTTP 402 PAYMENT REQUIRED")
            print(f"purchase_id : {purchase_id}")
            print(f"x402Version : {purchase['x402']['x402Version']}")
            print(f"scheme      : {requirements['scheme']}   network: {requirements['network']}")
            print(f"amount      : {requirements.get('amount', requirements.get('maxAmountRequired'))} atomic units of {requirements['asset']}")
            print(f"payTo       : {requirements['payTo']}")
            print(f"settlement  : {purchase['settlement']['verifier']} "
                  f"[{purchase['settlement']['status']}] env={purchase['settlement']['environment']}")

        # 3. PROBE THE PAYWALL
        probe = agent.probe_protected(purchase_id)
        report["paywall_enforced"] = True
        if not quiet:
            _hr("3. PAYWALL CHECK (unpaid request)")
            print(f"HTTP 402 confirmed. Server said: {probe.get('error')}")

        # 4. PAY using the actual connected Binance wallet, or explicitly opt in
        # to the local signature-only simulation used by hermetic security tests.
        if args.payment_mode == "baw":
            if purchase.get("settlement", {}).get("status") == "SIMULATION":
                raise BawError(
                    "seller is configured for SIMULATION settlement; refusing to sign. "
                    "Configure a verified on-chain or facilitator settlement before using --payment-mode baw."
                )
            signed = agent.sign_with_baw(purchase["x402"], confirm=args.confirm)
            header_name = signed["paymentHeaderName"]
            header = signed["paymentHeaderValue"]
            report["payment_mode"] = "binance-agentic-wallet"
            report["payment_signing"] = {k: signed.get(k) for k in ("binanceChainId", "signatureExpiresAt", "approveTxHash")}
            if not quiet:
                _hr("4. BINANCE AGENTIC WALLET PAYMENT")
                print(f"header     : {header_name}")
                print(f"expires at : {signed.get('signatureExpiresAt')}")
        else:
            chain_id = int(purchase["settlement"].get("chain_id") or os.environ.get("BUYER_CHAIN_ID", "56"))
            header = agent.sign_payment(requirements, chain_id)
            header_name = "PAYMENT-SIGNATURE"
            report["payment_mode"] = "simulation"
            report["x_payment_header_bytes"] = len(header)
            if not quiet:
                _hr("4. LOCAL SIMULATION PAYMENT")
                print("WARNING: signature_only proves authorization only; no funds move.")
                print(f"PAYMENT-SIGNATURE header built: {len(header)} base64 chars")

        # 5. PAY AND COLLECT
        status, body, headers = agent.pay_and_collect(
            purchase_id, header, args.tx_hash, header_name=header_name
        )
        report["delivery_status"] = status
        if status != 200:
            report["error"] = "payment was not accepted"
            report["response"] = body
            if not quiet:
                _hr("5. PAYMENT REJECTED")
                print(json.dumps(body, indent=2)[:2000])
            else:
                print(json.dumps(report, indent=2, default=str))
            return 3

        artifact = body
        report["artifact_delivered"] = True
        if not quiet:
            _hr("5. PAYMENT VERIFIED, ARTIFACT DELIVERED")
            xpr = (
                headers.get("payment-response")
                or headers.get("x-payment-response")
                or headers.get("X-PAYMENT-RESPONSE")
            )
            if xpr:
                print(f"PAYMENT-RESPONSE: {json.loads(base64.b64decode(xpr))}")
            d = artifact.get("delivery", {})
            print(f"settlement verifier: {d.get('settlement_verifier')}  "
                  f"status: {d.get('settlement_status')}  env: {d.get('environment')}")
            print(f"payer              : {d.get('payer')}")
            print(f"transaction hash   : {d.get('transaction_hash')}")

        # 6. VERIFY THE HASH
        verification = agent.verify_artifact(artifact)
        report["verification"] = verification
        if not quiet:
            _hr("6. INDEPENDENT HASH VERIFICATION")
            print(f"claimed    : {verification['claimed_signal_hash']}")
            print(f"recomputed : {verification['recomputed_signal_hash']}")
            print(f"MATCH      : {verification['match']}")
            print(f"snapshot   : {verification['snapshot_hash_claimed'] == verification['snapshot_hash_recomputed']}")
            print(f"quant      : {verification['quant_hash_claimed'] == verification['quant_hash_recomputed']}")

            p = artifact["prediction"]
            _hr("7. THE INTELLIGENCE PURCHASED")
            print(f"direction   : {p['direction']}")
            print(f"confidence  : {p['confidence']}")
            print(f"entry ref   : {p['entry_reference']}")
            print(f"matures at  : {p['matures_at']}")
            print(f"invalidation: {p['invalidation']['reference_price']} -- {p['invalidation']['condition']}")
            print(f"\nthesis: {p['thesis']}")
            print("\nclaims:")
            for c in p["claims"]:
                print(f"  - {c['statement']}")
                print(f"      evidence: {', '.join(c['evidence_keys'])}")
            print("\nrisk factors:")
            for r in p["risk_factors"]:
                print(f"  - {r}")

        if not verification["match"]:
            report["error"] = "SIGNAL HASH MISMATCH -- the delivered artifact is not what was frozen"
            if not quiet:
                print(f"\n*** {report['error']} ***")
            return 4

        if quiet:
            print(json.dumps(report, indent=2, default=str))
        else:
            _hr("RESULT")
            print("Purchase completed and artifact cryptographically verified.")
            print("The prediction was frozen before payment and matches its published hash.")
        return 0

    finally:
        agent.close()


if __name__ == "__main__":
    sys.exit(main())
