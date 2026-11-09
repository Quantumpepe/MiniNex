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
    ready:de?'Update bereit – App schliessen und neu öffnen':'Update ready – close and reopen the app',
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
