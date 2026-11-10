# MiniNex multi-chain LIVE rollout – one controlled activation

## User-approved native coin order

BTC → ETH → BNB → POL → SOL → XRP → ADA → TRX

USDC and USDT are **settlement currencies**, not additional speculative coins.
Per-chain token representations, issuers and bridge risks may differ.\nOn Polygon PoS, POL is native gas; Circle-native USDC is distinct from USDC.e.
Native coins may have separate wallet addresses: do not reuse an Ethereum
address for native BTC, XRP, Cardano, Solana or TRON.

## Compact eight-coin UI target

The next visual rollout should use two compact rows of four coins, or a
fixed-height scroll area with hidden scrollbars on small screens. Wallet
balances open on tap; selecting a coin opens only that asset's trading
settings, and changing values never collapses it. Keep the existing
BTC/ETH paper controls functional until their additional native
wallet adapters are tested. Showing a new coin in the registry must
not be mistaken for a real balance, live price or executable trade.

## Central GO LIVE

The backend now maintains a strict eight-network registry and a private,
authenticated `GET /api/live/readiness`. Every asset has 12 mandatory
verification gates; they are ALL false unless implementation and independent
evidence exist. The central operator GO LIVE control is not operational yet.

There must be no bypass based on frontend flags, environment variables,
paper budgets, presence of an API key, or a Privy policy ID alone.

A future operator-only GO LIVE action must:
1. Confirm all **eight** networks' safety gates from independently tested,
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
| POL | Polygon PoS | Circle-native USDC (not USDC.e) | EVM chain 137, POL gas, independent Polygon token and router verification |
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

## BTC stage 1: independent native BTC indicative cross-chain quote

The authenticated `POST /api/live/btc/quote` supports:

```json
{"side":"SELL","stable":"USDC","amount":"0.1"}
```

- A SELL gets a **native Bitcoin** BTC.BTC → ETH.USDC / ETH.USDT
  indicative THORChain quote, not an Ethereum wrapped-BTC token.
- A BUY reverses the flow: ETH.USDC / ETH.USDT → BTC.BTC.
- Amounts are validated exactly at Bitcoin's 8 decimal places or the
  selected stablecoin's 6 decimal places, then converted to the THORChain
  public quote format (always 1e8 units).
- The server verifies **both** the user's BTC wallet and Ethereum
  settlement wallet from Privy and checks independently verified source
  balances and native-gas reserves.
- It uses the documented, read-only public
  `https://swap.thorchain.org/mcp` `get_swap_quote` method with no
  `destination`. Therefore it never obtains or exposes a usable
  deposit address or memo; no transaction can be constructed or sent.
- Output is indicative, not minimum received. High quote price impact,
  missing amounts, unexpected assets, executable deposit instructions
  and uncertain provider responses fail closed.

The public MCP has a per-IP rate limit (60 requests / 10 minutes), so it
is suitable only for validation, NOT automated production quoting. Before
LIVE BTC: an accountable production aggregator contract, quote expiry,
memos/refunds, UTXO selection and chain confirmation depth, user-owned
Privy native BTC signing, approvals on the ERC20 ETH side, actual
minimum-out and network cost checks, and reconciled multi-chain receipt
tracking are mandatory. BTC may remain on a different network from
USDC/USDT between trades; MiniNex must explicitly track BOTH balances.

Official route docs:
- https://swap.thorchain.org/developers/quickstart
- https://swap.thorchain.org/developers/mcp

## Existing modules remain unchanged

BTC/ETH simulated strategies remain separate. The existing ETH quote
preflight, Privy risk review, ETH LIVE risk controls and BTC/ETH order
audit are independent and remain hard-disabled for execution. The
`mininex_live_order_intents` PostgreSQL table currently accepts only BTC
and ETH; widen with an explicit transactionally tested migration and real
chain-specific order models before using the other six engines.

Next implementation steps: finish safe ETH unsigned swap preview and
signer consent, prove ETH receipts/fee accounting on mainnet with tiny
limits, implement native BTC settlement adapter, then BNB/POL/SOL/XRP/ADA,
then TRX, with separate acceptance tests for each network.

## Verification references

- Polygon PoS network chain 137, native gas token POL:
  https://support.polygon.technology/support/solutions/articles/82000889434-how-to-add-polygon-mainnet-on-metamask-
- Circle Polygon PoS native USDC vs bridged USDC.e:
  https://help.circle.com/support/en/usdc-supported-blockchains-minting-redemption-faqs?id=kb_article_view&sysparm_article=KB0010590


- Uniswap current router addresses:
  https://developers.uniswap.org/docs/trading/swapping-api/supported-chains
- Circle USDC XRPL:
  https://www.circle.com/blog/now-available-usdc-on-the-xrpl
- Circle USDCx Cardano:
  https://www.circle.com/blog/usdcx-on-cardano-now-available-via-circle-xreserve
- Circle USDC TRON deprecation:
  https://www.circle.com/blog/circle-is-discontinuing-support-for-usdc-on-the-tron-blockchain
