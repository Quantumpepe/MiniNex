import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const source=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const status=source('../src/UpdateStatus.jsx');
const updater=source('../src/otaUpdater.js');
const styles=source('../src/styles.css');

test('pending OTA explains activation is delayed, requires full restart and preserves app data in both languages',()=>{
  assert.match(status,/ready:de\?'Update heruntergeladen – Aktivierung noch ausstehend'/);
  assert.match(status,/status\.stage==='ready'&&<div className="update-activation-note"/);
  assert.match(status,/Android-App-Übersicht entfernen/);
  assert.match(status,/erneut öffnen/);
  assert.match(status,/Falls nötig nochmals schliessen und öffnen/);
  assert.match(status,/Bitte MiniNex nicht deinstallieren/);
  assert.match(status,/Wallet und PAPER-Daten bleiben erhalten/);
  assert.match(status,/Installierte Webversion/);
  assert.match(status,/neueste geprüfte Version/);
  assert.match(status,/Activation may take some time/);
  assert.match(status,/Do not uninstall MiniNex/);
  assert.match(styles,/\.update-activation-note\{/);
});

test('a queued update does not falsely report new version as installed',()=>{
  assert.match(updater,/await CapacitorUpdater\.next\(\{id:bundle\.id\}\)/);
  assert.match(updater,/publish\(\{stage:'ready',version:installed,availableVersion:candidate\.version,reason:null\}\)/);
  assert.doesNotMatch(updater,/publish\(\{stage:'ready',version:candidate\.version/);
  assert.match(updater,/stage:next\?\.version===candidate\.version\?'ready':'current'/);
  assert.match(updater,/const installed=activeOtaVersion\(current\?\.bundle\)/);
  assert.match(status,/status\.version==='builtin'/);
  assert.match(status,/status\.availableVersion/);
});
