# MiniNex read-only real wallet balances (BTC / ETH / USDC / USDT)

## Scope
This module **only reads public mainnet balances** for wallets linked to the
currently authenticated Privy user. It never signs, delegates, deposits,
withdraws, approves, swaps, or executes a real transaction. The existing
PAPER portfolios, budget settings, market engine and scheduler remain wholly
separate and unaffected.

## Networks
- **BTC:** native Bitcoin mainnet, Privy `bitcoin-segwit` wallet; **confirmed**
  balance from mempool.space Esplora `chain_stats.funded_txo_sum -
  chain_stats.spent_txo_sum` in satoshis. Pending transactions are **not**
  included in the confirmed amount.
- **ETH:** Ethereum **mainnet** only, embedded Privy EVM wallet, balance from
  `https://eth.blockscout.com/api/v2/addresses/{address}` in wei.
- **USDC and USDT:** Ethereum mainnet **ERC-20 contracts only**, from the same
  Blockscout address `token-balances` endpoint. Token contracts are
  allowlisted and must use 6 decimals. Polygon, BNB, bridged versions and
  other chains are **not** part of this initial integration.
- Figures are native coin/token quantities, **not USD valuations** or
  amounts authorized for automated trading.

## Trust and failure boundaries
1. Frontend retrieves a short-lived Privy access token from the official SDK.
2. Backend validates the token signature and user DID.
3. Backend independently asks Privy for that DID's linked addresses using
   `MININEX_PRIVY_APP_SECRET` from server environment, not from the browser.
4. Invalid/ambiguous wallets or missing Privy API secret cause a **503**.
5. Blockchain requests use fixed public URLs, never a client-supplied endpoint
   or address. Invalid responses fail closed with **503**; they are not
   presented as zero balances.
6. A genuinely uncreated wallet displays **Wallet not yet created** (no amount).
   A validated zero balance is displayed as `0`.
7. Response uses `Cache-Control: private, no-store`. Frontend clears values on
   identity changes, failures and manual refreshes. No wallet data is merged
   into PAPER or fee/tax ledgers.

## Deployment and test
- Render must have `MININEX_PRIVY_VERIFICATION_KEY` and
  `MININEX_PRIVY_APP_SECRET` configured on the backend; the app secret
  MUST NOT be prefixed `VITE_` or sent to the frontend.
- After merging this backend change, manually redeploy the Render service.
- Frontend will be distributed through existing checked GitHub OTA releases
  to compatible native Android shells. No new Android plugin is needed.
- In MiniNex Wallet after login, use **Refresh balances** to see the four
  values. Until the two external data sources and private Privy verification
  succeed, an explicit **unavailable** message is normal.
- Test with an empty wallet, pending/unconfirmed BTC transaction, Ethereum
  mainnet USDC/USDT, account logout/login, expired tokens and provider outage
  before enabling any payment or deposit workflow.
- These explorer reads do not prove spending readiness. A separate,
  independently reviewed live wallet execution layer is still required.
