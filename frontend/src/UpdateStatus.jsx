import React,{useEffect,useState} from 'react';
import {RefreshCcw} from 'lucide-react';
import {checkMiniNexUpdates,otaIsSupported,subscribeOtaStatus} from './otaUpdater.js';

export default function UpdateStatus({lang}) {
  const [status,setStatus]=useState({stage:'inactive'});
  useEffect(()=>subscribeOtaStatus(setStatus),[]);
  const de=lang==='de';
  const supported=otaIsSupported();
  const working=status.stage==='checking'||status.stage==='downloading';
  const labels={
    inactive:de?'Nur in der installierten Android-/iOS-App':'Only in the installed Android/iOS app',
    current:de?'Kein neueres Update gefunden':'No newer update found',
    checking:de?'Prüfe GitHub auf Updates …':'Checking GitHub for updates…',
    downloading:de?'Geprüftes Update wird heruntergeladen …':'Downloading verified update…',
    ready:de?'Update heruntergeladen – Aktivierung noch ausstehend':'Update downloaded – activation still pending',
    unavailable:de?'Kein kompatibles Update verfügbar':'No compatible update available',
    error:de?'Update-Prüfung oder Installation fehlgeschlagen':'Update check or installation failed'
  };
  const reasonLabels={
    'no-release':de?'Noch kein Update veröffentlicht':'No update published yet',
    'incompatible-release':de?'GitHub-Version passt nicht zur installierten APK':'GitHub release is incompatible with this APK',
    release:de?'GitHub konnte nicht erreicht werden':'Could not reach GitHub',
    'installed-version':de?'Installierte Version konnte nicht gelesen werden':'Could not read installed version',
    download:de?'Download oder Prüfsumme fehlgeschlagen':'Download or checksum validation failed',
    activation:de?'Aktivierung konnte nicht vorgemerkt werden':'Could not schedule activation'
  };
  const checked=status.lastChecked
    ? new Date(status.lastChecked).toLocaleTimeString(de?'de-CH':'en-GB',{hour:'2-digit',minute:'2-digit'})
    : null;
  return <section className="panel settings-panel">
    <div className="settings-row">
      <span className="settings-icon"><RefreshCcw size={20}/></span>
      <div className="update-status-details">
        <h2>{de?'Automatische Updates':'Automatic updates'}</h2>
        <p className="small-note" role="status" aria-live="polite">{labels[status.stage]||labels.inactive}</p>
        {status.reason&&<p className="small-note">{reasonLabels[status.reason]||reasonLabels.release}</p>}
        {status.version&&<p className="small-note">{de?'Installierte Webversion: ':'Installed web version: '}{status.version==='builtin'?(de?'APK-Version':'APK version'):status.version}</p>}
        {status.availableVersion&&<p className="small-note">{de?'Neueste geprüfte Version: ':'Latest verified version: '}{status.availableVersion}</p>}
        {checked&&<p className="small-note">{de?'Letzte Prüfung: ':'Last checked: '}{checked}</p>}
        {status.stage==='ready'&&<div className="update-activation-note" role="note">
          <strong>{de?'Das Update ist sicher heruntergeladen, aber noch nicht aktiv.':'The update is safely downloaded but not yet active.'}</strong>
          <p>{de
            ? 'Die Aktivierung kann etwas dauern. MiniNex vollständig schliessen (auch aus der Android-App-Übersicht entfernen) und erneut öffnen. Falls nötig nochmals schliessen und öffnen.'
            : 'Activation may take some time. Fully close MiniNex (including removing it from Android recent apps) and reopen it. Repeat if needed.'}</p>
          <p>{de
            ? 'Die bisherige Version läuft bis dahin weiter. Bitte MiniNex nicht deinstallieren: Wallet und PAPER-Daten bleiben erhalten.'
            : 'The previous version keeps working in the meantime. Do not uninstall MiniNex: wallet and PAPER data are preserved.'}</p>
          <p>{de
            ? 'Erst wenn „Installierte Webversion“ die neueste geprüfte Version anzeigt, ist das Update aktiviert.'
            : 'The update is active only when “Installed web version” matches the latest verified version.'}</p>
        </div>}
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
