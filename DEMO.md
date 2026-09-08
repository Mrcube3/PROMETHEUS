# PROMETHEUS — Demo Runbook

Everything below runs the **real application**. There is no demo mode, no fixture
data, and no separate code path. The commands here are the same ones a production
operator would run.

Total runtime: about 12 minutes, most of which is waiting for a real 10-minute
prediction horizon to actually expire.

---

## Setup

```bash
pip install -r requirements.txt
```

```bash
python -m uvicorn prometheus.api.app:app --host 127.0.0.1 --port 8402
```

The agent starts working immediately: it contacts Binance, computes features,
generates predictions, validates them, freezes them and lists them.

Open <http://127.0.0.1:8402>.

---

## Act 1 — "It is really talking to Binance" (30s)

```bash
curl -s http://127.0.0.1:8402/provider-status | python -m json.tool
```

Point at:

- `market_data.status` = `VERIFIED_LIVE`, with the actual host and latency
- `market_data.not_implemented` listing account data, orders and **withdrawals** —
  no API key is held, so those capabilities do not exist in this build
- `binance_agent_os.status` = `UNAVAILABLE` — *"no Agent OS MCP server is mounted in
  this runtime; no tool signatures are claimed"*
- inactive model adapters marked `ADAPTER_ONLY`, not "integrated"

> The honesty here is the feature. Most hackathon entries would call these "integrated".

---

## Act 2 — "The paywall is real" (45s)

```bash
curl -s "http://127.0.0.1:8402/api/marketplace/signals?limit=3" | python -m json.tool
```

Every listing shows asset, horizon, price, model identity, freshness and **three
hashes**. There is no `direction`, no `confidence`, no `thesis`, no `evidence`. Those
are absent by construction — the preview is built from an allowlist, so a new field
cannot leak into it by accident.

Note the `signal_hash`. **The prediction is already frozen.** Nobody can change it now,
including the agent that wrote it.

---

## Act 3 — The machine-to-machine purchase (90s)

This is the centrepiece. A **separate process**, sharing no memory and no database
handle with the seller, buys intelligence over HTTP.

```bash
python buyer_agent.py --base-url http://127.0.0.1:8402
```

Walk through the seven printed stages:

1. **DISCOVER** — reads the open marketplace. `protected fields visible pre-payment: NONE`
2. **HTTP 402 PAYMENT REQUIRED** — real x402 v1: `scheme exact`, `network bsc-testnet`,
   amount in atomic units, `payTo`, and the token's EIP-712 domain
3. **PAYWALL CHECK** — requests the protected resource unpaid, receives 402
4. **SIGN** — builds a genuine EIP-3009 `TransferWithAuthorization` and signs it with
   secp256k1. Real cryptography, not a stub
5. **DELIVERED** — the server verified the signature server-side and released the artifact
6. **INDEPENDENT HASH VERIFICATION** —

   ```
   claimed    : sha256:95da7c52ee3e237d6ff6584fefe9d9a077202e837b65166cb15e8ed976ad1485
   recomputed : sha256:95da7c52ee3e237d6ff6584fefe9d9a077202e837b65166cb15e8ed976ad1485
   MATCH      : True
   ```

   The buyer recomputes the hash **with its own code** — `buyer_agent.py` deliberately
   reimplements canonical hashing with stdlib `json` rather than importing the
   seller's module.
7. **THE INTELLIGENCE** — direction, confidence, thesis, every claim with its cited
   evidence keys, risk factors, and an invalidation level placed at 1.5× ATR

**The line to say out loud:** *"The buyer did not have to trust the seller. It proved
the artifact it received is the artifact that was frozen before it paid."*

---

## Act 4 — Try to break it (90s)

```bash
python verify_security.py --base-url http://127.0.0.1:8402
```

30 live attacks against the running server. Highlights:

- `{"paid": true}` as an `X-PAYMENT` header — rejected; it does not decode into a
  payment payload at all
- amount tampered after signing — rejected, signature no longer recovers to the payer
- recipient tampered after signing — rejected
- replaying a used authorization nonce on a fresh invoice — **409**, an authorization
  buys exactly once
- same `Idempotency-Key` twice — same purchase, not two charges
- redelivery — byte-identical artifact
- full passport before maturity — 402

Expected: `RESULT: 30/30 checks passed`.

---

## Act 5 — "It cannot lie about its evidence" (45s)

```bash
python -m pytest tests/test_prometheus.py::TestEvidenceValidator -v
```

The acceptance test from the build directive, implemented literally:

- five consecutive valid signals are accepted
- a fabricated evidence key (`ema_200`, which the quant engine never computes) is
  injected — **and must be rejected**
- a key that exists but was `UNAVAILABLE` for this snapshot is also rejected
- an unsupported claim is never repaired into something sellable

```bash
curl -s http://127.0.0.1:8402/api/rejections | python -m json.tool
```

Rejected drafts are public. A marketplace that hides its rejects is not auditable.

---

## Act 6 — Reality arrives (2 min, the payoff)

Wait for a 10-minute horizon to expire, then:

```bash
curl -s "http://127.0.0.1:8402/api/journal?limit=200" \
  | python -c "import sys,json;[print(x['payload']) for x in json.load(sys.stdin)['entries'] if x['event']=='OUTCOME_RECORDED']"
```

The agent fetched Binance again, applied the resolution rule fixed *before* it ever
made a prediction — *the close of the first 1-minute candle at or after expiry* — and
recorded what actually happened. The original prediction was not touched: the outcome
is a separate, immutable row.

```bash
curl -s http://127.0.0.1:8402/api/reputation | python -m json.tool
```

**The most important thing on screen:**

```json
"directional_accuracy_display": "INSUFFICIENT_SAMPLE",
"n": 3,
"min_reputation_sample": 20
```

Three correct predictions out of three is a 100% record. PROMETHEUS refuses to display
it. Sample size travels with every figure.

And the pricing consequence:

```json
"pricing_classification": "BASE_PRICE_INSUFFICIENT_HISTORY"
```

Below the sample threshold the price is exactly `BASE_PRICE`. A confidence of 1.0 and
a confidence of 0.05 produce the identical price — with no track record, confidence is
just an assertion.

---

## Act 7 — Immutability (30s)

```bash
python -m pytest tests/test_prometheus.py::TestDatabase -v
```

The database itself refuses to rewrite history: SQLite triggers reject any UPDATE to a
frozen signal's payload or hashes, any UPDATE or DELETE on the journal, and any UPDATE
to a recorded outcome. Not policy — enforced below the application.

---

## The closing line

> PROMETHEUS does not ask you to trust its AI.
>
> It freezes the prediction before the market moves, sells it to a machine that verifies
> the hash independently, waits for reality, scores itself honestly — and refuses to
> report an accuracy it has not earned.

---

## Optional: real on-chain settlement

The default verifier is a `SIMULATION` and is labelled as one everywhere. To settle for
real on BSC:

```bash
export PROM_SETTLEMENT_VERIFIER=onchain
export PROM_X402_PAY_TO=0xYourReceiveAddress
python -m uvicorn prometheus.api.app:app
```

The server **refuses to start** without a real receive address. Then:

```bash
python buyer_agent.py --tx-hash 0xYourBroadcastTransferHash
```

PROMETHEUS reads the receipt over JSON-RPC, matches the ERC-20 `Transfer` log against
`payTo`, asset and amount, applies a confirmation depth, and only then releases the
artifact.

PROMETHEUS never originates a payment. It is a merchant — there is no withdrawal code
path in this repository.

---

## If something goes wrong on stage

| Symptom | Cause | Fix |
|---|---|---|
| `no signals are currently listed` | autopilot has not produced one yet | wait ~20s, or `curl -X POST "http://127.0.0.1:8402/api/admin/generate?asset=BTCUSDT&horizon=10M"` |
| Buyer selects a STAND_DOWN | not possible — stand-downs are never listed | — |
| Outcomes still empty | horizon has not expired | check `matures_at`; force with `curl -X POST http://127.0.0.1:8402/api/admin/resolve` |
| Binance unreachable | network / region | `/health` shows the real error; the client fails over to `data-api.binance.vision` automatically |
