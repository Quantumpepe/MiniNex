# MiniNex Paket 2 — LIVE rollout / operator dependency

**Status (17 Nov 2026): GitHub PR #194 is a development branch. Not deployed.**
This document is not an instruction to auto-merge, transfer money, or grant
Privy access. Rollout requires the wallet owner's explicit approval.

## What has been coded

- Owner-only five-asset EVM trader consent and per-coin LIVE session UI;
  signed EIP-191 owner intent, server-built exact Privy policy, owner-approved
  `addSigners`, checked remote signer, explicit LIVE start and pause.
- Separate bilingual fee consent flow for the exact 5%-of-realized-net-profit
  High-Water-Mark fee, once-only USD/USDC/USDT obligation journal, and
  guarded auto-fee sender with on-chain settlement reconciliation.
- Complete historical LINK/UNI ERC20 transfer audit and FIFO HWM accounting
  for fully proved trade histories. Historical prices older than supported
  provider coverage and full native BNB/POL/AVAX custody remain incomplete.
- Trading and fee policies never share a signer ID: Privy only supports one
  override policy per additional signer. No permission may silently replace
  another coin's approved policy or the existing ETH signer.

## Owner-required Privy identifiers — **never** put secrets in GitHub

Retain the current ETH signer quorum `MININEX_PRIVY_ETH_SIGNER_ID` and
`MININEX_PRIVY_ETH_POLICY_OWNER_ID` unchanged. Each of these **ten**
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

This is a **provider limitation**, not an order/day trading or budget limit.
The existing pinned P-256 public authentication member may be reused only
if Privy explicitly accepts it for independently configured key quorums.
Never invent or regenerate the original private key. Verify the actual
Privy API behavior in a controlled rollout; remote GET is mandatory.

Treasury wallets must be configured independently and verified on the
correct chain: `MININEX_TREASURY_WALLET` (ETH network for LINK/UNI),
`MININEX_TREASURY_BNB_WALLET`,
`MININEX_TREASURY_POL_WALLET`,
`MININEX_TREASURY_AVAX_WALLET`.
Do not copy a treasury address or policy from a different chain by default.

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
   is complete. LINK/UNI code/CI is not an on-chain live verification.

**Never classify:** CI as Render deployment; an EIP-191 signature as a
Privy signer grant; an ACTIVE server session as a completed blockchain
trade; an assessed HWM debt as paid ERC20 fees; or a finalized swap leg
as complete native wallet settlement.
