# MiniNex performance contract

**Product requirement: MiniNex must feel immediate and remain fast even on
older Android phones and modest Render instances. No improvement is acceptable
if it silently makes the app slower.**

All future pull requests should check these rules before merging:

1. **No unnecessary polling.** Prefer one server-side market data fan-out
   (WebSocket/shared cache) to repeated per-coin or per-user API calls.
   Respect provider limits, timeouts, stale data and exponential backoff.
2. **No perpetual UI timers for static status.** Use event-driven updates,
   local expiry timeouts only when necessary, and clear timers/listeners on
   unmount or login/account changes. Never poll quotes every second.
3. **Keep the main screen light.** Keep the 12-coin grid responsive, the main
   screen compact and avoid expensive re-renders, heavy animations,
   blocking requests, large new bundles or unnecessary SDK dependencies.
4. **Preserve BTC/ETH independence and safety.** Do not alter strategy
   throughput, budgets, execution permissions, or move capital across coins
   as a side-effect of wallet, quote, market or UI improvements.
5. **Measure regressions.** Each new background task/API source should have
   an explicit request budget, concurrency ceiling and safe failure mode.
   Each PR should pass frontend/backend tests and production build before
   main-branch merge. Check real-device responsiveness before enabling LIVE.
6. **Don't fabricate freshness.** Expired quotes/prices are not actionable.
   Prefer showing "not available" to stale data that appears live.

Current manual Wallet swap quote previews use one local timeout per quote:
15-second expiry from ETH provider response; at most 30 seconds for
indicative BTC quote timestamps. No additional network request is triggered.
There is no transaction signing or LIVE trading enablement here.
