# PROMETHEUS — Architecture

## The single design constraint

Every decision in this system answers one question:

> How would a sceptic verify that this agent did not cheat?

Cheating, in a prediction marketplace, means editing the prediction after seeing the
market, cherry-picking which predictions to count, computing a flattering accuracy
from four data points, or delivering something other than what was sold. Each of those
is closed off by construction rather than by policy.

| Attack | Structural defence |
|---|---|
| Rewrite the prediction after the fact | SHA-256 freeze before listing, hash published in the invoice, SQLite trigger rejects the UPDATE |
| Quietly drop the losers | `outcomes` has a UNIQUE constraint per signal and an immutability trigger; the resolution rule is fixed in advance |
| Brag about 3/3 | Reputation returns `INSUFFICIENT_SAMPLE` below the configured minimum; `n` accompanies every figure |
| Deliver a different artifact than sold | Delivery reads the frozen blob; the buyer recomputes the hash with its own code |
| Fabricate supporting evidence | Evidence validator rejects any citation not present and computed |
| Claim payment that did not happen | Delivery gates on database state written only by a server-side verifier |
| Overstate integration maturity | Every subsystem carries a status from a fixed vocabulary; unreached adapters are `ADAPTER_ONLY` |

---

## Layering

```
                    ┌──────────────────────────────────┐
                    │  API  (FastAPI, OpenAPI)         │
                    │  dashboard · marketplace · x402  │
                    └───────────────┬──────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
┌───────▼────────┐        ┌─────────▼─────────┐       ┌─────────▼────────┐
│  Marketplace   │        │  Signal Engine    │       │   Scheduler      │
│  purchases     │        │  generate/freeze  │       │   autonomous     │
│  delivery gate │        │  price/list       │       │   loop           │
└───────┬────────┘        └─────────┬─────────┘       └─────────┬────────┘
        │                           │                           │
┌───────▼────────┐   ┌──────────────▼──────────┐   ┌────────────▼───────┐
│  Payments      │   │  Validator · Passport   │   │  Outcome · Reputation │
│  x402 · verify │   │  Pricing                │   │                     │
└───────┬────────┘   └──────────────┬──────────┘   └────────────┬───────┘
        │                           │                           │
        └───────────────┬───────────┴───────────────────────────┘
                        │
        ┌───────────────▼───────────────────────────────┐
        │  Foundation                                    │
        │  canonical · provenance · config · db          │
        │  market/binance · quant/features · model/*     │
        └────────────────────────────────────────────────┘
```

Dependencies point downward only. The foundation knows nothing about marketplaces.

---

## The freeze

```python
snapshot_hash = sha256(canonical_json(snapshot))
quant_hash    = sha256(canonical_json(quant_packet))
signal_hash   = sha256(canonical_json(immutable_prediction_fields))
```

Canonical form: UTF-8, keys sorted by code point, separators `,` and `:`, no
insignificant whitespace, non-ASCII emitted literally.

**Floats are rejected at serialisation time.** This is the load-bearing decision. A
hash a buyer cannot reproduce in another language proves nothing, and float
repr is not a stable cross-language contract, so decimals travel as strings and
`Decimal` is normalised (`1.50` and `1.5` must not produce different hashes).

Ordering matters: freeze happens **before** pricing and **before** listing. The
artifact is immutable before anyone knows what it costs, let alone how it turns out.

`buyer_agent.py` deliberately reimplements the hash with plain stdlib `json` rather
than importing `prometheus.canonical` — a verification that shares code with the thing
it verifies proves less.

---

## Provenance

Every externally sourced value is wrapped:

```json
{
  "value": "79305.98000000",
  "source": "binance:GET /api/v3/ticker/price[price]",
  "timestamp": "2026-09-08T09:32:11.000Z",
  "retrieved_at": "2026-09-08T09:32:11.412Z",
  "age_ms": 412,
  "freshness": "FRESH",
  "status": "VERIFIED_LIVE",
  "classification": "BINANCE_REPORTED"
}
```

`classification` separates what Binance reported from what PROMETHEUS derived. There is
no code path that turns `None` into `0`: a missing datum is an `UNAVAILABLE` field with
`value: null`, and a signal whose entry reference is `EXPIRED` is rejected rather than
published against stale data.

---

## Model authority boundary

The model returns exactly this shape:

```python
class ModelSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: Direction
    confidence: Decimal            # [0, 1]
    thesis: str
    claims: list[Claim]            # each cites evidence_keys
    risk_factors: list[str]
    invalidation: Invalidation
```

`extra="forbid"` means a model that tries to return `{"price": 999}` fails validation
outright. There is no field through which it can set a price, declare a payment,
score itself, or touch reputation — so those capabilities do not need to be policed at
runtime. They are unrepresentable.

Provider failure policy: retry exactly once, then fail closed. **No provider
substitution** — silently answering with a different model would make
`model_provider` on the passport a lie.

---

## Evidence validation

```
model cites ──▶ exists in quant packet? ──no──▶ UNSUPPORTED_CLAIM   ▶ reject
                        │yes
                        ▼
              actually computed?  ──no──▶ UNAVAILABLE_EVIDENCE      ▶ reject
                        │yes
                        ▼
        invalidation coherent with direction and distance?  ──no──▶ reject
                        │yes
                        ▼
                      accept
```

The two-stage check matters. A key can exist as a concept (`atr_14`) while being
`UNAVAILABLE` for this snapshot because there were too few candles. Citing it is still
unsupported — a claim resting on a feature that could not be computed is not evidence.

Rejections are persisted and served at `/api/rejections`. A marketplace that hides its
rejects is not auditable.

---

## Payment state machine

```
CREATED ──▶ PAYMENT_REQUIRED ──▶ PAYMENT_SUBMITTED ──▶ VERIFYING ──▶ PAID ──▶ REFUNDED
                 ▲                      │                  │
                 └──────────────────────┴──────────────────┘
                        (rejected: retryable while the invoice lives)
                                        │
                                        ├──▶ UNKNOWN   (chain pending — never PAID)
                                        ├──▶ FAILED    (terminal: burned nonce)
                                        └──▶ EXPIRED   (terminal)
```

A rejected verification returns the invoice to `PAYMENT_REQUIRED` so the buyer can
retry with a corrected payload or a mined transaction. `FAILED` is reserved for
genuinely unrecoverable conditions and is terminal. `UNKNOWN` — the chain has not
confirmed — is never treated as paid; it is a separate state precisely so it cannot be
confused with one.

Replay protection: a UNIQUE index on the authorization nonce, checked *before* the
nonce is recorded so a replay is refused with 409 rather than tripping the index.

---

## Delivery gate

Exactly one function releases the artifact:

```python
if purchase["state"] != "PAID" or purchase["verification"] != "VERIFIED":
    raise PurchaseError(..., status=402)
```

Both values are read from the **database**, and are written only after a verifier
returned `verified=True`. Nothing a client sends can reach them. `{"paid": true}` does
not decode into a `PaymentPayload` at all — it fails structural validation before any
verifier is consulted.

Delivery is idempotent through a UNIQUE constraint on `purchase_id` in `deliveries`;
a redelivery returns the stored bytes, so a buyer cannot be sold two different
artifacts for one payment.

---

## Outcome resolution

The rule is published before any signal exists:

> resolution price = close of the first 1-minute Binance candle whose close time is
> at or after the horizon expiry

It never inspects highs or lows and never chooses among candidates — exactly one
candle qualifies. Entry is the frozen `entry_reference`, recorded before the outcome
was knowable.

```
LONG   :  (exit - entry) / entry
SHORT  :  (entry - exit) / entry
NEUTRAL / STAND_DOWN : return recorded, but NOT_SCORED
```

A move below the 2 bps flat threshold is `FLAT`, not a win. The threshold is fixed in
advance so it cannot be tuned to flatter the record. If the price cannot be retrieved
the signal becomes `UNRESOLVED` — never guessed.

---

## Pricing

```
if resolved_signals < MIN_REPUTATION_SAMPLE:
    price = BASE_PRICE                       # BASE_PRICE_INSUFFICIENT_HISTORY
else:
    price = BASE_PRICE × confidence × freshness × accuracy × cohorts × demand
    price = clamp(price, MIN_PRICE, MAX_PRICE)
```

Every multiplier is individually clamped before it is applied, so no single factor can
dominate, and cohort factors stay neutral until the cohort itself passes the sample
threshold. Each quote records its base, every multiplier, the formula version and the
bounds applied, so any listed price can be recomputed from the record.

At cold start the confidence and freshness factors are recorded but **not applied** —
the first prices this agent ever charges must not be a function of a two-sample
accuracy figure.

---

## Concurrency and idempotency

A single SQLite connection behind a re-entrant lock. The workload is a handful of
writes per minute, so contention is irrelevant, and this removes a whole class of
cross-connection WAL visibility bugs.

Idempotency is a table, not a convention: `(scope, key) → stored response`. The INSERT
is the concurrency primitive — whoever wins the unique index defines the answer
everybody else replays. Covers purchase creation, payment verification and delivery.

---

## Extension points

| To change | Touch |
|---|---|
| Use a real LLM | `PROM_MODEL_PROVIDER=anthropic` — one class, nothing else |
| Real on-chain settlement | `PROM_SETTLEMENT_VERIFIER=onchain` + `PROM_X402_PAY_TO` |
| Longer horizons | `PROM_HORIZONS=1H,4H,24H` |
| New features | Add to `quant/features.py`; they become citable automatically |
| Different venue | Implement the `snapshot()` / `price_at_or_after()` pair |

Adding a feature to the quant engine automatically widens what the model may cite,
because the validator derives its allowlist from the packet rather than a hardcoded
list.
