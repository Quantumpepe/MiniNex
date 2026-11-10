import {Capacitor} from '@capacitor/core';
import {API_BASE} from './data.js';
import {App as NativeApp} from '@capacitor/app';
import {CapacitorUpdater} from '@capgo/capacitor-updater';
import {activeOtaVersion,isVerifiedDownloadedBundle,parseOtaRelease,shouldInstallOta} from './otaRelease.js';

// The Render backend caches GitHub Releases, avoiding per-device REST API limits.
const RELEASE_URL=API_BASE+'/api/updates/latest';
const NATIVE_ABI=String(import.meta.env?.VITE_MININEX_NATIVE_ABI||'').toLowerCase();
const NATIVE_SHA=String(import.meta.env?.VITE_MININEX_BUILD_SHA||'').toLowerCase();
const NATIVE_BUILT_AT=String(import.meta.env?.VITE_MININEX_BUILD_AT||'');
const CHECK_INTERVAL_MS=60*60*1000;
const ERROR_RETRY_INTERVAL_MS=2*60*1000;
const TRANSIENT_RETRY_DELAY_MS=5000;
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));

let lastCheck=0;
let checking=false;
let started=false;
let retryScheduled=false;
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
  const interval=state.stage==='error'&&state.reason!=='release-rate-limit'
    ? ERROR_RETRY_INTERVAL_MS:CHECK_INTERVAL_MS;
  if(!force&&now-lastCheck<interval)return;
  checking=true;
  lastCheck=now;
  publish({stage:'checking',lastChecked:now,reason:null,httpStatus:null});
  let phase='release';
  try{
    const request=()=>fetch(RELEASE_URL,{
      method:'GET',headers:{Accept:'application/json'},cache:'no-store'
    });
    let response;
    try {response=await request();}
    catch(err){
      if(err instanceof TypeError)throw Object.assign(new Error('Network error'),{otaReason:'release-network'});
      throw err;
    }
    // A brief bounded retry for transient upstream errors, never on 403/429.
    if([502,503,504].includes(response.status)){
      await wait(TRANSIENT_RETRY_DELAY_MS);
      try {response=await request();}
      catch(err){
        if(err instanceof TypeError)throw Object.assign(new Error('Network error'),{otaReason:'release-network'});
        throw err;
      }
    }
    // Public Render OTA metadata endpoint returns 503 while its cache is empty.
    if(!response.ok){
      const limited=response.status===429||
        (response.status===403&&(response.headers.get('x-ratelimit-remaining')==='0'||
          response.headers.get('retry-after')!==null));
      throw Object.assign(new Error('OTA metadata HTTP '+response.status),{
        otaReason:limited?'release-rate-limit':'release-http',httpStatus:response.status
      });
    }
    let release;
    try {release=await response.json();}
    catch {throw Object.assign(new Error('Invalid OTA response'),{otaReason:'release-invalid-response'});}
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
  }catch(error){
    // Fail closed: show the real release lookup failure, not a generic outage.
    // Never expose wallet data or error body, only a safe HTTP status code.
    const reason=phase==='release'?(error?.otaReason||'release-network'):phase;
    publish({stage:'error',reason,httpStatus:phase==='release'?(error?.httpStatus||null):null});
    // Retry one failed check after a short delay without retrying GitHub rate limits.
    // Keep the active native/PAPER bundle untouched on all failures.
    if(reason!=='release-rate-limit'&&!retryScheduled){
      retryScheduled=true;
      setTimeout(()=>{
        retryScheduled=false;
        if(state.stage==='error')void checkMiniNexUpdates();
      },ERROR_RETRY_INTERVAL_MS);
    }
  }finally{checking=false;}
}

// Apply a *verified already queued* update only after a deliberate user tap.
// next() never activates while the app stays foregrounded; reload() does.
// Never load a random stored bundle or reload with a missing/mismatched queue.
// This operation intentionally restarts the JS/WebView and may drop unsaved UI
// edits, so it must never be called automatically during an active session.
export async function activateMiniNexUpdate() {
  if(!otaIsSupported()||checking||state.stage!=='ready'||!state.availableVersion)
    return;
  checking=true;
  const target=state.availableVersion;
  publish({stage:'activating',reason:null});
  try {
    const [current,next]=await Promise.all([
      CapacitorUpdater.current(),CapacitorUpdater.getNextBundle()
    ]);
    const installed=activeOtaVersion(current?.bundle);
    if(installed===target){
      publish({stage:'current',version:installed,availableVersion:target,reason:null});
      return;
    }
    if(!installed||!next||typeof next.id!=='string'||!next.id||
       next.id==='builtin'||next.version!==target)
      throw new Error('Verified pending update not found');
    // Capgo v8 explicitly documents reload() as applying a bundle queued
    // via next(); unlike set(), this never selects an arbitrary bundle ID.
    await CapacitorUpdater.reload();
    // On success the native WebView reloads; a fresh startup confirms current().
    // Do not claim success before that independent native verification.
  }catch {
    publish({stage:'error',reason:'activation',version:null});
  }finally {
    checking=false;
  }
}

// Read release metadata through our Render backend cache; no Capgo telemetry,
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
