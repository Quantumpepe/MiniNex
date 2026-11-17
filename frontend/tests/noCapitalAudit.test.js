import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {COINS} from '../src/coins.js';
import {SEND_ASSETS,sendAssetSpec,exactSendUnits} from '../src/walletSendRules.js';
import {MANUAL_SWAP_ASSETS,manualSwapQuoteRequest} from '../src/manualSwapRules.js';

const source=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const all=COINS.map(coin=>coin.symbol);

test('all 12 displayed coins have explicit send capability classification',()=>{
  assert.deepEqual(all,['BTC','ETH','BNB','SOL','XRP','TRX','POL','AVAX',
                        'LINK','DOGE','UNI','ARB']);
  assert.deepEqual(SEND_ASSETS.slice(0,12),all);
  for(const asset of all){
    const spec=sendAssetSpec(asset);
    if(['ETH','BNB','POL','AVAX','LINK','UNI'].includes(asset)){
      assert.ok(spec,asset);
      assert.ok(Number.isInteger(spec.chainId));
      assert.ok(Number.isInteger(spec.decimals));
      assert.equal(exactSendUnits('0',spec.decimals),null);
    }else{
      assert.equal(spec,null,asset);
    }
  }
});

test('both stablecoin swap directions are quote previews, not send instructions',()=>{
  assert.deepEqual(MANUAL_SWAP_ASSETS.slice(0,12),all);
  for(const asset of all)for(const stable of ['USDC','USDT']){
    for(const [from,to] of [[asset,stable],[stable,asset]]){
      const request=manualSwapQuoteRequest(from,to,'1');
      assert.ok(request,from+'->'+to);
      assert.equal(request.from,from);
      assert.equal(request.to,to);
      assert.ok(request.path.startsWith('/api/'));
    }
  }
  const ui=source('../src/ManualSwapPreview.jsx');
  assert.doesNotMatch(ui,/eth_sendTransaction|sendRawTransaction|signTransaction\(/);
  assert.match(ui,/t\.walletSwapNotEnabled/);
  assert.match(ui,/validManualSwapQuote/);
});

test('wallet send and BTC feature require an explicit user action',()=>{
  const wallet=source('../src/WalletSend.jsx');
  const btc=source('../src/BtcSendEntry.jsx');
  assert.match(wallet,/async function send\(\)/);
  assert.match(wallet,/provider\.request\(\{method:'eth_sendTransaction'/);
  assert.match(wallet,/onClick=\{send\}/);
  assert.match(btc,/onClick=\{\(\)=>setRequested\(true\)\}/);
  assert.match(btc,/await import\('buffer'\)/);
  assert.doesNotMatch(source('../src/main.jsx'),/BtcWalletSend\.jsx/);
});
