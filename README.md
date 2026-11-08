# MiniNex V0.5 — BTC/ETH paper AUTO + Android/iOS app

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
