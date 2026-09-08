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

## 2. Binance Agent OS and Skills Hub

**This section supersedes an earlier, over-hasty verdict.** A first pass concluded
"UNAVAILABLE" purely because no MCP server was mounted in this runtime. That was a
statement about the runtime, not about the platform. A proper pass found the real
surface.

### Skills Hub — VERIFIED_LOCAL, installed

Official repository `binance/binance-skills-hub` (1018 stars, pushed 2026-09-03).
Skills are `SKILL.md` files carrying YAML frontmatter.

Installed with the documented command:

```
npx skills add https://github.com/binance/binance-skills-hub
```

19 official skills landed under `.agents/skills/`: `binance` (core CLI skill),
`binance-agentic-wallet`, `binance-trading-signal`, `binance-wallet-tracker`,
`binance-leaderboard`, `crypto-market-rank`, `meme-rush`, `query-token-info`,
`query-token-audit`, `query-address-info`, `trading-signal`, `academy-skill`,
`fiat`, `p2p`, `payment-assistant`, `onchain-pay-open-api`, `square-post`,
`binance-sports-ai-analyzer`, `binance-tokenized-securities-info`.

**Status: `VERIFIED_LOCAL`.**

### Agent OS surface — `binance-cli`

The core skill (`skills/binance/binance/SKILL.md`, v2.0.0, author Binance) documents
the Agent OS surface as the official `binance-cli`, from `github.com/binance/binance-cli`
(public, Rust, v2.1.1).

Command surface verified from `references/spot.md`:

- **Market** — unauthenticated: `ticker-price`, `ticker-book-ticker`, `ticker24hr`,
  `depth`, `klines`, `avg-price`, `agg-trades`, and more.
- **Account** and **Trade** — explicitly marked *auth required*.

Auth contract from `references/auth.md`: `BINANCE_API_KEY`, `BINANCE_SECRET_KEY`,
and `BINANCE_API_ENV` of `prod|testnet|demo` (default `prod`). The skill also
mandates that production transactions require the user to type `CONFIRM`.

### Installation outcome on this host — BLOCKED

| Attempt | Result |
|---|---|
| Official installer (`binance-cli-installer.sh`, 52,871 bytes, sha256 `99b8c7f1…6040`) | **Failed**: `there isn't a download for your platform x86_64-pc-windows-gnu` |
| Release assets for v2.1.1 | Only `aarch64-apple-darwin`, `x86_64-apple-darwin`, `aarch64-unknown-linux-gnu`, `x86_64-unknown-linux-gnu`. **No Windows build exists.** |
| npm `@binance/binance-cli` | Latest is `1.3.0`; the official skill instructs uninstalling any v1.x |
| `cargo install --git … --locked` | **Failed**: `openssl-sys 0.9.116` cannot find an OpenSSL installation for `x86_64-pc-windows-msvc`. `binance-cli` pins `openssl = "0.10"` and exposes no `[features]`, so neither `vendored` nor a rustls backend can be selected from outside |
| WSL | `wsl.exe` present, **no distro installed** |

**Status on this host: `UNAVAILABLE` — upstream packaging gap, not a code defect.**
On Linux or macOS the official installer works and the integration activates with no
code change.

### Implementation decision

`binance-cli` is integrated as an **independent corroboration surface** for market
data, in `prometheus/market/agentos.py`.

Rationale: PROMETHEUS seals a market snapshot into a hash and sells it. If the only
witness to that snapshot is one HTTP client, a bad response or a hijacked host
silently becomes sold evidence. So the Agent OS CLI — a separate binary on its own
transport — is asked the same question, and the verdict is written into the snapshot
*before* hashing.

| Verdict | Meaning | Effect |
|---|---|---|
| `AGREED` | Both sources within tolerance (default 50 bps) | Publish |
| `DISPUTED` | Sources disagree beyond tolerance | **Signal refused**, recorded as `CORROBORATION_FAILED` |
| `UNAVAILABLE` | No second witness | Publish, but the snapshot records that it had a single witness |

`UNAVAILABLE` is deliberately distinct from `AGREED`: an absent witness is never
counted as confirmation, and a missing CLI degrades the product rather than halting it.

Scope is deliberately narrow and enforced by tests:

- only the unauthenticated `spot ticker-price` command is ever issued;
- `BINANCE_API_KEY` / `BINANCE_SECRET_KEY` are **stripped from the subprocess
  environment**, so a key present in the host process cannot leak to the CLI;
- the module exposes exactly three public methods (`ticker_price`, `status`,
  `version`) — there is no account, order, wallet or withdrawal path to disable,
  because none is written.

PROMETHEUS therefore still holds no Binance API key, and the Agent OS integration
does not change that.

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
| GREEN — Binance Skills Hub | `VERIFIED_LOCAL` | 19 official skills installed via `npx skills add` | Installed under `.agents/skills/` |
| RED — `binance-cli` on this host | `UNAVAILABLE` | Upstream ships no Windows build; cargo build blocked on OpenSSL/MSVC | Integrated as corroboration surface; activates unchanged on Linux/macOS |
| GREEN — Agent OS corroboration logic | `VERIFIED_LOCAL` | 19 tests covering AGREED / DISPUTED / UNAVAILABLE | Disputed snapshots are refused publication |
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
