import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {activeOtaVersion,isVerifiedDownloadedBundle,parseOtaRelease,shouldInstallOta,otaBuildNumber} from '../src/otaRelease.js';

const ABI='a'.repeat(64),SHA='b'.repeat(40),CHECKSUM='c'.repeat(64);
const tag='mininex-ota-v0.8.253';
const assetUrl='https://github.com/Quantumpepe/MiniNex/releases/download/'+tag+'/mininex-web.zip';
const good=()=>({
  tag_name:tag,draft:false,prerelease:false,
  published_at:'2026-11-09T20:05:00Z',target_commitish:SHA,
  body:'Verified MiniNex web-only update.\nnative-abi: '+ABI+'\nsource-commit: '+SHA+'\nsha256: '+CHECKSUM+'\n',
  assets:[{name:'mininex-web.zip',browser_download_url:assetUrl,
    size:1954021,digest:'sha256:'+CHECKSUM}]
});

test('accepts only an immutable, hashed GitHub release for this exact native shell',()=>{
  const c=parseOtaRelease(good(),ABI);
  assert.equal(c.version,'0.8.253');
  assert.equal(c.buildNumber,253);
  assert.equal(c.checksum,CHECKSUM);
  assert.equal(c.url,assetUrl);
  assert.equal(c.sourceSha,SHA);
});

test('rejects misleading, unauthorized, or incompatible downloads',()=>{
  const mutations=[
    x=>{x.tag_name='v0.8.253';},
    x=>{x.draft=true;},
    x=>{x.prerelease=true;},
    x=>{x.assets[0].browser_download_url='https://evil.test/payload.zip';},
    x=>{x.assets[0].digest='sha256:'+'d'.repeat(64);},
    x=>{x.assets[0].size=999999999;},
    x=>{x.assets[0].name='evil.zip';},
    x=>{x.body=x.body.replace('native-abi: '+ABI,'native-abi: '+'e'.repeat(64));},
    x=>{x.body=x.body.replace('source-commit: '+SHA,'source-commit: '+'f'.repeat(40));},
    x=>{x.body=x.body.replace('sha256: '+CHECKSUM,'sha256: nope');},
    x=>{x.published_at='not a date';},
  ];
  for(const mutate of mutations){
    const input=good();mutate(input);
    assert.equal(parseOtaRelease(input,ABI),null);
  }
  assert.equal(parseOtaRelease(good(),'d'.repeat(64)),null);
});

test('never downgrades, reruns a queued bundle, or overwrites an equal/newer APK',()=>{
  const c=parseOtaRelease(good(),ABI);
  assert.equal(otaBuildNumber('builtin'),null);
  assert.equal(otaBuildNumber('0.8.253'),253);
  assert.equal(shouldInstallOta(c,{
    currentVersion:'builtin',nativeBuildSha:'a'.repeat(40),nativeBuildAt:'2026-11-09T19:00:00Z'
  }),true);
  assert.equal(shouldInstallOta(c,{
    currentVersion:'builtin',nativeBuildSha:SHA,nativeBuildAt:'2026-11-09T19:00:00Z'
  }),false);
  assert.equal(shouldInstallOta(c,{
    currentVersion:'builtin',nativeBuildSha:'a'.repeat(40),nativeBuildAt:'2026-11-10T00:00:00Z'
  }),false);
  assert.equal(shouldInstallOta(c,{currentVersion:'0.8.252'}),true);
  assert.equal(shouldInstallOta(c,{currentVersion:'0.8.253'}),false);
  assert.equal(shouldInstallOta(c,{currentVersion:'0.8.254'}),false);
  assert.equal(shouldInstallOta(c,{currentVersion:'unknown'}),false);
  assert.equal(shouldInstallOta(c,{currentVersion:'0.8.252',pendingVersion:'0.8.253'}),false);
});

test('native ABI is deterministic and installation uses a pinned Capacitor 8 updater',()=>{
  const fingerprint=execFileSync(process.execPath,['scripts/native-abi.mjs'],{encoding:'utf8'});
  assert.match(fingerprint,/^[a-f0-9]{64}\n$/);
  const pkg=JSON.parse(readFileSync(new URL('../package.json',import.meta.url),'utf8'));
  const config=JSON.parse(readFileSync(new URL('../capacitor.config.json',import.meta.url),'utf8'));
  assert.match(pkg.dependencies['@capacitor/core'],/^\^8/);
  assert.match(pkg.dependencies['@capgo/capacitor-updater'],/^8\./);
  assert.equal(config.plugins.CapacitorUpdater.autoUpdate,false);
  assert.equal(config.plugins.CapacitorUpdater.statsUrl,'');
});

test('APK boot readiness and release publisher protect native layout and secrets',()=>{
  const main=readFileSync(new URL('../src/main.jsx',import.meta.url),'utf8');
  const updater=readFileSync(new URL('../src/otaUpdater.js',import.meta.url),'utf8');
  const app=readFileSync(new URL('../src/App.jsx',import.meta.url),'utf8');
  const ci=readFileSync(new URL('../../.github/workflows/frontend-check.yml',import.meta.url),'utf8');
  const android=readFileSync(new URL('../../.github/workflows/android-debug.yml',import.meta.url),'utf8');
  assert.ok(main.indexOf('notifyMiniNexReady();')<main.indexOf('createRoot('));
  assert.match(updater,/CapacitorUpdater\.notifyAppReady\(\)/);
  assert.match(updater,/CapacitorUpdater\.download\(/);
  assert.match(updater,/CapacitorUpdater\.next\(\{id:bundle.id\}\)/);
  assert.doesNotMatch(updater,/CapacitorUpdater\.set\(/);
  assert.match(app,/<UpdateStatus lang=\{language\}\/>/);
  assert.match(ci,/needs: frontend-check/);
  assert.match(ci,/github\.event_name == 'push' && github\.ref == 'refs\/heads\/main'/);
  assert.match(ci,/native-abi: %s/);
  assert.match(ci,/gh release create/);
  assert.match(android,/VITE_MININEX_NATIVE_ABI/);
  assert.doesNotMatch(android,/gh release create/);
});

test('Capgo builtin is identified by bundle ID, not native version like 1.0',()=>{
  const candidate=parseOtaRelease(good(),ABI);
  const installed=activeOtaVersion({id:'builtin',version:'1.0'});
  assert.equal(installed,'builtin');
  assert.equal(activeOtaVersion({id:'builtin',version:'0.8.10'}),'builtin');
  assert.equal(shouldInstallOta(candidate,{
    currentVersion:installed,nativeBuildSha:'a'.repeat(40),
    nativeBuildAt:'2026-11-09T19:00:00Z'
  }),true);
  // Previous code incorrectly treated '1.0' as a non-OTA version, rejecting update.
  assert.equal(shouldInstallOta(candidate,{
    currentVersion:'1.0',nativeBuildSha:'a'.repeat(40),
    nativeBuildAt:'2026-11-09T19:00:00Z'
  }),false);
});

test('downloaded OTA versions are comparable but corrupted native state is not',()=>{
  assert.equal(activeOtaVersion({id:'x1',version:'0.8.252'}),'0.8.252');
  assert.equal(activeOtaVersion({id:'x1',version:'not-an-ota-version'}),null);
  assert.equal(activeOtaVersion({version:'0.8.252'}),null);
  assert.equal(activeOtaVersion(null),null);
  const candidate=parseOtaRelease(good(),ABI);
  assert.equal(shouldInstallOta(candidate,{currentVersion:activeOtaVersion({id:'x1',version:'0.8.252'})}),true);
  assert.equal(shouldInstallOta(candidate,{currentVersion:activeOtaVersion({id:'x1',version:'0.8.254'})}),false);
});

test('OTA status shows current installed version, latest release, and an actionable check',()=>{
  const ui=readFileSync(new URL('../src/UpdateStatus.jsx',import.meta.url),'utf8');
  const updater=readFileSync(new URL('../src/otaUpdater.js',import.meta.url),'utf8');
  assert.match(ui,/Jetzt prüfen/);
  assert.match(ui,/checkMiniNexUpdates\(\{force:true\}\)/);
  assert.match(ui,/status\.availableVersion/);
  assert.match(ui,/status\.version/);
  assert.match(ui,/status\.reason/);
  assert.match(updater,/activeOtaVersion\(current\?\.bundle\)/);
  assert.doesNotMatch(updater,/current\?\.bundle\?\.version\|\|'builtin'/);
  assert.match(updater,/stage:'downloading'/);
  assert.match(updater,/stage:'ready'/);
});

test('Capgo v8 pending is a SUCCESSFUL download awaiting activation, not a failure',()=>{
  // Android v8.54.2 finishDownload() creates BundleInfo with status PENDING;
  // the downloaded ZIP was already checked against expected SHA-256.
  const pending={id:'abc1234567',version:'0.8.173',status:'pending',checksum:CHECKSUM};
  assert.equal(isVerifiedDownloadedBundle(pending,'0.8.173'),true);
  assert.equal(isVerifiedDownloadedBundle({...pending,status:'success'},'0.8.173'),true);
  assert.equal(isVerifiedDownloadedBundle({...pending,status:'downloading'},'0.8.173'),false);
  assert.equal(isVerifiedDownloadedBundle({...pending,status:'error'},'0.8.173'),false);
  assert.equal(isVerifiedDownloadedBundle({...pending,status:'deleted'},'0.8.173'),false);
  assert.equal(isVerifiedDownloadedBundle({...pending,status:null},'0.8.173'),false);
  assert.equal(isVerifiedDownloadedBundle({...pending,id:'builtin'},'0.8.173'),false);
  assert.equal(isVerifiedDownloadedBundle({...pending,id:''},'0.8.173'),false);
  assert.equal(isVerifiedDownloadedBundle({...pending,version:'0.8.169'},'0.8.173'),false);
  assert.equal(isVerifiedDownloadedBundle(null,'0.8.173'),false);
  const updater=readFileSync(new URL('../src/otaUpdater.js',import.meta.url),'utf8');
  assert.match(updater,/isVerifiedDownloadedBundle\(bundle,candidate\.version\)/);
  assert.match(updater,/CapacitorUpdater\.next\(\{id:bundle\.id\}\)/);
  assert.doesNotMatch(updater,/bundle\?\.status!=='success'/);
});


test('OTA GitHub failure diagnosis is specific and safe for users',()=>{
  const updater=readFileSync(new URL('../src/otaUpdater.js',import.meta.url),'utf8');
  const ui=readFileSync(new URL('../src/UpdateStatus.jsx',import.meta.url),'utf8');
  assert.match(updater,/response\.status===429/);
  assert.match(updater,/x-ratelimit-remaining/);
  assert.match(updater,/release-rate-limit/);
  assert.match(updater,/release-network/);
  assert.match(updater,/release-invalid-response/);
  assert.match(updater,/\[502,503,504\]/);
  assert.match(updater,/ERROR_RETRY_INTERVAL_MS/);
  assert.match(ui,/HTTP \$\{status\.httpStatus\}/);
  assert.match(ui,/GitHub-Anfragelimit erreicht/);
  assert.doesNotMatch(ui,/GitHub konnte nicht erreicht werden/);
});
