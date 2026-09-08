# Binance Capability Matrix

Evidence status for the PROMETHEUS build on the development host (`win32`).
Discovery was closed before implementation; uncertain capabilities are isolated
or left unavailable rather than inferred.

| Capability | Status | Evidence | Implementation decision |
|---|---|---|---|
| Binance Spot public market data | `VERIFIED_LIVE` | Live probes in `DISCOVERY.md` section 1 returned successful responses for ping, time, price, book ticker, depth, klines, 24-hour ticker, and exchange info | Use the public REST client for observation and outcome resolution |
| Binance market-data failover | `VERIFIED_LIVE` | `https://data-api.binance.vision/api/v3/ping` returned successfully | Fail over automatically when the primary host is unavailable |
| Binance account, balances, positions, and orders | `UNAVAILABLE` | No Binance API credentials are held by PROMETHEUS | No account or trading operations are implemented or claimed |
| Binance withdrawals | `UNAVAILABLE` | No credentialed wallet or withdrawal capability is present in the selected workflow | Excluded from the autonomous workflow |
| Binance Skills Hub | `VERIFIED_LOCAL` | 19 official skills were installed from `binance/binance-skills-hub`; details are in `DISCOVERY.md` section 2 | Retain as local capability documentation; do not infer provider access from installation alone |
| `binance-cli` Agent OS market probe on this host | `UNAVAILABLE` | Official v2.1.1 releases have no Windows build; WSL has no installed distro | Keep the read-only corroboration adapter; it reports unavailable until a real CLI probe succeeds |
| Agent OS corroboration logic | `VERIFIED_LOCAL` | Tests cover `AGREED`, `DISPUTED`, and `UNAVAILABLE` verdicts | Seal the verdict into the snapshot; refuse disputed observations and record single-witness observations |
| Agent OS authenticated account/trade operations | `NOT_IMPLEMENTED` | Only the unauthenticated ticker-price corroboration command is exposed | No account, order, wallet, or withdrawal path exists in the Agent OS adapter |
| BSC RPC read access | `VERIFIED_LIVE` | `eth_chainId` probes returned chain 97 for testnet and 56 for mainnet | Use read-only receipt verification for the optional on-chain settlement verifier |
| Binance x402 facilitator | `UNVERIFIED` | No public facilitator base URL was confirmed from primary documentation during discovery | Keep the facilitator adapter off by default; never report it as active without a successful provider check |

## Authority and safety notes

- `VERIFIED_LIVE` means a real provider was contacted successfully for the stated
  capability; it does not imply that adjacent capabilities are available.
- The Agent OS integration strips `BINANCE_API_KEY` and `BINANCE_SECRET_KEY` from
  its child environment and only issues the unauthenticated market probe.
- PROMETHEUS holds no Binance API key and never originates withdrawals or trades.
- Full discovery evidence, x402 findings, token-contract checks, and model-provider
  status are recorded in [`DISCOVERY.md`](DISCOVERY.md) and [`AGENT_OS.md`](AGENT_OS.md).
