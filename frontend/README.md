# MiniNex V0.6 — installable Android/iOS app project

MiniNex runs as a local **Capacitor 8** app (React/Vite). Android and iOS package
compiled `frontend/dist` files into their native WebView. **No Render Static Site
is used or needed.** The existing read-only public paper API remains on Render.
Its address is technically discoverable through network inspection, never a secret.

## Important
- **PAPER-ONLY DEMO**, no real money, wallets, deposits, signatures, user balances,
  real trading, or private API credentials.
- DE and EN are selectable in Settings.
- The temporary test application ID is `com.mininex.preview`. Choose an ID you own
  before signing/releasing to either app store.
- No JS app or API URL can be made completely invisible to an app user.

## Android (Windows or macOS)
Install Node 22+, Android Studio and the Android SDK:

```bash
cd frontend
npm install
npm test
npm run build
npx cap add android
npx cap sync android
npx cap open android
```

In Android Studio: Build -> Build APK(s) or Run on a connected phone.
For later source updates: `npm run native:android:sync`, then rebuild.

The included GitHub Actions workflow builds a **debug APK** on pushes to main
and can be started manually under **Actions -> MiniNex Android debug APK**.
Download the resulting APK from the successful run's **Artifacts** area.
This debug package is for testing, not a Google Play release. Android may show
an installation warning for APKs outside the Play Store.

## iOS (Mac required)
Install Xcode 26+ (Capacitor 8) and Apple's development tools:

```bash
cd frontend
npm install
npm run build
npx cap add ios
npx cap sync ios
npx cap open ios
```

Use Xcode with your Apple development team to install on an iPhone. TestFlight
and App Store uploads require Apple signing/provisioning and review. This repo
does **not** produce a signed IPA automatically.

## Security and privacy
The GitHub repository is currently **public**. Change repository visibility in
GitHub Settings -> Danger Zone -> Change visibility if you want private source.
Even a private repo cannot conceal a mobile app's API endpoints or bundled JS.

The backend allows Capacitor local origins `https://localhost` (Android) and
`capacitor://localhost` (iOS) for the anonymous demo API. Before real
accounts/trading, add secure authentication and authorization; CORS is not
an access-control mechanism. Create a permanent bundle ID, native app icon,
versioned dependency lockfile, and code-signing setup before public release.

## Compact no-scroll phone Home (V0.6)

The phone overview fits within one viewport with no vertical Home scrolling. Detailed Trade, Wallet and Settings tabs can scroll. No synthetic profit or trade controls were introduced. The displayed market price is quoted in USDT independently of the paper portfolio settlement token. Existing API data and paper-only constraints remain unchanged.


## V0.6.1 Mobile Navigation

Mobile Home uses two prominent buttons (Trade history / Wallet) instead of the duplicate fixed bottom navigation. Tap the MiniNex logo to return Home; tap the header gear to open Settings and switch DE/EN. The Desktop sidebar and paper-only trading engine are unchanged.


## V0.7 Privy wallet integration (safe staged rollout)
- The mobile Wallet screen shows a **real Privy login and embedded EVM wallet** only when the public `VITE_PRIVY_APP_ID` is configured. Without it, a clear setup message is shown (no simulated wallet address).
- Sign-in uses the official Privy React SDK with email login and automatic EVM wallet provisioning. A Privy App ID is a **public identifier**, not an app secret. Never put the Privy app secret or authorization keys in the frontend or GitHub repository.
- Register appropriate authentication origins in the Privy dashboard and configure the public App ID at build time, e.g. `VITE_PRIVY_APP_ID=your-app-id npm run build`. GitHub Android builds may read the **Actions Variable** `MININEX_PRIVY_APP_ID`; they never read a private secret.
- Privy officially documents the React web SDK and the Expo/React Native mobile SDK. **Capacitor WebView compatibility has not yet been validated on physical Android and iOS devices.** Do not advertise native wallet login as production-ready before that testing.
- The EVM address shown is **not a native Bitcoin receiving address**. No native BTC wallet, deposits, withdrawals, transaction signing, delegation, trading or backend identity linking are enabled. Wallet addresses cannot currently be used as MiniNex funding destinations.
- The existing backend API remains independent anonymous **PAPER_AUTO_DEMO**. Its `wallet_connected:false` is expected and distinct from a user's Privy authentication in the client. Never send real funds to an address displayed in this preview.
- Before real assets: server-side verification of Privy access tokens and wallet ownership, separate persistent per-user accounting, independent BTC wallet integration, budget/policy enforcement, secure signers and real-world security review are required.
- The GitHub **frontend-check** workflow runs tests and Vite builds only and does **not** create or publish an APK.

## Privy App ID connected
The public Privy App ID for MiniNex is now compiled as the default app identifier, with `VITE_PRIVY_APP_ID` overriding it when set. This is not an app secret. To test email login, verify this Privy app is enabled for email authentication and that the mobile Capacitor origins are permitted in the Privy dashboard. Native device compatibility and real transaction authorization are not yet validated.


## V0.7.1: server-side Privy login verification
- The logged-in Wallet page sends a fresh Privy bearer access token to `GET /api/auth/me` **only** after sign-in.
- The server verifies the ES256 signature, issuer `privy.io`, exact app audience, expiry, required claims and Privy DID. The token is never logged or stored.
- For Render set `MININEX_PRIVY_VERIFICATION_KEY` to the **public access-token verification PEM** from your MiniNex Privy app settings. Paste it as a Render environment variable, **never into GitHub or the frontend**.
- Until that variable is present, the protected endpoint returns HTTP 503 and Wallet shows an unconfigured state. Unauthenticated callers receive HTTP 401.
- This is **identity only**: response explicitly sets `wallet_connected:false`, `trading_enabled:false`. It does not bind a wallet to a user, prove a wallet address is owned, or permit deposits/trades.
- Capacitor native Privy email login still requires real-device testing and compatible permitted origins.


## V0.8 wallet database indicator
The Wallet screen fetches `GET /api/account` only after Privy JWT verification has succeeded, then shows whether the user's private PostgreSQL account is persisted. BTC and ETH each have a separate **realized-net HWM** at a fixed 5% fee rate; all shown fee data are explicitly marked **PREPARED / NOT COLLECTED**. No client can submit or change HWM or realized PnL. The existing demo balances are not persisted and remain strictly isolated. Home has no added rows or scrolling.

## V0.8.1 Verification retry
If authenticated wallet or database verification fails, tap **Verbindung erneut pruefen / Check connection again** in Wallet to fetch a fresh Privy token and re-check the backend; this remains read-only. For a private backend smoke test run `python -m backend.diagnostics` in Render Shell. An offline HTML preview cannot authenticate a real user.
