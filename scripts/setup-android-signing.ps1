# MiniNex - one-time, stable Android TEST signing setup.
# Requires: keytool (Java 21), GitHub CLI (gh) logged into Quantumpepe/MiniNex.
# Run locally on the owner's Windows machine. Never upload the keystore to Git.

$ErrorActionPreference = 'Stop'
$repository = 'Quantumpepe/MiniNex'
$alias = 'mininex-test'
$privateFolder = Join-Path $HOME 'MiniNex-Private-Keys'
$keystore = Join-Path $privateFolder 'mininex-test.jks'

foreach ($name in @('gh','keytool')) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "Tool '$name' ist nicht installiert oder nicht im PATH. Bitte GitHub CLI und Java 21 installieren."
    }
}
& gh auth status
if ($LASTEXITCODE -ne 0) {
    throw 'GitHub CLI nicht angemeldet. Zuerst gh auth login ausfuehren.'
}
& gh repo view $repository --json name --jq '.name' | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Kein Zugriff auf das MiniNex-Repository.'
}

New-Item -ItemType Directory -Path $privateFolder -Force | Out-Null
if (-not (Test-Path -LiteralPath $keystore)) {
    Write-Host 'Erstelle den dauerhaften MiniNex-Testschluessel. Bitte ein starkes Kennwort setzen.'
    Write-Host 'Keystore-Kennwort merken und offline sichern.'
    & keytool -genkeypair -keystore $keystore -storetype PKCS12 -alias $alias -keyalg RSA -keysize 3072 -validity 7300 -dname 'CN=MiniNex Test,OU=Android,O=MiniNex,C=CH'
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $keystore)) {
        throw 'Der Android-Testschluessel konnte nicht erstellt werden.'
    }
} else {
    Write-Host 'Vorhandenen Schluessel wiederverwenden; kein neues Schluesselpaar wird erzeugt.'
}

$secure = Read-Host 'Kennwort des gespeicherten Keystores' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
if ([string]::IsNullOrEmpty($password)) {
    throw 'Leeres Kennwort ist unzulaessig.'
}

# StandardInput.Write sends exact bytes (without a trailing newline).
# PowerShell's pipeline would append a newline to passwords.
function Set-RepoSecret([string]$name, [string]$value) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = 'gh'
    $info.Arguments = "secret set $name --repo $repository"
    $info.UseShellExecute = $false
    $info.RedirectStandardInput = $true
    $info.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::Start($info)
    try {
        $process.StandardInput.Write($value)
        $process.StandardInput.Close()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw "GitHub Secret '$name' konnte nicht gespeichert werden."
        }
    } finally {
        $process.Dispose()
    }
}

try {
    # GitHub CLI encrypts action secrets for the selected repository.
    $b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($keystore))
    Set-RepoSecret 'MININEX_APK_KEYSTORE_B64' $b64
    Set-RepoSecret 'MININEX_APK_KEYSTORE_PASSWORD' $password
    Set-RepoSecret 'MININEX_APK_KEY_ALIAS' $alias
    Set-RepoSecret 'MININEX_APK_KEY_PASSWORD' $password
    Write-Host 'OK: Alle vier Signing-Secrets sind im GitHub-Repository hinterlegt.'
    Write-Host "Der private Keystore liegt nur auf diesem PC: $keystore"
    Write-Host 'Sichere die JKS-Datei und das Kennwort auf einem privaten, sicheren Backup.'
    Write-Host 'Als Naechstes: GitHub Actions -> MiniNex Android validation -> Run workflow.'
} finally {
    $password = $null
    $b64 = $null
    $secure = $null
}
