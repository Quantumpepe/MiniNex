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
