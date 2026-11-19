# BTC / SOL / XRP: native LIVE rollout gate (19 November 2026)

Scope: Third trading workstream. Keep fixed 12 coins:
BTC ETH BNB SOL XRP TRX POL AVAX LINK DOGE UNI ARB.
This development PR does not change Render LIVE toggles, sign user wallets,
submit transactions or grant anything in Privy.

## Code and regressions already implemented in PR #202

- BTC: existing owner-linked native SegWit wallet, manually signed UTXO send,
  public cross-chain quotes, native chain receipt and pause controls preserved.
- SOL/XRP: current verified native Privy wallet and independent mainnet holdings
  now gate the common per-coin BUY/SELL trade preview, together with the
  current owner-linked Ethereum USDC/USDT settlement wallet.
- SOL: original wallet signature, finalized slot, owner balance delta and
  network fee inspected on independently fetched mainnet transactions.
- XRP: original account, Payment transaction, matching hash, tesSUCCESS in a
  validated ledger and actual fee inspected. XRP currently has USDC settlement.
- Authenticated endpoint: GET /api/live/native/source-receipt/{asset}/{hash},
  strictly SOL/XRP and original Privy-owned wallet.
- The app uses owner-bound SOL/XRP trade-plan responses and offers the
  matching SOL/XRP receipt check next to the original BTC/ETH readbacks.
- Absolutely no fabricated native transaction, position, payout or fee.
  None of these read-only proofs constitutes a completed THORChain swap.
- No user budget, coin size, daily turnover or artificial execution caps added.

## Remaining required coding for real, automatic native LIVE

1. Fetch a fresh executable THORChain quote with independently checked
   network, owner destination, inbound vault, memo/tag, minimum output,
   expiry and refund policy. The public MCP used now is quote-only.
2. Confirm and integrate production Privy signer/delegation APIs for BTC,
   Solana and XRPL. Privy supports Bitcoin and Solana server wallets; XRPL
   support was announced October 2026 and its actual account-specific
   production signing and granular policy availability still need checking.
3. Persist original owner and per-coin budget-revision-bound swap intentions,
   signer-policy digest, exact quote terms and one-shot send claims in Postgres.
   Never automatically resend after unknown submit outcome.
4. Build exact source-chain transactions: BTC SegWit with real miner fee and
   swap memo or supported memoless method; Solana valid blockhash and chain
   instructions; XRP validated sequence, last ledger, Fee, DestinationTag,
   Memo and account reserve. No alternate external wallets.
5. Verify both source and destination transactions on their own chains and
   match THORChain execution/timeout/refund status before posting any position
   or wallet portfolio PnL. Never equate source finality to swap completion.
6. Settle actual per-coin FIFO/net costs and user-authorized 5% high-water-mark
   fee ONLY after independently verified successful real round trips.
7. Validate on explicitly approved mainnet wallets: BTC/SOL/XRP entry, exit,
   auto trading, pause/resume, incomplete swap recovery and exact order history.

No automatic LIVE launch until all requirements are met and user confirms
Privy policies. EVM switches never enable native signing by implication.
TRX and DOGE follow only after these three chains.
