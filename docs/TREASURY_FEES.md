# MiniNex: 5% performance fee and owner treasury (pre-live)

## Single owner setting

On the **Render backend** environment, add:

```text
MININEX_TREASURY_WALLET=0xYOUR_ETHEREUM_TREASURY_ADDRESS
```

Insert **only a public 0x Ethereum-compatible address** (40 hexadecimal
characters after `0x`), never a seed phrase or private key. It must be a
nonzero destination under the owner's control and able to receive ERC-20
USDC/USDT on Ethereum mainnet. Use the correct network when checking the
address. Without the setting or with an invalid address, treasury readiness is
false. Readiness is visible via authenticated `GET /api/treasury/status`.
No owner address is returned to normal users.

The treasury network is currently **Ethereum mainnet only**; settlement tokens
are **canonical USDC or USDT** (not native BTC). The user may trade BTC or ETH
strategies, but the eventual 5% fee is settled independently from the asset
being traded. Native BTC wallets cannot directly send ERC-20 stablecoins:
cross-chain settlement/bridge and verified execution routes are **not yet
implemented**.

## Charge policy

- Fixed rate: **5% of verified realized net gains, after trade/route/network
  costs and before the performance fee**.
- The existing durable high-water-mark book is kept **per Privy DID and per
  trading asset (BTC and ETH separately)**. A prior realized loss, or a loss
  after a prior high, must be recovered before the next fee becomes due.
- Unrealized gains are not chargeable. The live subsystem must also reconcile
  unrealized losses/equity, deposits/withdrawals, native coin cost basis and
  quote-currency conversion before actual fee collection.
- Verified live executions are deduplicated by
  `(privy_did,asset,execution_id)`; their HWM update, fee event and treasury
  obligation commit in the same PostgreSQL transaction.
- Each positive assessed fee creates a USD-denominated
  `mininex_treasury_obligations` row with state
  `AWAITING_VERIFIED_LIVE_SETTLEMENT`. These are **accounting liabilities,
  not received funds**.
- An ERC-20 token quantity is **not** guessed from USD parity: live execution
  needs an independently verified token quote and on-chain receipt before
  marking a liability paid.
- Personal/global PAPER simulations never call the trusted live-close method
  and do not create owner fee obligations.

## What this does NOT yet do

The backend intentionally has **no code that signs, transfers, pulls, approves
or automatically collects** treasury fees. Adding the public address only
selects the intended recipient and makes the ledger ready. Before real payouts
are possible, complete and security-review the Privy live execution module,
verified ERC-20 token routing, accurate cost basis and settlement quotes, fee
deduction/receipt reconciliation, transactional retries and tax records.
The env flag `MININEX_LIVE_TRADING_ENABLED` **only disables simulations**; it
does NOT authorize real trading, collection or wallet access.

Do not mark an invoice paid before independently confirming the settlement
transaction on the correct chain.
