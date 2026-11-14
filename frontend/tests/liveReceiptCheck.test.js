import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {receiptRequest,validReceiptResponse}
  from '../src/liveReceiptRules.js';

const ETH='0x'+'a'.repeat(64);
const BTC='b'.repeat(64);

const btcData=req=>({
  mode:'BTC_MAINNET_WALLET_TX_OBSERVATION',
  asset:'BTC',network:'bitcoin-mainnet',txid:req.hash,
  confirmations_observed:6,net_wallet_movement_btc:'-0.04',
  explorer_reports_confirmed:true,
  crosschain_swap_fill_verified:false,
  ethereum_stablecoin_settlement_verified:false,
  trade_order_recorded:false,execution_enabled:false,can_submit:false,
});
const ethSwapData=req=>({
  mode:'ETH_ROUTER02_ERC20_FILL_OBSERVATION',
  asset:'ETH',network:'ethereum-mainnet',tx_hash:req.hash,
  side:req.type==='ROUTER02'?'BUY':'SELL',
  swap_output_asset:'WETH',destination_received_amount:'0.25',
  swap_erc20_leg_verified:true,native_eth_trade_cycle_complete:false,
  real_order_recorded:false,execution_enabled:false,can_submit:false,
});
const wethData=req=>({
  mode:'ETH_MAINNET_WETH9_CONVERSION_RECEIPT',
  asset:'ETH',network:'ethereum-mainnet',tx_hash:req.hash,
  operation:req.type,confirmed_native_conversion:true,
  weth9_event_amount:'0.25',
  swap_leg_linked:false,native_eth_trade_cycle_complete:false,
  execution_enabled:false,can_submit:false,
});

test('BTC transaction hash cannot be borrowed as ETH transaction or vice versa',()=>{
  assert.equal(receiptRequest('BTC',ETH,'BTC_CHAIN','BUY','USDC'),null);
  assert.equal(receiptRequest('ETH',BTC,'ROUTER02','BUY','USDC'),null);
  assert.equal(receiptRequest('ADA',BTC,'ROUTER02','BUY','USDC'),null);
  assert.equal(receiptRequest('BTC','zzz','BTC_CHAIN','BUY','USDC'),null);
  assert.equal(receiptRequest('ETH','0x123','ROUTER02','BUY','USDC'),null);
  const request=receiptRequest('BTC',BTC,'BTC_CHAIN','SELL','USDC');
  assert.equal(request.path,'/api/live/btc/tx-receipt/'+BTC);
  assert.equal(validReceiptResponse(btcData(request),request),true);
});

test('ETH two ERC20 swap directions and both WETH9 stages share one viewer',()=>{
  const buy=receiptRequest('ETH',ETH,'ROUTER02','BUY','USDC');
  assert.match(buy.path,/router02-receipt\/0x/);
  assert.match(buy.path,/side=BUY&stable=USDC/);
  assert.equal(validReceiptResponse(ethSwapData(buy),buy),true);
  const sell=receiptRequest('ETH',ETH,'ROUTER02','SELL','USDT');
  assert.match(sell.path,/side=SELL&stable=USDT/);
  assert.equal(validReceiptResponse({...ethSwapData(sell),side:'SELL'},sell),true);
  for(const operation of ['DEPOSIT','WITHDRAW']){
    const request=receiptRequest('ETH',ETH,operation,'BUY','USDC');
    assert.match(request.path,new RegExp('operation='+operation));
    assert.equal(validReceiptResponse(wethData(request),request),true);
  }
});

test('do not show fake receipt states as successful real trades',()=>{
  const request=receiptRequest('BTC',BTC,'BTC_CHAIN','BUY','USDC');
  for(const mutation of [
    d=>{d.crosschain_swap_fill_verified=true;},
    d=>{d.ethereum_stablecoin_settlement_verified=true;},
    d=>{d.trade_order_recorded=true;},
    d=>{d.execution_enabled=true;},
    d=>{d.can_submit=true;},
    d=>{d.txid='a'.repeat(64);},
    d=>{d.net_wallet_movement_btc='NaN';},
    d=>{d.confirmations_observed=-1;},
  ]){
    const data=btcData(request);mutation(data);
    assert.equal(validReceiptResponse(data,request),false);
  }
  const eth=receiptRequest('ETH',ETH,'ROUTER02','BUY','USDC');
  for(const mutate of [
    d=>{d.native_eth_trade_cycle_complete=true;},
    d=>{d.real_order_recorded=true;},
    d=>{d.tx_hash='0x'+'d'.repeat(64);},
    d=>{d.destination_received_amount='NaN';},
    d=>{d.execution_enabled=true;},
    d=>{d.can_submit=true;},
  ]){
    const d=ethSwapData(eth);mutate(d);
    assert.equal(validReceiptResponse(d,eth),false);
  }
  const w=receiptRequest('ETH',ETH,'DEPOSIT','BUY','USDC');
  const invalid=wethData(w);
  invalid.swap_leg_linked=true;
  assert.equal(validReceiptResponse(invalid,w),false);
});

test('receipt viewer exists only for selected BTC/ETH, no second global trading setup',()=>{
  const read=(name)=>readFileSync(new URL(name,import.meta.url),'utf8');
  const panel=read('../src/LiveTradePlan.jsx');
  const receipt=read('../src/LiveReceiptCheck.jsx');
  const settings=read('../src/TradingSetupPreview.jsx');
  const language=read('../src/i18n.js');
  assert.match(panel,/<LiveReceiptCheck/);
  assert.match(panel,/\['BTC','ETH'\]\.includes\(asset\)/);
  assert.match(settings,/key=\{opened\} asset=\{opened\}/);
  assert.match(receipt,/onSubmit=\{check\}/);
  assert.match(receipt,/getAccessToken\(\)/);
  assert.match(receipt,/new AbortController/);
  assert.match(receipt,/validReceiptResponse\(data,request\)/);
  assert.doesNotMatch(receipt,/sendTransaction|eth_sendTransaction|signTransaction|delegateWallet/);
  assert.match(language,/liveReceiptNoTradeClaim:/);
  assert.match(language,/liveReceiptUnwrap:/);
});
