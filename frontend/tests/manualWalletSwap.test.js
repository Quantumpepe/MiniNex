import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const component=readFileSync(new URL('../src/ManualSwapPreview.jsx',import.meta.url),'utf8');
const signer=readFileSync(new URL('../src/EvmLiveSwap.jsx',import.meta.url),'utf8');

test('manual EVM swap selects one asset, chain and correct buy/sell direction',()=>{
  assert.match(component,/manualEvmCoin=\['BNB','POL','AVAX','ARB','LINK','QNT','UNI'\]/);
  assert.match(component,/stableCoins=\['USDC','USDT'\]/);
  assert.match(component,/stableCoins\.includes\(from\)\?'BUY':'SELL'/);
  assert.match(component,/asset=\{ownerEvmAsset\}/);
  assert.match(component,/stable=\{ownerEvmStable\}/);
  assert.equal((component.match(/<EvmLiveSwap/g)||[]).length,1);
});

test('owner-signed EVM swap must be server-quoted and never zero-min or V2',()=>{
  assert.match(signer,/\/api\/live\/evm\/owner-swap-preparation\//);
  assert.match(signer,/validateOwnerSwapPreparation/);
  assert.match(signer,/eth_sendTransaction/);
  assert.match(signer,/finalized-swap\//);
  assert.match(signer,/notifyConfirmedWalletTransfer/);
  assert.doesNotMatch(signer,/amountOutMin\s*:\s*['"]0['"]/);
  assert.doesNotMatch(signer,/buildEvmSwapTransaction|buildBnbSwapTransaction/);
});
