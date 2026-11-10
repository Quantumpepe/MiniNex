# MiniNex multi-chain LIVE rollout – one controlled activation

## User-approved native coin order

BTC → ETH → BNB → SOL → XRP → ADA → TRX

USDC and USDT are **settlement currencies**, not additional speculative coins.
Per-chain token representations, issuers and bridge risks may differ.
Native coins may have separate wallet addresses: do not reuse an Ethereum
address for native BTC, XRP, Cardano, Solana or TRON.

## Central GO LIVE

The backend now maintains a strict seven-network registry and a private,
authenticated `GET /api/live/readiness`. Every asset has 12 mandatory
verification gates; they are ALL false unless implementation and independent
evidence exist. The central operator GO LIVE control is not operational yet.

There must be no bypass based on frontend flags, environment variables,
paper budgets, presence of an API key, or a Privy policy ID alone.

A future operator-only GO LIVE action must:
1. Confirm all **seven** networks' safety gates from independently tested,
   versioned execution adapters; the owner can deliberately choose a staged
   launch only if the scope is explicitly shown.
2. Require a separate strongly authenticated owner action with tamper-evident
   audit logs and explicit confirmation.
3. Never bypass user-specific, revocable Privy signer permissions or per-coin
   budget/transaction limits; one operator switch is NOT user consent.
4. Start/stop eligible LIVE workers with idempotent persistent state and
   on-chain receipt reconciliation. Reconcile unresolved orders before new
   broadcasts. A stop must prevent new entries and clearly distinguish
   liquidation-to-stablecoin from pausing; neither guarantees reversal of
   transactions already sent.
5. Default to OFF on any provider, RPC, wallet, signer, data-quality,
   approval or health failure. Testing and audits are mandatory.

## Planned route adapters (not operational)

| Coin | Native blockchain | Settlement candidates | Major blocker |
|---|---|---|---|
| BTC | Bitcoin mainnet | USDC / USDT via vetted cross-chain partner | Atomic cross-chain settlements, fees, finality, refunds and wallet signer |
| ETH | Ethereum mainnet | Native USDC / USDT ERC-20 | Complete signing, decoded router calldata, approvals, nonce/receipt validation |
| BNB | BNB Smart Chain | Verified BSC USDT asset | EVM chain 56 swap, issuer/token audit and gas |
| SOL | Solana mainnet | Native USDC and verified SPL USDT | Solana wallet, route provider, blockhash and receipts |
| XRP | XRP Ledger | Native XRPL USDC | Issuer and TrustLine checks, wallet signer and DEX fills |
| ADA | Cardano mainnet | Cardano USDCx (not Circle-native USDC) | Cardano-native wallet, eUTXO constraints, DEX + cross-chain USDC/USDT conversion |
| TRX | TRON mainnet | Native TRC-20 USDT | TRON wallet, energy/bandwidth, audited TRC20 execution |

**Important:** Circle ended support for USDC on TRON; do not route a
new USDC-TRC20 trade. Cardano USDCx differs from Circle-native USDC.
BNB Chain has bridged/pegged stablecoins that must be independently
verified before displaying them as usable.

There is NO generalized `transfer`, `approve`, `sign`, `swap` or `activate`
route exposed by this registry. It is a backend implementation contract,
not a claim that seven LIVE trading engines exist.

## Existing modules remain unchanged

BTC/ETH simulated strategies remain separate. The existing ETH quote
preflight, Privy risk review, ETH LIVE risk controls and BTC/ETH order
audit are independent and remain hard-disabled for execution. The
`mininex_live_order_intents` PostgreSQL table currently accepts only BTC
and ETH; widen with an explicit transactionally tested migration and real
chain-specific order models before using the other five engines.

Next implementation steps: finish safe ETH unsigned swap preview and
signer consent, prove ETH receipts/fee accounting on mainnet with tiny
limits, implement native BTC settlement adapter, then BNB/SOL/XRP/ADA,
then TRX, with separate acceptance tests for each network.

## Verification references

- Uniswap current router addresses:
  https://developers.uniswap.org/docs/trading/swapping-api/supported-chains
- Circle USDC XRPL:
  https://www.circle.com/blog/now-available-usdc-on-the-xrpl
- Circle USDCx Cardano:
  https://www.circle.com/blog/usdcx-on-cardano-now-available-via-circle-xreserve
- Circle USDC TRON deprecation:
  https://www.circle.com/blog/circle-is-discontinuing-support-for-usdc-on-the-tron-blockchain
