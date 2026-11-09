import React,{useEffect,useState} from 'react';
import {CheckCircle2,RefreshCcw} from 'lucide-react';
import {checkMiniNexUpdates,otaIsSupported,subscribeOtaStatus} from './otaUpdater.js';

export default function UpdateStatus({lang}) {
  const [status,setStatus]=useState({stage:'inactive'});
  useEffect(()=>subscribeOtaStatus(setStatus),[]);
  const de=lang==='de';
  const supported=otaIsSupported();
  const working=status.stage==='checking'||status.stage==='downloading';
  const pending=status.stage==='ready';
  // Green only when the running version actually matches the verified release.
  // A queued / downloaded update is not an installed update.
  const installedLatest=status.stage==='current'&&
    !!status.version&&!!status.availableVersion&&
    status.version===status.availableVersion;
  const spinning=working||pending;
  const labels={
    inactive:de?'Nur in der installierten Android-/iOS-App':'Only in the installed Android/iOS app',
    current:de?'Kein neueres Update gefunden':'No newer update found',
    checking:de?'Prüfe auf Updates …':'Checking for updates…',
    downloading:de?'Geprüftes Update wird heruntergeladen …':'Downloading verified update…',
    ready:de?'Update heruntergeladen – Aktivierung noch ausstehend':'Update downloaded – activation still pending',
    unavailable:de?'Kein kompatibles Update verfügbar':'No compatible update available',
    error:de?'Update-Prüfung oder Installation fehlgeschlagen':'Update check or installation failed'
  };
  const reasonLabels={
    'no-release':de?'Noch kein Update veröffentlicht':'No update published yet',
    'incompatible-release':de?'GitHub-Version passt nicht zur installierten APK':'GitHub release is incompatible with this APK',
    'release-network':de?'Update-Dienst nicht erreichbar':'Update service unavailable',
    'release-http':de?'Update-Dienst antwortet mit Fehler':'Update service returned an error',
    'release-rate-limit':de?'Update-Dienst vorübergehend ausgelastet':'Update service temporarily busy',
    'release-invalid-response':de?'Ungültige Antwort vom Update-Dienst':'Invalid response from update service',
    release:de?'Update konnte nicht geprüft werden':'Could not check update',
    'installed-version':de?'Installierte Version konnte nicht gelesen werden':'Could not read installed version',
    download:de?'Download oder Prüfsumme fehlgeschlagen':'Download or checksum validation failed',
    activation:de?'Aktivierung konnte nicht vorgemerkt werden':'Could not schedule activation'
  };
  const checked=status.lastChecked
    ? new Date(status.lastChecked).toLocaleTimeString(de?'de-CH':'en-GB',{hour:'2-digit',minute:'2-digit'})
    : null;
  return <section className="panel settings-panel">
    <div className="settings-row">
      <span className={'settings-icon update-progress-icon'+
        (spinning?' is-spinning':'')+(installedLatest?' is-complete':'')}
        aria-hidden="true">
        {installedLatest?<CheckCircle2 size={22}/>:<RefreshCcw size={22}/>}
      </span>
      <div className="update-status-details">
        <h2>{de?'Automatische Updates':'Automatic updates'}</h2>
        <p className="small-note" role="status" aria-live="polite">
          {installedLatest?(de?'Update aktiviert · aktuell':'Update active · up to date'):
            labels[status.stage]||labels.inactive}
        </p>
        {status.reason&&<p className="small-note">{reasonLabels[status.reason]||reasonLabels.release}{status.reason==='release-http'&&Number.isInteger(status.httpStatus)?` (HTTP ${status.httpStatus})`:''}</p>}
        {status.version&&<p className="small-note">{de?'Installierte Webversion: ':'Installed web version: '}{status.version==='builtin'?(de?'APK-Version':'APK version'):status.version}</p>}
        {status.availableVersion&&<p className="small-note">{de?'Neueste geprüfte Version: ':'Latest verified version: '}{status.availableVersion}</p>}
        {checked&&<p className="small-note">{de?'Letzte Prüfung: ':'Last checked: '}{checked}</p>}
        {pending&&<p className="small-note update-activation-note" role="note">
          {de
            ? 'Die Aktivierung kann etwas dauern. App vollständig schliessen und erneut öffnen, falls nötig wiederholen. Nicht deinstallieren – Wallet und PAPER-Daten bleiben erhalten.'
            : 'Activation can take some time. Fully close and reopen the app; repeat if needed. Do not uninstall – wallet and PAPER data stay safe.'}
        </p>}
        {supported&&<button type="button" className="update-check-button"
          onClick={()=>{void checkMiniNexUpdates({force:true});}}
          disabled={working} aria-label={de?'Jetzt nach Updates suchen':'Check for updates now'}>
          <RefreshCcw size={15}/>{working?(de?'Prüfe …':'Checking…'):(de?'Jetzt prüfen':'Check now')}
        </button>}
        <p className="small-note">{de?'Nur geprüfte Oberflächen-Updates; native Änderungen brauchen weiterhin eine APK.':'Verified interface updates only; native changes still require an APK.'}</p>
      </div>
    </div>
  </section>;
}
