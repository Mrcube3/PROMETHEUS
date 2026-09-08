# PROMETHEUS — DISCOVERY REPORT

Discovery window: timeboxed, closed at architecture freeze.
Host: win32, Python 3.11.9, no Docker daemon available.
All evidence below was produced by live commands run from this machine.

---

## 1. Binance market data

Probed directly with `curl` against `https://api.binance.com`.

| Endpoint | Result |
|---|---|
| `GET /api/v3/ping` | `200` |
| `GET /api/v3/time` | `{"serverTime":1788828931786}` |
| `GET /api/v3/ticker/price?symbol=BTCUSDT` | `{"symbol":"BTCUSDT","price":"79276.88000000"}` |
| `GET /api/v3/ticker/bookTicker?symbol=BTCUSDT` | `{"bidPrice":"79276.88000000","bidQty":"5.71880000","askPrice":"79276.89000000","askQty":"0.17354000"}` |
| `GET /api/v3/depth?symbol=BTCUSDT&limit=5` | `lastUpdateId` + `bids`/`asks` arrays returned |
| `GET /api/v3/klines?symbol=BTCUSDT&interval=1m&limit=3` | 12-element kline arrays returned |
| `GET /api/v3/ticker/24hr?symbol=BTCUSDT` | full 24h statistics object returned |
| `GET /api/v3/exchangeInfo?symbol=BTCUSDT` | `rateLimits`: REQUEST_WEIGHT 6000/min, RAW_REQUESTS 300000/5min |

`https://data-api.binance.vision/api/v3/ping` also returned `200` and is wired as an
automatic failover host.

**Status: `VERIFIED_LIVE`.** These are public, unauthenticated market-data endpoints.
PROMETHEUS uses only these. No API key is held, so no account, balance, order or
position capability is claimed anywhere in this build.

### Kline field mapping (verified against the live response above)

Index `0 openTime, 1 open, 2 high, 3 low, 4 close, 5 volume, 6 closeTime, 7 quoteAssetVolume, 8 trades, 9 takerBuyBaseVolume, 10 takerBuyQuoteVolume, 11 ignore`.

---

## 2. Binance Agent OS / MCP

Searched official announcement material for the Binance Agent OS Mini Hackathon
(Track A, 20K USDC; deadline 2026-09-08 23:59 UTC) and the Binance MCP Server.

No Binance Agent OS MCP server is mounted in this runtime and no Agent OS credential
is present in the environment. Tool names, parameters and schemas could therefore not
be verified against a running server.

**Status: `UNAVAILABLE` in this environment.**

**Decision:** PROMETHEUS does not fabricate Agent OS tool signatures. It is built as a
standalone HTTP service that is itself consumable by any MCP/agent host, and it exposes
an OpenAPI document plus a machine-readable marketplace API so an Agent OS client can
drive it without the dashboard. No Agent OS call is claimed as working.

---

## 3. x402

### Protocol source

Official specification files downloaded from the x402 specification repository:

| File | Bytes |
|---|---|
| `specs/transports-v1/http.md` | 5428 |
| `specs/schemes/exact/scheme_exact.md` | 11005 |
| `specs/schemes/exact/scheme_exact_evm.md` | 21131 |

Retained under `.discovery/`. Every protocol detail in this build is copied from those
files — none is invented.

Verified v1 HTTP transport:

- `402 Payment Required` plus JSON body `{x402Version, error, accepts[]}`.
- `accepts[]` entry fields: `scheme`, `network`, `maxAmountRequired`, `asset`, `payTo`,
  `resource`, `description`, `mimeType`, `outputSchema`, `maxTimeoutSeconds`,
  `extra{name,version}`.
- Client retries with request header `X-PAYMENT` = base64(JSON `PaymentPayload`).
- `PaymentPayload` = `{x402Version, scheme, network, payload:{signature, authorization:{from,to,value,validAfter,validBefore,nonce}}}`.
- Server replies with `X-PAYMENT-RESPONSE` = base64(JSON `{success, transaction, network, payer}`);
  failures carry `errorReason`.
- Default `extra.assetTransferMethod` is `eip3009`; `extra.name` / `extra.version` are the
  token's EIP-712 domain name and version.
- Facilitator role is `POST /verify` and `POST /settle`.

### Binance x402

Binance x402 targets BNB Chain, with `eip3009`, `permit2-exact` and `permit2-upto` transfer
methods and USDT/USDC/USD1/U as settlement assets. No public Binance facilitator base URL
was confirmed from primary documentation during the window.
**Status: `UNVERIFIED` — isolated behind a configurable facilitator adapter, off by default.**

### Chain access

| RPC | `eth_chainId` |
|---|---|
| `https://bsc-testnet-dataseed.bnbchain.org` | `0x61` (97, BSC testnet) |
| `https://data-seed-prebsc-1-s1.bnbchain.org:8545` | `0x61` (97) |
| `https://bsc-dataseed.bnbchain.org` | `0x38` (56, BSC mainnet) |

**Status: `VERIFIED_LIVE` (read access).**

### Settlement token — verified by reading the contract, not by trusting a search

Secondary sources gave conflicting BSC-testnet stablecoin addresses. Per the source
hierarchy, that conflict was resolved by querying the chain directly rather than
picking one:

| Address | `eth_getCode` | `name()` | `symbol()` | `decimals()` | `DOMAIN_SEPARATOR()` | `authorizationState()` |
|---|---|---|---|---|---|---|
| `0x66E972502A34A625828C544a1914E8D8cc2A9dE5` | 7763 bytes | `Tether USD` | `USDT` | 18 | absent | absent |
| `0x377533d0e68a22cf180205e9c9ed980f74bc5050` | 1952 bytes | `USDT BSC Token` | `USDT` | 18 | absent | absent |

Both are genuinely deployed ERC-20 contracts. **Neither implements EIP-3009** — no
`DOMAIN_SEPARATOR`, no `authorizationState`.

Consequences, stated rather than papered over:

- The `onchain` verifier works with either, because it matches ERC-20 `Transfer` logs,
  which every ERC-20 emits. This is the real settlement path in this build.
- Gasless EIP-3009 settlement through a facilitator would require a 3009-capable
  token. No such token was verified on BSC testnet in this window, so that path stays
  **`UNVERIFIED`**.
- The first contract is the default `PROM_X402_ASSET` (18 decimals, matching the
  verified `decimals()`), and the EIP-712 domain name/version are configurable so the
  operator can point at a 3009 token when one is confirmed.

### Hard constraint on settlement origination

This agent holds no funded wallet and is not permitted to originate transfers of funds.
PROMETHEUS is therefore built as a merchant/receiver, which is the role the product
actually needs. Three settlement verifiers exist, and every purchase records which one ruled:

| Verifier | What it proves | Status |
|---|---|---|
| `onchain` | Reads the real BSC transaction receipt over JSON-RPC and matches the ERC-20 `Transfer` log against `payTo`, `asset` and amount, with confirmations | `VERIFIED_LIVE` capable (read path proven above) |
| `facilitator` | Delegates to a configured x402 facilitator `/verify` + `/settle` | `ADAPTER_ONLY` until a facilitator URL is configured and reached |
| `signature_only` | Performs real EIP-712 / EIP-3009 secp256k1 signature recovery and checks the authorization binds the exact amount, asset, receiver and validity window — but does not move funds | `SIMULATION` |

`signature_only` is labelled `SIMULATION` in the database, the API, the delivery receipt and
the dashboard. It is never reported as testnet and never as live.

---

## 4. Model provider

No `ANTHROPIC_API_KEY` and no other LLM key is present in the environment; no Ollama
endpoint answered on `localhost:11434`.

- `anthropic` adapter — `ADAPTER_ONLY` (activates on `PROM_MODEL_PROVIDER=anthropic` plus a key)
- `ollama` adapter — `ADAPTER_ONLY` (activates on `PROM_MODEL_PROVIDER=ollama` plus a reachable host)
- `heuristic` provider — `VERIFIED_LOCAL`, the default

The default provider is a deterministic rule engine, versioned `heuristic/prometheus-rules-1.0.0`.
It is not described as an LLM anywhere in the product. It emits the identical validated
schema and cites only real evidence keys, so the evidence validator, freeze, pricing, delivery,
outcome and reputation paths are exercised identically whichever provider is configured.

---

## 5. Environment libraries

Present: `fastapi 0.139.0`, `pydantic 2.13.4`, `httpx 0.28.1`, `uvicorn 0.51.0`,
`eth-account 0.13.7`, `web3 7.16.0`. Absent: `sqlalchemy`, `jinja2` — stdlib `sqlite3` and a
static dashboard are used instead, so the build has no unmet dependency.

---

## 6. CAPABILITY MATRIX

| CAPABILITY | STATUS | EVIDENCE | IMPLEMENTATION DECISION |
|---|---|---|---|
| GREEN — Binance spot market data | `VERIFIED_LIVE` | Live 200s plus bodies, section 1 | Sole market source |
| GREEN — Binance failover host | `VERIFIED_LIVE` | `data-api.binance.vision` 200 | Automatic failover |
| RED — Binance account / orders | `UNAVAILABLE` | No API key held | Not implemented, not claimed |
| RED — Binance Agent OS MCP | `UNAVAILABLE` | No server mounted | No tool signatures invented; OpenAPI exposed instead |
| GREEN — x402 v1 wire protocol | `VERIFIED_LOCAL` | Official specs, section 3 | Implemented verbatim, conformance-tested |
| RED — EIP-3009 settlement token on BSC testnet | `UNVERIFIED` | Contract reads show no `DOMAIN_SEPARATOR` / `authorizationState` | Gasless facilitator path not claimed; `onchain` Transfer-log path used instead |
| GREEN — EIP-3009 / EIP-712 recovery | `VERIFIED_LOCAL` | `eth-account 0.13.7` | Real cryptographic verification |
| GREEN — BSC RPC read access | `VERIFIED_LIVE` | `eth_chainId` 0x61 / 0x38 | On-chain settlement verifier |
| YELLOW — Binance x402 facilitator | `UNVERIFIED` | No primary base URL confirmed | Configurable adapter, off by default |
| RED — Outbound payment origination | `UNAVAILABLE` | No funded wallet; not permitted | Merchant / receiver role only |
| YELLOW — Anthropic model provider | `ADAPTER_ONLY` | No key present | Adapter shipped, inactive |
| YELLOW — Ollama model provider | `ADAPTER_ONLY` | No endpoint answered | Adapter shipped, inactive |
| GREEN — Heuristic model provider | `VERIFIED_LOCAL` | Runs locally | Default provider |
| GREEN — Persistence / scheduler / API | `VERIFIED_LOCAL` | stdlib plus FastAPI | Core runtime |

Discovery closed. Building.
