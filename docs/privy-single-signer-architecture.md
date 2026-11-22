# MiniNex: one backend signer, scoped policies for all 12 coins

**Product requirement (2026-11-22):** The owner configures Privy once. Users log
in and select a coin plus their own budget; when they press Start Trade they
approve only the wallet authority required for that trading scope. No manual
per-coin key quorum, no per-coin Render signer ENV, no global Allow-all policy.

## Privy constraint

Privy currently permits only **one override policy per additional signer on
a given wallet**. Thus aliasing the existing six `MININEX_PRIVY_EVM_*_TRADE_SIGNER_ID`
variables to the same quorum would **overwrite or conflict with other coins'
policies**. Simply removing the existing uniqueness checks is prohibited.

The same server-held signing key/quorum may be registered once and reused
across user wallets. For each **EVM wallet**, create **one scoped combined policy**
whose rules are the union of explicitly consented, budget-bound, chain-bound
coin rules. Only one signer/policy pair belongs to that wallet. Each native
wallet chain requires a separate policy with its own `chain_type`; use the
same central technical key/quorum only where Privy actually supports that
chain/signature type.

## Existing and target scope

- Existing ETH and six additional EVM coin flows are separate and must
  continue to work until a verified migration. QNT is Ethereum ERC-20 but
  is not yet in the original six-coin automatic grant implementation.
- Backend-only policy composer:
  `backend/shared_evm_consolidated_wallet_policy.py`; currently an **OFFLINE
  primitive**, not connected to owner grant, broadcast, or live session start.
- Future one-time owner configuration uses the already registered
  `MiniNex-Backend-Signer-V2` quorum ID and matching existing P-256 key.
  **Do not hardcode the dashboard quorum ID or introduce twelve new secrets.**
- Wallet-specific aggregate policies must include *only* coins explicitly
  enabled by that user and exactly the user's chosen budget. Other coins
  default to DENY.
- A new coin or larger budget must **not** silently expand an existing
  wallet policy. At the user's Start Trade action, obtain affirmative wallet
  consent and atomically update its one EVM policy under the policy owner;
  verify Privy's current remote policy and the signer assignment.
- Existing approvals, active orders and finalized receipts must be
  reconciled before modifying or replacing an in-use permission. A pause or
  revocation must immediately block future new orders without erasing
  historical user transactions.
- Trading fee rules remain exact, chain-specific, recipient-specific and
  limited to 5% of independently verified net profit above HWM. A policy
  rule authorizes *potential* fee transfers, not an actual liability.
- Non-EVM assets BTC, SOL, XRP, TRX need chain-specific delegated signer
  compatibility, source-wallet, nonce/UTXO, receipt, route, and finality
  verification. A shared quorum by itself does **not** make them live.
- User has **no additional work in Privy or Render** just to represent more
  coins; any unavoidable chain capability limitations belong in product
  engineering and must be disclosed.

## Implementation sequence and acceptance

1. Extend the offline EVM composer to ETH and QNT after their strict
   policy/Router02 routes are proven, using the correct native ERC-20 rules.
2. Add durable per-wallet aggregate-policy record with locked per-coin
   budgets, current consent revision, active coin list, policy ID and
   digest, bound to the original authenticated Privy DID and wallet ID.
3. Implement Privy policy creation and owner-authorized rule updates,
   including uncertain POST reconciliation and remote readback, without
   replacing or escalating an existing signer implicitly.
4. Update `Start Trade` to present **one clear wallet confirmation** for a
   new or expanded user scope, and to start only after the aggregate remote
   policy / signer / budget / quorum are independently verified.
5. Rewire all EVM auto-order, receipt, HWM fee and pause/revoke checks to
   the wallet-wide grant plus separate per-coin budget. Migrate legacy
   per-coin grants with owner approval, never silently.
6. Implement native BTC, SOL, XRP, TRX under their actual supported wallet
   APIs and per-chain policy types; do not infer chain authority.
7. Confirm CI, Privy SDK integration, two simultaneous EVM coins and both
   sequential grant increases, reject unsupported token/recipient,
   revoke/paused cases, then explicit owner-approved mainnet smoke tests.

**Release gate:** Not complete until multi-coin trading from one original EVM
wallet is proven without creating extra quorums and without either policy
overriding the other; all 12 coins require independent evidence.
