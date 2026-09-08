"""Settlement verification.

Three verifiers, each with an honest and clearly separated claim:

``signature_only``  SIMULATION.
    Performs real EIP-712 / EIP-3009 secp256k1 signature recovery and checks that
    the signed authorization binds the exact amount, receiver, validity window and
    nonce. It proves a specific key authorised a specific payment. It does NOT
    prove funds moved, because nothing was broadcast. Never reported as testnet or
    live, and every artifact it produces carries `SIMULATION`.

``onchain``  VERIFIED_LIVE / VERIFIED_TESTNET.
    Reads the real transaction receipt over JSON-RPC and matches an ERC-20
    Transfer log against payTo, asset and amount, with a confirmation depth. The
    chain, not the buyer, is the authority.

``facilitator``  ADAPTER_ONLY until reached.
    Delegates to a configured x402 facilitator POST /verify and POST /settle.

A buyer assertion is never evidence. There is no code path anywhere in this module
that accepts `{"paid": true}` from a client.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from ..config import (
    VERIFIER_FACILITATOR,
    VERIFIER_ONCHAIN,
    VERIFIER_SIGNATURE_ONLY,
    Config,
)
from ..provenance import Status, now_iso
from .x402 import (
    ASSET_TRANSFER_METHOD_PERMIT2,
    PERMIT2_ADDRESS,
    X402_EXACT_PERMIT2_PROXY_ADDRESS,
    PaymentPayload,
    PaymentRequirements,
)

# keccak256("Transfer(address,address,uint256)")
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

VERIFIER_VERSION = "settlement-verifier-1.0.0"


def _chain_id_for(requirements: PaymentRequirements, configured: int) -> int:
    """Resolve the EIP-712 chain from the invoice when it is explicit.

    Persisted v1 invoices used the label ``bsc-testnet``; keep that legacy
    mapping while v2 uses the authoritative CAIP-2 ``eip155:<id>`` label.
    """
    if requirements.network == "bsc-testnet":
        return 97
    if requirements.network == "bsc":
        return 56
    if requirements.network.startswith("eip155:"):
        try:
            return int(requirements.network.split(":", 1)[1])
        except ValueError:
            pass
    return configured


@dataclass
class VerificationResult:
    """The outcome of one settlement verification attempt."""

    verified: bool
    verifier: str
    status: Status
    environment: str
    reason: str
    payer: str | None = None
    tx_hash: str | None = None
    settlement_status: str | None = None
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "verifier": self.verifier,
            "verifier_version": VERIFIER_VERSION,
            "status": self.status.value,
            "environment": self.environment,
            "reason": self.reason,
            "payer": self.payer,
            # Never a placeholder: null means no transaction hash exists.
            "transaction_hash": self.tx_hash,
            "settlement_status": self.settlement_status,
            "evidence": self.evidence or {},
            "checked_at": now_iso(),
        }


def _eip712_typed_data(
    payload: PaymentPayload, *, chain_id: int, token_name: str, token_version: str, verifying_contract: str
) -> dict[str, Any]:
    """EIP-3009 TransferWithAuthorization typed data.

    Struct and domain are the EIP-3009 / EIP-712 standard forms, which is what the
    x402 `exact` EVM scheme signs.
    """
    if payload.permit2_authorization is not None:
        p = payload.permit2_authorization
        return {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "PermitWitnessTransferFrom": [
                    {"name": "permitted", "type": "TokenPermissions"},
                    {"name": "spender", "type": "address"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "witness", "type": "Witness"},
                ],
                "TokenPermissions": [
                    {"name": "token", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                ],
                "Witness": [
                    {"name": "to", "type": "address"},
                    {"name": "validAfter", "type": "uint256"},
                ],
            },
            "primaryType": "PermitWitnessTransferFrom",
            "domain": {
                "name": "Permit2",
                "chainId": chain_id,
                "verifyingContract": PERMIT2_ADDRESS,
            },
            "message": {
                "permitted": {"token": p.token, "amount": int(p.amount)},
                "spender": p.spender,
                "nonce": int(p.nonce),
                "deadline": int(p.deadline),
                "witness": {"to": p.witness_to, "validAfter": int(p.witness_valid_after)},
            },
        }

    a = payload.authorization
    return {
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
            "name": token_name,
            "version": token_version,
            "chainId": chain_id,
            "verifyingContract": verifying_contract,
        },
        "message": {
            "from": a.from_,
            "to": a.to,
            "value": int(a.value),
            "validAfter": int(a.validAfter),
            "validBefore": int(a.validBefore),
            "nonce": bytes.fromhex(a.nonce[2:]),
        },
    }


def recover_signer(
    payload: PaymentPayload, *, chain_id: int, token_name: str, token_version: str, verifying_contract: str
) -> str:
    """Recover the address that signed the authorization. Real secp256k1 recovery."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    typed = _eip712_typed_data(
        payload,
        chain_id=chain_id,
        token_name=token_name,
        token_version=token_version,
        verifying_contract=verifying_contract,
    )
    signable = encode_typed_data(full_message=typed)
    return Account.recover_message(signable, signature=payload.signature)


def _check_authorization_terms(
    payload: PaymentPayload, requirements: PaymentRequirements, *, now: int
) -> str | None:
    """Return a rejection reason, or None when the terms match the invoice."""
    a = payload.authorization

    if a.to.lower() != requirements.payTo.lower():
        return f"authorization pays {a.to}, but this invoice requires {requirements.payTo}"

    required = int(requirements.maxAmountRequired)
    if int(a.value) != required:
        return f"authorization value {a.value} does not equal the required {required}"

    if payload.network != requirements.network:
        return f"payload network {payload.network!r} does not match invoice network {requirements.network!r}"

    if payload.scheme != requirements.scheme:
        return f"payload scheme {payload.scheme!r} does not match invoice scheme {requirements.scheme!r}"

    transfer_method = requirements.extra.get("assetTransferMethod", "eip3009")
    if transfer_method == ASSET_TRANSFER_METHOD_PERMIT2:
        p = payload.permit2_authorization
        if p is None:
            return "invoice requires Permit2 but payload contains an EIP-3009 authorization"
        if p.token.lower() != requirements.asset.lower():
            return f"permit2 token {p.token} does not match invoice asset {requirements.asset}"
        if p.spender.lower() != X402_EXACT_PERMIT2_PROXY_ADDRESS.lower():
            return (
                f"permit2 spender {p.spender} is not the canonical x402 exact Permit2 proxy "
                f"{X402_EXACT_PERMIT2_PROXY_ADDRESS}"
            )
    elif payload.permit2_authorization is not None:
        return "invoice requires EIP-3009 but payload contains a Permit2 authorization"

    # x402 v2 binds the signed payload to the complete accepted requirement.
    # Without these checks a valid signature for another merchant, asset or
    # amount could be replayed against this resource.
    if payload.accepted is not None:
        accepted = payload.accepted
        if accepted.network != requirements.network:
            return "accepted payment network does not match this invoice"
        if accepted.amount != requirements.amount:
            return "accepted payment amount does not match this invoice"
        if accepted.asset.lower() != requirements.asset.lower():
            return "accepted payment asset does not match this invoice"
        if accepted.payTo.lower() != requirements.payTo.lower():
            return "accepted payment recipient does not match this invoice"
        if accepted.extra.get("assetTransferMethod", "eip3009") != transfer_method:
            return "accepted payment transfer method does not match this invoice"

    valid_after, valid_before = int(a.validAfter), int(a.validBefore)
    if now < valid_after:
        return f"authorization is not yet valid (validAfter {valid_after}, now {now})"
    if now >= valid_before:
        return f"authorization expired (validBefore {valid_before}, now {now})"
    if valid_before <= valid_after:
        return "authorization validity window is empty"

    return None


class SettlementVerifier:
    """Dispatches to the configured verifier."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def describe(self) -> dict[str, Any]:
        kind = self.cfg.settlement_verifier
        if kind == VERIFIER_SIGNATURE_ONLY:
            return {
                "verifier": kind,
                "status": Status.SIMULATION.value,
                "environment": "SIMULATION",
                "chain_id": self.cfg.x402_chain_id,
                "network": self.cfg.x402_network,
                "asset": self.cfg.x402_asset,
                "proves": (
                    "a specific private key cryptographically authorised this exact amount, "
                    "asset, receiver and validity window"
                ),
                "does_not_prove": "that any funds moved on any chain",
            }
        if kind == VERIFIER_ONCHAIN:
            return {
                "verifier": kind,
                "status": (
                    Status.VERIFIED_TESTNET.value if self.cfg.x402_chain_id == 97
                    else Status.VERIFIED_LIVE.value
                ),
                "environment": self.cfg.environment,
                "proves": "an on-chain ERC-20 Transfer to payTo of the required amount, confirmed",
                "does_not_prove": "anything about off-chain intent",
                "rpc": self.cfg.evm_rpc_url,
                "confirmations_required": self.cfg.required_confirmations,
            }
        if kind == VERIFIER_FACILITATOR:
            return {
                "verifier": kind,
                "status": (
                    Status.ADAPTER_ONLY.value if not self.cfg.x402_facilitator_url
                    else Status.UNVERIFIED.value
                ),
                "environment": self.cfg.environment,
                "facilitator_url": self.cfg.x402_facilitator_url or None,
                "proves": "whatever the configured facilitator attests via /verify and /settle",
            }
        return {"verifier": kind, "status": Status.NOT_IMPLEMENTED.value, "environment": "UNKNOWN"}

    def verify(
        self, payload: PaymentPayload, requirements: PaymentRequirements, *, tx_hash: str | None = None
    ) -> VerificationResult:
        kind = self.cfg.settlement_verifier
        if kind == VERIFIER_SIGNATURE_ONLY:
            return self._verify_signature_only(payload, requirements)
        if kind == VERIFIER_ONCHAIN:
            return self._verify_onchain(payload, requirements, tx_hash)
        if kind == VERIFIER_FACILITATOR:
            return self._verify_facilitator(payload, requirements)
        return VerificationResult(
            verified=False, verifier=kind, status=Status.NOT_IMPLEMENTED,
            environment="UNKNOWN", reason=f"settlement verifier {kind!r} is not implemented",
        )

    # -- signature only (SIMULATION) ----------------------------------------
    def _verify_signature_only(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerificationResult:
        now = int(time.time())
        terms_error = _check_authorization_terms(payload, requirements, now=now)
        if terms_error:
            return VerificationResult(
                verified=False, verifier=VERIFIER_SIGNATURE_ONLY, status=Status.SIMULATION,
                environment="SIMULATION", reason=terms_error, payer=payload.authorization.from_,
            )
        try:
            recovered = recover_signer(
                payload,
                chain_id=_chain_id_for(requirements, self.cfg.x402_chain_id),
                token_name=requirements.extra.get("name", self.cfg.x402_asset_name),
                token_version=requirements.extra.get("version", self.cfg.x402_asset_version),
                verifying_contract=requirements.asset,
            )
        except Exception as exc:
            return VerificationResult(
                verified=False, verifier=VERIFIER_SIGNATURE_ONLY, status=Status.SIMULATION,
                environment="SIMULATION", reason=f"signature recovery failed: {type(exc).__name__}: {exc}",
            )

        if recovered.lower() != payload.authorization.from_.lower():
            return VerificationResult(
                verified=False, verifier=VERIFIER_SIGNATURE_ONLY, status=Status.SIMULATION,
                environment="SIMULATION",
                reason=(
                    f"signature recovers to {recovered}, which is not the declared payer "
                    f"{payload.authorization.from_}"
                ),
            )

        return VerificationResult(
            verified=True, verifier=VERIFIER_SIGNATURE_ONLY, status=Status.SIMULATION,
            environment="SIMULATION",
            reason=(
        "payment authorization signature verified and bound to the invoice terms. "
                "SIMULATION: no funds moved on any chain."
            ),
            payer=recovered,
            tx_hash=None,  # no transaction exists, so no hash is reported
            settlement_status="AUTHORIZED_NOT_SETTLED",
            evidence={
                "recovered_signer": recovered,
                "chain_id": _chain_id_for(requirements, self.cfg.x402_chain_id),
                "verifying_contract": requirements.asset,
                "eip712_domain": {
                    "name": requirements.extra.get("name", self.cfg.x402_asset_name),
                    "version": requirements.extra.get("version", self.cfg.x402_asset_version),
                },
                "authorization": payload.authorization.to_dict(),
                "simulation_notice": (
                    "This verifier is a SIMULATION. It is not testnet settlement and not live "
                    "settlement. Configure PROM_SETTLEMENT_VERIFIER=onchain for real settlement."
                ),
            },
        )

    # -- on-chain ------------------------------------------------------------
    def _rpc(self, method: str, params: list[Any]) -> Any:
        resp = httpx.post(
            self.cfg.evm_rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=self.cfg.http_timeout_s,
        )
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"RPC error from {method}: {body['error']}")
        return body.get("result")

    def _verify_onchain(
        self, payload: PaymentPayload, requirements: PaymentRequirements, tx_hash: str | None
    ) -> VerificationResult:
        env = self.cfg.environment
        status = (
            Status.VERIFIED_TESTNET if self.cfg.x402_chain_id == 97 else Status.VERIFIED_LIVE
        )
        if not tx_hash:
            return VerificationResult(
                verified=False, verifier=VERIFIER_ONCHAIN, status=status, environment=env,
                reason="on-chain verification requires a transaction hash; none was supplied",
                payer=payload.authorization.from_,
            )

        now = int(time.time())
        terms_error = _check_authorization_terms(payload, requirements, now=now)
        if terms_error:
            return VerificationResult(
                verified=False, verifier=VERIFIER_ONCHAIN, status=status, environment=env,
                reason=terms_error, payer=payload.authorization.from_, tx_hash=tx_hash,
            )

        try:
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
        except Exception as exc:
            return VerificationResult(
                verified=False, verifier=VERIFIER_ONCHAIN, status=Status.BROKEN, environment=env,
                reason=f"could not read receipt: {type(exc).__name__}: {exc}", tx_hash=tx_hash,
            )

        if receipt is None:
            # Not mined yet, or does not exist. Neither is 'paid'.
            return VerificationResult(
                verified=False, verifier=VERIFIER_ONCHAIN, status=status, environment=env,
                reason="transaction not found or not yet mined",
                tx_hash=tx_hash, settlement_status="PENDING",
                payer=payload.authorization.from_,
            )

        if int(receipt.get("status", "0x0"), 16) != 1:
            return VerificationResult(
                verified=False, verifier=VERIFIER_ONCHAIN, status=status, environment=env,
                reason="transaction reverted on chain", tx_hash=tx_hash,
                settlement_status="REVERTED", payer=payload.authorization.from_,
            )

        # Confirmation depth.
        try:
            head = int(self._rpc("eth_blockNumber", []), 16)
            mined_at = int(receipt["blockNumber"], 16)
            confirmations = max(0, head - mined_at + 1)
        except Exception as exc:
            return VerificationResult(
                verified=False, verifier=VERIFIER_ONCHAIN, status=Status.BROKEN, environment=env,
                reason=f"could not establish confirmation depth: {exc}", tx_hash=tx_hash,
            )

        if confirmations < self.cfg.required_confirmations:
            return VerificationResult(
                verified=False, verifier=VERIFIER_ONCHAIN, status=status, environment=env,
                reason=(
                    f"only {confirmations} confirmation(s); "
                    f"{self.cfg.required_confirmations} required"
                ),
                tx_hash=tx_hash, settlement_status="PENDING", payer=payload.authorization.from_,
            )

        # Find an ERC-20 Transfer log on the required asset, to payTo, of the exact amount.
        required_amount = int(requirements.maxAmountRequired)
        pay_to = requirements.payTo.lower()
        asset = requirements.asset.lower()
        matched: dict[str, Any] | None = None

        for log in receipt.get("logs", []):
            if (log.get("address") or "").lower() != asset:
                continue
            topics = log.get("topics") or []
            if len(topics) < 3 or topics[0].lower() != ERC20_TRANSFER_TOPIC:
                continue
            # topics[1] = from, topics[2] = to, each a left-padded 32-byte address.
            to_addr = "0x" + topics[2][-40:]
            if to_addr.lower() != pay_to:
                continue
            data = log.get("data") or "0x0"
            amount = int(data, 16) if data not in ("0x", "") else 0
            if amount != required_amount:
                continue
            matched = {
                "from": "0x" + topics[1][-40:],
                "to": to_addr,
                "amount": str(amount),
                "log_index": log.get("logIndex"),
                "contract": log.get("address"),
            }
            break

        if matched is None:
            return VerificationResult(
                verified=False, verifier=VERIFIER_ONCHAIN, status=status, environment=env,
                reason=(
                    f"no ERC-20 Transfer log of {required_amount} units of {requirements.asset} "
                    f"to {requirements.payTo} was found in this transaction"
                ),
                tx_hash=tx_hash, settlement_status="NO_MATCHING_TRANSFER",
                payer=payload.authorization.from_,
            )

        # The chain says the payer is whoever the token moved from.
        if matched["from"].lower() != payload.authorization.from_.lower():
            return VerificationResult(
                verified=False, verifier=VERIFIER_ONCHAIN, status=status, environment=env,
                reason=(
                    f"on-chain sender {matched['from']} does not match the declared payer "
                    f"{payload.authorization.from_}"
                ),
                tx_hash=tx_hash, settlement_status="PAYER_MISMATCH",
            )

        return VerificationResult(
            verified=True, verifier=VERIFIER_ONCHAIN, status=status, environment=env,
            reason=(
                f"confirmed ERC-20 Transfer of {required_amount} units to {requirements.payTo} "
                f"with {confirmations} confirmation(s)"
            ),
            payer=matched["from"], tx_hash=tx_hash, settlement_status="SETTLED",
            evidence={
                "chain_id": self.cfg.x402_chain_id,
                "rpc": self.cfg.evm_rpc_url,
                "block_number": int(receipt["blockNumber"], 16),
                "confirmations": confirmations,
                "transfer_log": matched,
            },
        )

    # -- facilitator ---------------------------------------------------------
    def _verify_facilitator(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerificationResult:
        env = self.cfg.environment
        url = self.cfg.x402_facilitator_url
        if not url:
            return VerificationResult(
                verified=False, verifier=VERIFIER_FACILITATOR, status=Status.ADAPTER_ONLY,
                environment=env,
                reason=(
                    "no PROM_X402_FACILITATOR_URL configured; this adapter has never "
                    "contacted a facilitator"
                ),
            )
        body = {
            "x402Version": payload.x402Version,
            "paymentPayload": payload.to_dict(),
            "paymentRequirements": requirements.to_dict(),
        }
        try:
            v = httpx.post(f"{url}/verify", json=body, timeout=self.cfg.http_timeout_s)
            if v.status_code != 200:
                return VerificationResult(
                    verified=False, verifier=VERIFIER_FACILITATOR, status=Status.BROKEN,
                    environment=env, reason=f"facilitator /verify returned HTTP {v.status_code}: {v.text[:200]}",
                )
            vr = v.json()
            if not vr.get("isValid", vr.get("valid", False)):
                return VerificationResult(
                    verified=False, verifier=VERIFIER_FACILITATOR, status=Status.VERIFIED_LIVE,
                    environment=env,
                    reason=f"facilitator rejected the payload: {vr.get('invalidReason') or vr}",
                    payer=payload.authorization.from_, evidence={"verify_response": vr},
                )

            s = httpx.post(f"{url}/settle", json=body, timeout=max(30.0, self.cfg.http_timeout_s))
            if s.status_code != 200:
                return VerificationResult(
                    verified=False, verifier=VERIFIER_FACILITATOR, status=Status.BROKEN,
                    environment=env, reason=f"facilitator /settle returned HTTP {s.status_code}: {s.text[:200]}",
                    evidence={"verify_response": vr},
                )
            sr = s.json()
            if not sr.get("success", False):
                return VerificationResult(
                    verified=False, verifier=VERIFIER_FACILITATOR, status=Status.VERIFIED_LIVE,
                    environment=env, reason=f"facilitator settlement failed: {sr.get('errorReason') or sr}",
                    payer=sr.get("payer") or payload.authorization.from_,
                    tx_hash=sr.get("transaction") or None,
                    settlement_status="FAILED", evidence={"verify_response": vr, "settle_response": sr},
                )

            # Only report a hash the facilitator actually returned.
            tx = sr.get("transaction") or None
            return VerificationResult(
                verified=True, verifier=VERIFIER_FACILITATOR, status=Status.VERIFIED_LIVE,
                environment=env, reason="facilitator verified and settled the payment",
                payer=sr.get("payer") or payload.authorization.from_,
                tx_hash=tx, settlement_status="SETTLED",
                evidence={"verify_response": vr, "settle_response": sr},
            )
        except httpx.HTTPError as exc:
            return VerificationResult(
                verified=False, verifier=VERIFIER_FACILITATOR, status=Status.UNAVAILABLE,
                environment=env, reason=f"facilitator unreachable: {type(exc).__name__}: {exc}",
            )
