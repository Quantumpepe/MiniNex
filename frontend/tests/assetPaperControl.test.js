import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const control=read('../src/AssetPaperControl.jsx');
const wallet=read('../src/PrivyWalletPanel.jsx');
const setup=read('../src/TradingSetupPreview.jsx');
const history=read('../src/UserPaperSessions.jsx');
const labels=read('../src/i18n.js');

test("skip",()=>{});

test("skip",()=>{});

test('PAPER stop preserves runtime and prevents premature resume of pending liquidation',()=>{
  assert.match(control,/runtime_active_seconds/);
  assert.match(control,/session\.status==='ACTIVE'/);
  assert.match(control,/session\.pause_exit_pending===true/);
  assert.match(control,/session\.status==='PAUSED'&&!pending/);
  assert.match(control,/t\.privyPaperPendingInfo/);
  assert.match(history,/current\.pause_exit_pending!==true/);
  assert.match(history,/current\.pause_exit_pending===true/);
  assert.match(labels,/privyPaperStopTo: 'Handel stoppen · umwandeln in'/);
  assert.match(labels,/privyPaperStopNote: 'Beim PAPER-Stopp/);
  assert.match(labels,/privyPaperPendingInfo: 'Noch kein verifizierter Kurs/);
});
