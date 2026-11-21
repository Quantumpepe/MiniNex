# Shared CoinGecko API preparation for Nexus and MiniNex

Status: prepared on the MiniNex development branch; not yet a production deploy or paid-key verification.

## Plan-independent backend

Nexus currently has a paid CoinGecko Basic subscription (USD 35/month, as reported by the owner). MiniNex must be able to use the same account's API credentials **after the owner configures the key on its own Render backend**. Upgrading that existing account to Analyst or a higher paid plan does not require changing the API URL or SDK: each paid plan uses `https://pro-api.coingecko.com/api/v3` and `x-cg-pro-api-key`. CoinGecko plan features and quotas depend on the active subscription, not a hard-coded MiniNex tier.

The backend-only environment variables:

- `MININEX_COINGECKO_PRO_API_KEY`: the same paid CoinGecko API key already used by Nexus; do not copy it into this repository, the frontend, Android app, logs or chat.
- `MININEX_COINGECKO_DEMO_API_KEY`: optional free Demo credential when no paid key is configured. Paid key takes precedence.
- `MININEX_COINGECKO_REFERENCE_POLL_SECONDS`: optional 60–150 second interval for MiniNex *reference-data polling*, with a minimum 120 seconds for keyless public requests. For two projects sharing the Basic plan, 120 seconds is a reasonable starting setting to conserve credits; this setting never limits trading budgets or order frequency.

No key is needed now to continue software development. Adding or rotating a key later requires configuring Render and restarting/redeploying the backend to pick up the environment change.

## Scope

- `backend/coingecko_provider.py`: central trusted path, the twelve canonical coin IDs, stablecoin IDs, paid/demo URL and header configuration.
- `backend/extra_market_data.py`: one batched request for **all 12** market assets per polling interval; the existing ten extra-coin reference consumer remains unchanged. BTC/ETH already have separate Binance/Kraken feed and do not depend on this reference response.
- `backend/shared_evm_historical_usd.py`: recent historical transaction valuation uses the same paid transport and keeps the existing historical EVM allowlist.
- Requests are server-side. Never use reference/spot quotes as proofs of executed swap prices or as wallet authority. Market feed fallbacks and existing strategy calculations stay in place.
- Local diagnostics count reference requests/successes **since the backend process started**. These counters do not include Nexus traffic, other processes, or the MiniNex historical valuations; use the CoinGecko Developer Dashboard for real account-wide usage.
- No shared data proxy between Nexus and MiniNex has been created: each backend can independently call CoinGecko. A central caching/gateway service can be evaluated later if combined traffic justifies it.

## Shared-plan credits and licensing

Both projects spend from the same CoinGecko subscription quota if they use the same account/key. At 60-second cadence the batched MiniNex market-reference loop makes about 43,200 requests in a 30-day month; at 120 seconds, about 21,600. Historical lookups and all Nexus API traffic add to these figures. Neither figure is a full usage prediction. Monitor provider-side aggregate credits and 429 responses rather than claiming MiniNex local counters measure total account usage.

As of November 2026, the published monthly plans are Basic USD 35/100k credits; Analyst USD 129/500k; Lite USD 499/2m; Pro USD 999/5m+. The actual available endpoints and historic depth depend on the plan; upgrading does not automatically unlock complete historical tax accounting. CoinGecko's standard commercial licence permits in-app display with attribution but does not authorize reselling or redistributing the raw API. Verify rights for both apps before wide rollout.

Reference: https://www.coingecko.com/en/api/pricing and https://support.coingecko.com/articles/1043089972-what-are-the-differences-between-commercial-and-custom-licenses

## Validation status

Unit tests cover the shared paid key in headers, the same transport across plan upgrades, 12-coin batching, polling overrides, and paid historical lookups. Green CI alone does not establish that a real paid key has been configured or that CoinGecko serves every desired dataset for that plan. Do not deploy or activate trading from this change alone.
