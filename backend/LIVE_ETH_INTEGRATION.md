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

## Stage 13: alternative direct SwapRouter02 policy path, read-only

To compare the working Nexus-style restricted contract-call idea with
the current MiniNex Universal Router, `eth_swaprouter02_preflight.py`
records the official Ethereum SwapRouter02 address:
`0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45`.
It independently validates only the direct single-hop
`exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))`
selector `0x04e45aaf`, with an EXACT quote-bound stable/WETH token pair,
recipient fixed to the verified user wallet, exact input, minimum received,
approved V3 fee tier and zero price-limit override. No dynamic multicall
ABI is accepted by this narrow reviewer.

**Important native-ETH gap:** an ERC20 stable -> WETH direct swap returns
**WETH, not native ETH**. The reverse path requires a pre-existing WETH
balance. Actual MiniNex native ETH -> USDC/USDT -> native ETH requires
separately reviewed wrap/unwrap steps, funding authorization, gas,
allowance, receipts and wallet accounting. SwapRouter02 `exactInputSingle`
does not contain a deadline field (unlike the older V3 SwapRouter).
The final implementation must address route freshness independently.

The current production quote and unsigned swap-plan endpoints still
use Uniswap's Universal Router. This addition has **no API endpoint, no
provider calls and no option to sign or broadcast**. It is intentionally
not plugged into Privy `eth_sendTransaction` or the LIVE reservation
worker. The existing Privy draft based on Universal Router `execute`
must not be reused with the SwapRouter02 address.

Next decide the full native wrap/unwrap approach and then independently
prove the corresponding narrow Privy rule set for each user (wallet,
chain, token pair, recipient and atomic spend bounds), before creating
or applying an ALLOW policy. Never apply a broad `execute`, `multicall`,
`transfer` or wildcard signer permission merely to unblock development.

Official references:
https://developers.uniswap.org/docs/protocols/v3/deployments/v3-ethereum-deployments
https://github.com/Uniswap/swap-router-contracts/blob/main/contracts/interfaces/IV3SwapRouter.sol

## Stage 14: native ETH <-> WETH9 conversion review (read-only)

The original MiniNex external asset must remain **native ETH** (not WETH).
SwapRouter02 `exactInputSingle` accepts ERC20 tokens, so a future
direct-router ETH/stablecoin implementation would require TWO individually
reconciled transactions, rather than treating WETH as the final asset:

- BUY native ETH using stable: `exactInputSingle` USDC/USDT -> WETH
  paid directly to the verified wallet; then `WETH9.withdraw(uint256)`
  of the **actually received and verified** WETH into native ETH.
- SELL native ETH for stable: `WETH9.deposit()` of exactly the intended
  native ETH amount; after confirmed on-chain WETH receipt, swap that
  verified WETH to USDC/USDT with `exactInputSingle`.

`eth_native_weth9_review.py` strictly inspects an internal candidate WETH9
deposit/withdraw transaction: chain 1, fixed mainnet WETH9 address
`0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2`, verified wallet sender,
canonical selectors (`0xd0e30db0` deposit, `0x2e1a7d4d` withdraw),
exact ETH value/withdraw bytes and a conservative reported ETH gas reserve.
It also checks the reported wallet WETH balance before a withdrawal.
It never returns transaction calldata, verifies live RPC balances or
signs anything. All such inputs MUST be independently obtained by a
future authenticated integration before considering any actual order.

These TWO separate on-chain steps are **not atomic**. A second-step
failure can leave WETH temporarily in the wallet. Never claim native
ETH/stable settlement or charge performance fees until independently
verified receipts, actual token deltas and gas expenditure are reconciled.
Re-quote, re-evaluate risk limits/allowances and block on uncertain
transaction outcomes before proceeding. Ensure the total portfolio
value remains correct even with an intermediate WETH position; do not
mislabel it as native ETH.

**Privy policy impact:** Restricted approval would ultimately need
three separate reviewed function families:
`WETH9.deposit`, `WETH9.withdraw`, and SwapRouter02 `exactInputSingle`,
plus narrowly scoped ERC20 allowances when needed. A single
`eth_sendTransaction` rule merely allowing a contract or method is NOT
sufficient. For dynamic token recipient, amount and minimum-output
bounds, pair Privy's immutable restrictions with independently verified
per-order calldata, budget/nonce/receipt gates. Never grant wildcard
`*`, broad `multicall`, unrestricted `transfer`, or arbitrary `execute`.
After a carefully validated one-time user grant, a delegated Privy signer
could perform permitted steps automatically; this is not yet deployed.

The existing Universal Router preview remains unchanged. No automatic
execution, wallet delegation, user permission, transaction creation
or new API endpoint is added. These WETH steps are non-executable
comparisons only. The draft Privy `execute` policy remains unsaved.

Sources:
https://github.com/gnosis/canonical-weth/blob/master/contracts/WETH9.sol
https://github.com/Uniswap/swap-router-contracts/blob/main/contracts/V3SwapRouter.sol

## Stage 15: user-specific Privy ETH policy blueprints (NO creation)

`eth_privy_user_policy_blueprint.py` is an **offline**, *pure* constructor
and strict whole-draft reviewer for six bounded `eth_sendTransaction`
ALLOW rules. It does **not** POST to Privy, install a signer, grant
permission, or enable any transaction.

**Identity and ownership**: A future enrollment workflow must fetch the
Privy wallet server-side from the verified user DID and require explicit,
revocable consent. It must supply `policy_owner_id` independently of
the execution quorum (`executor_signer_id`). The owner MUST NOT be
the executor, and the owner policy controls must be checked on actual
Privy-fetched policy metadata before association. Policy updates that
widen permissions need renewed, explicit user authorization.

**Individual caps, never a hardcoded 100 USDC**: The draft accepts exact
per-user integer atomic `usdc_max_input_micro`, `usdt_max_input_micro`,
`weth_max_input_wei`, and `native_wrap_max_wei`, plus a short expiry.
Those values must be derived by a future enrollment worker from **verified**
individual LIVE settings, fresh token prices, and the user's signed
authorization, not from simulated paper positions or unauthenticated
request bodies. The caps are per-transaction, NOT cumulative; enforce
rolling spend/exposure server-side and, if available for this wallet
method, via separately reviewed Privy stateful limits. Never make an
unbounded allowance for any token.

The four SwapRouter02 V3 rules each contain: chain 1, exact router
address, expiry, zero native value, `exactInputSingle` ABI/function name,
exact tokenIn and tokenOut (USDC/USDT and WETH), exact recipient wallet,
V3 fee tiers 500/3000, zero sqrtPriceLimit, positive + capped amountIn,
and positive amountOutMinimum. The WETH9 `deposit()` rule limits exact
ETH value to a wallet-specific ceiling; the `withdraw(wad)` rule requires
zero ETH value and a positive, capped WETH amount. Every ALLOW is
independently constrained because Privy ORs across rules; adding one
broader ALLOW could bypass the entire intended restriction.

**Crucial remaining gaps**: Static `amountOutMinimum > 0` does NOT
prove quote-derived slippage, per-order price protections or freshness;
all must be independently enforced on the exact submitted calldata.
A separate, exact ERC20 approval/allowance strategy is not yet
implemented. Client wallet consent, verifier input provenance, atomic
multi-leg reservation, nonce handling, per-leg receipts and recovery
from partial WETH settlement are also incomplete. Wallet address string
comparison in Privy is case-sensitive; verify the precise encoded case
and ABI field handling in development before any enrollment.

With only the initial Privy Dashboard draft, it is NOT safe to choose
ALLOW or Create policy yet. The future production flow should
automatically generate and independently review a distinct policy
for each user wallet, configure a separate policy owner and additional
signer only after opt-in, and keep all LIVE execution gated until
the complete native ETH cycle passes real-world integration checks.

Docs: https://docs.privy.io/controls/policies/overview
and https://docs.privy.io/controls/policies/create-a-policy

## Stage 16: verified Privy wallet + LIVE limits enrollment preflight (read-only)

An authenticated `GET /api/live/eth/enrollment-preflight` now accepts only
the DID obtained from the validated Privy JWT. It retrieves the user's
Privy record from Privy, extracts the **unique** Privy-managed Ethereum
wallet and server-observed wallet ID, verifies its Privy wallet owner,
network and address with a separate Privy GET, and looks up the account's
**separate** LIVE ETH capital, order and daily limits from PostgreSQL.
The endpoint never accepts client-selected wallet addresses, limit values,
signer IDs or policy IDs. It rejects cross-user wallets and malformed
limits, and flags any unverified existing additional signers.

Two public **identifiers**, `MININEX_PRIVY_ETH_POLICY_OWNER_ID` and
`MININEX_PRIVY_ETH_SIGNER_ID`, are checked for being nonempty, syntactically
valid and different. Missing configuration remains a blocked preflight;
do **not** paste a private key into any of these settings. Privy API
secrets are managed separately. No policy is created or changed.

The response intentionally **does not** manufacture token-atomic limits
from a USD budget by blindly assuming USDC/USDT always equal $1. It
does not return an authorization-ready policy draft. Before any later
enrollment, the backend must independently derive caps using fresh prices,
attach an owner-controlled policy, collect a separate user signature
over exact wallet, allowed actions and caps using an expiring one-time
nonce, verify revocation, and pin the entire policy body retrieved from
Privy. The user explicitly approves and can revoke the delegation.

Regardless of verified wallet or budget readiness, the endpoint ALWAYS
returns `can_grant_permission:false`, `can_sign:false`,
`can_submit:false` and `execution_enabled:false`. LIVE remains paused.
The normal paper Strategist and combined Portfolio are unchanged.

## Stage 17: EIP-191 user enrollment intent and local revoke (NOT a Privy grant)

A future frontend can explicitly ask the user to sign a **readable**
one-time Ethereum EIP-191 message tied to their server-verified Privy ETH
wallet, login session, 256-bit random nonce, five-minute expiration, exact
separate LIVE ETH capital/order/daily limits and revision, mainnet route,
token pair scope and separate policy owner/executor IDs. It does NOT
contain a transaction, approve arbitrary contracts, or authorize a signer.

These authenticated endpoints are available for staged UI integration:

- `POST /api/live/eth/consent/challenge`: independently verifies the Privy
  user and managed wallet, separately checks the LIVE limits, rejects
  unverified additional signers and absent owner/executor separation, then
  stores one unique challenge under a locked LIVE account row and returns
  the human-readable text for wallet `personal_sign` / EIP-191 signing.
- `POST /api/live/eth/consent/confirm`: accepts **only** a 65-byte hex
  Ethereum EOA signature. It rechecks the Privy wallet, frozen LIVE limits
  revision, signer/owner IDs, user session and stored message SHA-256,
  verifies the message signer by independent EIP-191 public-key recovery,
  atomically marks the challenge `SIGNED_INTENT` once and rejects replay,
  expiry, changed wallets, signatures or limits.
- `POST /api/live/eth/consent/revoke`: marks an existing local intent
  `REVOKED` and pauses MiniNex LIVE trading in the **same transaction**,
  incrementing the LIVE limits revision to invalidate stale requests.

**Limits:** This is an **intent** to request future enrollment, not a
Privy authorization, not permission to create a real policy, and not
approval for automatic transactions. Privy must later ask the user to
explicitly delegate the *exact reviewed signer/policy* and expose a
separately verified signer removal/revocation flow **before actual live
activation**. Revoke here is LOCAL ONLY: it stops MiniNex execution but
does not yet remove a signer from Privy's service; currently no signer
is created by this code. Contract-wallet EIP-1271 consent signatures
are intentionally not accepted. Each new challenge replaces an older
challenge; changes to LIVE limits invalidate older signed intents.
Login or a prepared policy/limit alone never constitutes consent.
The server never receives a private wallet key.

The backend preserves the current Universal Router swap preview,
paper trading, account budgets and global Portfolio. Real trading remains
disabled and no Privy policy or additional signer can be created.

## Stage 19: automatic per-user Privy ETH policy candidate (preview only)

An authenticated read-only `GET /api/live/eth/policy-candidate` now joins
the independently verified, **unique user-owned Privy wallet** (Stage 16),
the LIVE ETH capital/order/day limits (NOT paper capital), two separate
configured Privy quorum IDs, fresh ETH/USDC/USDT CoinGecko reference
prices and the six-route ETH policy blueprint (Stage 15).

The backend automatically constructs a **separate** user-wallet policy
candidate with exact EVM mainnet/router/ABI/call/asset/recipient restrictions,
positive trade amounts, a quote-output minimum check, per-order token
atomic caps, and a 24-hour expiry. It refuses malformed, expired or
off-peg references (stablecoin outside $0.99-$1.01); order caps are
calculated from that user's exact LIVE maximum order, **not** a fixed
100 USDC demo value. These atomic caps are **not** cumulative. Existing
daily limits and reserved capital must still be enforced atomically by a
future real trade engine, with independently refreshed on-chain prices,
slippage, verified balances and ERC20 allowances.

The GET response contains only validity and canonical policy candidate
SHA-256, not reusable policy contents or wallet secrets. No unverified
browser-provided wallet address, pricing, limits or signer identity can
influence the candidate. A missing Privy wallet, unavailable spot prices,
unrecognized additional signer, disabled/malformed budget or absent policy
owner cause fail-closed results. Public CoinGecko prices are a preliminary
reference, **not** an executable Uniswap quote or reliable authority for
real spending.

**Not enabled / deliberately NOT done in this stage:** No Privy policy
POST/PUT/DELETE, no wallet additional signer, no Privy user delegation,
no on-chain ERC20 allowance, no irrevocable unlimited token approval,
no execution or automated live trading. A valid policy *candidate* is
never authorization. The EIP-191 signed intent of Stage 17 does not
create or verify a Privy additional signer; users must explicitly grant
one in Privy for the exact restricted policy and be able to revoke it
remotely. A future provisioner must compare the whole fetched Privy
policy to the exact expected owner-bound body, store an idempotent
wallet-specific policy ID and refuse broadened rules or old revisions
before attaching the user signer. Do not assume a GET report is sufficient
for Privy authorization.

References: https://docs.privy.io/controls/policies/create-a-policy
and https://docs.privy.io/controls/policies/overview.

## Stage 20: Privy REST policy transport and exact readback (DISABLED)

`eth_privy_policy_transport.py` implements the low-level Privy REST
`POST /v1/policies` request format (with `version:1.0`, Ethereum
`chain_type`, six individually restricted rules, exact `owner_id` equal
to the **independent policy owner quorum**, and the
`privy-idempotency-key` request header). The backend can verify the exact
server-retrieved policy using `GET /v1/policies/{id}`. The owner is
**not** the execution-signing quorum. Application Basic authentication
uses the server-held Privy app secret, never the browser.

Before a request is prepared, the full individual wallet/caps/version
template must match the audited Stage 15 builder. A deterministic
idempotency value binds exact policy bytes to the verified account, wallet
and LIVE limits revision. On remote readback, comparison strips only
Privy-generated policy/rule IDs and timestamps and rejects missing,
duplicated, modified or additional ALLOW/DENY rules, broadened calldata,
changed addresses, wrong owner and malformed server responses. Privy
policy creation alone does not attach any signer or authorize fund movement.

**Shipping and safety status:** The lower-level HTTP transport is exercised
ONLY via test mocks. `provision_eth_privy_policy` always fails closed and
there is no public policy-creation or signer-grant endpoint. A saved
EIP-191 enrollment intent is still NOT a scoped Privy signer grant.
No real HTTP POST is made from the production route graph. All LIVE
signing and execution stay disabled.

Before allowing a worker to call this transport, implement durable,
per-DID/wallet policy creation state: persist a `SUBMITTING` record
under a database lock BEFORE any remote POST; recover uncertain network
outcomes without blindly reposting (Privy idempotency is only
time-bounded); pin the full remote policy and matched owner; collect
a separate user-approved Privy delegation of the *exact restricted*
policy; independently verify its signer attachment; implement and
test remote Privy revocation and a local stop switch. Also finish
allowance strategy, quote/calldata verification, cumulative budget
enforcement, settlement recovery and independent policy review
before any LIVE launch. Do not turn on a hidden environment toggle
or attach existing user wallets merely because a policy candidate
exists.

Sources: https://docs.privy.io/controls/policies/create-a-policy,
https://docs.privy.io/controls/policies/get-a-policy,
and https://docs.privy.io/controls/policies/overview.

## Stage 21: durable per-user Privy policy ledger + revoke-request guard

The authenticated **POST** `/api/live/eth/policy/prepare` requires
fresh, independently verified Privy wallet metadata, verified LIVE
capital/order/day limits, separate policy owner and execution signer
quorums, a current external ETH/USDC/USDT reference price, AND a
matching, previously authenticated **SIGNED_INTENT** consent record
(verified EIP-191 message from Stage 17).

The resulting exact six-rule Privy policy *candidate* (not a remote
policy or wallet delegation) is stored once per user in PostgreSQL:
the unique verified wallet, LIVE limits revision, independent owner,
execution signer, six-rule policy JSON, canonical SHA-256,
deterministic idempotency key and expiration. The database holds its
LIVE limits, signed intent and policy record row locks in a single
transaction. Identical repeated prepares return the same original
record; a different wallet, cap revision, policy digest, expiry or
previous `SUBMITTING`, `UNCERTAIN`, `REMOTE_VERIFIED_UNATTACHED`
or revoked state fails closed. A future internal worker-only helper
can atomically reserve a single `SUBMITTING` attempt before a remote
POST and refuses a second reservation. No route currently invokes
this helper or sends a POST to Privy.

The authenticated **GET** `/api/live/eth/policy/lifecycle` returns
only the user's inert persisted status, SHA-256, wallet/public policy
ID and whether the LIVE limits revision still matches. Even
`REMOTE_VERIFIED_UNATTACHED` does **not** grant wallet authority.

The existing **POST** `/api/live/eth/consent/revoke` now stops all
MiniNex ETH LIVE sends, invalidates the signed intent, and
atomically marks any stored policy record `REVOKE_REQUESTED`.
The authenticated **GET**
`/api/live/eth/policy/revocation-review` independently fetches
Privy server wallet metadata (not a browser assertion) and reports
whether all additional signers are absent on the exact verified
user-owned wallet. A missing field, different owner, other wallet
or remaining signer fails closed.

**Remote signer removal and remote policy deletion are NOT yet
implemented.** A local stop and remote GET do not remove a Privy
signer. Policies must not be deleted before their signing access
is safely removed: deleting a policy must never inadvertently
produce an unrestricted signer. A future, separately authorized
Privy wallet-owner removal flow and owner-signed policy lifecycle
operations must be independently verified before enabling any
actual delegation. The policy transport from Stage 20 remains
hard-disabled in production. No trading, live signing or ERC20
approval path is activated.

## Stage 22: user-authorized Privy signer removal + independent confirmation

Privy's supported removal flow for user-owned embedded wallets is
`useSigners().removeSigners({address})`, invoked from the user's
authenticated wallet UI **only after explicit confirmation**. It removes
**all** additional signers on that wallet, potentially including signers
from other applications; the UI warns about this beforehand.

MiniNex's existing revoke button now:
1. Calls authenticated POST `/api/live/eth/consent/revoke` FIRST.
   The backend atomically pauses ETH LIVE orders, revokes the
   signed enrollment intent and marks any durable ETH user policy
   `REVOKE_REQUESTED`. If that fails, it still attempts a separate
   best-effort LIVE stop.
2. Prompts Privy to remove signers via the **owner's own session**
   (`useSigners().removeSigners`). The server never holds or
   impersonates the user's wallet authorization key.
3. Independently calls authenticated GET
   `/api/live/eth/policy/wallet-signer-absence`. The server itself
   fetches the exact verified user-owned ETH wallet from Privy and
   confirms the `additional_signers` field **exists and is empty**.
   Missing fields, remaining signers, mismatched user/address/wallet ID
   and failed Privy API calls fail closed. SDK resolve alone is NOT
   proof of successful removal.
4. If local stop and remote absence are verified, optionally calls
   POST `/api/live/eth/policy/confirm-revocation` to atomically
   finalize a matching existing `REVOKE_REQUESTED` policy record
   as `REVOKED`, after checking locked LIVE stop, revoked signed
   intent, wallet and execution signer IDs. Legacy users without
   a staged policy simply keep the verified remote absence report.

The server-side confirmer records **remote signer absence observed at
that instant**, not proof of continued absence or deletion of an
orphaned Privy policy. The user can later authorize new signers
themselves; a future live execution engine must re-check wallet
permissions and the LIVE budget before every order. No remote policy
DELETE is attempted; deleting a policy while a signer remains could
expose accidental unrestricted authority in some configurations.

**Grant/enablement remains OFF.** Privy requires the wallet owner to
explicitly add the precise backend signer with the exact restricted
policy ID (`useSigners().addSigners({address,signers:[{signerId,
policyIds:[policyId]}]})`), and an empty policy list is unsafe.
MiniNex has not yet activated policy creation or wallet delegation,
and EIP-191 enrollment intent alone never grants any rights.
No backend automatic signer enrollment, live execution, funding
transfer or approvals were added. Strategist and simulation unchanged.

Privy docs:
- https://docs.privy.io/wallets/using-wallets/signers/remove-signers
- https://docs.privy.io/wallets/using-wallets/signers/add-signers

## Stages 23-24: user-initiated Privy policy creation + separate wallet permission

**Stage 23, real Privy policy creation (NO wallet grant):** The user
explicitly starts setup from the MiniNex ETH LIVE controls. Privy's
`useSignMessage` prompts the verified embedded Ethereum wallet to sign
a one-time, nonce-bound EIP-191 MiniNex enrollment INTENT using Privy's
native signing UI. The server checks the same authenticated DID, session,
wallet, LIVE limits revision, independent policy-owner and execution
signer quorums. A POST to `/api/live/eth/policy/prepare` stores the
individual six-rule policy in PostgreSQL. A *separate*, explicit POST
`/api/live/eth/policy/create` rechecks the trusted wallet, locked LIVE
limits, signed intent, unexpired policy and a current independent ETH/
USDC/USDT price reference. Only a <=5-minute-old prepared policy may be
submitted. Under row locks, the server permanently reserves a single
`SUBMITTING` attempt **and commits before** calling Privy
`POST /v1/policies`. The request includes a deterministic idempotency
key and independent policy-owner quorum. After Privy responds, the server
GETs the exact newly created policy and checks its full remote owner,
method, conditions, recipient, contract, chain, size and rule count. A
matching remote readback is stored as `REMOTE_VERIFIED_UNATTACHED`.
Timeout, bad readback or ambiguous POST outcome becomes `UNCERTAIN`,
with no background automatic retry or duplicate creation. An existing
stop/revoke never gets overwritten by a late POST response.

**Stage 24, separate explicit Privy wallet-owner grant:** An authenticated
`GET /api/live/eth/policy/grant-review` independently rechecks the
verified user-owned wallet, stored LIVE limits revision and full policy
freshly fetched from Privy. It supplies *only* the matching wallet,
executor signer quorum and user-specific Policy ID (no private key).
When enabled **after a separate security audit**, the MiniNex UI offers
the user Privy's native
`useSigners().addSigners({address, signers:[{signerId,policyIds:[policyId]}]})`.
The user must approve this separately in their own Privy wallet. The
backend *never* registers a signer, infers delegation from an EIP-191
intent, grants an empty policy override or accepts client-provided
signer/policy IDs. Subsequent authenticated POST
`/api/live/eth/policy/confirm-grant` only stores `GRANT_VERIFIED`
after fresh Privy wallet metadata shows exactly **one additional signer**
matching the expected quorum and one exact override policy, the full
remote policy is verified again and locked LIVE limits/consent/revoke
state are unchanged.

**Important runtime safety:** `MININEX_PRIVY_ETH_GRANT_ENABLED` defaults
to OFF. It must stay unset until a controlled Privy ABI-parser/Uniswap
route and on-chain allowance review, atomic cumulative daily budget
enforcement, safe transaction preflight, remote removeSigners and
uncertain-creation reconciliation have passed a separate security
gate. Its only accepted value is `enabled-after-audit`. No automatic
wallet grant occurs merely because a policy exists. Even when a user
has granted the exact scoped signer, LIVE trade submission, gas
spending and automatic strategies remain disabled in the backend. The
Stage 19 policy itself enforces per-order maximums but **not** cumulative
daily spending; activating automated real trading from the mere
presence of a grant would be unsafe. The signed intent and Privy
grant are two distinct approval events.

The existing owner-approved Privy removeSigners/revoke flow remains
available. If the wallet owner revokes midway through a policy POST, the
DB never resurrects authorization, and orphaned/uncertain policy
creation needs reconciliation before another attempt. These changes
must be deployed together, with no destructive migration and no
changes to Paper simulations, Strategist, coin-separated trade history
or the global Portfolio.

References:
https://docs.privy.io/controls/policies/create-a-policy
https://docs.privy.io/controls/policies/get-a-policy
https://docs.privy.io/wallets/using-wallets/signers/add-signers
https://docs.privy.io/wallets/using-wallets/ethereum/sign-a-message
https://docs.privy.io/wallets/using-wallets/signers/remove-signers

## Stages 25–32: immutable consent, fail-closed policy recovery and LIVE risk audit

After Stage 24, all actual signing and trades stay disabled. The following
non-executable, per-user safeguards are implemented and tested together:

1. **Stage 25 – Consent immutability.** EIP-191 consent challenges refuse
   a new nonce when the authenticated DID already has a durable Privy policy
   record, including PREPARED, UNCERTAIN, REVOKED or GRANT_VERIFIED. This
   prevents a second challenge from erasing a prior signed/revoked intent
   before remote Privy policy reconciliation. New challenges before any
   policy still replace their previous unconfirmed nonce as intended.
2. **Stage 26 – Uncertain Privy POST handling.** Malformed, mismatched
   and improperly identified remote policy create/readback results are
   atomically marked `UNCERTAIN`; no false success, automatic POST
   retry, signer grant or trade is permitted.
3. **Stage 27 – Expiration reporting.** The authenticated existing
   `GET /api/live/eth/policy/lifecycle` now reports the remote policy
   record/expiry and seconds remaining, while all signing flags remain
   false. Expiry does **not** automatically renew a grant.
4. **Stage 28 – Rolling ETH LIVE exposure.** New authenticated read-only
   `GET /api/live/eth/ledger-audit` acquires the user's ETH LIVE risk
   row lock, separately totals **all unresolved pending orders** (even old
   SENDING/REORG_PENDING) and confirmed orders from the last 24 hours.
   Pending orders consume both reserved capital and rolling daily
   turnover, old unresolved orders remain blocked until reconciled.
   Does not count Paper trades or infer live token balances.
5. **Stage 29 – Consolidated safety checks.** Read-only backend review
   combines independently verified Privy wallet ownership, policy
   lifecycle/expiry and LIVE revision, explicit wallet grant readback
   when applicable, unresolved order counts and spending limits.
   It always reports execution disabled, and clearly lists blockers
   such as uncertain policies, stale orders, invalid wallets, expired
   grants and missing ABI/approval safeguards.
6. **Stage 30 – User-bound diagnostic route.** Authenticated
   `GET /api/live/eth/prelaunch-safety` exposes this report to the
   user's own Privy DID only. No client-provided wallet address,
   policy ID, signer identity or balance can alter it. No POST route
   accepts a readiness report as authorization to move funds.
7. **Stage 31 – Mobile-friendly EN/DE review.** The existing ETH
   controls panel gains an explicit on-demand **ETH safety check**
   button and a compact explanation of reserved capital, 24h confirmed
   exposure, remaining daily risk budget, expired policies and
   pending reconciliation. The report never displays Paper funds as
   deposited capital or purports to convert coins automatically.
8. **Stage 32 – Regression coverage.** Tests protect wallet replay
   and consent lockout, malformed Privy readback, stale/overspent
   USD counts, pending-state SQL row locks, cross-user JWT isolation,
   remote Privy failures and frontend refusal to display an unsafe
   response as LIVE readiness.

No LIVE trading, on-chain spending, gas fees, ERC20 allowance,
additional signing permission, policy self-renewal or auto liquidation
is activated by these features. A policy `GRANT_VERIFIED` status is
NOT sufficient for real execution. A separate audit of Privy's
ABI interpretation, cumulative spend enforcement at transaction
submission, fresh executable quotes, allowances, receipts, revoke
enforcement and failure recovery is mandatory before any live launch.
**Never** assume a policy record alone means real funds can move.

## Stages 33–40: protect wallet authority while editing ETH limits

These stages further harden **live-disabled** per-user ETH controls. They do
not enable transactions, grant signers, delete policies at Privy, or
automatically liquidate positions.

33. **Compile-time signer grant safety gate.** Privy backend signers can
    authorize spending within a policy even when MiniNex's trade executor is
    off. Therefore `MININEX_PRIVY_ETH_GRANT_ENABLED` alone can no longer
    expose `addSigners`: an independent code-level audit guard remains
    **hard-disabled** until wallet-owner policy-parser, exact quote,
    cumulative spending and revoke guarantees have passed real-safe review.
34. **ETH limit-change invalidation.** Editing per-user LIVE ETH capital,
    max-order or daily turnover now atomically pauses sends, increments
    revision, revokes any old EIP-191 consent, and marks every existing
    unreconciled/restricted Privy policy `REVOKE_REQUESTED`. Previously,
    a changed local budget could invalidate the backend consent but leave an
    existing backend signer with its old remote on-chain policy.
35. **Emergency stop invalidation.** A stop request now performs the same
    local consent/policy revocation *even if the trading_paused boolean was
    already true*, preserving the same LIVE row-lock/consent/policy order.
    A backend-local stop **does not remove Privy additional signers**, undo
    submitted blockchain transactions, sell to stables or prove permission
    revoked at the wallet provider.
36. **Database transaction regressions.** Tests verify the exact atomic
    order of the limit update or stop, consent revocation, and policy
    REVOKE_REQUESTED transition; repeated stop is safe and no missing policy
    is reported as a completed remote revoke.
37. **Owner-facing revoke reminder.** After editing LIVE limits or stopping,
    MiniNex shows a separate DE/EN warning when the server reports a
    pending Privy signer removal. A stored REVOKE_REQUESTED state also
    keeps the explicit native-Privy revoke control available even if the
    app's `delegated` hint is out of date.
38. **Verified enrollment source of truth.** Client-provided `delegated`
    metadata no longer blocks initial policy preparation or a reviewed
    scoped-grant offer; the server must independently fetch exact Privy
    wallet ownership, additional signers and full policy bytes before it
    permits any restricted operation. This avoids blocking clean user
    wallets based on an unrelated SDK boolean while retaining server-side
    safety.
39. **Explicit Privy unresolved-signers blocker.** The read-only
    `GET /api/live/eth/prelaunch-safety` audit reports
    `OWNER_PRIVY_SIGNER_REMOVAL_NOT_YET_VERIFIED` for
    `REVOKE_REQUESTED` rather than silently treating a local stop as
    actual Privy signer removal.
40. **Full CI and release review.** Run Python/Node regression suites,
    frontend production build and update-package validation before
    merging. After merge, identify the correct MiniNex Render service,
    verify the deployed commit, and never use a blanket deploy or
    modify an unrelated application.

**Still open:** An authenticated one-shot policy create does not remove
remote wallet signing risk by itself. Before real trades, independently
review ABI parser interpretation and calldata bytes, exact Quote/minOut,
ERC-20 approvals and allowance revocation, gas and fees, real wallet
balances, rolling 24h anti-replay spend ledger *inside the transaction
submission path*, recovery of ambiguous POSTs/receipts, Privy owner
permission removal, and end-to-end execution tests. The current code and
UI do not claim that budget values are wallet balances or that pressing
Stop converts a position into USDC/USDT.

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
