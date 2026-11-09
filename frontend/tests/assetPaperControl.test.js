import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const control=read('../src/AssetPaperControl.jsx');
const wallet=read('../src/PrivyWalletPanel.jsx');
const setup=read('../src/TradingSetupPreview.jsx');
const history=read('../src/UserPaperSessions.jsx');
const labels=read('../src/i18n.js');

test('BTC and ETH settings each include authenticated stop-to-selected-stable PAPER control',()=>{
  assert.match(wallet,/const renderSessionControls = asset =>/);
  assert.match(wallet,/<AssetPaperControl asset=\{asset\} t=\{t\} getAccessToken=\{getAccessToken\}/);
  assert.match(wallet,/renderSessionControls=\{renderSessionControls\}/);
  assert.match(setup,/\{renderSessionControls\?\.\(asset\)\}/);
  assert.match(control,/API_BASE\+'\/api\/user-paper\/'\+asset/);
  assert.match(control,/headers:\{Authorization:'Bearer '\+token,Accept:'application\/json'\}/);
  assert.match(control,/command\('pause'\)/);
  assert.match(control,/command\('resume'\)/);
  assert.match(control,/command\('start'\)/);
  assert.match(control,/t\.privyPaperStopTo\} \{stable\}/);
  assert.match(control,/\['USDC','USDT'\]\.includes\(session\.settlement\)/);
  assert.match(control,/wallet_funds_used===false/);
  assert.match(control,/data\.real_trading_enabled===false/);
  assert.match(control,/paper_available!==true/);
});

test('pause keeps settings panel open, even while selecting mode, budget, save or pausing',()=>{
  assert.match(setup,/const \[opened,setOpened\]=useState\(\{BTC:false,ETH:false\}\)/);
  assert.match(setup,/const toggleAsset = asset => setOpened/);
  assert.equal([...setup.matchAll(/toggleAsset\(asset\)/g)].length,1);
  assert.equal([...setup.matchAll(/setOpened\(/g)].length,1);
  assert.match(setup,/hidden=\{!open\}/);
  assert.match(setup,/onClick=\{\(\)=>save\(asset\)\}/);
  assert.match(wallet,/onClick=\{\(\)=>saveBudget\(asset\)\}/);
  assert.doesNotMatch(control,/toggleAsset|setOpened|onClose|closeModal/);
  assert.doesNotMatch(control,/sendTransaction|sendBitcoin|signMessage|privateKey/);
});

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
