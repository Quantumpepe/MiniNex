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
