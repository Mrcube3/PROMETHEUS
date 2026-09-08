# Binance Agent OS and Skills Hub Integration

## What is integrated

| Component | Status | Evidence |
|---|---|---|
| Binance Skills Hub | `VERIFIED_LOCAL` | 19 official skills installed via `npx skills add` |
| `binance-cli` v2.1.1 | `VERIFIED_LIVE` | Official binary, live mainnet probe, `api_env=prod` |
| Corroboration logic | `VERIFIED_LOCAL` | 19 tests covering AGREED / DISPUTED / UNAVAILABLE |

## What it is used for

Not a second data feed. An **independent second witness**.

PROMETHEUS seals a market snapshot into a SHA-256 hash and sells it. If the only
witness to that snapshot is one HTTP client, then a bad response, a stale cache or
a hijacked host silently becomes evidence that gets hashed, sold and defended.

So the official Binance Agent OS CLI — a separate binary, its own transport, its
own TLS stack — is asked the same question, and the verdict is written into the
snapshot **before hashing**.

| Verdict | Meaning | Effect |
|---|---|---|
| `AGREED` | Both sources within tolerance (default 50 bps) | Publish |
| `DISPUTED` | Sources disagree beyond tolerance | **Signal refused**, recorded `CORROBORATION_FAILED` |
| `UNAVAILABLE` | No second witness reachable | Publish, permanently recorded as single-witness |

`UNAVAILABLE` is deliberately distinct from `AGREED`. **An absent witness is never
counted as confirmation.** A missing CLI degrades the product; it does not halt it.

Live example, sealed into a real signal's snapshot:

```json
"corroboration": {
  "verdict": "AGREED",
  "rest_price": "2484.39000000",
  "agent_os_price": "2484.04000000",
  "deviation_bps": "1.4088",
  "tolerance_bps": "50",
  "command": "binance-cli spot ticker-price --symbol ETHUSDT",
  "cli_version": "binance-cli 2.1.1",
  "surface": "binance-agent-os:binance-cli",
  "skill": "binance/binance-skills-hub:skills/binance/binance@2.0.0"
}
```

Because this sits inside the hashed snapshot, a buyer can verify how many
independent sources backed the prediction they paid for.

## Scope — read-only, enforced by tests

* Only the **unauthenticated** `spot ticker-price` command is ever issued.
* `BINANCE_API_KEY` and `BINANCE_SECRET_KEY` are **stripped from the subprocess
  environment**, so a key present in the host process cannot leak to the CLI.
  There is a test that sets both and asserts neither reaches the child.
* The module exposes exactly three public methods: `ticker_price`, `status`,
  `version`. There is no account, order, wallet or withdrawal path to disable,
  because none is written.

PROMETHEUS holds no Binance API key. The Agent OS integration does not change that.

The official skill mandates that production transactions require the user to type
`CONFIRM`. PROMETHEUS sidesteps that requirement entirely by never issuing a
transaction.

## Setup

### Linux / macOS

```bash
npx skills add https://github.com/binance/binance-skills-hub
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/binance/binance-cli/releases/latest/download/binance-cli-installer.sh | sh
```

Then `PROM_AGENTOS_BINARY=binance-cli` and it activates.

### Windows

Upstream ships **no Windows build** of `binance-cli` v2.x — release assets cover
only `x86_64-apple-darwin`, `aarch64-apple-darwin`, `x86_64-unknown-linux-gnu` and
`aarch64-unknown-linux-gnu`. A source build also fails: `binance-cli` pins
`openssl = "0.10"` with no `[features]` section, so `openssl-sys` cannot find an
OpenSSL installation on MSVC and cannot be switched to a vendored or rustls
backend from outside.

The official Linux binary is therefore reached through WSL:

```powershell
wsl --install -d Debian --no-launch
debian.exe install --root
wsl -d Debian -u root -- bash -lc "apt-get update -qq && apt-get install -y -qq curl xz-utils"
wsl -d Debian -u root -- bash -lc "curl --proto '=https' --tlsv1.2 -LsSf https://github.com/binance/binance-cli/releases/latest/download/binance-cli-installer.sh | sh"
```

Note `xz-utils`: the minimal Debian image lacks `xz`, and the installer unpacks a
`.tar.xz`, so the install fails without it.

Then configure the bridge:

```
PROM_AGENTOS_WSL_DISTRO=Debian
PROM_AGENTOS_BINARY=/root/.cargo/bin/binance-cli
```

The provider prefixes commands with `wsl.exe -d <distro> -u root --`. Nothing
downstream changes — the same command string is recorded in the corroboration
record either way.

## Verifying it works

```bash
curl -s http://127.0.0.1:8402/provider-status | python -m json.tool
```

```json
"binance_agent_os": {
  "status": "VERIFIED_LIVE",
  "binary": "/root/.cargo/bin/binance-cli",
  "transport": "wsl:Debian",
  "version": "binance-cli 2.1.1",
  "api_env": "prod",
  "authenticated": false,
  "not_implemented": ["account data", "order placement", "wallet", "withdrawals"],
  "probe": {"symbol": "BTCUSDT", "price": "78632.46000000", "latency_ms": 1323}
}
```

`VERIFIED_LIVE` is only ever reported when an actual probe succeeded — there is a
live security check asserting exactly that.
