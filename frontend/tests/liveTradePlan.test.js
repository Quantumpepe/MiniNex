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
  return {
    mode:'UNIFIED_ELEVEN_COIN_TRADE_PLAN',
    asset:request.asset,side:request.side,stable:request.stable,
    source_asset:request.inputAsset,destination_asset:request.outputAsset,
    input_amount:request.amount,network:LIVE_NETWORKS[request.asset],
    quote_level:eth?'WALLET_VERIFIED_UNSIGNED_SWAP':
      btc?'WALLET_VERIFIED_CROSSCHAIN_INDICATIVE':
      'VENUE_INDICATIVE_WALLET_NOT_VERIFIED',
    wallet_funds_verified:eth||btc,
    quote_provider:eth?'UNISWAP_ETHEREUM':'THORCHAIN_PUBLIC_MCP',
    estimated_output_amount:'10.1',minimum_output_amount:eth?'10':null,
    route_plan_only:true,real_order_created:false,wallet_signed:false,
    funds_transferred:false,can_submit:false,execution_enabled:false,
  };
}

test('each of eleven coins uses one quote form but independent settlement',()=>{
  assert.deepEqual(LIVE_PLAN_COINS,[
    'BTC','ETH','BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI']);
  assert.equal(LIVE_PLAN_COINS.includes('ADA'),false);
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

test('each coin preserves its own allowed settlement, not a global override',()=>{
  assert.equal(normalizedLiveStable('BNB','USDC'),'USDT');
  assert.equal(normalizedLiveStable('TRX','USDC'),'USDT');
  assert.equal(normalizedLiveStable('XRP','USDT'),'USDC');
  assert.equal(normalizedLiveStable('SOL','USDT'),'USDT');
  assert.equal(normalizedLiveStable('ADA','USDC'),null);
  assert.equal(createLivePlanRequest('TRX','BUY','USDC','12'),null);
  assert.equal(createLivePlanRequest('ADA','SELL','USDC','1'),null);
});

test('amounts are exact input units, not simulated USD budget',()=>{
  assert.equal(createLivePlanRequest('ETH','SELL','USDC','0.000000000000000001').body.amount,
    '0.000000000000000001');
  assert.equal(createLivePlanRequest('BTC','SELL','USDC','0.00000001').body.amount,'0.00000001');
  assert.equal(createLivePlanRequest('SOL','SELL','USDC','0.000000001'),null);
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
