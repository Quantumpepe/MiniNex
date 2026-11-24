import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {manualSwapQuoteRequest,validManualSwapWalletReadiness,
  liveEvmStableSupported} from '../src/manualSwapRules.js';
import {verifyOwnerEvmMinedTx} from '../src/evmOwnerSwapPreparation.js';

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

test('real EVM wallet send verifies on-chain original calldata, value and chain',()=>{
  const tx={from:'0x'+'a'.repeat(40),to:'0x'+'b'.repeat(40),
    data:'0xd0e30db0',value:'0x1234'};
  const onchain={hash:'0x'+'c'.repeat(64),from:tx.from,to:tx.to,
    input:tx.data,value:'0x1234',chainId:'0x38'};
  assert.equal(verifyOwnerEvmMinedTx(onchain,tx,onchain.hash,56),true);
  for(const change of [
    {input:'0x095ea7b3'},{value:'0x1235'},{chainId:'0x1'},
    {to:'0x'+'d'.repeat(40)},{from:'0x'+'d'.repeat(40)},
    {hash:'0x'+'d'.repeat(64)},
  ])assert.throws(()=>verifyOwnerEvmMinedTx(
    {...onchain,...change},tx,onchain.hash,56));
  assert.match(signer,/verifyOwnerEvmMinedTx\(original,pending\.tx/);
});

test('only actual same-chain EVM stable routes mount the real swap signer',()=>{
  for(const [asset,valid] of [
    ['LINK','USDC'],['QNT','USDT'],['UNI','USDC'],
  ])assert.equal(liveEvmStableSupported(asset,valid),true);
  for(const [asset,invalid] of [
    ['BNB','USDC'],['BNB','USDT'],['POL','USDT'],['POL','USDC'],
    ['AVAX','USDT'],['AVAX','USDC'],['ARB','USDT'],['ARB','USDC'],
  ])assert.equal(liveEvmStableSupported(asset,invalid),false);
  assert.equal(manualSwapQuoteRequest('USDT','BNB','0.000000000000000001'),null);
  assert.ok(manualSwapQuoteRequest('USDT','BNB','0.000001'));
  assert.match(component,/liveOwnerEvmPair&&request&&<EvmLiveSwap/);
});

test('manual EVM swap saves a canonical owner receipt before marking the leg complete',()=>{
  assert.match(signer,/record-manual-leg/);
  assert.match(signer,/manual-legs/);
  assert.match(signer,/recorded\?\.recorded!==true/);
  assert.match(signer,/recorded\.tx_hash!==pending\.hash\.toLowerCase\(\)/);
  assert.match(signer,/native_coin_cycle_complete!==false/);
  assert.match(signer,/realHistory\.map\(/);
});


test('Android OTA reload resumes original EVM receipt without broadcasting a replay',()=>{
  assert.match(signer,/readPendingOwnerSwap\(window\.localStorage/);
  assert.match(signer,/savePendingOwnerSwap\(window\.localStorage,submitted\)/);
  assert.match(signer,/clearSubmitted\(pending\.hash\)/);
  assert.match(signer,/const pairSide=pending\.side/);
  assert.match(signer,/const pairStable=pending\.stable/);
  assert.match(signer,/setPlan\(restored\.plan\)/);
  assert.match(signer,/setPhase\('submitted'\)/);
  assert.match(signer,/Bestehende Transaktion prüfen/);
  assert.match(signer,/recorded\.side!==pairSide/);
  assert.match(signer,/recorded\.stable!==pairStable/);
});

test('manual EVM price failures show distinct quoted RPC and allowance stages',()=>{
  assert.match(signer,/Actual mainnet EVM V3 quote unavailable/);
  assert.match(signer,/Verified EVM source allowance unavailable/);
  assert.match(signer,/EVM swap quote and wallet proofs do not match/);
  assert.match(signer,/Kein Swap gesendet/);
});

test('native quote shows the actual venue floor instead of generic 503 for 3 XRP',()=>{
  assert.match(component,/response.status===409 && request.coin==='MULTICHAIN'/);
  assert.match(component,/info\.detail\.source_asset===request\.from/);
  assert.match(component,/minimum_source_amount/);
  assert.match(component,/THORChain verlangt mindestens/);
  assert.match(component,/Es wurde kein Swap gesendet/);
});
