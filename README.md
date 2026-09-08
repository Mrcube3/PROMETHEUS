# PROMETHEUS — Autonomous Signal Economy

**Predict. Sell. Prove. Repeat.**

Binance Agent OS Mini Hackathon — Track A

---

PROMETHEUS is a machine-to-machine intelligence economy. It watches verified Binance
spot markets, computes deterministic features, generates evidence-validated
predictions, **cryptographically freezes them before the outcome is knowable**, sells
them over x402, independently verifies settlement, and then scores itself against
reality — forever.

It does not ask you to trust its analysis. It asks you to check its record:

> Here is exactly what I predicted, hashed before the market moved. Here is the
> evidence I used. Here is what a buyer paid. Here is what actually happened.

---

## The claim this project actually makes

Most "AI trading agent" demos are unfalsifiable: the prediction is shown to you after
the fact, so you cannot tell a good model from a good story. PROMETHEUS is built so
that particular lie is structurally impossible.

Before anything is listed for sale, the prediction is serialised canonically and
hashed. The buyer receives that hash **in the payment invoice, before paying**. After
delivery the buyer recomputes the hash from the artifact, with its own code. If the
seller had altered one character of the thesis, changed the direction, or nudged the
entry price, the hashes would not match.

`buyer_agent.py` does exactly this, in a separate process, and prints both hashes.

---

## Production end-to-end run target

With a connected Binance Agentic Wallet and a configured live settlement verifier,
the same public flow produces output of this shape:

```
1. DISCOVER      2 signal(s) listed. Selected sig_819c739370d0be98565ec8ae
                 price 0.250000 USDT
                 protected fields visible pre-payment: NONE
2. HTTP 402      scheme exact, network eip155:56
                 250000000000000000 atomic units
3. PAYWALL       HTTP 402 confirmed on the protected resource
4. SIGN          Binance Agentic Wallet x402 payment, chainId 56
5. DELIVERED     settlement verifier: facilitator/onchain  env: LIVE
6. VERIFY        claimed    : sha256:95da7c52ee3e237d6ff6584fefe9d9a077202e837b65166cb15e8ed976ad1485
                 recomputed : sha256:95da7c52ee3e237d6ff6584fefe9d9a077202e837b65166cb15e8ed976ad1485
                 MATCH      : True
7. INTELLIGENCE  LONG ETHUSDT, confidence 0.6630, entry 2488.14
                 invalidation 2485.92963595 (1.5x ATR)
```

Test results:

```
python -m pytest tests/ -q     ->  95 passed
python verify_security.py      ->  35/35 checks passed
```

---

## Quick start

The seller can start without credentials. A real buyer payment requires the
Binance Agentic Wallet CLI (`baw`) and a connected, funded wallet. The local
signature-only flow is available only when explicitly requested for tests.

```bash
pip install fastapi uvicorn pydantic httpx eth-account pytest
```

```bash
python -m uvicorn prometheus.api.app:app --host 127.0.0.1 --port 8402
```

Open <http://127.0.0.1:8402> for the dashboard, or <http://127.0.0.1:8402/docs> for
OpenAPI. The autopilot begins generating signals immediately.

Then, in a second terminal, buy one as an independent machine:

```bash
python buyer_agent.py --base-url http://127.0.0.1:8402 --payment-mode baw
```

For a hermetic, non-settling test only:

```bash
python buyer_agent.py --base-url http://127.0.0.1:8402 --payment-mode simulation
```

And attack the running server:

```bash
python verify_security.py --base-url http://127.0.0.1:8402
```

---

## The loop

```
OBSERVE ─→ ANALYZE ─→ PREDICT ─→ VALIDATE ─→ FREEZE ─→ PRICE ─→ OFFER
   ↑                                                               │
   │                                                               ▼
REPRICE ←─ REPUTATION ←─ SCORE ←─ RESOLVE ←─ DELIVER ←─ VERIFY ←─ PAY
```

| Stage | What is authoritative |
|---|---|
| OBSERVE | Binance public spot endpoints, every field provenance-tagged |
| ANALYZE | Deterministic code. RSI, ATR, EMA, realised vol, book imbalance |
| PREDICT | The model provider — advisory only |
| VALIDATE | Evidence validator. Fabricated citation ⇒ signal destroyed |
| FREEZE | SHA-256 over canonical JSON |
| PRICE | Bounded deterministic formula. The model cannot influence it |
| PAY | x402 v2 |
| VERIFY | Server-side settlement verifier |
| DELIVER | Only on `PAID` + `VERIFIED`, read from the database |
| RESOLVE | Binance, via a rule fixed before publication |
| SCORE | Resolved outcomes only |

---

## Design decisions worth defending

### The model is advisory. Code is authoritative.

The model may interpret, argue and decline. It **cannot** compute a feature, set a
price, declare a payment successful, release an artifact, score itself, or edit
history — because the schema it returns has no field for any of those things.

```python
assert "price" not in ModelSignal.model_fields        # tested
assert "reputation" not in ModelSignal.model_fields   # tested
assert "outcome" not in ModelSignal.model_fields      # tested
```

### The evidence validator

Every claim must cite evidence keys that exist **and** were actually computed. A model
that cites `ema_200` when the quant engine never produced one has its signal rejected
outright and recorded in a public `rejections` table. Unsupported claims are never
repaired into sellable intelligence.

The acceptance test from the build directive is implemented literally: five
consecutive valid signals are accepted, then a fabricated key is injected and **must**
be rejected.

### Cold-start pricing is honest

Below 20 resolved signals the price is exactly `BASE_PRICE`, classified
`BASE_PRICE_INSUFFICIENT_HISTORY`. A confidence of 1.0 and a confidence of 0.05
produce the identical price, because with no track record, confidence is just an
assertion. Tested.

### Reputation refuses to flatter itself — twice over

Three correct predictions out of three is a 100% record. PROMETHEUS displays
`INSUFFICIENT_SAMPLE`. Sample size `n` accompanies every figure, everywhere.

Size alone is not enough, and this build proved it. At 27 resolved signals — above
the threshold — it was reporting 88% accuracy. Every one of those calls was `SHORT`,
during a single BTC downtrend. That figure measured the market, not the agent.

So composition is now checked too. A headline stands bare only when the sample can
support it; otherwise it is qualified and the reason is named:

| Caveat | Trigger |
|---|---|
| `SINGLE_DIRECTION_SAMPLE` | ≥80% of scored calls share one direction |
| `SINGLE_ASSET_SAMPLE` | every scored call on one asset |
| `INSUFFICIENT_SAMPLE` | below the configured minimum |

The raw number is never hidden, only qualified. Tested.

### STAND_DOWN is a product outcome

When spread exceeds half of ATR, or the directional score sits inside the neutral
band, the agent declines. Stand-downs are frozen and journaled for the record but
**not listed for sale** — charging for "no edge here" behind a preview that hides
direction would be selling the buyer something they did not agree to.

### History is append-only, enforced by the database

SQLite triggers reject any UPDATE to a frozen signal's payload or hashes, any UPDATE
or DELETE on the journal, and any UPDATE to a recorded outcome. Corrections create new
records. Tested by attempting the forbidden writes.

---

## Payments: what is real, and what is not

This is the part most likely to be overstated in a hackathon, so it is stated flatly.

The **x402 v2 wire protocol is implemented** from the current official specification
and Binance Agentic Wallet reference — the `PAYMENT-REQUIRED` challenge,
`PAYMENT-SIGNATURE` request header, `PAYMENT-RESPONSE` settlement header, CAIP-2
network identifiers, and the `exact` scheme's EIP-3009 and Permit2 payloads.

**Signature verification is real cryptography.** The server reconstructs the EIP-712
`TransferWithAuthorization` or Permit2 `PermitWitnessTransferFrom` typed data, recovers
the signer with secp256k1, and checks that the signed authorization binds the exact
amount, asset, recipient and validity window on the invoice **it** issued. Tampering
with the amount after signing breaks recovery, and the server rejects it. Tested.

Three settlement verifiers exist, and every purchase records which one ruled:

| Verifier | Proves | Status |
|---|---|---|
| `signature_only` | a specific key authorised this exact payment | **`SIMULATION`** |
| `onchain` | a confirmed ERC-20 `Transfer` to `payTo` on BSC | `VERIFIED_TESTNET` / `VERIFIED_LIVE` |
| `facilitator` | whatever a configured facilitator attests | `VERIFIED_LIVE` only after real settlement |

**The default verifier is a SIMULATION and says so** — in the database, the API, the
delivered artifact, the dashboard banner and the buyer agent's output. It proves
authorisation. **It does not prove that funds moved**, because nothing is broadcast.
It is never labelled testnet and never labelled live.

For real settlement, configure the on-chain verifier:

```bash
export PROM_SETTLEMENT_VERIFIER=onchain
export PROM_X402_PAY_TO=0xYourReceiveAddress
python -m uvicorn prometheus.api.app:app
```

For the same-day BSC USDT x402 path, use the public Dexter facilitator and Permit2.
This is a third-party x402 facilitator, not Binance's onboarded merchant API:

```bash
export PROM_SETTLEMENT_VERIFIER=facilitator
export PROM_X402_FACILITATOR_URL=https://x402.dexter.cash
export PROM_X402_ASSET_TRANSFER_METHOD=permit2
export PROM_X402_PAY_TO=0xYourBrotherReceiveAddress
```

The server **refuses to start** in on-chain mode without a real receive address —
verifying payments against the zero address would report settlement that never
happened. The buyer then broadcasts its own transfer and passes the hash; PROMETHEUS
reads the receipt over JSON-RPC, matches the `Transfer` log against `payTo`, asset and
amount, applies a confirmation depth, and only then releases the artifact.

**PROMETHEUS never originates a payment.** It is a merchant. It holds no key, and there
is no withdrawal code path in this repository — not disabled, absent.

A transaction hash is **never synthesised**. Under `signature_only` the field is
`null`, not a plausible-looking hex string.

---

## What is NOT working, stated plainly

| Capability | Status | Why |
|---|---|---|
| Binance Agent OS MCP | `UNAVAILABLE` | No server mounted, no credential in this runtime. **No tool signatures were invented.** |
| Binance account / orders / withdrawals | `UNAVAILABLE` | No API key held. Not implemented, not claimed. |
| On-chain x402 settlement | `VERIFIED_LIVE` capable, **not exercised** | Requires a real merchant address and confirmed settlement transaction. |
| Binance hosted B402 merchant API | `UNAVAILABLE` without partner onboarding | Binance documents credentials, RSA signing, IP allowlisting and a provisioned base URL. |
| Dexter x402 facilitator on BSC | `REACHABLE` | Public `/supported` advertises BSC exact Permit2 and sponsored-approval extensions; a real settlement is still required for `VERIFIED_LIVE`. |
| Binance Agentic Wallet buyer | `ADAPTER_ONLY` until connected and funded | Uses documented `baw x402-payment preview/sign`; the buyer refuses to sign when the seller is still in simulation mode. |
| Anthropic / Ollama providers | `ADAPTER_ONLY` | No key, no reachable endpoint. Adapters shipped, inactive. |

The default model provider is a **deterministic rule engine**, `heuristic /
prometheus-rules-1.0.0`. It is **not an LLM and is never described as one**. It reads
the same features an LLM would and emits the same validated schema, so every
economically meaningful path — validation, freeze, pricing, settlement, delivery,
outcome, reputation — runs identically. Swapping in a real model changes one class.

### Binance Agent OS — a second witness, not decoration

The official `binance-cli` is integrated as an **independent corroboration
surface**. PROMETHEUS seals a market snapshot into a hash and sells it, so a
single-witness snapshot is a liability: a bad response or hijacked host silently
becomes sold evidence. The Agent OS CLI — separate binary, separate transport — is
asked the same question, and the verdict is sealed into the snapshot *before*
hashing:

| Verdict | Effect |
|---|---|
| `AGREED` | Publish |
| `DISPUTED` | **Signal refused**, recorded `CORROBORATION_FAILED` |
| `UNAVAILABLE` | Publish, permanently recorded as single-witness |

`UNAVAILABLE` is deliberately not `AGREED` — an absent witness is never
confirmation. Live, from a real signal: REST `2484.39`, Agent OS `2484.04`,
`1.4088` bps apart, `AGREED`.

Scope is read-only and test-enforced: only the unauthenticated `spot ticker-price`
command is issued, `BINANCE_API_KEY`/`BINANCE_SECRET_KEY` are stripped from the
subprocess environment, and the module has no account, order or withdrawal path to
disable because none is written. **PROMETHEUS holds no Binance API key.**

Full evidence, including live command output, is in [DISCOVERY.md](DISCOVERY.md)
and [AGENT_OS.md](AGENT_OS.md).

---

## API

| Route | Purpose |
|---|---|
| `GET /api/marketplace/signals` | Public listings. No protected fields. |
| `GET /api/marketplace/signals/{id}/preview` | Public preview |
| `POST /api/marketplace/signals/{id}/purchase` | Open a purchase → **402** + requirements |
| `GET /api/purchases/{id}/delivery` | **The x402 resource.** 402 unpaid, artifact when verified |
| `GET /api/purchases/{id}` | Payment state and settlement evidence |
| `GET /api/reputation` | Track record, with `n` |
| `GET /api/signals/{id}/passport` | Full passport, released after maturity |
| `GET /api/journal` | Append-only event log |
| `GET /api/rejections` | Drafts that were refused |
| `GET /api/treasury` | Economic ledger |
| `GET /health`, `GET /provider-status` | Honest integration status |

These are PROMETHEUS routes. **None is a Binance endpoint.**

The protected resource is a single URL that speaks plain x402, so any generic x402
client can drive it with no PROMETHEUS-specific knowledge. The demo path and the
production path are the same path — there is no separate demo mode anywhere in this
repository.

---

## Configuration

Every setting is env-overridable with a `PROM_` prefix. Defaults run with no secrets.

```bash
PROM_ASSETS=BTCUSDT,ETHUSDT,SOLUSDT
PROM_HORIZONS=10M,15M            # also supports 30M, 1H, 4H, 24H
PROM_MODEL_PROVIDER=heuristic    # or anthropic, ollama
PROM_BASE_PRICE=0.25
PROM_MIN_REPUTATION_SAMPLE=20
PROM_SETTLEMENT_VERIFIER=facilitator      # or onchain, signature_only for tests
PROM_X402_FACILITATOR_URL=https://x402.dexter.cash
PROM_X402_ASSET_TRANSFER_METHOD=permit2   # BSC USDT uses Permit2
PROM_X402_PAY_TO=0x...                    # brother's receive-only wallet
```

Short horizons exist so a full lifecycle can be observed in minutes. They are a real
product configuration, not a demo mode — the architecture supports 1H, 4H and 24H by
changing one variable.

---

## Layout

```
prometheus/
  canonical.py        deterministic JSON + SHA-256 (floats rejected)
  provenance.py       Field envelope, freshness, classification
  config.py           configuration + refuse-to-start guards
  db.py               SQLite, immutability triggers, idempotency
  market/binance.py   public spot client, host failover
  quant/features.py   deterministic features + derivations
  model/              provider interface, heuristic, anthropic, ollama
  signals/            schema, evidence validator, engine, passport
  payments/           x402 v2 types, settlement verifiers
  pricing.py          bounded deterministic pricing
  marketplace.py      purchase lifecycle, delivery gate
  outcome.py          resolution against Binance
  reputation.py       outcomes-only scoring + calibration
  scheduler.py        autonomous loop
  api/app.py          FastAPI + OpenAPI
buyer_agent.py        independent buyer, independent hash verification
verify_security.py    live attacks against a running server
tests/                95 tests
DISCOVERY.md          evidence and capability matrix
```

---

## Licence

MIT
