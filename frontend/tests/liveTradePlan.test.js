import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {
  LIVE_PLAN_COINS,LIVE_SETTLEMENTS,LIVE_NETWORKS,
  normalizedLiveStable,createLivePlanRequest,validLiveTradePlan,
} from '../src/liveTradePlanRules.js';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const setup=read('../src/TradingSetupPreview.jsx');
const livePanel=read('../src/LiveTradePlan.jsx');
const labels=read('../src/i18n.js');

function response(request){
  const eth=request.asset==='ETH',btc=request.asset==='BTC';
  const erc20=['LINK','UNI','ARB'].includes(request.asset);
  const nativeEvm=['BNB','POL','AVAX'].includes(request.asset);
  const nativeOwner=['SOL','XRP'].includes(request.asset);
  const chainIds={ETH:1,BNB:56,POL:137,AVAX:43114,LINK:1,UNI:1,ARB:42161};
  return {
    mode:'UNIFIED_ELEVEN_COIN_TRADE_PLAN',
    asset:request.asset,side:request.side,stable:request.stable,
    source_asset:request.inputAsset,destination_asset:request.outputAsset,
    input_amount:request.amount,network:LIVE_NETWORKS[request.asset],
    chain_id:chainIds[request.asset]??null,
    quote_level:eth?'WALLET_VERIFIED_UNSIGNED_SWAP':
      btc?'WALLET_VERIFIED_CROSSCHAIN_INDICATIVE':
      erc20?'WALLET_VERIFIED_UNSIGNED_ERC20_SWAP':
      nativeEvm?'WALLET_VERIFIED_UNSIGNED_NATIVE_EVM_SWAP':
      nativeOwner?'OWNER_WALLET_BALANCE_VERIFIED_NATIVE_INDICATIVE':
      'VENUE_INDICATIVE_WALLET_NOT_VERIFIED',
    wallet_funds_verified:eth||btc||erc20||nativeEvm||nativeOwner,
    source_chain_balance_verified:nativeOwner,
    network_fee_spendability_verified:false,
    destination_settlement_finalized:false,
    quote_provider:eth?'UNISWAP_ETHEREUM':
      erc20?(request.asset==='ARB'?'UNISWAP_V3_CHAIN_SPECIFIC':'UNISWAP_ETHEREUM_ERC20'):
      nativeEvm?'UNISWAP_V3_CHAIN_SPECIFIC':'THORCHAIN_PUBLIC_MCP',
    native_wrap_unwrap_not_yet_settled:nativeEvm,
    estimated_output_amount:'10.1',
    minimum_output_amount:eth||erc20||nativeEvm?'10':null,
    route_plan_only:true,real_order_created:false,wallet_signed:false,
    funds_transferred:false,can_submit:false,execution_enabled:false,
  };
}

test('all twelve coins use one quote form with independent settlement',()=>{
  assert.deepEqual(LIVE_PLAN_COINS,[
    'BTC','ETH','BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI','ARB']);
  assert.equal(LIVE_PLAN_COINS.includes('ARB'),true);
  assert.deepEqual(LIVE_SETTLEMENTS.BNB,['USDT']);
  assert.deepEqual(LIVE_SETTLEMENTS.TRX,['USDT']);
  assert.deepEqual(LIVE_SETTLEMENTS.XRP,['USDC']);
  assert.deepEqual(LIVE_SETTLEMENTS.POL,['USDC']);
  for(const asset of LIVE_PLAN_COINS){
    for(const side of ['BUY','SELL']){
      const stable=LIVE_SETTLEMENTS[asset][0];
      const req=createLivePlanRequest(asset,side,stable,'12.5');
      assert.equal(req.path,'/api/live/trade-plan/'+asset);
      assert.equal(req.inputAsset,side==='BUY'?stable:asset);
      assert.equal(req.outputAsset,side==='BUY'?asset:stable);
      assert.equal(validLiveTradePlan(response(req),req),true);
    }
  }
});

test('new EVM quotes use verified owner wallets and their exact network identity',()=>{
  for(const asset of ['ETH','BNB','POL','AVAX','LINK','UNI','ARB']){
    const request=createLivePlanRequest(
      asset,'BUY',LIVE_SETTLEMENTS[asset][0],'1');
    const real=response(request);
    assert.equal(validLiveTradePlan(real,request),true);
    real.chain_id=999;
    assert.equal(validLiveTradePlan(real,request),false);
  }
  assert.deepEqual(LIVE_SETTLEMENTS.AVAX,['USDC']);
  assert.equal(createLivePlanRequest('AVAX','BUY','USDT','10'),null);
  assert.equal(createLivePlanRequest('BNB','BUY','USDT','0.000000000000000001')?.amount,
    '0.000000000000000001');
  assert.equal(createLivePlanRequest('POL','SELL','USDC','0.000000000000000001')?.amount,
    '0.000000000000000001');
  assert.equal(createLivePlanRequest('UNI','SELL','USDC','0.000000000000000001')?.amount,
    '0.000000000000000001');
});

test('each coin preserves its own allowed settlement, not a global override',()=>{
  assert.equal(normalizedLiveStable('BNB','USDC'),'USDT');
  assert.equal(normalizedLiveStable('TRX','USDC'),'USDT');
  assert.equal(normalizedLiveStable('XRP','USDT'),'USDC');
  assert.equal(normalizedLiveStable('SOL','USDT'),'USDT');
  assert.equal(normalizedLiveStable('ARB','USDC'),'USDC');
  assert.equal(createLivePlanRequest('TRX','BUY','USDC','12'),null);
  assert.equal(createLivePlanRequest('ARB','SELL','USDC','1')?.amount,'1');
});

test('amounts are exact input units, not simulated USD budget',()=>{
  assert.equal(createLivePlanRequest('ETH','SELL','USDC','0.000000000000000001').body.amount,
    '0.000000000000000001');
  assert.equal(createLivePlanRequest('BTC','SELL','USDC','0.00000001').body.amount,'0.00000001');
  assert.equal(createLivePlanRequest('SOL','SELL','USDC','0.000000001')?.amount,'0.000000001');
  assert.equal(createLivePlanRequest('SOL','SELL','USDC','0.0000000001'),null);
  assert.equal(createLivePlanRequest('XRP','SELL','USDC','0.0000001'),null);
  assert.equal(createLivePlanRequest('ETH','BUY','USDC','0.0000001'),null);
  for(const raw of ['0','0.0','-1','NaN','1e8','01','',null,4,'10000000'.repeat(8)]){
    assert.equal(createLivePlanRequest('ETH','BUY','USDC',raw),null);
  }
});

test('real quote validation prevents calling a paper fill an executable order',()=>{
  const request=createLivePlanRequest('DOGE','SELL','USDC','12');
  const mutationCases=[
    r=>{r.can_submit=true;},r=>{r.wallet_signed=true;},
    r=>{r.execution_enabled=true;},r=>{r.funds_transferred=true;},
    r=>{r.real_order_created=true;},r=>{r.route_plan_only=false;},
    r=>{r.wallet_funds_verified=true;},r=>{r.quote_level='WALLET_VERIFIED_UNSIGNED_SWAP';},
    r=>{r.network='ethereum-mainnet';},r=>{r.asset='BTC';},
    r=>{r.source_asset='ETH';},r=>{r.estimated_output_amount='NaN';},
    r=>{r.estimated_output_amount='0';},r=>{r.minimum_output_amount='12';},
  ];
  for(const mutate of mutationCases){
    const result=response(request);mutate(result);
    assert.equal(validLiveTradePlan(result,request),false);
  }
});

test('quote is rendered for selected coin only; old PAPER and AUTO controls remain',()=>{
  assert.match(setup,/LiveTradePlan from '\.\/LiveTradePlan\.jsx'/);
  assert.match(setup,/!selectedCoin\.inProgress&&<LiveTradePlan/);
  assert.match(setup,/key=\{opened\} asset=\{opened\}/);
  assert.match(setup,/defaultStable=\{selectedSettings\?\.stable\|\|replayOptions\[opened\]\?\.stable/);
  assert.match(setup,/<ExtraCoinStrategyReplay/);
  assert.match(setup,/renderSessionControls\?\.\(opened\)/);
  assert.match(setup,/const MODES = \['LONG','SPOT_SHORT','AUTO'\]/);
  assert.match(livePanel,/getAccessToken\(\)/);
  assert.match(livePanel,/new AbortController\(\)/);
  assert.match(livePanel,/\[asset,defaultStable,enabled,userId\]/);
  assert.match(livePanel,/validLiveTradePlan\(result,request\)/);
  assert.doesNotMatch(livePanel,/eth_sendTransaction|sendTransaction\(|signTransaction\(|approve\(/);
  assert.match(labels,/livePlanNotExecutable:/);
  assert.match(labels,/livePlanVenueOnly:/);
});

test('SOL and XRP quote checks are actual owner-bound readbacks, never executed trades',()=>{
  for(const asset of ['SOL','XRP']){
    const request=createLivePlanRequest(asset,'SELL','USDC','2');
    const good=response(request);
    assert.equal(validLiveTradePlan(good,request),true);
    for(const [field,wrong] of [
      ['wallet_funds_verified',false],
      ['source_chain_balance_verified',false],
      ['network_fee_spendability_verified',true],
      ['destination_settlement_finalized',true],
      ['quote_level','VENUE_INDICATIVE_WALLET_NOT_VERIFIED'],
      ['quote_provider','FAKE'],
    ]){
      const mutated={...good,[field]:wrong};
      assert.equal(validLiveTradePlan(mutated,request),false,asset+' '+field);
    }
  }
});
