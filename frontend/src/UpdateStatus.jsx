import React,{useEffect,useState} from 'react';
import {RefreshCcw} from 'lucide-react';
import {otaIsSupported,subscribeOtaStatus} from './otaUpdater.js';

export default function UpdateStatus({lang}) {
  const [status,setStatus]=useState({stage:'inactive'});
  useEffect(()=>subscribeOtaStatus(setStatus),[]);
  const de=lang==='de';
  const labels={
    inactive:de?'Nur in der installierten Android-/iOS-App':'Only in the installed Android/iOS app',
    current:de?'Automatische Updates aktiv':'Automatic updates enabled',
    checking:de?'Prüfe auf neue Version':'Checking for an update',
    downloading:de?'Lade geprüfte Version herunter':'Downloading verified update',
    ready:de?'Update bereit – wird beim nächsten App-Start aktiviert':'Update ready – activates on next app launch',
    error:de?'Update-Prüfung fehlgeschlagen – aktuelle Version bleibt aktiv':'Update check failed – current version remains active'
  };
  return <section className="panel settings-panel">
    <div className="settings-row">
      <span className="settings-icon"><RefreshCcw size={20}/></span>
      <div><h2>{de?'Automatische Updates':'Automatic updates'}</h2>
        <p className="small-note">{labels[status.stage]||labels.inactive}</p>
        {otaIsSupported()&&<p className="small-note">{de?'Nur geprüfte Oberflächen-Updates; native Änderungen brauchen weiterhin eine APK.':'Verified interface updates only; native changes still require an APK.'}</p>}
      </div>
    </div>
  </section>;
}
