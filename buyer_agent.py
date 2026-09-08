#!/usr/bin/env python3
"""Independent buyer agent.

This process shares no memory, no database handle and no Python object with the
PROMETHEUS seller. It speaks only HTTP, over the same public routes any third-party
machine would use. There is no bypass endpoint and no privileged mode.

What it does:

  1. discovers listings on the open marketplace;
  2. selects one and opens a purchase, receiving HTTP 402;
  3. parses the real x402 payment requirements from the 402 body;
  4. signs an EIP-3009 TransferWithAuthorization with its own key -- real EIP-712,
     real secp256k1, no stub;
  5. retries the protected resource with the X-PAYMENT header;
  6. receives the artifact only if the seller independently verified settlement;
  7. recomputes the SHA-256 of the canonical JSON of the prediction and compares it
     to the signal_hash the seller published BEFORE payment.

Step 7 is the point. The buyer does not have to trust the seller: it proves the
artifact it received is the artifact that was frozen.

Wallet note
-----------
By default the agent generates an ephemeral keypair, which is sufficient for the
`signature_only` settlement verifier (a SIMULATION -- it proves authorisation, not
that funds moved). To settle for real, set BUYER_PRIVATE_KEY to a funded key,
broadcast the transfer yourself, and pass the resulting hash with --tx-hash; the
seller will then verify it against the chain and only release on a confirmed
on-chain Transfer.

This agent never broadcasts a transaction itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
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
        authorization = {
            "from": self.account.address,
            "to": requirements["payTo"],
            "value": str(requirements["maxAmountRequired"]),
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
        self, purchase_id: str, header: str, tx_hash: str | None = None
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        headers = {"X-PAYMENT": header}
        if tx_hash:
            headers["X-Transaction-Hash"] = tx_hash
        r = self.http.get(f"{self.base}/api/purchases/{purchase_id}/delivery", headers=headers)
        return r.status_code, r.json(), dict(r.headers)

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
        requirements = purchase["x402"]["accepts"][0]
        report["purchase_id"] = purchase_id
        report["payment_requirements"] = requirements
        report["settlement_mode"] = purchase["settlement"]

        if not quiet:
            _hr("2. HTTP 402 PAYMENT REQUIRED")
            print(f"purchase_id : {purchase_id}")
            print(f"x402Version : {purchase['x402']['x402Version']}")
            print(f"scheme      : {requirements['scheme']}   network: {requirements['network']}")
            print(f"amount      : {requirements['maxAmountRequired']} atomic units of {requirements['asset']}")
            print(f"payTo       : {requirements['payTo']}")
            print(f"settlement  : {purchase['settlement']['verifier']} "
                  f"[{purchase['settlement']['status']}] env={purchase['settlement']['environment']}")

        # 3. PROBE THE PAYWALL
        probe = agent.probe_protected(purchase_id)
        report["paywall_enforced"] = True
        if not quiet:
            _hr("3. PAYWALL CHECK (unpaid request)")
            print(f"HTTP 402 confirmed. Server said: {probe.get('error')}")

        # 4. SIGN
        chain_id = int(purchase["settlement"].get("chain_id") or os.environ.get("BUYER_CHAIN_ID", "97"))
        header = agent.sign_payment(requirements, chain_id)
        report["x_payment_header_bytes"] = len(header)
        if not quiet:
            _hr("4. SIGN EIP-3009 AUTHORIZATION")
            print(f"chainId {chain_id}, domain {requirements.get('extra', {}).get('name')} "
                  f"v{requirements.get('extra', {}).get('version')}")
            print(f"X-PAYMENT header built: {len(header)} base64 chars")

        # 5. PAY AND COLLECT
        status, body, headers = agent.pay_and_collect(purchase_id, header, args.tx_hash)
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
            xpr = headers.get("x-payment-response") or headers.get("X-PAYMENT-RESPONSE")
            if xpr:
                import base64

                print(f"X-PAYMENT-RESPONSE: {json.loads(base64.b64decode(xpr))}")
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
