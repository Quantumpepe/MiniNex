# MiniNex V0.6 — BTC/ETH paper AUTO + Android/iOS app

Independent BTC/ETH market monitoring and **simulated**, paper-only AUTO
strategy. The local Capacitor mobile project is in [frontend/](frontend/README.md).

- FastAPI backend on Render supplies read-only BTC/ETH prices and a paper portfolio.
- React/Capacitor 8 app bundles UI for Android and iOS; DE/EN selectable.
- No public frontend website or Render Static Site is required.
- Android debug APK is built via GitHub Actions (when workflow succeeds).
- iOS install requires macOS/Xcode and Apple development signing.

**No wallet integration or real orders are enabled.** Paper balances reset on
server restart; estimated profits are demonstrations, not live trading results.

See [mobile setup](frontend/README.md). The repository is currently PUBLIC;
make it private under GitHub Settings if source-code privacy is required.

### V0.6 compact phone Home

The Home screen is designed to fit a phone viewport without vertical scrolling: DE/EN language switch, BTC/ETH selection, estimated demo portfolio, verified market sparkline, paper AUTO status and net P/L. More extensive Trade/Wallet/Settings pages can scroll when necessary. Real orders remain disabled; users can install only through the official stores after review and signing.

## V0.7.1 Privy identity verification
MiniNex now includes a fail-closed identity-only GET /api/auth/me endpoint. It requires a valid Privy JWT signed by the configured MiniNex public verification key; no real wallets, funds, trades or positions are enabled by this endpoint. Deployment requires setting MININEX_PRIVY_VERIFICATION_KEY in Render. See frontend/README.md.

## MiniNex V0.8 PostgreSQL persistence (identity-only)
- Configure `DATABASE_URL` in the Render MiniNex backend service with the Render Postgres **Internal Database URL**. Never paste the URL or credentials into GitHub, frontend, screenshots or chat.
- `GET /api/account` requires the valid MiniNex Privy JWT, registers that user's DID, and initializes private BTC/ETH fee ledgers without enabling deposits or live trading. A missing/unreachable database returns HTTP 503 and existing public paper-market endpoints remain separate.
- The ledger is backed by PostgreSQL and uses transaction-locked, idempotent event IDs to prevent double charging. Fee rate is fixed at 5% of cumulative realized net USD gains exceeding the previous HWM after loss recovery. Results are *assessed only*; no payout or collection exists.
- No public endpoint can append trade events. That write path must only be connected to a future fully verified live settlement engine. Paper engine balances/trades remain ephemeral and do **not** enter the live fee ledger.
- The HWM rules require correct verified realized-cost accounting and fiat normalization before future live use. Additional equity-HWM checks, deposits/withdrawals, trading authorization, native BTC handling and audit checks are still required.


## V0.8.1 Privy + PostgreSQL Render-Shell system test
On your Render MiniNex backend **Shell**, run `python -m backend.diagnostics`. It safely validates:
1. A correctly formatted ES256 public verification key (without printing it).
2. Rejection of missing/invalid tokens.
3. An isolated temporary PostgreSQL account with separate BTC/ETH zeroed HWM rows.
4. Reading those rows from a fresh database connection, then deleting the temporary account.
The diagnostic **does not** prove that a real user can log in via Privy inside an Android/iOS Capacitor app; that still needs a real device with login allowed origins. It never accesses the user's wallet, makes trades, or collects fees. Actual schema is initialized by the prior DB setup.

## LIVE roadmap – ETH step 1 (quote preflight)

The backend now offers an authenticated, **read-only real Uniswap V3 Ethereum
mainnet quote** endpoint for ETH ⇄ USDC/USDT, with verified Privy wallet
ownership, independently fetched balances, exact base-unit input and strict
route/recipient checks. Requires a secret `MININEX_UNISWAP_API_KEY` on Render.
No transaction is sent, no live trade authority is granted and the demo trading
logic is unchanged. See [the full integration plan](backend/LIVE_ETH_INTEGRATION.md)
for the eight still-required LIVE execution components and safety gates.

### ETH LIVE stage 2 – independent risk controls (still disabled)

The backend also has dedicated user-scoped LIVE ETH capital/order/daily spending
caps and a `POST /api/live/eth/stop` kill switch. A saved limit is NOT a Privy
wallet authorization: all accounts start paused and no endpoint may enable
trading. See [LIVE ETH plan](backend/LIVE_ETH_INTEGRATION.md). The Android
frontend and the BTC/ETH simulation are unchanged.

### ETH LIVE stage 3 – server-verified Privy signer (read-only)

`GET /api/live/eth/authorization` now inspects the independent user-owned
Ethereum wallet and its exact additional signer / override-policy **ID**
directly in Privy's server records. A matching ID **does not prove the policy
rules are safe**, and the endpoint never enables trading. Before real
execution, MiniNex still needs an audited restrictive policy, explicit
user consent/revocation, an authenticated intent/order lifecycle, and
receipt reconciliation. See [ETH LIVE integration](backend/LIVE_ETH_INTEGRATION.md).

### ETH LIVE stage 4 – wallet-side safety controls

The mobile wallet now has an independent ETH LIVE section for persistent
capital/order/daily caps, verified Privy signer status, immediate backend stop,
and a separately confirmed Privy wallet signer revocation action. **Privy's
removeSigners removes all additional signers**; the screen warns before action.
There is no grant/enrollment button until the policy contents and execution
engine have passed security checks. ETH LIVE trading is not yet active.
Frontend changes are checked by GitHub CI with npm tests and a Vite build.

### LIVE stage 5 – shared BTC/ETH order history and reconciliation states

A new per-Privy-account, PostgreSQL-backed **LIVE order intent ledger** provides
a shared lifecycle for ETH and future native BTC execution:
RESERVED, SENDING, RECONCILIATION_REQUIRED, SUBMITTED, CONFIRMED, and
terminal failure/cancellation states. The authenticated
`GET /api/live/orders/{ETH|BTC}` API is **read-only**. A timed-out broadcast
cannot be blindly retried as a new transaction. Actual order creation,
wallet signing and live execution remain hard-disabled until the separate
authorization and receipt-verification engine is implemented.

### ETH LIVE stage 6 – Privy restricted policy inspection

Authenticated `GET /api/live/eth/policy` can independently retrieve Privy's
real ETH signer policy rules and compare them with an operator-pinned SHA-256,
strict Ethereum mainnet, recipient, calldata, amount, router and expiry checks.
The checks are deliberately conservative, not an external security audit:
**one shared policy is not sufficient for every user's wallet without binding
their own recipient and budget.** Adding signers and sending trades are still
disabled until user-specific policies, full authorization and the live
transaction executor are securely completed.

### Seven-coin LIVE roadmap and central GO LIVE gate

MiniNex's planned native LIVE order is **BTC, ETH, BNB, SOL, XRP, ADA and
TRX last**. USDC/USDT remain settlement assets, not trading engines. The
authenticated `GET /api/live/readiness` lists each native network, candidate
settlements and twelve mandatory safety gates. Every engine and the future
single operator GO LIVE action remain locked. The existing BTC/ETH PAPER
features, account schema and ETH quote development are not reclassified as
LIVE. See [the seven-chain rollout specification](backend/LIVE_SEVEN_CHAIN_ROLLOUT.md).

### ETH LIVE stage 7 – unsigned Uniswap swap preview

An authenticated `POST /api/live/eth/swap-plan` can check an actual USDC/USDT
approval, request a fresh Uniswap ETH quote and request a simulated unsigned
swap using Universal Router 2.1.2. The backend only returns validated
outer-transaction metadata, NEVER raw calldata, signer authorization, or a
broadcasted transaction. Nested router command validation and real mainnet
receipt handling still block GO LIVE. See
[ETH LIVE integration](backend/LIVE_ETH_INTEGRATION.md).
