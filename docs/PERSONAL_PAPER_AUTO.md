# MiniNex personal automatic paper sessions

## Operational behavior

- Each Privy DID and asset (BTC or ETH) has a separate PostgreSQL portfolio and
  transaction ledger. None of these are wallet balances, signer permissions or
  verified tax activity.
- An authenticated user saves nonzero demo budget preference, then starts
  the personal test session. Start snapshots the settlement currency, trading
  mode and timeframe. The demo allocates **virtual** $1,000 * budget percentage.
- LONG starts with virtual stablecoins. SPOT_SHORT starts with a **virtual
  BTC/ETH holding** valued from a verified independent market snapshot.
- ACTIVE sessions are checked automatically by the backend every approximately
  15 seconds even when the app is closed. Paused sessions are not evaluated.
  A PostgreSQL row lock and a 12-second idempotency interval prevent double
  processing by overlapping web workers/manual test requests. Trades and position
  state commit together, with no partial visible fill.
- Public market data uses Binance spot prices independently checked against
  Kraken. Invalid or stale prices never trigger orders. All fills, spreads,
  slippage and network fees are **indicative simulation values**.
- The existing dynamic net-profit trailing and post-exit rebound logic are
  reused for each owner's virtual portfolio. 5m/15m/1h select the maximum
  rolling analysis lookback, **not the mandatory waiting period**. All three
  can start evaluating signals after ~190 seconds of actually observed,
  timestamped price samples (including a verified 180-second momentum window).
  As more observations arrive, the strategy automatically uses the full
  selected 5m/15m/1h history. **24h will explicitly WAIT until a
  genuine 24h history source is implemented**. Do not misrepresent the
  existing 1-hour memory window as 24-hour signals.
- The process scans at most 30 oldest-updated active sessions per cycle. This
  bounds database/CPU load; sizing, metrics, backpressure and dedicated worker
  deployment are required before scaling up to hundreds of simultaneous users.
- Frontend status/positions/trades refresh roughly every 20 seconds.

## Deploy

1. Deploy new backend Python code to Render and confirm PostgreSQL schema bootstrap.
2. Confirm `DATABASE_URL` points to the durable MiniNex PostgreSQL instance.
3. `MININEX_PAPER_AUTO_ENABLED=true` is the default when DATABASE_URL exists.
   Set it to `false` on Render if you need to stop automatic demo evaluations
   without changing users' saved ACTIVE session status.
4. Rebuild the Android APK from GitHub after frontend CI is green. The APK has
   no signing keys for execution and cannot trade real user funds.
5. Test two different Privy accounts and both assets, pause/resume, close the
   Android app, reopen, restart Render and confirm state continuity.

## Future live cutover

- `MININEX_LIVE_TRADING_ENABLED=true` is only a **paper-mode shutoff**, not
  a live trading implementation. It stops personal paper worker scheduling and
  disables public/personal paper endpoints; the frontend uses
  `/api/user-paper/availability` to hide both overview and wallet demo trades.
- It does **not** activate trading, wallet delegation, signing, or settlement.
  The future verified live execution subsystem must be built and tested separately.
- Simulated records must NEVER be moved into live transaction history or tax
  reports. Legacy paper rows may be retained internally for audit but remain
  invisible in normal live user screens.
