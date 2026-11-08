# MiniNex Blockchain History and Tax Exports

## Currently implemented

- Read-only public explorer views for native BTC and ETH.
- Ethereum-mainnet ERC-20 USDC and USDT transfer views with **strict contract allowlists** (symbols alone are not trusted).
- Paginated older history, controlled by the user with **Load older transactions**.
- Explicit **Save verified page for tax report** action. Each request checks the verified Privy DID, independently retrieves wallet linkage from Privy's server API, fetches only one bounded explorer page, and stores only confirmed transactions.
- Idempotent PostgreSQL records scoped to the Privy DID; duplicates are ignored.
- Authenticated annual PDF/CSV export reads only stored confirmed activity, not simulation.
- No fund movement, blockchain signing, trades or withdrawals are enabled.

## Required Render configuration

The archive action is **disabled by default** without these server-side values:

1. Existing `MININEX_PRIVY_VERIFICATION_KEY` (Privy's ES256 public verification key for JWTs).
2. **NEW** `MININEX_PRIVY_APP_SECRET` from the same Privy app as `cmw7sb7ty013o0gl55aqyzi3y` (Render backend environment variable only).
3. Existing `DATABASE_URL` for PostgreSQL.

Set the secret only in the **Render backend** environment settings. Do not insert it into the frontend, GitHub source, APK, or screenshots. Once configured, redeploy the Render backend. No extra Privy signing authorization is used for this read-only verification.

## Limitations (must remain visible in UI)

- Only pages explicitly archived after backend verification appear in tax exports. Viewing a page does not archive it.
- The history is not yet continuously synchronized in the background. Complete historical backfill and reliable cursors across explorer reorgs remain follow-up work.
- Stablecoins currently refer only to Ethereum mainnet USDC and USDT, not other chains or bridged alternatives.
- The ETH explorer's normal transaction view does not include all internal contract value flows. Token transfer view filters a canonical contract, but full DeFi accounting needs further reconciliation.
- BTC UTXO change and internal transfers can appear as net flows; they are not automatically taxable events. Deposits/withdrawals are transaction descriptions, not tax classifications.
- Missing original acquisition prices, country-specific tax-currency valuation rates, year-end balances and transfers on external wallets are *never invented*. PDF/CSV exports are supporting documents, **not complete tax returns for any jurisdiction**.
- Repeated synchronization needs rate limiting, scheduled jobs and monitoring before a public multi-user launch.
