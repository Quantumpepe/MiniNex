# MiniNex: GitHub-hosted automatic UI updates

## Behavior
- Initial **signed Android APK** must include the native Capacitor 8 updater plugin.
- MiniNex checks the **public GitHub Releases API** at startup and after returning from background (at most once per hour).
- It downloads new verified **web assets only** (HTML/CSS/JS) and queues them for the next WebView launch. It does not interrupt a running session.
- The plugin requires `notifyAppReady()` during startup; missing readiness triggers native rollback.
- The app displays update state in Settings (DE/EN).
- No Capgo account, credential, analytics endpoint or subscription required.

## Safety constraints
- Only finalized, non-prerelease `mininex-ota-v0.8.<run>` releases in `Quantumpepe/MiniNex`.
- Only the exact HTTPS `mininex-web.zip` GitHub Release asset, with SHA-256 checksum from release notes and (if available) matching GitHub-computed asset digest.
- Release must match the **native shell ABI fingerprint** (hash of Capacitor/Capgo dependencies and native config plus manually bumped `NATIVE_SCHEMA`). This avoids using a web bundle that depends on unavailable native plugins. If native project code changes without dependencies/config changes, increment `NATIVE_SCHEMA` in `frontend/scripts/native-abi.mjs`.
- Never load an older release than currently installed; never update an APK built after the release. Never contact untrusted hosts.
- No real wallet transaction is triggered by the updater. The live trading backend is **not** enabled by OTA updates.
- Backend changes still deploy separately through Render. Native changes require a new signed APK, not an OTA update.
- This is SHA-256 integrity protection through GitHub HTTPS, **not independent signed update manifests**. Before general release to paying users, use a separately held signing key / signed manifests and independently audited updater controls.

## Release automation
1. Every PR runs npm tests, a production Vite build and an offline Capgo CLI archive-format smoke check.
2. A **main-branch push** that passes checks triggers the `publish-ota` job, which creates a Web-only ZIP with the pinned Capgo CLI, computes SHA-256, and publishes a GitHub Release with the source SHA and native ABI.
3. Only the publisher job has `contents:write` permissions. No release occurs on PRs.
4. To suspend distribution, disable the `publish-ota` job; the installed app keeps working with its existing bundle.
5. A previous known-good version can be restored by publishing a **new** release with the compatible version content at a higher OTA version number. Never overwrite published releases.

## Testing
- Verify frontend CI plus signed Android debug workflow. APK build must receive `VITE_MININEX_NATIVE_ABI`, `VITE_MININEX_BUILD_SHA`, and `VITE_MININEX_BUILD_AT` from the Android workflow.
- Open Settings on the phone: updates enabled only in the native signed app; the web preview displays inactive.
- After installation, make a harmless frontend-only change on main, verify the GitHub OTA release appears, reopen app twice: first pass downloads, later pass activates.
- Offline, wrong SHA, wrong ABI, bad ZIP and incomplete JS startup must leave the last working app intact.
- Existing APKs without this native plugin cannot receive OTA until the new APK is installed once.

## Caveats
- GitHub has API-rate limits and outages; fail closed and retry after reopening.
- Retained data / login depends on web origin and native plugin behavior; test Privy login and personal paper sessions on an actual device. A native successful startup does not prove trading logic correctness.
