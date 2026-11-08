# MiniNex Android Test-APK: dauerhaft gleiche Signatur

**Problem:** GitHub Actions erzeugt auf jedem neuen Runner einen frischen Android-Debug-Schlüssel. Deshalb blockiert Android ein Update über die alte APK, obwohl die Paket-ID gleich ist.

**Lösung:** Der manuell gestartete Workflow `.github/workflows/android-debug.yml` verlangt künftig eine *einmalig* erzeugte, dauerhafte Test-Signatur. Vier Repository-Secrets müssen vor dem nächsten APK-Build eingerichtet sein. Eine fehlende Signatur führt bewusst zum Abbruch des Builds, statt unbemerkt eine neue inkompatible APK auszugeben.

## Einmalig auf einem vertrauenswürdigen Windows-PC

PowerShell mit installiertem Java 21 / `keytool`:

```powershell
New-Item -ItemType Directory -Force -Path "$HOME\MiniNex-Private-Keys" | Out-Null
$keyfile = Join-Path "$HOME\MiniNex-Private-Keys" "mininex-test.jks"
$keytoolAlias = "mininex-test"
# Eigene starke Kennwörter wählen, nicht in Chat, Git oder Logs teilen.
keytool -genkeypair -v -keystore "$keyfile" -storetype PKCS12 -alias "$keytoolAlias" -keyalg RSA -keysize 3072 -validity 7300
[Convert]::ToBase64String([IO.File]::ReadAllBytes($keyfile)) | Set-Clipboard
```

Füge den Zwischenablageinhalt als **Repository Secret** `MININEX_APK_KEYSTORE_B64` ein. Danach zusätzlich:
- `MININEX_APK_KEYSTORE_PASSWORD`: gewähltes Keystore-Kennwort
- `MININEX_APK_KEY_ALIAS`: `mininex-test`
- `MININEX_APK_KEY_PASSWORD`: Kennwort für den Schlüssel (bei PKCS12 normalerweise gleich dem Keystore-Kennwort)

Pfad: GitHub Repository → **Settings → Secrets and variables → Actions → New repository secret**.

**Wichtig:** Die `.jks`-Datei und die Kennwörter sicher offline sichern. Nie in ein Repository hochladen. Diese Test-Signatur wird nur für Debug-APKs verwendet, nicht für Store-Releases.

Danach GitHub → Actions → **MiniNex Android validation (manual only)** → **Run workflow**.

## Einmalige Migration

Eine mit einem früheren zufälligen Debug-Schlüssel signierte APK lässt sich nicht nahtlos auf die neue Signatur umstellen. Nur für diesen Übergang ist eine letzte Deinstallation nötig. **Vorher Daten sichern**, falls lokale App-Daten wichtig sind. Anschließend wird jede neue APK mit demselben Schlüssel signiert und erhält einen höheren Android-`versionCode`, sodass spätere Updates ohne Deinstallation möglich sind.

## Privy-E-Mail-Login

Der Android-Build benutzt dieselbe öffentliche Privy App ID wie der bereits getestete Backend-JWT-Audience-Wert. Keine Secret Keys gehören ins Frontend.

Wenn keine E-Mail mit dem Einmalcode eintrifft, zunächst in Spam/Werbung prüfen, die Adresse kontrollieren und nicht wiederholt einen neuen Code anfordern. Der Versand liegt bei Privy; das GitHub-Buildprotokoll kann keinen erfolgten E-Mail-Versand bestätigen. Privy empfiehlt auch, `[email protected]` als sicheren Absender einzutragen. Wenn auf dem Gerät eine Fehlermeldung erscheint, ihren Wortlaut für die weitere Diagnose notieren.
