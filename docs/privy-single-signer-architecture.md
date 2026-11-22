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

- Existing ETH and six additional EVM coin LIVE routes remain separate
  and continue unchanged until a verified migration. QNT is Ethereum ERC-20
  but not yet in the original six-coin automatic LIVE grant routes.
  The OFFLINE one-wallet composer now covers **all eight EVM assets**:
  ETH, BNB, POL, AVAX, LINK, QNT, UNI and ARB.
- Backend-only strict combined policy composer:
  `backend/shared_evm_consolidated_wallet_policy.py` (**OFFLINE**).
  `shared_evm_wallet_scope_transition.py` compares current/new policies
  and produces an original-wallet-bound, short-lived EIP-191 challenge for
  scope increases; no separate preliminary intent on first Privy addSigners.
  `shared_evm_wallet_policy_intent_ledger.py` stores and consumes one-use
  signed update intents under DB locks (no remote write).
  `shared_evm_wallet_policy_schema.py` defines the durable wallet grant
  and consent journals.
  `shared_evm_wallet_policy_remote_review.py` strictly verifies the
  original user wallet, single matching signer, rules and hashes.
  A new authenticated **READ-ONLY** endpoint
  `GET /api/live/evm/wallet-policy/status` independently verifies all
  original budget revisions, Privy owner/signing quorum and policy.
  The read-only endpoint cannot change a signer or start a LIVE trade.
- **New first-wallet enrollment endpoints (implemented in the Draft branch):**
  `POST /api/live/evm/wallet-policy/prepare/{asset}` builds an immutable
  first-coin policy using the original user's saved capital and fresh
  ETH/QNT/EVM quotes. `POST /api/live/evm/wallet-policy/publish` claims
  a one-shot durable policy POST and independently reads Privy back;
  uncertain POST results are never replayed. `GET
  /api/live/evm/wallet-policy/grant-offer` returns the exact wallet-owner
  approval request; after a single explicit Privy `addSigners` call,
  `POST /api/live/evm/wallet-policy/confirm-grant` verifies the matching
  signer, precise policy and original wallet using Privy GET before
  recording `GRANT_VERIFIED`. None of these endpoints executes an
  automatic trade, and the existing Android `Start Trade` is **NOT yet
  connected** to them. Old per-coin signers cannot be silently replaced.
  Backend tests cover all eight EVM assets, invalid budget revisions,
  legacy signer races, duplicate grant attempts and remote mismatches.
- **New policy-owner PATCH signing primitive (not connected to an update
  controller):** `backend/shared_evm_wallet_policy_owner_signature.py`
  builds the exact signed PATCH payload with `privy-request-expiry`,
  idempotency key and verified Policy Owner quorum P-256 key. It can
  *sign* only an exact bounded combined policy; it does not send a
  PATCH, expand wallet rights, alter a user's session or bypass
  existing budget consent. This follows Privy's official authorization
  signature payload. Remote PATCH, durable transition claim and
  EIP-191 intent-consumption workflow still need to be connected.
- **Current passing CI:** [GitHub Actions run 45054190252](https://github.com/Quantumpepe/MiniNex/actions/runs/45054190252)
  has **5,130 passing backend tests**, no failures (1 warning).
  This is not a mainnet signing or trade test.
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
- User has **no additional work in Privy or Render** merely to represent
  additional coins. A **Policy Owner quorum** remains distinct from the
  technical trading signer ID. Privy requires an **authorization signature
  from the policy owner quorum** to PATCH an existing policy. The read-only
  diagnostic now checks if the existing pinned P-256 member actually belongs
  to that registered owner quorum. If not, one legitimate owner signing
  credential must be provisioned after verification; it must never be
  replaced with an unrestricted or unsigned policy update.
  This is a single operator-level issue, not per-coin setup.
  Any unavoidable native-chain support limitations must be disclosed.

## Implementation sequence and acceptance

1. **DONE (offline):** Extend the strict EVM policy factory to ETH and
   QNT, using exact Router02, native wrap for ETH and ERC-20 for QNT;
   no new LIVE trading endpoints or owner grants.
2. **DONE (foundations):** Add durable wallet-policy/one-use-consent tables,
   original DID/wallet/coin budget revision checks, remote Policy Owner
   quorum diagnostics and strictly pinned remote policy audit. An old
   legacy grant never automatically becomes a shared wallet grant.
3. **PARTIAL:** Existing user wallet scope expansion now has an
   authenticated one-coin EIP-191 intent staging and confirmation:
   `POST /api/live/evm/wallet-policy/extend/{asset}/prepare`,
   `POST /api/live/evm/wallet-policy/extend/confirm`. Both validate
   the current original wallet, backend signer, Privy policy and current
   coin-budget revisions. They store an immutable, five-minute user-signed
   policy digest but do NOT send Privy PATCH, add signers or start trading.
   A new first-wallet policy can now be prepared/published
   (with one-shot DB claim and remote policy GET) and user-authorized
   `addSigners` can be independently verified/recorded. **Still OPEN:**
   update an existing policy via Privy PATCH with Policy Owner quorum
   signature after a user-approved one-use EIP-191 scope-change intent,
   durable one-attempt claim and uncertain-outcome reconciliation. Never
   replace or escalate an original signer implicitly.
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

**Current verified progress (2026-11-22):** Draft PR #208 is isolated
from `main` and Render. See GitHub backend auth checks for this branch.
The new status endpoint is owner-authenticated and read-only. New initial
policy preparation, publication and signed-wallet grant confirmation
endpoints are also implemented but **not wired to the production frontend
or LIVE worker**; production Start Trade, native coins, and per-coin
signers have NOT been switched to this design.

**Release gate:** Not complete until multi-coin trading from one original EVM
wallet is proven without creating extra quorums and without either policy
overriding the other; all 12 coins require independent evidence.
