# MiniNex Paket 2 — LIVE rollout / operator dependency

**Status (19 Nov 2026): GitHub PR #194 is a development branch. Not deployed.**
This document is not an instruction to auto-merge, transfer money, or grant
Privy access. Rollout requires the wallet owner's explicit approval.

## What has been coded

- Owner-only six-asset EVM trader consent and per-coin LIVE session UI;
  signed EIP-191 owner intent, server-built exact Privy policy, owner-approved
  `addSigners`, checked remote signer, explicit LIVE start and pause.
- Separate bilingual fee consent flow for the exact 5%-of-realized-net-profit
  High-Water-Mark fee, once-only USD/USDC/USDT obligation journal, and
  guarded auto-fee sender with on-chain settlement reconciliation.
- Complete historical LINK/UNI/ARB ERC20 transfer audit and FIFO HWM accounting
  for fully proved trade histories. Historical prices older than supported
  provider coverage and full native BNB/POL/AVAX custody remain incomplete.
- Trading and fee policies never share a signer ID: Privy only supports one
  override policy per additional signer. No permission may silently replace
  another coin's approved policy or the existing ETH signer.

## Owner-required Privy identifiers — **never** put secrets in GitHub

Retain the current ETH signer quorum `MININEX_PRIVY_ETH_SIGNER_ID` and
`MININEX_PRIVY_ETH_POLICY_OWNER_ID` unchanged. Each of these **twelve**
additional EVM policy scopes requires a separate registered Privy signer
quorum ID, distinct from the ETH signer, from the policy owner, and from
all other fee/trade signer IDs:

| Asset | Trading signer Render env | Fee signer Render env |
|---|---|---|
| BNB | `MININEX_PRIVY_EVM_BNB_TRADE_SIGNER_ID` | `MININEX_PRIVY_EVM_BNB_FEE_SIGNER_ID` |
| POL | `MININEX_PRIVY_EVM_POL_TRADE_SIGNER_ID` | `MININEX_PRIVY_EVM_POL_FEE_SIGNER_ID` |
| AVAX | `MININEX_PRIVY_EVM_AVAX_TRADE_SIGNER_ID` | `MININEX_PRIVY_EVM_AVAX_FEE_SIGNER_ID` |
| LINK | `MININEX_PRIVY_EVM_LINK_TRADE_SIGNER_ID` | `MININEX_PRIVY_EVM_LINK_FEE_SIGNER_ID` |
| UNI | `MININEX_PRIVY_EVM_UNI_TRADE_SIGNER_ID` | `MININEX_PRIVY_EVM_UNI_FEE_SIGNER_ID` |
| ARB | `MININEX_PRIVY_EVM_ARB_TRADE_SIGNER_ID` | `MININEX_PRIVY_EVM_ARB_FEE_SIGNER_ID` |

This is a **provider limitation**, not an order/day trading or budget limit.
The existing pinned P-256 public authentication member may be reused only
if Privy explicitly accepts it for independently configured key quorums.
Never invent or regenerate the original private key. Verify the actual
Privy API behavior in a controlled rollout; remote GET is mandatory.

Treasury wallets must be configured independently and verified on the
correct chain: `MININEX_TREASURY_WALLET` (ETH network for LINK/UNI),
`MININEX_TREASURY_BNB_WALLET`,
`MININEX_TREASURY_POL_WALLET`,
`MININEX_TREASURY_AVAX_WALLET`,
`MININEX_TREASURY_ARB_WALLET` (Arbitrum One, chain 42161).
Do not copy a treasury address or policy from a different chain by default.

Arbitrum One is an ERC-20 network for ARB: use `MININEX_ARBITRUM_RPC_URL`
for token/receipt verification; Arbitrum ETH pays gas. ARB contract:
`0x912CE59144191C1204E64559FE8253a0e49E6548` (18 decimals),
Circle native USDC `0xaf88d065e77c8cC2239327C5EDb3A432268e5831`
(6 decimals), Arbitrum USDT0 `0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9`
(6 decimals). No wrapped/native ARB deposit/withdrawal should be assumed.
Do not enable any newly installed EVM auto-trader or auto-fee worker until
independent owner-specific Privy permissions and treasury details are verified.

## Deployment and proof sequence

1. Confirm GitHub backend, frontend, and PostgreSQL schema CI are green.
2. Confirm all separate Privy key quorum IDs in the actual owner account;
   keep the ETH signer and current owner roles untouched. Put public IDs in
   **Render environment**, not in source. Preserve existing Render secrets.
3. Review token addresses, per-chain treasury wallets and exact owner scopes.
   Do not enable new signing in production just because CI passes.
4. Merge PR #194 only after all known blockers are resolved, then deploy
   the **backend and frontend as a single controlled release** and check
   the database schema migrations. The branch does not deploy to Render
   when Render follows `main`.
5. Backend flags are opt-in; `MININEX_EVM_AUTO_WORKER_ENABLED=explicit-live`
   and `MININEX_LIVE_TRADING_ENABLED=true` require a separate owner-controlled
   activation. Native passive settlement uses
   `MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED=explicit-readonly`.
   Auto-fee execution also requires
   `MININEX_EVM_AUTO_FEES_ENABLED=explicit-owner-privy-grant`.
   Do not toggle these before Privy signer/policy setup and dry-run checks.
6. Each actual Privy wallet owner signs the coin intent and separately
   calls native `addSigners` for **that exact** trading policy, then
   separately signs/grants the optional fee policy. Browser/UI approval
   cannot be manufactured by the backend or by a CI run.
7. Independently inspect signed policy, specific signer, EVM network,
   wallet, on-chain gas, original transactions and history. Carry out
   **small real BUY and SELL only with explicit owner permission**.
   Confirm canonical finality, correctly isolated coin history, actual
   cash/asset holdings, fee accounting and final ERC20 treasury proof.
8. BNB/POL/AVAX native wallet and historical external-transfer custody
   is **not** fully reconciled for automatic HWM fee assessment. No profit
   fee may be automatically assessed on those assets until that evidence
   is complete. LINK/UNI/ARB code/CI is not an on-chain live verification.

**Never classify:** CI as Render deployment; an EIP-191 signature as a
Privy signer grant; an ACTIVE server session as a completed blockchain
trade; an assessed HWM debt as paid ERC20 fees; or a finalized swap leg
as complete native wallet settlement.


## Native BNB/POL/AVAX historical custody: new read-only evidence

New modules in this same PR (development only):

- `shared_evm_native_trace_provenance.py`: the actual original
  `trace_transaction` must show the wallet-paid native deposit or internal
  wrapped-token withdrawal, canonical finalized block, exact native amount
  and receipt gas. An incomplete trace tree fails closed.
- `shared_evm_native_value_interval_audit.py`: complete paginated
  `trace_filter` scan of outgoing/incoming native calls across the original
  MiniNex trade interval, checked against canonical mined block hashes.
  Unknown native deposits/withdrawals are disclosed, never silently
  credited as MiniNex PnL.
- `shared_evm_native_gas_balance_proof.py`: every *observed* wallet-signed
  root transaction needs its own mined receipt. Actual gas plus every
  observed native-value movement must explain archived `eth_getBalance`
  before/after the interval exactly. A single mismatch invalidates the
  audit.
- `shared_evm_native_history_readiness.py`: reloads all original confirmed
  BNB/POL/AVAX owner fills from PostgreSQL (not the UI history page),
  binds historical USD prices and original native conversion traces, then
  combines the interval transfer and gas-balance proofs. READ ONLY.
- `shared_evm_historical_fill_bridge.py` now independently requests the
  native conversion call-trace proof before marking a native original
  conversion historically priced; it still never reports complete external
  inventory or triggers HWM/fees.

**Historical archive RPC requirements:** For each applicable native chain the
operator-selected HTTPS RPC must implement `trace_transaction`,
`trace_filter` with `after`/`count` paging, archive
`eth_getBalance`, and historical receipts/blocks. Many ordinary public
RPCs will reject these; this fails closed for the **optional accounting
readers only** and is not an automatic trading suspension. RPC page/window
sizes are transport batches, **not order/day/capital limits**.

**Still not proven, and therefore not fee-eligible:** Wallet native
initial asset inventory and cost basis before MiniNex's first observed
trade; any historical source-of-funds that predates the scanned interval;
a complete wallet-lifetime gas history; and every older block-time USD
quotation outside available immutable data/provider coverage. Even when
observed transfers and native balance changes reconcile, these proofs
deliberately return `fee_assessment_eligible=false`.
Never manually override this to charge 5% on native trades.

This branch still requires independent Privy signer grants and a controlled
Render/Android rollout; no release or real money movement has happened here.


## 19 November 2026 – Backend reliability continuation (Paket 2)

New code on the existing **open PR #194**, not the deployed `main` branch:

- Passive Router02 swap finality, original native BUY/SELL deposit/withdraw
  receipts, native downstream swap/cycle recovery and original 5%-Treasury
  receipt finality use **rotating bounded work pages**. Previously each
  20/30/80-row oldest-first query could reselect the same pending records
  and starve valid newer ones. This change does NOT add user/order/capital
  limits and does NOT retry an uncertain wallet transaction.
- The shared EVM worker rotates the ACTIVE-session scan across all BNB,
  POL, AVAX, LINK, UNI and ARB owner sessions. Even if the session query
  fails temporarily, already submitted blockchain transactions still
  receive passive finality and HWM readback in that worker cycle.
- The 5%-HWM obligation selector also rotates across owners. A failed
  Privy-policy availability check cannot monopolize the backend fee queue.
  All sends remain behind the original database one-shot claim and exact
  separate owner-approved wallet/fee policy. Disabled means **no sends**.
- Native BNB/POL/AVAX archived historical custody now also reads
  `eth_getTransactionCount` for the wallet at **start_block - 1** and at
  **end_block**, matching the nonce difference to every trace-filtered
  owner-originated EOA transaction/receipt. This catches a missing zero-
  value gas-paying send that native balance arithmetic alone might miss.
  Absent/inconsistent archival nonces fail the optional historical audit,
  never trading execution. The native audit remains **READ ONLY**, and
  pre-existing asset cost basis / wallet lifetime still prevent automatic
  native 5%-HWM fee assessment.
- Added regression tests for work-page fairness, original receipt uniqueness,
  missing archival sender nonces, session-scan failures and no fee send/retry.
  Confirm CI for the current HEAD before describing the branch as tested.

**Render service verified:** TBP-AI workspace, `mininex-backend` service,
`main` branch with auto-deploy enabled. PR #194 is not deployed to this
service. No manual Render trigger should be used to deploy PR code while
`main` remains unchanged; merging a tested PR would itself trigger the
configured auto-deploy. Preserve all current environment variables and
require the respective owner Privy grants before any real live execution.
