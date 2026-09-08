"""x402 protocol types.

Every field name, header name and encoding rule in this module comes from the
official x402 specification files captured during discovery and retained under
`.discovery/`:

  * x402 v2 core specification -- PaymentRequired/PaymentPayload/SettlementResponse
  * `specs/schemes/exact/scheme_exact_evm.md` -- exact/EIP-3009 payload shape

Nothing here is invented. See DISCOVERY.md section 3.
"""

from __future__ import annotations

import base64
import binascii
import enum
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

# Header names from the current x402 HTTP transport. The v1 aliases remain
# accepted by the decoder so previously issued invoices can be reconciled, but
# every new invoice is v2.
HEADER_PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
HEADER_PAYMENT = "PAYMENT-SIGNATURE"
HEADER_PAYMENT_RESPONSE = "PAYMENT-RESPONSE"
LEGACY_HEADER_PAYMENT = "X-PAYMENT"
LEGACY_HEADER_PAYMENT_RESPONSE = "X-PAYMENT-RESPONSE"

X402_VERSION = 2
X402_VERSION_V1 = 1
SCHEME_EXACT = "exact"
ASSET_TRANSFER_METHOD_EIP3009 = "eip3009"
ASSET_TRANSFER_METHOD_PERMIT2 = "permit2"

# Canonical x402/Uniswap Permit2 addresses. Permit2 is deployed at the same
# address across supported EVM networks; the exact proxy is the spender that
# binds the witness recipient and prevents a facilitator from redirecting funds.
PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
X402_EXACT_PERMIT2_PROXY_ADDRESS = "0x402085c248EeA27D92E8b30b2C58ed07f9E20001"


class PaymentState(str, enum.Enum):
    CREATED = "CREATED"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    PAYMENT_SUBMITTED = "PAYMENT_SUBMITTED"
    VERIFYING = "VERIFYING"
    PAID = "PAID"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    REFUNDED = "REFUNDED"


# UNKNOWN is deliberately absent from every path that leads to delivery.
TERMINAL_STATES = {PaymentState.PAID, PaymentState.FAILED, PaymentState.EXPIRED, PaymentState.REFUNDED}
DELIVERABLE_STATES = {PaymentState.PAID}

# A rejected payment attempt returns the invoice to PAYMENT_REQUIRED so the buyer
# can retry with a corrected payload or a mined transaction, which is what the x402
# flow expects. FAILED is reserved for genuinely unrecoverable conditions -- a
# burned authorization nonce, for instance -- and is terminal.
ALLOWED_PAYMENT_TRANSITIONS: dict[PaymentState, set[PaymentState]] = {
    PaymentState.CREATED: {PaymentState.PAYMENT_REQUIRED, PaymentState.EXPIRED},
    PaymentState.PAYMENT_REQUIRED: {
        PaymentState.PAYMENT_SUBMITTED, PaymentState.EXPIRED, PaymentState.FAILED,
    },
    PaymentState.PAYMENT_SUBMITTED: {
        PaymentState.VERIFYING, PaymentState.PAYMENT_REQUIRED,
        PaymentState.FAILED, PaymentState.EXPIRED,
    },
    PaymentState.VERIFYING: {
        PaymentState.PAID, PaymentState.PAYMENT_REQUIRED, PaymentState.FAILED,
        PaymentState.UNKNOWN, PaymentState.EXPIRED,
    },
    # An UNKNOWN payment may be re-verified. It may never be assumed paid.
    PaymentState.UNKNOWN: {
        PaymentState.VERIFYING, PaymentState.PAYMENT_REQUIRED,
        PaymentState.FAILED, PaymentState.EXPIRED,
    },
    PaymentState.PAID: {PaymentState.REFUNDED},
    PaymentState.FAILED: set(),
    PaymentState.EXPIRED: set(),
    PaymentState.REFUNDED: set(),
}


class IllegalPaymentTransition(ValueError):
    pass


def assert_payment_transition(current: PaymentState, target: PaymentState) -> None:
    if target not in ALLOWED_PAYMENT_TRANSITIONS.get(current, set()):
        raise IllegalPaymentTransition(
            f"payment state {current.value} -> {target.value} is not permitted"
        )


class MalformedPayment(ValueError):
    """The payment header could not be decoded into a valid PaymentPayload."""


@dataclass
class PaymentRequirements:
    """One entry of the ``accepts`` array in a 402 response body."""

    scheme: str
    network: str
    # Internally retain the old Python attribute name for compatibility with
    # persisted v1 rows. Wire serialization uses v2's `amount` field.
    maxAmountRequired: str
    asset: str
    payTo: str
    resource: str | dict[str, Any]
    description: str
    mimeType: str = "application/json"
    outputSchema: Any = None
    maxTimeoutSeconds: int = 300
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def amount(self) -> str:
        return self.maxAmountRequired

    def resource_info(self) -> dict[str, Any]:
        if isinstance(self.resource, dict):
            return dict(self.resource)
        return {
            "url": self.resource,
            "description": self.description,
            "mimeType": self.mimeType,
        }

    def to_dict(self, *, version: int = X402_VERSION) -> dict[str, Any]:
        if version == X402_VERSION_V1:
            return {
                "scheme": self.scheme,
                "network": self.network,
                "maxAmountRequired": self.maxAmountRequired,
                "asset": self.asset,
                "payTo": self.payTo,
                "resource": self.resource,
                "description": self.description,
                "mimeType": self.mimeType,
                "outputSchema": self.outputSchema,
                "maxTimeoutSeconds": self.maxTimeoutSeconds,
                "extra": self.extra,
            }
        return {
            "scheme": self.scheme,
            "network": self.network,
            "amount": self.amount,
            "asset": self.asset,
            "payTo": self.payTo,
            "maxTimeoutSeconds": self.maxTimeoutSeconds,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PaymentRequirements":
        return cls(
            scheme=d["scheme"],
            network=d["network"],
            maxAmountRequired=str(d.get("amount", d.get("maxAmountRequired"))),
            asset=d["asset"],
            payTo=d["payTo"],
            resource=d.get("resource", ""),
            description=d.get("description", ""),
            mimeType=d.get("mimeType", "application/json"),
            outputSchema=d.get("outputSchema"),
            maxTimeoutSeconds=int(d.get("maxTimeoutSeconds", 300)),
            extra=d.get("extra") or {},
        )


def payment_required_body(
    requirements: list[PaymentRequirements], error: str = "Payment required to access this resource",
    *, version: int = X402_VERSION,
) -> dict[str, Any]:
    """Build a standards-compliant PaymentRequired object."""
    if version == X402_VERSION_V1:
        return {
            "x402Version": X402_VERSION_V1,
            "error": error,
            "accepts": [r.to_dict(version=X402_VERSION_V1) for r in requirements],
        }
    return {
        "x402Version": version,
        "error": error,
        "resource": requirements[0].resource_info() if requirements else {},
        "accepts": [r.to_dict(version=version) for r in requirements],
    }


def encode_payment_required(body: dict[str, Any]) -> str:
    """Encode PaymentRequired for the PAYMENT-REQUIRED header."""
    return base64.b64encode(
        json.dumps(body, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def decode_payment_required(raw: str) -> dict[str, Any]:
    try:
        return json.loads(base64.b64decode(raw.strip(), validate=True).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MalformedPayment(f"PAYMENT-REQUIRED is not valid base64 JSON: {exc}") from exc


@dataclass
class Authorization:
    """EIP-3009 ``transferWithAuthorization`` parameters."""

    from_: str
    to: str
    value: str
    validAfter: str
    validBefore: str
    nonce: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_,
            "to": self.to,
            "value": self.value,
            "validAfter": self.validAfter,
            "validBefore": self.validBefore,
            "nonce": self.nonce,
        }


@dataclass
class Permit2Authorization:
    """Permit2 ``permitWitnessTransferFrom`` parameters in x402 wire shape."""

    token: str
    amount: str
    from_: str
    spender: str
    nonce: str
    deadline: str
    witness_to: str
    witness_valid_after: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "permitted": {"token": self.token, "amount": self.amount},
            "from": self.from_,
            "spender": self.spender,
            "nonce": self.nonce,
            "deadline": self.deadline,
            "witness": {"to": self.witness_to, "validAfter": self.witness_valid_after},
        }


@dataclass
class PaymentPayload:
    """The decoded PAYMENT-SIGNATURE header."""

    x402Version: int
    scheme: str
    network: str
    signature: str
    authorization: Authorization
    permit2_authorization: Permit2Authorization | None = None
    resource: dict[str, Any] | None = None
    accepted: PaymentRequirements | None = None

    def to_dict(self) -> dict[str, Any]:
        authorization = (
            {"permit2Authorization": self.permit2_authorization.to_dict()}
            if self.permit2_authorization is not None
            else {"authorization": self.authorization.to_dict()}
        )
        payload = {
            "signature": self.signature,
            **authorization,
        }
        if self.x402Version == X402_VERSION_V1:
            return {
                "x402Version": self.x402Version,
                "scheme": self.scheme,
                "network": self.network,
                "payload": payload,
            }
        return {
            "x402Version": self.x402Version,
            "resource": self.resource or {},
            "accepted": self.accepted.to_dict(version=X402_VERSION) if self.accepted else {},
            "payload": payload,
        }

    def encode(self) -> str:
        return base64.b64encode(
            json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).decode("ascii")


_HEX_ADDR_LEN = 42
_HEX_SIG_LEN = 132  # 0x + 65 bytes
_HEX_NONCE_LEN = 66  # 0x + 32 bytes


def _require_hex(value: Any, length: int, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != length:
        raise MalformedPayment(f"{label} must be a 0x-prefixed hex string of length {length}")
    try:
        bytes.fromhex(value[2:])
    except ValueError as exc:
        raise MalformedPayment(f"{label} is not valid hex: {exc}") from exc
    return value


def _require_uint_str(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MalformedPayment(f"{label} must be a decimal string")
    try:
        n = int(str(value))
    except ValueError as exc:
        raise MalformedPayment(f"{label} is not an integer: {exc}") from exc
    if n < 0:
        raise MalformedPayment(f"{label} must not be negative")
    return str(n)


def _uint_to_bytes32(value: str) -> str:
    """Canonical bytes32 nonce used as the internal authorization identity."""
    n = int(value)
    if n >= 2**256:
        raise MalformedPayment("permit2Authorization.nonce exceeds uint256")
    return "0x" + n.to_bytes(32, "big").hex()


def decode_payment_header(raw: str) -> PaymentPayload:
    """Decode and structurally validate a PAYMENT-SIGNATURE header.

    Malformed input raises; it never yields a partially trusted payload.
    """
    if not raw or not raw.strip():
        raise MalformedPayment("PAYMENT-SIGNATURE header is empty")
    try:
        decoded = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MalformedPayment(f"PAYMENT-SIGNATURE is not valid base64: {exc}") from exc
    try:
        body = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedPayment(f"PAYMENT-SIGNATURE did not contain valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise MalformedPayment("PAYMENT-SIGNATURE JSON must be an object")

    version = body.get("x402Version")
    if version not in (X402_VERSION_V1, X402_VERSION):
        raise MalformedPayment(f"unsupported x402Version {version!r}; this server speaks {X402_VERSION}")

    accepted = None
    if version == X402_VERSION:
        accepted_raw = body.get("accepted")
        if not isinstance(accepted_raw, dict):
            raise MalformedPayment("accepted must be an object in x402 v2")
        accepted = PaymentRequirements.from_dict(accepted_raw)
        scheme = accepted.scheme
        network = accepted.network
    else:
        scheme = body.get("scheme")
        network = body.get("network")
    if scheme != SCHEME_EXACT:
        raise MalformedPayment(f"unsupported scheme {scheme!r}; this server accepts {SCHEME_EXACT!r}")

    if not isinstance(network, str) or not network:
        raise MalformedPayment("network must be a non-empty string")

    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise MalformedPayment("payload must be an object")

    signature = _require_hex(payload.get("signature"), _HEX_SIG_LEN, "payload.signature")
    auth = payload.get("authorization")
    permit2_raw = payload.get("permit2Authorization")
    if isinstance(auth, dict) and isinstance(permit2_raw, dict):
        raise MalformedPayment("payload must contain only one authorization method")

    permit2_authorization = None
    if isinstance(permit2_raw, dict):
        permitted = permit2_raw.get("permitted")
        witness = permit2_raw.get("witness")
        if not isinstance(permitted, dict):
            raise MalformedPayment("permit2Authorization.permitted must be an object")
        if not isinstance(witness, dict):
            raise MalformedPayment("permit2Authorization.witness must be an object")
        token = _require_hex(permitted.get("token"), _HEX_ADDR_LEN, "permit2Authorization.permitted.token")
        amount = _require_uint_str(permitted.get("amount"), "permit2Authorization.permitted.amount")
        payer = _require_hex(permit2_raw.get("from"), _HEX_ADDR_LEN, "permit2Authorization.from")
        spender = _require_hex(permit2_raw.get("spender"), _HEX_ADDR_LEN, "permit2Authorization.spender")
        nonce = _require_uint_str(permit2_raw.get("nonce"), "permit2Authorization.nonce")
        deadline = _require_uint_str(permit2_raw.get("deadline"), "permit2Authorization.deadline")
        witness_to = _require_hex(witness.get("to"), _HEX_ADDR_LEN, "permit2Authorization.witness.to")
        valid_after = _require_uint_str(witness.get("validAfter"), "permit2Authorization.witness.validAfter")
        permit2_authorization = Permit2Authorization(
            token=token,
            amount=amount,
            from_=payer,
            spender=spender,
            nonce=nonce,
            deadline=deadline,
            witness_to=witness_to,
            witness_valid_after=valid_after,
        )
        authorization = Authorization(
            from_=payer,
            to=witness_to,
            value=amount,
            validAfter=valid_after,
            validBefore=deadline,
            nonce=_uint_to_bytes32(nonce),
        )
    else:
        if not isinstance(auth, dict):
            raise MalformedPayment("payload.authorization must be an object")
        authorization = Authorization(
            from_=_require_hex(auth.get("from"), _HEX_ADDR_LEN, "authorization.from"),
            to=_require_hex(auth.get("to"), _HEX_ADDR_LEN, "authorization.to"),
            value=_require_uint_str(auth.get("value"), "authorization.value"),
            validAfter=_require_uint_str(auth.get("validAfter"), "authorization.validAfter"),
            validBefore=_require_uint_str(auth.get("validBefore"), "authorization.validBefore"),
            nonce=_require_hex(auth.get("nonce"), _HEX_NONCE_LEN, "authorization.nonce"),
        )

    return PaymentPayload(
        x402Version=version,
        scheme=scheme,
        network=network,
        signature=signature,
            authorization=authorization,
            permit2_authorization=permit2_authorization,
            resource=body.get("resource") if isinstance(body.get("resource"), dict) else None,
            accepted=accepted,
        )


def encode_settlement_response(
    *, success: bool, network: str, payer: str | None,
    transaction: str = "", error_reason: str | None = None,
) -> str:
    """Build the base64 PAYMENT-RESPONSE header value.

    ``transaction`` stays an empty string unless a real hash exists. A transaction
    hash is never synthesised.
    """
    body: dict[str, Any] = {
        "success": success,
        "transaction": transaction or "",
        "network": network,
        "payer": payer or "",
    }
    if error_reason:
        body["errorReason"] = error_reason
    return base64.b64encode(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")


def decode_settlement_response(raw: str) -> dict[str, Any]:
    return json.loads(base64.b64decode(raw.strip(), validate=True).decode("utf-8"))


def random_nonce() -> str:
    """A 32-byte EIP-3009 nonce."""
    return "0x" + secrets.token_hex(32)
