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
- Native SELL payout proof now also independently validates a THORChain-linked
  finalized Ethereum USDC/USDT Transfer to the original Privy-linked owner,
  with canonical Ethereum block and exact atomic amount. The authenticated
  inspection endpoint performs no writes. An internal-only conditional journal
  finalizer can store proof digest, amount and final block. This does not
  record realized gain, a 5% fee or a completed native BUY.
- Native BUY destination-chain proof now checks one THORChain outbound against
  the actual owner BTC receipt (six confirmations), Solana finalized transfer
  or XRP validated Payment (exact delivered drops). The owner-only BUY readback
  does not send, settle a trade, record gains or charge any fees. Original
  Ethereum BUY source settlement still needs its own validation.

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
5. Native SELL source + THORChain + canonical Ethereum destination proof is
   independently checkable; native BUY receipt inspection is also implemented.
   Finish verified Ethereum BUY source finality, durable native BUY payout
   posting and refund flows before posting positions or wallet PnL. Never equate source finality to swap completion.
6. Settle actual per-coin FIFO/net costs and user-authorized 5% high-water-mark
   fee ONLY after independently verified successful real round trips.
7. Validate on explicitly approved mainnet wallets: BTC/SOL/XRP entry, exit,
   auto trading, pause/resume, incomplete swap recovery and exact order history.

No automatic LIVE launch until all requirements are met and user confirms
Privy policies. EVM switches never enable native signing by implication.
TRX and DOGE follow only after these three chains.


## Additional BUY source and refund binding (same PR)

- For BTC/SOL/XRP BUY, an actual finalized Ethereum mainnet source receipt
  can now be independently inspected for exact owner address, original hash,
  canonical block, official ERC-20 USDC/USDT Transfer, THORChain router
  Deposit event, depositWithExpiry parameters, current trusted Asgard vault,
  original coin destination and refund-to-owner memo.
- An internal-only fair-page passive source reconciler can commit that
  original receipt digest into the immutable journal; it cannot send.
- The private THORChain quote includes the explicit original-source
  refund_address request and verifies the refund field in the returned memo.
  Native wallet case sensitivity is preserved on SOL/XRP.
- An exact unsigned Ethereum original BUY depositWithExpiry transaction
  constructor uses the current owner-bound quote and canonical ABI. Its
  internal output explicitly requires separate Privy policy confirmation,
  live gas/allowance verification and wallet signing before any send.
- Actual Privy native signing, ERC20 allowance, native chain send builders,
  retry/refund reconciliation, full FIFO/HWM and real-money mainnet validation
  remain outside these read-only steps. No Render environment toggles changed.
