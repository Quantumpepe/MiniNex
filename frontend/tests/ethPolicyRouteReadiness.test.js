import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createLivePlanRequest} from '../src/liveTradePlanRules.js';
import {
  validEthPolicyQuote,validEthPolicyAllowance,checkEthPolicyStatus,
} from '../src/ethPolicyReadinessRules.js';

const ROUTER='0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45';
const request=()=>createLivePlanRequest('ETH','BUY','USDC','100');
const quote=(req=request())=>({
  mode:'ETH_ROUTER02_POLICY_ABI_QUOTE_ONLY',asset:'ETH',
  network:'ethereum-mainnet',side:req.side,stable:req.stable,
  input_amount:req.amount,route:'UNISWAP_V3_SINGLE_POOL',
  swap_call_abi:'exactInputSingle',
  router:ROUTER,quote_fee_tier:500,swap_output_token:req.side==='BUY'?'WETH':req.stable,
  native_eth_cycle_non_atomic:true,
  native_eth_cycle_stages:req.side==='BUY'?[
    {name:'SWAP_STABLE_TO_WETH'},{name:'UNWRAP_RECEIVED_WETH'},
  ]:[{name:'WRAP_NATIVE_ETH'},{name:'SWAP_CONFIRMED_WETH_TO_STABLE'}],
  wallet_identity_verified:true,onchain_swap_quote:true,
  unsigned_swap_validated:true,unsigned_swap_hash:'a'.repeat(64),
  expected_swap_output_amount:'0.1',minimum_swap_output_amount:'0.099',
  approval_required_not_yet_checked:true,
  native_wrap_unwrap_not_yet_settled:true,
  privy_policy_attached:false,privy_signer_authorized:false,
  execution_enabled:false,can_submit:false,wallet_signed:false,
  funds_transferred:false,quoted_at:Math.floor(Date.now()/1000),
  quote_ttl_seconds:15,
});
const allowance=(req=request())=>({
  mode:'ETH_ROUTER02_REAL_ALLOWANCE_READ_ONLY',
  asset:'ETH',network:'ethereum-mainnet',
  side:req.side,stable:req.stable,input_amount:req.amount,
  source_token:req.side==='BUY'?req.stable:'WETH',
  spending_router:ROUTER,wallet_identity_verified:true,
  signed_erc20_approval_created:false,privy_grant_verified:false,
  native_eth_unwrap_or_wrap_settled:false,allowance_not_spend_authority:true,
  execution_enabled:false,can_submit:false,
  source_funds_sufficient:true,router_allowance_sufficient:true,
  manual_token_approval_would_be_required:false,
  conservative_native_gas_reserve_met:true,
  verified_source_balance:'1000',verified_source_balance_atomic:'1000000000',
  existing_router_allowance:'1000',existing_router_allowance_atomic:'1000000000',
  native_eth_gas_balance:'1',
});

test('only one real Router02 policy-compatible quote is accepted for ETH',()=>{
  const req=request(),a=quote(req);
  assert.equal(validEthPolicyQuote(a,req),true);
  const sell=createLivePlanRequest('ETH','SELL','USDT','0.1');
  assert.equal(validEthPolicyQuote(quote(sell),sell),true);
  for(const mutate of [
    v=>{v.swap_call_abi='execute';},
    v=>{v.swap_output_token='ETH';},
    v=>{v.router='0x'+'b'.repeat(40);},
    v=>{v.native_eth_cycle_non_atomic=false;},
    v=>{v.privy_policy_attached=true;},
    v=>{v.wallet_signed=true;},
    v=>{v.execution_enabled=true;},
    v=>{v.unsigned_swap_validated=false;},
    v=>{v.quote_fee_tier=10000;},
    v=>{v.minimum_swap_output_amount='0.2';},
  ]){
    const v=quote();mutate(v);
    assert.equal(validEthPolicyQuote(v,req),false);
  }
});

test('real balance and zero allowance are separate from signer permissions',()=>{
  const req=request(),a=allowance(req);
  assert.equal(validEthPolicyAllowance(a,req),true);
  assert.equal(checkEthPolicyStatus(quote(req),a,req),'PREVIEW_ONLY');
  const missing=allowance(req);
  missing.router_allowance_sufficient=false;
  missing.manual_token_approval_would_be_required=true;
  missing.existing_router_allowance='0';
  missing.existing_router_allowance_atomic='0';
  assert.equal(validEthPolicyAllowance(missing,req),true);
  assert.equal(checkEthPolicyStatus(quote(req),missing,req),'ALLOWANCE_MISSING');
  const short=allowance(req);
  short.source_funds_sufficient=false;
  assert.equal(checkEthPolicyStatus(quote(req),short,req),'SOURCE_BALANCE_MISSING');
  const gas=allowance(req);
  gas.conservative_native_gas_reserve_met=false;
  assert.equal(checkEthPolicyStatus(quote(req),gas,req),'GAS_RESERVE_MISSING');
});

test('untrusted provider can never imply trade success or native ETH/WETH equivalence',()=>{
  const req=request();
  for(const mutate of [
    v=>{v.asset='BTC';},v=>{v.native_eth_unwrap_or_wrap_settled=true;},
    v=>{v.privy_grant_verified=true;},
    v=>{v.allowance_not_spend_authority=false;},
    v=>{v.signed_erc20_approval_created=true;},
    v=>{v.source_token='ETH';},
    v=>{v.router_allowance_sufficient=true;v.manual_token_approval_would_be_required=true;},
    v=>{v.existing_router_allowance_atomic='NaN';},
    v=>{v.conservative_native_gas_reserve_met='yes';},
  ]){
    const v=allowance(req);mutate(v);
    assert.equal(validEthPolicyAllowance(v,req),false);
  }
});

test('ETH-only optional check reuses current coin request, not a global selector',()=>{
  const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
  const quoteUI=read('../src/LiveTradePlan.jsx');
  const widget=read('../src/EthPolicyRouteReadiness.jsx');
  const settings=read('../src/TradingSetupPreview.jsx');
  const labels=read('../src/i18n.js');
  assert.match(quoteUI,/asset==='ETH'&&<EthPolicyRouteReadiness/);
  assert.match(quoteUI,/request=\{quote\.request\}/);
  assert.match(widget,/\/api\/live\/eth\/router02-quote/);
  assert.match(widget,/\/api\/live\/eth\/router02-allowance/);
  assert.match(widget,/new AbortController/);
  assert.match(widget,/validEthPolicyQuote\(route,request\)/);
  assert.match(widget,/validEthPolicyAllowance\(allowance,request\)/);
  assert.doesNotMatch(widget,/sendTransaction|eth_sendTransaction|addSigners\(/);
  assert.match(settings,/opened==='BTC'&&<LiveBtcControls/);
  assert.doesNotMatch(settings,/applyGlobal|Für alle 11 Coins übernehmen/);
  assert.match(labels,/ethRouteReadinessCaveat:/);
});
