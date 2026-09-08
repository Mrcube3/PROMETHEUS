"""x402 v1 protocol types.

Every field name, header name and encoding rule in this module comes from the
official x402 specification files captured during discovery and retained under
`.discovery/`:

  * `specs/transports-v1/http.md`        -- 402 body, X-PAYMENT, X-PAYMENT-RESPONSE
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

# Header names, verbatim from the v1 HTTP transport spec.
HEADER_PAYMENT = "X-PAYMENT"
HEADER_PAYMENT_RESPONSE = "X-PAYMENT-RESPONSE"

X402_VERSION = 1
SCHEME_EXACT = "exact"
ASSET_TRANSFER_METHOD_EIP3009 = "eip3009"


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
    """The X-PAYMENT header could not be decoded into a valid PaymentPayload."""


@dataclass
class PaymentRequirements:
    """One entry of the ``accepts`` array in a 402 response body."""

    scheme: str
    network: str
    maxAmountRequired: str
    asset: str
    payTo: str
    resource: str
    description: str
    mimeType: str = "application/json"
    outputSchema: Any = None
    maxTimeoutSeconds: int = 300
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
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

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PaymentRequirements":
        return cls(
            scheme=d["scheme"],
            network=d["network"],
            maxAmountRequired=str(d["maxAmountRequired"]),
            asset=d["asset"],
            payTo=d["payTo"],
            resource=d["resource"],
            description=d.get("description", ""),
            mimeType=d.get("mimeType", "application/json"),
            outputSchema=d.get("outputSchema"),
            maxTimeoutSeconds=int(d.get("maxTimeoutSeconds", 300)),
            extra=d.get("extra") or {},
        )


def payment_required_body(
    requirements: list[PaymentRequirements], error: str = "Payment required to access this resource"
) -> dict[str, Any]:
    """The exact JSON body served with HTTP 402."""
    return {
        "x402Version": X402_VERSION,
        "error": error,
        "accepts": [r.to_dict() for r in requirements],
    }


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
class PaymentPayload:
    """The decoded X-PAYMENT header."""

    x402Version: int
    scheme: str
    network: str
    signature: str
    authorization: Authorization

    def to_dict(self) -> dict[str, Any]:
        return {
            "x402Version": self.x402Version,
            "scheme": self.scheme,
            "network": self.network,
            "payload": {
                "signature": self.signature,
                "authorization": self.authorization.to_dict(),
            },
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


def decode_payment_header(raw: str) -> PaymentPayload:
    """Decode and structurally validate an X-PAYMENT header.

    Malformed input raises; it never yields a partially trusted payload.
    """
    if not raw or not raw.strip():
        raise MalformedPayment("X-PAYMENT header is empty")
    try:
        decoded = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MalformedPayment(f"X-PAYMENT is not valid base64: {exc}") from exc
    try:
        body = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedPayment(f"X-PAYMENT did not contain valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise MalformedPayment("X-PAYMENT JSON must be an object")

    version = body.get("x402Version")
    if version != X402_VERSION:
        raise MalformedPayment(f"unsupported x402Version {version!r}; this server speaks {X402_VERSION}")

    scheme = body.get("scheme")
    if scheme != SCHEME_EXACT:
        raise MalformedPayment(f"unsupported scheme {scheme!r}; this server accepts {SCHEME_EXACT!r}")

    network = body.get("network")
    if not isinstance(network, str) or not network:
        raise MalformedPayment("network must be a non-empty string")

    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise MalformedPayment("payload must be an object")

    auth = payload.get("authorization")
    if not isinstance(auth, dict):
        raise MalformedPayment("payload.authorization must be an object")

    signature = _require_hex(payload.get("signature"), _HEX_SIG_LEN, "payload.signature")
    return PaymentPayload(
        x402Version=version,
        scheme=scheme,
        network=network,
        signature=signature,
        authorization=Authorization(
            from_=_require_hex(auth.get("from"), _HEX_ADDR_LEN, "authorization.from"),
            to=_require_hex(auth.get("to"), _HEX_ADDR_LEN, "authorization.to"),
            value=_require_uint_str(auth.get("value"), "authorization.value"),
            validAfter=_require_uint_str(auth.get("validAfter"), "authorization.validAfter"),
            validBefore=_require_uint_str(auth.get("validBefore"), "authorization.validBefore"),
            nonce=_require_hex(auth.get("nonce"), _HEX_NONCE_LEN, "authorization.nonce"),
        ),
    )


def encode_settlement_response(
    *, success: bool, network: str, payer: str | None,
    transaction: str = "", error_reason: str | None = None,
) -> str:
    """Build the base64 X-PAYMENT-RESPONSE header value.

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
