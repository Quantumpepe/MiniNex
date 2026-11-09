import {Capacitor} from '@capacitor/core';
import {App as NativeApp} from '@capacitor/app';
import {CapacitorUpdater} from '@capgo/capacitor-updater';
import {activeOtaVersion,isVerifiedDownloadedBundle,parseOtaRelease,shouldInstallOta} from './otaRelease.js';

const RELEASE_URL='https://api.github.com/repos/Quantumpepe/MiniNex/releases/latest';
const NATIVE_ABI=String(import.meta.env?.VITE_MININEX_NATIVE_ABI||'').toLowerCase();
const NATIVE_SHA=String(import.meta.env?.VITE_MININEX_BUILD_SHA||'').toLowerCase();
const NATIVE_BUILT_AT=String(import.meta.env?.VITE_MININEX_BUILD_AT||'');
const CHECK_INTERVAL_MS=60*60*1000;

let lastCheck=0;
let checking=false;
let started=false;
let state={stage:'inactive',version:null,availableVersion:null,lastChecked:null,reason:null};
const subscribers=new Set();
const publish=next=>{
  state={...state,...next};
  for(const listener of subscribers){try{listener(state);}catch{/* UI listener failed */}}
};
export function subscribeOtaStatus(fn) {
  subscribers.add(fn);
  fn(state);
  return ()=>subscribers.delete(fn);
}

export function otaIsSupported() {
  return Capacitor.isNativePlatform()&&Capacitor.isPluginAvailable('CapacitorUpdater')&&
    /^[a-f0-9]{64}$/.test(NATIVE_ABI)&&/^[a-f0-9]{40}$/.test(NATIVE_SHA)&&
    Number.isFinite(Date.parse(NATIVE_BUILT_AT));
}

// Call immediately after the JS entry point starts, before network calls.
// Capgo will automatically roll back an update that fails to boot in time.
export function notifyMiniNexReady() {
  if(!Capacitor.isNativePlatform()||!Capacitor.isPluginAvailable('CapacitorUpdater'))return;
  CapacitorUpdater.notifyAppReady().catch(()=>publish({stage:'error'}));
}

export async function checkMiniNexUpdates({force=false}={}) {
  if(!otaIsSupported()||checking)return;
  const now=Date.now();
  if(!force&&now-lastCheck<CHECK_INTERVAL_MS)return;
  checking=true;
  lastCheck=now;
  publish({stage:'checking',lastChecked:now,reason:null});
  let phase='release';
  try{
    const response=await fetch(RELEASE_URL,{
      method:'GET',headers:{Accept:'application/vnd.github+json'},cache:'no-store'
    });
    // First installation before the first published release.
    if(response.status===404){publish({stage:'unavailable',reason:'no-release'});return;}
    if(!response.ok)throw Error('Release lookup unavailable');
    const release=await response.json();
    const candidate=parseOtaRelease(release,NATIVE_ABI);
    if(!candidate){publish({stage:'unavailable',reason:'incompatible-release'});return;}
    publish({availableVersion:candidate.version});
    phase='installed-version';
    const [current,next]=await Promise.all([
      CapacitorUpdater.current(),CapacitorUpdater.getNextBundle()
    ]);
    // Capgo's builtin bundle often reports the native appVersion as its
    // 'version' (e.g. 1.0), while bundle.id is literally 'builtin'.
    const installed=activeOtaVersion(current?.bundle);
    if(!installed)throw Error('Native installed bundle unavailable');
    if(!shouldInstallOta(candidate,{
      currentVersion:installed,
      pendingVersion:next?.version||null,
      nativeBuildSha:NATIVE_SHA,nativeBuildAt:NATIVE_BUILT_AT
    })){
      publish({stage:next?.version===candidate.version?'ready':'current',
        version:installed,reason:null});
      return;
    }
    phase='download';
    publish({stage:'downloading',version:installed,availableVersion:candidate.version});
    const bundle=await CapacitorUpdater.download({
      version:candidate.version,url:candidate.url,checksum:candidate.checksum
    });
    // Capgo 8 returns "pending" for a fully downloaded, checksum-verified ZIP.
    // "success" is the installed state. Rejecting "pending" previously
    // prevented every valid Android OTA from ever reaching next().
    if(!isVerifiedDownloadedBundle(bundle,candidate.version))
      throw Error('Downloaded bundle is not ready for activation');
    phase='activation';
    await CapacitorUpdater.next({id:bundle.id});
    // App keeps running; update is loaded on next restart, not during a trade.
    // The downloaded bundle is queued, NOT installed yet. Report the actual
    // running version until CapacitorUpdater.current() confirms activation.
    // Otherwise the settings screen falsely implies activation has finished.
    publish({stage:'ready',version:installed,availableVersion:candidate.version,reason:null});
  }catch{
    // Fail closed. Running app and paper trading remain unchanged if offline.
    // Show which part failed rather than incorrectly claiming 'up to date'.
    publish({stage:'error',reason:phase});
  }finally{checking=false;}
}

// Use the hosted release API only, no Capgo telemetry, custom update server,
// wallet credentials or private keys. Foreground checks are rate limited.
export function startMiniNexUpdates() {
  if(started||!otaIsSupported())return;
  started=true;
  publish({stage:'checking'});
  void checkMiniNexUpdates({force:true});
  NativeApp.addListener('appStateChange',({isActive})=>{
    if(isActive)void checkMiniNexUpdates();
  }).catch(()=>{});
}
