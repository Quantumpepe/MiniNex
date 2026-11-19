import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loadBtcTransferEngine} from '../src/btcTransferLoader.js';

test('BTC engine cannot load before explicit user action',async()=>{
  let called=0;
  const loader=()=>{called++;return {};};
  assert.deepEqual(await loadBtcTransferEngine({loader}),
    {ok:false,reason:'not_requested'});
  assert.equal(called,0);
});
test('BTC crypto import failure leaves MiniNex startup functional',async()=>{
  const result=await loadBtcTransferEngine({
    enabled:true,loader:async()=>{throw Error('WebView crypto unavailable');}
  });
  assert.deepEqual(result,{ok:false,reason:'engine_unavailable'});
});
test('BTC loader accepts only complete transaction implementation',async()=>{
  const incomplete=await loadBtcTransferEngine({
    enabled:true,loader:async()=>({prepare(){}})
  });
  assert.deepEqual(incomplete,{ok:false,reason:'engine_incompatible'});
  const engine={prepare(){},sign(){},broadcast(){}};
  const ok=await loadBtcTransferEngine({enabled:true,loader:async()=>engine});
  assert.equal(ok.ok,true);
  assert.equal(ok.engine,engine);
});
test('no BTC crypto loader import exists in production app or wallet',()=>{
  for(const path of ['../src/main.jsx','../src/App.jsx',
                     '../src/PrivyWalletPanel.jsx']){
    const file=readFileSync(new URL(path,import.meta.url),'utf8');
    assert.doesNotMatch(file,/btcTransferLoader|btcPrivyRawHash|btcSignerAdapter/);
  }
});
