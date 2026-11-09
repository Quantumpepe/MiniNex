import {Capacitor} from '@capacitor/core';
import {App as NativeApp} from '@capacitor/app';
import {CapacitorUpdater} from '@capgo/capacitor-updater';
import {parseOtaRelease,shouldInstallOta} from './otaRelease.js';

const RELEASE_URL='https://api.github.com/repos/Quantumpepe/MiniNex/releases/latest';
const NATIVE_ABI=String(import.meta.env?.VITE_MININEX_NATIVE_ABI||'').toLowerCase();
const NATIVE_SHA=String(import.meta.env?.VITE_MININEX_BUILD_SHA||'').toLowerCase();
const NATIVE_BUILT_AT=String(import.meta.env?.VITE_MININEX_BUILD_AT||'');
const CHECK_INTERVAL_MS=60*60*1000;

let lastCheck=0;
let checking=false;
let started=false;
let state={stage:'inactive',version:null,lastChecked:null};
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
  publish({stage:'checking',lastChecked:now});
  try{
    const response=await fetch(RELEASE_URL,{
      method:'GET',headers:{Accept:'application/vnd.github+json'},cache:'no-store'
    });
    // First installation before the first published release.
    if(response.status===404){publish({stage:'current'});return;}
    if(!response.ok)throw Error('Release lookup unavailable');
    const release=await response.json();
    const candidate=parseOtaRelease(release,NATIVE_ABI);
    if(!candidate){publish({stage:'current'});return;}
    const [current,next]=await Promise.all([
      CapacitorUpdater.current(),CapacitorUpdater.getNextBundle()
    ]);
    if(!shouldInstallOta(candidate,{
      currentVersion:current?.bundle?.version||'builtin',
      pendingVersion:next?.version||null,
      nativeBuildSha:NATIVE_SHA,nativeBuildAt:NATIVE_BUILT_AT
    })){
      publish({stage:next?.version===candidate.version?'ready':'current',
        version:current?.bundle?.version||'builtin'});
      return;
    }
    publish({stage:'downloading',version:candidate.version});
    const bundle=await CapacitorUpdater.download({
      version:candidate.version,url:candidate.url,checksum:candidate.checksum
    });
    if(!bundle?.id||bundle?.version!==candidate.version||bundle?.status!=='success')
      throw Error('Bundle not verified');
    await CapacitorUpdater.next({id:bundle.id});
    // App keeps running; update is loaded on next restart, not during a trade.
    publish({stage:'ready',version:candidate.version});
  }catch{
    // Fail closed. Running app and paper trading remain unchanged if offline.
    publish({stage:'error'});
  }finally{checking=false;}
}

// Use the hosted release API only, no Capgo telemetry, custom update server,
// wallet credentials or private keys. Foreground checks are rate limited.
export function startMiniNexUpdates() {
  if(started||!otaIsSupported())return;
  started=true;
  publish({stage:'current'});
  void checkMiniNexUpdates({force:true});
  NativeApp.addListener('appStateChange',({isActive})=>{
    if(isActive)void checkMiniNexUpdates();
  }).catch(()=>{});
}
