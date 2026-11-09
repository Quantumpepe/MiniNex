import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const source=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const status=source('../src/UpdateStatus.jsx');
const updater=source('../src/otaUpdater.js');
const styles=source('../src/styles.css');

test('pending OTA shows rotating wheel and brief explanation, without calling a download installed',()=>{
  assert.match(status,/import \{CheckCircle2,RefreshCcw\} from 'lucide-react'/);
  assert.match(status,/const pending=status\.stage==='ready'/);
  assert.match(status,/const spinning=working\|\|pending/);
  assert.match(status,/spinning\?' is-spinning':''/);
  assert.match(status,/pending&&<p className="small-note update-activation-note"/);
  assert.match(status,/Die Aktivierung kann etwas dauern/);
  assert.match(status,/App vollständig schliessen und erneut öffnen/);
  assert.match(status,/Nicht deinstallieren/);
  assert.match(status,/Wallet und PAPER-Daten bleiben erhalten/);
  assert.match(status,/Activation can take some time/);
  assert.match(styles,/\.update-progress-icon\.is-spinning svg/);
  assert.match(styles,/animation:mininex-ota-spin/);
  assert.match(styles,/@keyframes mininex-ota-spin/);
  assert.match(styles,/@media\(prefers-reduced-motion:reduce\)/);
});

test('wheel becomes green check only after native current() confirms installed latest',()=>{
  assert.match(status,/const installedLatest=status\.stage==='current'&&/);
  assert.match(status,/status\.version===status\.availableVersion/);
  assert.match(status,/installedLatest\?<CheckCircle2 size=\{22\}\/>:<RefreshCcw size=\{22\}\/>/);
  assert.match(status,/installedLatest\?'Update aktiviert · aktuell':'Update active · up to date'/);
  assert.match(status,/installedLatest\?' is-complete':''/);
  assert.match(styles,/\.update-progress-icon\.is-complete\{/);
  assert.match(styles,/color:#79f0ba/);
  assert.match(updater,/await CapacitorUpdater\.next\(\{id:bundle\.id\}\)/);
  assert.match(updater,/publish\(\{stage:'ready',version:installed,availableVersion:candidate\.version,reason:null\}\)/);
  assert.doesNotMatch(updater,/publish\(\{stage:'ready',version:candidate\.version/);
  assert.match(updater,/stage:next\?\.version===candidate\.version\?'ready':'current'/);
  assert.match(updater,/const installed=activeOtaVersion\(current\?\.bundle\)/);
  assert.match(status,/status\.version==='builtin'/);
  assert.match(status,/status\.availableVersion/);
});
