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

## Stage 3: independent Privy ETH additional signer inspection (read-only)

MiniNex can use Privy's **additional signers** for user-authorized offline trading.
Unlike merely validating a JWT, the signer must exist in Privy's **server-side**
wallet metadata for the independently verified owner and address.

New endpoint: `GET /api/live/eth/authorization`. The endpoint:
- Identifies the unique embedded Ethereum wallet from Privy's server-fetched
  user record, not from a frontend address or wallet identifier.
- Rejects a non-delegated wallet; fetches that wallet's metadata from
  `GET https://api.privy.io/v1/wallets/{wallet_id}` on Privy's servers.
- Verifies precise wallet owner, chain, address, expected additional signer
  quorum and **exact one matching override policy ID**.
- Distinguishes `policy_reference_verified` from
  `policy_contents_verified`. **Policy contents are not audited here.**
- Never returns a signer private key, creates additional signers, revokes an
  on-chain approval, unpauses LIVE or broadcasts an order.
- Explicitly returns `execution_enabled:false` even if a matching signer
  and policy reference already exist. If Privy metadata differs from the
  expected documented shape, detection fails closed.

Render environment (private server configuration, not frontend source):
- `MININEX_PRIVY_ETH_SIGNER_ID`: ID of MiniNex's own additional signer
  authorization-key quorum generated and registered with Privy.
- `MININEX_PRIVY_ETH_POLICY_ID`: ID of a *separately created and audited*
  policy restricting allowed chain, swap contracts, token permissions,
  calldata/typed data, spend amounts and signer lifetime.
- The **authorization key private material must never be stored in GitHub
  or the mobile client**. It is NOT created or transmitted by this PR.

A matching policy **ID** alone does not prove an adequately restricted
policy; before enrollment and LIVE activation we must fetch/audit policy rules
and implement frontend user-owned `useSigners().addSigners(...)` and
`removeSigners(...)` flows, plus a distinct backend LIVE intent/receipt
system. Privy requires the wallet owner to consent once to this limited grant;
the old PAPER budget slider provides no consent and must not be reused.
Revoke must stop new orders even if wallet-level signer removal fails.

Docs:
- https://docs.privy.io/wallets/using-wallets/signers/add-signers
- https://docs.privy.io/wallets/using-wallets/signers/remove-signers
- https://docs.privy.io/controls/policies/overview

## Stage 4: ETH LIVE controls in the mobile wallet

The Android/iOS React wallet panel now contains a separate, expandable
`ETH LIVE` safety section. This section is **not** the PAPER budget slider.

- Loads both `GET /api/live/eth/limits` and
  `GET /api/live/eth/authorization` under the authenticated Privy DID.
  Any missing/malformed response fails closed.
- Lets the user prepare exact USD capital, per-order and daily-turnover caps,
  using `PUT /api/live/eth/limits` with revision protection. Even after a
  successful save, backend orders and signing remain **disabled**.
- Exposes an explicit `POST /api/live/eth/stop` action. This prevents
  new future execution, but **does not** cancel broadcast transactions,
  sell positions or convert balances to USDC/USDT.
- When the user's own linked Privy Ethereum wallet has additional signers,
  offers a two-step, explicit **Remove wallet signers** action. It attempts the
  backend stop first, and calls Privy's supported
  `useSigners().removeSigners({address})` for wallet-owned revocation.
  The frontend clearly warns that Privy removes **all additional signers**,
  including third-party application signers, and that this does not swap funds.
  A failing backend does not prevent the user from attempting independent
  wallet revocation. Partial failures are communicated accurately.
- The app **does not** call `addSigners` yet. Enrollment stays locked until
  the real scoped policy is independently audited, tested, and the execution
  and reconciliation subsystem is ready. Even a matching policy ID is NOT
  permission to activate trading.

Frontend unit tests use integer micro-USD parsing and verify fail-closed
limits/authentication indicators. GitHub Actions runs `npm test` and
`npm run build` for frontend changes, in addition to existing backend tests.

## Stage 5: shared LIVE order intent and reconciliation ledger (ETH + BTC)

The PostgreSQL schema now includes `mininex_live_order_intents` for **both**
native BTC and ETH. It is separate from the PAPER tables and scoped to the
verified Privy DID, asset and unique idempotency key. The schema enforces
chain/asset consistency, valid states and immutable audit identifiers.

Read-only account endpoint:

- `GET /api/live/orders/ETH`
- `GET /api/live/orders/BTC`
- Optional `?limit=1..50`, default 30. Missing/invalid user token fails
  authentication; another user's orders are never returned.

State design:

`RESERVED → SENDING → SUBMITTED → CONFIRMED`

A timed-out send becomes `RECONCILIATION_REQUIRED`. It must **not** be
cancelled, re-signed or retried until the blockchain has been independently
reconciled. A reverted transaction, pre-send cancellation or chain reorg is
tracked explicitly. A merely submitted transaction is not a confirmed fill.
The schema can later be used by the separate BTC engine without remodeling
account history.

Pure risk helper `build_risk_snapshot` checks caps using exact USD decimals
and includes all outstanding orders within rolling 24h spending; its caller
must obtain those amounts from trusted on-chain fills and PostgreSQL rows
while holding the relevant DB lock. The real reservation method is
**hard-disabled** (`LIVE_RESERVATION_ACTIVATED = False`). There are no HTTP
write/execute routes. No LIVE order is sent, reserved, signed or broadcast
by this stage.

A newly discovered dependency issue in fresh PostgreSQL bootstrapping was
also corrected: `mininex_fee_events` is now created before
`mininex_treasury_obligations`, which references it by foreign key.
The integration still needs a real PostgreSQL migration smoke test.

**Next required actual execution work:** independently audited Privy signer
policy; user-owned explicit once-off grant; trusted market quotes and
token-decimal validation; atomic order reservation and nonce locking;
provider-specific ETH swap transaction simulation and signing; broadcast
reconciliation, actual receipts and cost basis; and safe asset liquidation.
Never enable with only a budget slider or matching Privy policy ID.

## Stage 6: inspect Privy's actual ETH policy rules (read-only)

`GET /api/live/eth/policy` independently fetches the operator-configured
Privy policy via `https://api.privy.io/v1/policies/{policy_id}` and checks
its actual `rules`, not merely the policy ID shown in wallet metadata.

Environment values on Render only:
- `MININEX_PRIVY_ETH_SIGNER_ID` / `MININEX_PRIVY_ETH_POLICY_ID` from stage 3.
- `MININEX_ETH_SWAP_ROUTER`: independently verified mainnet contract address;
  **do not assume a Uniswap quote's eventual execution router is the same.**
- `MININEX_PRIVY_ETH_POLICY_SHA256`: operator-pinned SHA-256 of canonical
  JSON containing `version`, `chain_type`, and full `rules`, computed
  after manual policy review. A policy update changes the expected digest.

The static guard rejects wrong network/policy ID, wildcard signing methods,
ALLOW rules lacking chain ID 1, exact router recipient, bounded ETH value,
ABI-scoped `exactInputSingle` function, bounded input amount, a destination
recipient equal to **the independently verified user wallet**, and an expiry
within 30 days. Every ALLOW rule must satisfy all constraints because
Privy's policy rules combine with OR semantics. Unknown/invalid rules fail
closed. This conservative prototype only covers `exactInputSingle`;
USDC/USDT allowance and other real swap shapes are deliberately *not*
implicitly permitted. Per-wallet recipient binding means one global policy
is generally insufficient for a multi-user production app; per-user
restrictive policy issuance needs a separate reviewed enrollment workflow.

**This is an automated structural review, not a third-party security audit.**
The response always reports `policy_contents_independently_audited=false`,
`grant_enrollment_enabled=false`, and `execution_enabled=false`.
No private keys, additional signer creation or real trades. The real
transaction builder must independently verify decoded calldata, Uniswap
router, token input/output, exact recipient, allowance amount, gas, nonce,
min-out and the wallet owner immediately before sending.

Privacy: Never log private Privy credentials or user identity in policy
verification errors; policy contents are not exposed in the API response.

## Stage 7: real Uniswap ETH unsigned transaction preview (not tradable)

The new authenticated `POST /api/live/eth/swap-plan` accepts the same strict
input body as `/api/live/eth/quote`: `side` BUY/SELL, `stable`
USDC/USDT and exact input `amount`.

It reads the user wallet directly from Privy and checks the wallet-specific
mainnet balances. For stablecoin BUYs it uses Uniswap `/check_approval` to
detect any outstanding ERC-20 approval or Permit2 permission. The endpoint
returns **APPROVAL_OR_PERMIT_REQUIRED** without approving or signing
anything when required. For eligible no-permit CLASSIC V3 quotes, it calls
`/quote` and `/swap` with `simulateTransaction:true`, then validates
the unsigned outer Ethereum transaction against:
- Mainnet chain ID 1, exact user's wallet as sender, and allowlisted
  Uniswap Universal Router 2.1.2 at
  `0x23617e59A5925b2A4Bf75d73ff6711cD0b29De85`.
- Router `execute(bytes,bytes[],uint256)` function selector, bounded
  calldata length, strict ETH input value, gas-limit and maximum
  possible gas exposure, and an extra ETH reserve.
- Exact quote input/output tokens, base units, recipient, 0.50% slippage
  maximum and no unapproved provider fee.

The preview returns sanitized route and fee metadata **without calldata,
permit signatures, approval instructions, transaction submission or any
wallet authorization**. All results always contain `can_submit:false`
and `execution_enabled:false`.

Do not use this preview as permission to trade: Uniswap Universal Router
contains nested commands that are **not yet decoded**. Production must
verify every inner command (including all token addresses, amounts,
recipient and minimum output), re-quote immediately before actual send,
independently simulate via trusted RPC, verify user signer delegation and
spending caps, persist nonce/idempotency under DB locks, and reconcile
actual chain receipt and token deltas.

The API key is `MININEX_UNISWAP_API_KEY` on Render, never in the frontend.
Uniswap official docs:
- https://developers.uniswap.org/docs/trading/swapping-api/supported-chains
- https://developers.uniswap.org/docs/api-reference/create_swap_transaction

## Stage 8: strict Universal Router envelope preflight (read-only)

The ETH swap-plan preview now parses the canonical ABI envelope for
`execute(bytes,bytes[],uint256)` instead of trusting just the selector.
It refuses expired deadlines or deadlines beyond the configured 120-second
window, malformed offsets, truncated arrays, unaccounted trailing bytes,
nonzero ABI padding, different command/input counts, partial-revert flags
and reserved command bits. It reports only structural verification and the
number of commands; it never returns encoded inputs or transaction calldata.

**Important:** this is not full decoding of individual commands. The
per-command recipient, token addresses, path, exact input, minimum output,
funding source and Permit2 effects are still **unverified**. Every preview
continues to return `router_commands_decoded:false`,
`router_command_contents_verified:false`, `safe_to_sign:false`,
`execution_enabled:false` and `can_submit:false`. It cannot trigger live
trading and is not a wallet permission or contract audit.

The backend's adversarial tests cover malformed offsets, data padding,
partial-revert flags, deadlines and command/input mismatches.

Uniswap Universal Router reference:
https://developers.uniswap.org/docs/protocols/universal-router/concepts/commands

## Stage 9: quote-bound V3 inner command preflight (read-only)

The unsigned ETH `/swap-plan` now accepts only two fully identified
Universal Router **2.1.2** command sequences, each limited to one V3 pool:

- BUY: `V3_SWAP_EXACT_IN` USDC/USDT -> WETH to the router, then
  `UNWRAP_WETH` to the verified user's ETH wallet.
- SELL: `WRAP_ETH` of the exact quoted amount to the router, then
  `V3_SWAP_EXACT_IN` WETH -> USDC/USDT to the user's ETH wallet.

Review validates six-field V3 exact-input ABI (including the empty
`minHopPriceX36` array), canonical addresses, exact quoted input, output
minimum at BOTH the swap and final unwrap (on BUY), recognized V3 fee
tiers, payer flag, intermediate custody, and final wallet recipient.
It rejects all other command patterns (including Permit2 permits, sweeps,
sub-plans, multi-hop, nonempty price guard arrays, and unreviewed routes).
Those routes simply cannot produce a plan until separately implemented.

**Still not signable:** This is a narrow static command-pattern verification.
Actual router-contract compatibility, live approvals, signer policies,
trusted-RPC simulation, per-coin capital reservations, nonce/receipt
reconciliation, real gas and token deltas, and user consent remain open.
Output continues to expose no calldata or approvals and always sets
`safe_to_sign:false`, `execution_enabled:false`, `can_submit:false`.

References: official Uniswap 2.1.2 deployments and router Dispatcher:
https://developers.uniswap.org/docs/trading/swapping-api/supported-chains
https://github.com/Uniswap/universal-router/blob/main/contracts/base/Dispatcher.sol

## Stage 10: unified Privy auto-trading readiness (read-only)

Authenticated `GET /api/live/eth/auto-preflight` combines three
**server-owned**, user-scoped sources: independently verified Privy
additional signer metadata, pinned Privy policy structure, and live ETH
capital/order/daily caps with the stop switch. It reports an explicit
per-gate checklist and blockers without exposing wallet IDs, secrets,
policy bodies, raw router calldata or approval instructions. No caller
may supply an address, signer, policy, venue, order notional or override.

**Important incompatibility:** the existing Privy static policy guard
inspects `exactInputSingle`, while the currently reviewed swap-plan uses
`execute(bytes,bytes[],uint256)` on Uniswap Universal Router 2.1.2.
These are different functions. A pinned policy passing its existing
structural checks therefore does **not** establish permission for the
current swap route. The new preflight explicitly reports
`policy_covers_current_router_call: false`. Do not fix this by weakening
the policy to a broad `execute` allow rule; that would expose arbitrary
nested router commands. Future restricted signer policy design must be
reviewed together with dynamic server-side calldata checks, user consent
and atomic nonce/order reservation.

The existing live ETH controls remain PAUSED by default and cannot
unpause through any API. The route is diagnostics only: it never
creates delegation, a wallet grant, a reservation or a transaction.
`preflight_passed`, `can_execute`, `can_reserve`,
`automated_signing_enabled` and `live_trading_enabled` are all false.
Tests verify verified-looking Privy evidence never bypasses these blocks.

## Stage 11: future LIVE signal idempotency and old-order blockers

The DB schema now includes `mininex_live_signal_bindings`, a separate,
currently empty registry. Its `(Privy user, asset, signal_digest)` primary
key permanently binds an immutable server-generated Strategist signal to
one order intent and one exact payload hash. A second UNIQUE constraint
prevents another signal from reusing the same intent key. The registry
references the original LIVE intent record and must later be populated
**inside the same transaction as the actual atomic order reservation**.

`build_strategy_signal_identity` creates a deterministic identity from
verified user, network, strategy run and immutable signal-event reference;
it also hashes side, stable, exact USD notional and approved quote digest.
Restarts or identical retries reproduce the same intent ID. A changed
payload for an already recorded signal is a hard conflict, never an excuse
to send again. This is an internal future-executor contract, not an API.

`get_live_order_safety` reads all unresolved BTC/ETH order states for the
verified account and asset, **without pagination**, including SENDING,
RECONCILIATION_REQUIRED, SUBMITTED and REORG_PENDING. The read-only ETH
auto-preflight includes the result and blocks any unresolved orders.

**Not operational yet:** The new signal table has no write path, and no
privileged worker currently creates or reserves LIVE orders. Its presence
does not prove atomic reservations are enforced; the readiness gate
`durable_signal_bindings_enforced` therefore remains **false**.
Live ETH is still paused, policy/Universal-Router authorization is
unresolved, and no signing or sending has been enabled. The future
executor must claim the signal and order atomically under the
per-user/asset budget lock, prove Privy consent, and reconcile
uncertain on-chain sends before another order.

## Stage 12: ETH atomic reservation transaction core (dormant)

The internal `live_eth_reservations.py` has a transaction-level **design
and test harness**, not a live order executor. A future privileged worker
must hold an account-specific PostgreSQL live ETH limits row lock before
examining the durable signal binding and the full pending/24-hour order
exposure. It must then insert the order intent and the immutable signal
binding in the **same** transaction. An identical signal is returned as
`DUPLICATE_NO_NEW_ORDER`; altered quote/amount for an already recorded
signal is an integrity conflict. An error in either SQL insert must roll
back the full transaction. Tests use a simulated SQL cursor/rollback.

The operation also demands a future independently verified Privy owner,
restricted signer, router-aware calldata policy and explicit revocable
user consent. Currently there is **no implementation that can produce
that trusted proof**; the existing `exactInputSingle` Privy policy still
cannot authorize the `execute(bytes,bytes[],uint256)` router call.
The internal Boolean proof fields are only test placeholders, not
cryptographic proof or acceptable production authentication. They
must be replaced by independently sourced, typed server evidence before
this internal code is wired to any worker.

`prepare_live_eth_reservation` and the existing
`prepare_live_order_reservation` remain hard-disabled (HTTP 503).
There are no routes, scheduled jobs or worker callers for the private
transaction helper. No live limit can be unpaused through an API.
No Privy delegation, approval, chain signature, transaction broadcast
or balance transfer has been enabled. Neither Strategist nor Portfolio
were modified.

Next separately audited work: wallet-specific revocable Privy
consent/policy provisioning, router-compatible restricted signing,
trusted independent Ethereum RPC simulation, nonce+reorg handling,
real ledger reconciliation and guarded tiny-value deployment.

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
