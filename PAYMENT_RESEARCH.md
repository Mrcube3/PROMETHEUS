# PROMETHEUS payment integration research

## Findings

Binance Agentic Wallet supports paying for x402 resources without the buyer obtaining Binance merchant API credentials. Binance's own hosted B402 merchant service separately requires partner onboarding. These are different roles and requirements. It is therefore incorrect to conclude that all x402 use through Binance Wallet must wait for merchant approval, or that a direct token transfer is the only available alternative.

Research and unsigned HTTP checks were performed on September 8, 2026. No new payment was signed, no settlement request with a payment authorization was submitted, and no wallet funds were moved during this research. Endpoint capability responses establish reachability and advertised support, not successful settlement.

## Binance Wallet and Binance B402

The official wallet reference exposes `x402-payment preview` and `x402-payment sign`. Preview accepts a merchant's payment requirements. Signing returns a `PAYMENT-SIGNATURE` header for the buyer to send back to the merchant. When approval is necessary, the response can also include an approval transaction hash. The reference documents BSC, Base, and Solana, and EIP-3009, Permit2, and SPL-transfer methods. It states that Permit2 approval gas on BSC is sponsored. None of this means the wallet's sign command itself settles the purchase.[1]

Inspection of the installed @binance/agentic-wallet CLI found preview and sign API endpoints and the corresponding commands. Its x402 command registration exposes those two operations; it does not supply a merchant settlement command. This corroborates the documented separation between wallet signing and seller settlement. It does not establish what undocumented backend capabilities might exist.

Binance's merchant API documentation requires registered credentials, RSA request signing, source-IP whitelisting, and an assigned base URL for `/papi/v2/b402/supported`, `/verify`, and `/settle`. Public Bazaar discovery has a separate access model and cannot substitute for those settlement APIs.[2] This access requirement applies to Binance's hosted merchant API, not to the x402 protocol as a whole.

Binance's supported-asset table assigns BSC USDT and USDC to Permit2. It assigns native EIP-3009 support to U and USD1. The prior PROMETHEUS invoice advertised BSC USDT with `assetTransferMethod=eip3009`. That does not match Binance's documented hosted settlement configuration.[3] The wallet skill contains an illustrative USDT EIP-3009 preview example, so there is a documentation conflict: preview acceptance alone cannot establish on-chain support. For Binance settlement, use the merchant API's actual `/supported` response, including its complete `extra` object, as instructed in the quick start.[4]

## Alternative facilitator evidence

| Route | Evidence | What remains unverified |
|---|---|---|
| Binance hosted B402 on BSC | Official documented service; onboarding required | Credentials, assigned endpoint, actual settlement |
| Dexter on BSC | Live `/healthz` and `/supported` returned HTTP 200; BSC exact Permit2 is advertised and the facilitator lists sponsored-approval extensions | A real signed Binance-wallet payment and confirmed settlement |
| AEON on BSC | BNB Chain references AEON; live unauthenticated GET `/supported` returned HTTP 200 and advertised v2 exact on chain 56 | Authentication for valid payment requests; exact compatibility with Binance Wallet signatures; settlement |
| OpenX402 on Base USDC | Provider describes permissionless access; live GET `/supported` returned HTTP 200 with Base USDC EIP-3009 | Merchant eligibility/whitelisting behavior and successful settlement with these wallets |
| Self-hosted x402 | The x402 protocol permits locally operated verification and settlement | Deployed contracts, gas, token and signature compatibility, end-to-end testing |

### AEON

BNB Chain's own ecosystem articles identify AEON as an x402 facilitator on BNB Chain.[5] AEON publishes `https://facilitator.aeon.xyz` and a v2 implementation.[6] A live GET to `/supported`, without credentials, returned v2 exact support for BSC mainnet (56), BSC testnet (97), Base (8453), and other networks.

There is a bounded access conflict. AEON's API guide calls a bearer token required, while its README conditionally attaches authentication only when an API key exists. An empty unsigned POST to `/verify`, without credentials, returned HTTP 500 with `Cannot read properties of undefined (reading 'substring')`. This proves the route exists but proves neither anonymous settlement nor an authentication requirement: validation may run before authentication.

AEON's guide documents a custom authorization for non-EIP-3009 ERC-20 tokens: a facilitator contract, prior ERC-20 approval, and a `tokenTransferWithAuthorization` typed message incorporating the token and `needApprove`. It must not be assumed interchangeable with either native EIP-3009 or Binance's Permit2 method. The public capability response lists network/scheme combinations but omits the EVM token-specific metadata needed to resolve this compatibility question.[6]

### OpenX402

OpenX402's own introduction advertises no signup or API-key requirement and also mentions pay-to whitelisting. Those statements need to be reconciled through the merchant flow before production use.[7] Live `/supported` returned Base mainnet USDC, native EIP-3009, and x402 v2; it did not advertise BSC. An empty unsigned `/verify` request returned HTTP 200 with `isValid:false` and `invalidReason:invalid_payload`, identifying missing payment objects. This is evidence of a publicly accessible validation route, not a successful payment.

Base-USDC is a promising compatibility candidate because Binance Wallet documents that network and method, and native USDC EIP-3009 is closer to the current parser's authorization shape. That is an engineering inference, not an end-to-end verification. It requires confirming both wallets' Base addresses and funding the buyer on Base. Existing BSC USDT cannot be used as Base USDC without an additional funding or conversion operation.

## Findings in PROMETHEUS

PROMETHEUS now accepts both the legacy EIP-3009 `payload.authorization` and the x402 v2
`payload.permit2Authorization` shape. Permit2 recovery uses the canonical Permit2
domain and enforces the canonical exact proxy as spender, the invoice asset, recipient,
amount and validity window. The facilitator adapter posts the standard x402 v2 body to
`/verify` and `/settle`; it does not claim to be Binance's RSA-authenticated merchant API.

The previous description of `facilitator.b402.ai` as Binance's official endpoint was unsupported. Binance's own docs describe a separately provisioned endpoint. Similar B402 naming is insufficient evidence of common ownership or compatibility. Earlier DNS failures do not establish that Binance's hosted service was down.

Changing the facilitator URL alone can leave token-method, signing-domain, approval-contract, authentication, and payload-shape mismatches unresolved. Those errors must be checked before another signature is requested.

## Deadline decision

Keep x402 as the target rather than automatically replacing it with a direct transfer. The
same-day candidate is now Binance Agentic Wallet plus BSC USDT Permit2 through Dexter.
The read-only Binance wallet preview accepted the actual PROMETHEUS invoice and returned
`READY_TO_SIGN`, with the buyer's 0.5 USDT balance, the brother's receive address, and
`needApproveFirst: true`. No signature or funds movement occurred during that preview.

The acceptance criterion is one real purchase through the production purchase endpoint: exact invoice terms, confirmed on-chain settlement, protected artifact delivery, and independent hash verification. Until that succeeds, classify the route as unverified settlement, even if discovery and preview work. Do not claim Binance-hosted settlement when a third-party facilitator is used. A plain token transfer with a receipt should remain separately labeled and requires an explicit scope decision.

## Sources

1. Binance, [Agentic Wallet x402 payment reference](https://github.com/binance/binance-skills-hub/blob/main/skills/binance-web3/binance-agentic-wallet/references/x402-payment.md). Also inspected the installed CLI implementation and local reference.
2. Binance, [Environments and API base URLs](https://developers.binance.com/zh-CN/docs/products/onchainpay-x402/basics/4.base-urls), page last modified September 4, 2026.
3. Binance, [Agentic Payments overview](https://developers.binance.com/zh-CN/docs/products/onchainpay-x402/introduction), page last modified September 7, 2026.
4. Binance, [Quick Start](https://developers.binance.com/zh-CN/docs/products/onchainpay-x402/quick-start), page last modified September 7, 2026.
5. BNB Chain, [BNB Chain AI Agent Landscape](https://www.bnbchain.org/en/blog/bnb-chain-ai-agent-landscape-agents-tools-and-payments).
6. AEON, [Facilitator API guide](https://github.com/AEON-Project/bnb-x402/blob/V2.0/facilitator.md) and [README](https://github.com/AEON-Project/bnb-x402/blob/V2.0/README.md). Live capability probe: [supported endpoint](https://facilitator.aeon.xyz/supported). The README identifies December 2025 as its update period; live behavior was checked separately.
7. OpenX402, [Introduction](https://docs.openx402.ai/). Live capability probe: [supported endpoint](https://facilitator.openx402.ai/supported).
8. x402 Foundation, [Facilitator concepts](https://docs.x402.org/core-concepts/facilitator).
