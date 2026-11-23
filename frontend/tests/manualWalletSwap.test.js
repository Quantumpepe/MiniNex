import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {manualSwapQuoteRequest,validManualSwapWalletReadiness} from '../src/manualSwapRules.js';

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

test('QNT and EVM preview amounts retain 18 decimal precision and resolve network',()=>{
  const q=manualSwapQuoteRequest('QNT','USDC','0.000000000000000001');
  assert.equal(q?.body.amount,'0.000000000000000001');
  const p=manualSwapQuoteRequest('POL','USDT','0.000000000000000001');
  assert.equal(p?.body.amount,'0.000000000000000001');
  const ready={
    mode:'MANUAL_SWAP_WALLET_READINESS_ONLY',
    source:{asset:'QNT',network:'ethereum-mainnet',record_status:'PRESENT'},
    destination:{asset:'USDC',network:'ethereum-mainnet',record_status:'PRESENT'},
    route_class:'SAME_CHAIN',linked_wallet_records_checked:true,
    ownership_proof_verified:false,native_signer_verified:false,
    network_fees_verified:false,minimum_received_guaranteed:false,
    refund_path_verified:false,signing_authorized:false,
    execution_ready:false,can_submit:false,
  };
  assert.equal(validManualSwapWalletReadiness(ready,'QNT','USDC'),true);
});

// A confirmed reverted transaction must not leave the manual swap UI
// permanently locked, while unknown or mismatched receipts stay blocked.
test('only an identity-matched on-chain revert permits explicit re-prepare',()=>{
  const ethSigner=readFileSync(new URL('../src/EthLiveSwap.jsx',import.meta.url),'utf8');
  assert.match(signer,/receipt\.status==='0x0'[\s\S]*?attempt\.current=false/);
  assert.match(signer,/setPending\(null\);setPlan\(null\)/);
  assert.match(signer,/receipt\.status!=='0x1'[\s\S]*?setPhase\('uncertain'\)/);
  assert.match(ethSigner,/found\.status==='0x0'[\s\S]*?attempted\.current=false/);
  assert.match(ethSigner,/found\.status!=='0x1'/);
});
