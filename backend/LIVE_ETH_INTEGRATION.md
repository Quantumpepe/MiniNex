# MiniNex LIVE – ETH implementation (stage 1: real quote, no execution)

The goal is **real ETH ⇄ USDC/USDT spot trading on Ethereum mainnet**, using
independently verified Privy wallets, separately granted automated execution
authority, actual quote/receipt reconciliation and bounded exposure. No new
MiniNex vault contract is necessary.

## What stage 1 does

- `POST /api/live/eth/quote` authenticates the user's Privy session;
  accepts only `{"side":"BUY"|"SELL","stable":"USDC"|"USDT","amount":"<decimal>"}`.
  A BUY spends the stablecoin; a SELL spends native ETH. All amounts are
  **input-token units**, not USD or estimated output.
- Resolves **the user's** Ethereum wallet from Privy's server-side user record;
  does not accept addresses, contracts, recipients, routers or chain IDs.
- Requires independently verified mainnet token and ETH balances, with a
  0.005 ETH minimum remaining gas cushion. It refuses unknown, stale/unavailable
  or insufficient balances. Exact units are enforced, not rounded.
- Requests a **real** Uniswap Trading API quote using private
  `MININEX_UNISWAP_API_KEY`, on Ethereum chain ID 1 only, Uniswap V3 / CLASSIC
  only, 0.50% maximum quoted slippage and exact Permit2 amount.
- Rejects unexpected tokens, recipients, routes, input amounts and invalid
  minimum output. Returns only estimated/minimum received amount,
  a short quote expiry and `execution_enabled: false`.
- `GET /api/live/eth/readiness` reports secret configuration **presence only**
  and explicitly states that trading/signing are disabled.
- Existing BTC/ETH demo strategies, account values, reporting and wallet
  operations are unchanged.

**This stage is intentionally not a trade engine yet.** There is no transaction
creation, signature, approval, order, swap submission or live position change.
The quote cannot be reused as an execution instruction.

## Render configuration

Set `MININEX_UNISWAP_API_KEY` to a private API key obtained from the
[Uniswap Developer Platform](https://developers.uniswap.org/docs/trading/swapping-api/start-building/integration-guide).
Keep it and `MININEX_PRIVY_APP_SECRET` in Render **environment secrets only**;
never commit them or put them in the frontend. Existing Privy public JWT
verification key and database configuration remain required.

When one of these values is missing or independently verified balances are
unavailable, the request fails closed.

## Stage 2: isolated LIVE budget controls and emergency stop

The following routes require a valid Privy identity, are owner-scoped and use
PostgreSQL transactions:

- `GET /api/live/eth/limits` reads planned LIVE spending limits.
- `PUT /api/live/eth/limits` persists exact USD caps with optimistic revision
  checks: `planned_capital_usd`, `max_order_usd`,
  `max_daily_turnover_usd`, `expected_revision`. This ALWAYS leaves
  trading paused. Limits are **not** an actual on-chain balance, deposit or
  signer authorization. No live funds can be spent by this API.
- `POST /api/live/eth/stop` keeps new orders disabled. In the eventual LIVE
  engine, it must additionally cancel not-yet-submitted orders, and must not
  misrepresent already submitted transactions as cancelled. On-chain
  approvals/delegations require a separate verified revocation workflow.
- `validate_spend_limits` is an internal fail-closed prerequisite for the future
  executor; it checks order, pending allocation and daily turnover against the
  independently configured caps. Both a **verified Privy signer grant** and
  unpaused state are mandatory. No current public route can set either.

This is a safety prerequisite for LIVE, not a claim that automatic swaps are
already functioning. Required next: cryptographically verified Privy signer
registration/revocation, atomic live order-intent reservations, Uniswap
`check_approval`/swap transaction validation, nonce tracking, submission,
receipts and cost accounting. These must be integrated before any activation.

## Execution work still required – ordered

1. **Privy authorization**: obtain explicit, revocable scoped wallet signer
   delegation from the user; enforce Privy policies and server spend controls.
   The current demo budget slider is NOT a live spending grant.
2. **ETH execution**: refresh quote just before submit; handle exact Permit2
   approvals and Uniswap `/swap` calldata; independently validate router,
   tokens, recipient, calldata, amount, chain ID, deadline and minimum received;
   simulate at a trusted RPC before signing.
3. **Capital protection**: a separate immutable-revision live budget ledger
   with per-asset reservations, daily caps, available balances, gas cushions
   and concurrency locks; never fund one coin using another coin's budget.
4. **Transaction lifecycle**: persist idempotency keys, transaction/nonce and
   pending status before any send; track chain receipts, revert outcomes,
   finality, reorgs and retries without double orders.
5. **Risk limits**: authenticated quote freshness, executable fee/gas estimates,
   slippage, max order size, whitelist, price impact, and circuit breakers.
6. **Stop/settlement**: stop new entries immediately, exit positions only with
   verified quotes; do not claim USDC/USDT has arrived until receipt is confirmed.
7. **Accounting**: actual token deltas + fees + high-water-mark performance fees,
   preserving the separation of capital, strategy and reporting.
8. **BTC engine**: provider and native Bitcoin↔stable cross-chain route, delays
   and finality handling; use the same safety/state machine as ETH.

Before enabling mainnet trading: integration tests, adversarial signing-policy
tests, real-chain receipt reconciliation, independent review, tiny-value staged
pilots, and explicit owner deployment approval are mandatory.

Uniswap docs: https://developers.uniswap.org/docs/trading/swapping-api/start-building/integration-guide

Privy policy documentation: https://docs.privy.io/security/wallet-infrastructure/policy-and-controls
