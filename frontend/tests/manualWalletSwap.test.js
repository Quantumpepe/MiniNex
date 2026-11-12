import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {MANUAL_SWAP_ASSETS,manualSwapQuoteRequest,validManualSwapQuote,manualSwapQuoteExpiryMs,validEthSwapFeeReview,validManualSwapRoute,validManualSwapWalletReadiness,validEthReceiptStatus}
  from '../src/manualSwapRules.js';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const component=read('../src/ManualSwapPreview.jsx');
const wallet=read('../src/PrivyWalletPanel.jsx');
const labels=read('../src/i18n.js');
const css=read('../src/styles.css');

test('wallet Swap is manual, accessible, directly below balances and no execution path',()=>{
  assert.match(wallet,/import ManualSwapPreview from '\.\/ManualSwapPreview\.jsx'/);
  assert.ok(wallet.indexOf('<ManualSwapPreview')>wallet.indexOf('<WalletBalances'));
  assert.ok(wallet.indexOf('<ManualSwapPreview')<wallet.indexOf('<TradingSetupPreview'));
  assert.match(component,/aria-expanded=\{open\}/);
  assert.match(component,/getAccessToken\(\)/);
  assert.match(component,/walletSwapNotEnabled/);
  assert.match(component,/walletSwapUnsupported/);
  assert.match(component,/requestId\.current/);
  assert.match(css,/\.mininex-wallet-swap\{/);
  assert.match(labels,/walletSwapTitle: 'Swap'/);
  assert.match(labels,/walletSwapNotEnabled: 'Manuelle Swaps/);
  assert.doesNotMatch(component,/\b(?:sendTransaction|signTransaction|approve|delegateWallet|broadcast)\s*\(/);
});

test('all twelve coins can request real venue quotes; ETH/BTC keep dedicated routes',()=>{
  assert.equal(MANUAL_SWAP_ASSETS.length,14);
  assert.deepEqual(manualSwapQuoteRequest('ETH','USDC','0.125'),{
    path:'/api/live/eth/quote',
    body:{side:'SELL',stable:'USDC',amount:'0.125'},
    coin:'ETH',from:'ETH',to:'USDC',
  });
  assert.deepEqual(manualSwapQuoteRequest('USDT','BTC','50'),{
    path:'/api/live/btc/quote',
    body:{side:'BUY',stable:'USDT',amount:'50'},
    coin:'BTC',from:'USDT',to:'BTC',
  });
  for(const [from,to,amount] of [
    ['ETH','BTC','2'],['LINK','USDC','10'],['ADA','USDC','20'],
    ['DOGE','USDT','100'],['USDC','USDT','1'],
    ['SOL','XRP','5'],['POL','AVAX','1'],['UNI','BNB','3'],
  ]) assert.deepEqual(manualSwapQuoteRequest(from,to,amount),{
    path:'/api/wallet/swap/live-quote',
    body:{source:from,target:to,amount},coin:'MULTICHAIN',from,to,
  });
  assert.equal(manualSwapQuoteRequest('BTC','BTC','1'),null);
  assert.equal(manualSwapQuoteRequest('USDC','USDT','0.1234567'),null);
  assert.equal(manualSwapQuoteRequest('ADA','USDC','0.0000001'),null);
  assert.equal(manualSwapQuoteRequest('SOL','BTC','0.123456789'),null);
});

test('manual quote validation strictly rejects malformed, zero or overprecise inputs',()=>{
  for(const amount of ['0','-1','1e5','NaN','Infinity','1,2','1.1234567','','1.',' 2','0.0000000']){
    assert.equal(manualSwapQuoteRequest('USDC','ETH',amount),null,amount);
  }
  assert.equal(manualSwapQuoteRequest('BTC','USDT','0.123456789'),null);
  assert.equal(manualSwapQuoteRequest('ETH','USDC','0.1234567890123456789'),null);
  assert.equal(manualSwapQuoteRequest('BTC','USDT','0.12345678')?.coin,'BTC');
  assert.equal(manualSwapQuoteRequest('ETH','USDC','0.123456789012345678')?.coin,'ETH');
});

test('backend response cannot be displayed as verified executable swap',()=>{
  const request=manualSwapQuoteRequest('ETH','USDC','1');
  const good={
    mode:'LIVE_QUOTE_ONLY',asset:'ETH',side:'SELL',
    input_asset:'ETH',output_asset:'USDC',input_amount:'1',
    chain_id:1,route:'UNISWAP_V3_CLASSIC',slippage_tolerance_pct:0.5,
    expected_output_amount:'3000',minimum_output_amount:'2985',execution_enabled:false,
    signing_authorized:false,transaction_submitted:false,
  };
  assert(validManualSwapQuote(good,request));
  for(const bad of [
    {...good,execution_enabled:true},
    {...good,signing_authorized:true},
    {...good,transaction_submitted:true},
    {...good,asset:'BTC'},
    {...good,output_asset:'USDT'},
    {...good,input_amount:'2'},
    {...good,expected_output_amount:'0'},
    {...good,expected_output_amount:'not available'},
    {...good,chain_id:8453},
    {...good,route:'UNISWAP_UNIVERSAL_ROUTER'},
    {...good,slippage_tolerance_pct:2},
    {...good,slippage_tolerance_pct:'0.5'},
    {...good,slippage_tolerance_pct:-0.1},
    {...good,minimum_output_amount:'3001'},
    {...good,minimum_output_amount:'0'},
    {...good,minimum_output_amount:undefined},
  ]) assert.equal(validManualSwapQuote(bad,request),false);
  const btcReq=manualSwapQuoteRequest('BTC','USDT','0.1');
  const btc={
    mode:'BTC_CROSSCHAIN_INDICATIVE_ONLY',asset:'BTC',side:'SELL',
    source_asset:'BTC',destination_asset:'USDT',input_amount:'0.1',
    destination_network:'ethereum-mainnet',route_provider:'THORCHAIN_PUBLIC_INDICATIVE',
    quoted_price_impact_bps:25,fee_breakdown_verified:false,
    deposit_address_provided:false,automated_signing_enabled:false,
    estimated_output_amount:'100',execution_enabled:false,
    can_submit:false,transaction_built:false,minimum_received_guaranteed:false,
  };
  assert(validManualSwapQuote(btc,btcReq));
  assert.equal(validManualSwapQuote({...btc,can_submit:true},btcReq),false);
  assert.equal(validManualSwapQuote({...btc,minimum_received_guaranteed:true},btcReq),false);
  for(const bad of [
    {...btc,destination_network:'bitcoin-mainnet'},
    {...btc,route_provider:'unverified_provider'},
    {...btc,deposit_address_provided:true},
    {...btc,automated_signing_enabled:true},
    {...btc,fee_breakdown_verified:true},
    {...btc,quoted_price_impact_bps:501},
    {...btc,quoted_price_impact_bps:'25'},
    {...btc,quoted_price_impact_bps:-1},
    {...btc,source_asset:'ETH'},
  ]) assert.equal(validManualSwapQuote(bad,btcReq),false);
  const btcBuyReq=manualSwapQuoteRequest('USDT','BTC','100');
  const btcBuy={...btc,side:'BUY',source_asset:'USDT',destination_asset:'BTC',
    destination_network:'bitcoin-mainnet',input_amount:'100'};
  assert.equal(validManualSwapQuote(btcBuy,btcBuyReq),true);
  assert.equal(validManualSwapQuote({...btcBuy,destination_network:'ethereum-mainnet'},btcBuyReq),false);
});


test('swap quote expiry stays local, short, and fail-closed without background polling',()=>{
  const now = 1_800_000_000_000;
  const ethReq = manualSwapQuoteRequest('ETH','USDC','1');
  const eth = {
    mode:'LIVE_QUOTE_ONLY',asset:'ETH',side:'SELL',
    input_asset:'ETH',output_asset:'USDC',input_amount:'1',
    chain_id:1,route:'UNISWAP_V3_CLASSIC',slippage_tolerance_pct:0.5,
    expected_output_amount:'3000',minimum_output_amount:'2985',execution_enabled:false,
    signing_authorized:false,transaction_submitted:false,
    quote_expires_at:now/1000+15,
  };
  assert.equal(manualSwapQuoteExpiryMs(eth,ethReq,now),now+15_000);
  assert.equal(manualSwapQuoteExpiryMs(eth,ethReq,now+15_000),null);
  assert.equal(manualSwapQuoteExpiryMs({...eth,quote_expires_at:now/1000-30},ethReq,now),null);
  assert.equal(manualSwapQuoteExpiryMs({...eth,quote_expires_at:now/1000+5000},ethReq,now),null);
  assert.equal(manualSwapQuoteExpiryMs({...eth,quote_expires_at:'invalid'},ethReq,now),null);
  assert.equal(manualSwapQuoteExpiryMs({...eth,execution_enabled:true},ethReq,now),null);
  assert.equal(manualSwapQuoteExpiryMs(eth,ethReq,NaN),null);

  const btcReq = manualSwapQuoteRequest('BTC','USDT','0.1');
  const btc={
    mode:'BTC_CROSSCHAIN_INDICATIVE_ONLY',asset:'BTC',side:'SELL',
    source_asset:'BTC',destination_asset:'USDT',input_amount:'0.1',
    destination_network:'ethereum-mainnet',route_provider:'THORCHAIN_PUBLIC_INDICATIVE',
    quoted_price_impact_bps:25,fee_breakdown_verified:false,
    deposit_address_provided:false,automated_signing_enabled:false,
    estimated_output_amount:'100',execution_enabled:false,
    can_submit:false,transaction_built:false,minimum_received_guaranteed:false,
    quote_checked_at:now/1000,
  };
  assert.equal(manualSwapQuoteExpiryMs(btc,btcReq,now),now+30_000);
  assert.equal(manualSwapQuoteExpiryMs(btc,btcReq,now+30_001),null);
  assert.equal(manualSwapQuoteExpiryMs({...btc,quote_checked_at:now/1000+5000},btcReq,now),null);
  assert.equal(manualSwapQuoteExpiryMs({...btc,quote_checked_at:now/1000+10},btcReq,now),null);
  assert.equal(manualSwapQuoteExpiryMs({...btc,can_submit:true},btcReq,now),null);

  assert.match(component,/manualSwapQuoteExpiryMs\(data,request\)/);
  assert.match(component,/setTimeout\(/);
  assert.match(component,/clearTimeout\(/);
  assert.match(component,/walletSwapExpired/);
  assert.doesNotMatch(component,/setInterval\s*\(/);
  assert.doesNotMatch(component,/setTimeout\s*\(\s*fetch/);
  assert.match(labels,/walletSwapExpired: 'Diese Kursvorschau ist abgelaufen/);
});


test('wallet quote network requests are cancelled when obsolete, never poll',()=>{
  // The preview must not leave expensive calls running after a different
  // amount/pair/account has been selected or the panel is closed.
  assert.match(component,/const inFlight=useRef\(null\)/);
  assert.match(component,/const abortInFlight=\(\)=>/);
  assert.match(component,/current\?\.abort\(\)/);
  assert.match(component,/signal:controller\.signal/);
  assert.match(component,/const timeout=setTimeout\(\(\)=>controller\.abort\(\),15_000\)/);
  assert.match(component,/clearTimeout\(timeout\)/);
  assert.match(component,/if\(inFlight\.current===controller\)inFlight\.current=null/);
  assert.match(component,/if\(id!==requestId\.current\)return/);
  assert.match(component,/if\(controller\.signal\.aborted\)throw/);
  assert.match(component,/useEffect\(\(\)=>\(\)=>\{requestId\.current\+=1;abortInFlight\(\);\},\[\]\)/);
  assert.match(component,/const reset=\(\)=>\{\s*requestId\.current\+=1;\s*abortInFlight\(\)/);
  assert.match(component,/\[userId,enabled\]/);
  assert.match(component,/onClick=\{\(\)=>\{reset\(\);setOpen/);
  assert.doesNotMatch(component,/setInterval\s*\(/);
  assert.doesNotMatch(component,/retry\s*\(/);
  assert.doesNotMatch(component,/\b(?:sendTransaction|signTransaction|approve|broadcast)\s*\(/);
});


test('manual swap quotes expose exact native route and risks without extra work',()=>{
  assert.match(component,/walletSwapRouteEth/);
  assert.match(component,/walletSwapRouteBtc/);
  assert.match(component,/walletSwapSlippage/);
  assert.match(component,/walletSwapPriceImpact/);
  assert.match(component,/quoted_price_impact_bps\/100/);
  assert.match(labels,/walletSwapRouteEth: 'Ethereum Mainnet/);
  assert.match(labels,/walletSwapRouteBtc: 'Natives Bitcoin/);
  assert.match(labels,/walletSwapFeesUnknown/);
  assert.doesNotMatch(component,/setInterval\s*\(/);
  assert.doesNotMatch(component,/\b(?:sendTransaction|signTransaction|approve|broadcast)\s*\(/);
});


test('ETH network fee preflight checks exact gas ceiling and cannot imply signing',()=>{
  const req=manualSwapQuoteRequest('USDC','ETH','100');
  const safe={
    mode:'LIVE_ETH_SWAP_PLAN_ONLY',
    asset:'ETH',chain_id:1,side:'BUY',
    input_asset:'USDC',output_asset:'ETH',input_amount:'100',
    expected_output_amount:'0.21',minimum_output_amount:'0.20895',
    execution_enabled:false,automatic_signing_enabled:false,can_submit:false,
    state:'UNSIGNED_TX_REVIEW_REQUIRED',
    transaction_built:true,erc20_approval_required:false,
    permit_signature_required:false,safe_to_sign:false,
    router_commands_decoded:false,calldata_security_review_complete:false,
    tx_outer_fields_verified:true,
    router:'0x23617e59A5925b2A4Bf75d73ff6711cD0b29De85',
    router_version:'2.1.2',
    max_gas_limit:300000,max_fee_per_gas_gwei:'4',
    max_possible_gas_eth:'0.0012',
  };
  assert.equal(validEthSwapFeeReview(safe,req),true);
  assert.equal(validEthSwapFeeReview(safe,manualSwapQuoteRequest('USDC','BTC','100')),false);
  for(const broken of [
    {...safe,execution_enabled:true},
    {...safe,can_submit:true},
    {...safe,automatic_signing_enabled:true},
    {...safe,safe_to_sign:true},
    {...safe,router_commands_decoded:true},
    {...safe,calldata_security_review_complete:true},
    {...safe,tx_outer_fields_verified:false},
    {...safe,chain_id:56},
    {...safe,input_asset:'USDT'},
    {...safe,input_amount:'100.01'},
    {...safe,router:'0x'+'b'.repeat(40)},
    {...safe,router_version:'2.2.0'},
    {...safe,max_gas_limit:3000000},
    {...safe,max_gas_limit:300000.5},
    {...safe,max_gas_limit:300001},
    {...safe,max_fee_per_gas_gwei:'4.0000000001'},
    {...safe,max_fee_per_gas_gwei:'300.000000001'},
    {...safe,max_fee_per_gas_gwei:'-1'},
    {...safe,max_fee_per_gas_gwei:'1e4'},
    {...safe,max_possible_gas_eth:'0.001200000000000001'},
    {...safe,max_possible_gas_eth:'0.00401'},
    {...safe,max_possible_gas_eth:'0'},
    {...safe,expected_output_amount:'0'},
    {...safe,minimum_output_amount:'0.22'},
    {...safe,erc20_approval_required:true},
    {...safe,permit_signature_required:true},
    {...safe,signature:'0xdeadbeef'},
    {...safe,data:'0x3593564c'},
    {...safe,calldata:'0x3593564c'},
  ]) assert.equal(validEthSwapFeeReview(broken,req),false,
    'rejected '+JSON.stringify(broken));
});

test('approval branch reports unavailable fee and never exposes an actionable transaction',()=>{
  const req=manualSwapQuoteRequest('USDT','ETH','10');
  const plan={
    mode:'LIVE_ETH_SWAP_PLAN_ONLY',asset:'ETH',chain_id:1,side:'BUY',
    input_asset:'USDT',output_asset:'ETH',input_amount:'10',
    expected_output_amount:'0.003',minimum_output_amount:'0.002985',
    execution_enabled:false,automatic_signing_enabled:false,can_submit:false,
    state:'APPROVAL_OR_PERMIT_REQUIRED',transaction_built:false,
    erc20_approval_required:true,permit_signature_required:false,
  };
  assert.equal(validEthSwapFeeReview(plan,req),true);
  assert.equal(validEthSwapFeeReview({...plan,transaction_built:true},req),false);
  assert.equal(validEthSwapFeeReview({...plan,erc20_approval_required:false},req),false);
  assert.equal(validEthSwapFeeReview({...plan,can_submit:true},req),false);
  assert.equal(validEthSwapFeeReview({...plan,safe_to_sign:true},req),false);
  assert.equal(validEthSwapFeeReview({...plan,max_possible_gas_eth:'0.0012'},req),false);
  assert.equal(validEthSwapFeeReview({...plan,erc20_approval_required:false,permit_signature_required:true},req),true);
});

test('fee review helper remains validated, but extra swap fee button is not in normal flow',()=>{
  assert.doesNotMatch(component,/onClick=\{checkFee\}/);
  assert.doesNotMatch(component,/API_BASE\+'\/api\/live\/eth\/swap-plan'/);
  assert.doesNotMatch(component,/walletSwapFeeMaximum/);
  assert.match(labels,/walletSwapFeeNeedsApproval/);
  assert.match(component,/onSubmit=\{checkQuote\}/);
});
test('all twelve coins and ETH stables validate exact route classification only',()=>{
  const symbols=[...MANUAL_SWAP_ASSETS];
  assert.equal(symbols.length,14);
  const network={
    BTC:'bitcoin-mainnet',ETH:'ethereum-mainnet',BNB:'bsc-mainnet',
    SOL:'solana-mainnet',XRP:'xrpl-mainnet',TRX:'tron-mainnet',
    POL:'polygon-pos-mainnet',AVAX:'avalanche-c-mainnet',
    LINK:'ethereum-mainnet',DOGE:'dogecoin-mainnet',
    UNI:'ethereum-mainnet',ADA:'cardano-mainnet',
    USDC:'ethereum-mainnet',USDT:'ethereum-mainnet',
  };
  for(const from of symbols)for(const to of symbols) {
    if(from===to)continue;
    const q=manualSwapQuoteRequest(from,to,'1');
    const data={
      mode:'MANUAL_SWAP_ROUTE_DISCOVERY_ONLY',
      source:{symbol:from,network:network[from]},
      destination:{symbol:to,network:network[to]},
      route_class:network[from]===network[to]?'SAME_CHAIN':'CROSS_CHAIN',
      quote_preview_implemented:!!q,
      quote_asset:q?.coin||null,
      quote_endpoint:q?.path||null,
      execution_ready:false,signing_authorized:false,
      transaction_built:false,can_submit:false,
      network_fee_verified:false,guaranteed_minimum_received:false,
      required_checks:[
        'verified_source_and_destination_wallets',
        'independently_verified_provider_and_contracts',
        'exact_spend_allowance_and_fees',
        'restricted_user_wallet_signing',
        'onchain_receipt_and_refund_reconciliation',
      ],
    };
    assert.equal(validManualSwapRoute(data,from,to),true,from+' > '+to);
    for(const change of [
      {can_submit:true},{signing_authorized:true},{execution_ready:true},
      {transaction_built:true},{guaranteed_minimum_received:true},
      {network_fee_verified:true},{quote_preview_implemented:!data.quote_preview_implemented},
      {quote_endpoint:'/api/unknown/execute'},{route_class:'CROSSCHAIN'},
      {source:{symbol:from,network:'unknown'}},{required_checks:[]},
    ]) assert.equal(validManualSwapRoute({...data,...change},from,to),false,
      'wrong '+from+' > '+to+' '+JSON.stringify(change));
  }
  assert.equal(validManualSwapRoute(null,'ETH','USDC'),false);
  assert.equal(validManualSwapRoute({},'ETH','USDC'),false);
  assert.equal(validManualSwapRoute({},'ETH','ETH'),false);
  assert.equal(validManualSwapRoute({},'WBTC','ETH'),false);
});

test('route classification stays in backend; the wallet has one primary quote action',()=>{
  assert.doesNotMatch(component,/onClick=\{checkRoute\}/);
  assert.doesNotMatch(component,/\/api\/wallet\/swap\/route\?/);
  assert.match(component,/walletSwapRouteEth/);
  assert.match(component,/walletSwapRouteBtc/);
  assert.match(component,/onSubmit=\{checkQuote\}/);
  assert.doesNotMatch(component,/setInterval\s*\(/);
});
test('real Ethereum receipt status is fail closed and never a live trade authorization',()=>{
  const hash='0x'+'a'.repeat(64);
  const base={
    mode:'ETH_MAINNET_RECEIPT_CHECK',network:'ethereum-mainnet',
    tx_hash:hash,execution_enabled:false,can_submit:false,
    swap_outputs_verified:false,live_order_recorded:false,
    confirmations:21,network_fee_eth:'0.000021',
    transaction_seen:true,transaction_finalized:true,transaction_success:true,
    state:'FINALIZED_SUCCESS',
  };
  assert.equal(validEthReceiptStatus(base,hash),true);
  for(const bad of [
    {execution_enabled:true},{can_submit:true},{swap_outputs_verified:true},
    {live_order_recorded:true},{network:'bsc-mainnet'},
    {tx_hash:'0x'+'f'.repeat(64)},{mode:'APPROVED_LIVE_TX'},
    {confirmations:0},{confirmations:-1},{confirmations:1.5},
    {transaction_finalized:false},{transaction_success:false},
    {network_fee_eth:'unknown'},{network_fee_eth:'0'},
    {network_fee_eth:'0.0000000000000000001'},
  ]) assert.equal(validEthReceiptStatus({...base,...bad},hash),false,
    JSON.stringify(bad));
  assert.equal(validEthReceiptStatus({...base,
    state:'INCLUDED_SUCCESS',transaction_finalized:false},hash),true);
  assert.equal(validEthReceiptStatus({...base,
    state:'FINALIZED_REVERTED',transaction_success:false},hash),true);
  assert.equal(validEthReceiptStatus({...base,
    state:'PENDING',transaction_finalized:false,transaction_success:null,
    confirmations:0,network_fee_eth:null},hash),true);
  assert.equal(validEthReceiptStatus({...base,
    state:'NOT_FOUND',transaction_seen:false,transaction_finalized:false,
    transaction_success:null,confirmations:0,network_fee_eth:null},hash),true);
  assert.equal(validEthReceiptStatus({},hash),false);
  assert.equal(validEthReceiptStatus(base,'0x123'),false);
});

test('technical ETH transaction hash checker is not a separate mandatory swap step',()=>{
  assert.doesNotMatch(component,/onClick=\{checkReceipt\}/);
  assert.doesNotMatch(component,/\/api\/live\/eth\/tx-receipt\//);
  assert.doesNotMatch(component,/walletSwapTxCheckTitle/);
  assert.match(component,/walletSwapNotEnabled/);
  assert.match(component,/walletSwapFeesUnknown/);
});
test('real provider quotes reject native network forgery and any executable instructions',()=>{
  const req=manualSwapQuoteRequest('SOL','XRP','1.25');
  const now=1_800_000_000_000;
  const good={
    mode:'NATIVE_MULTICHAIN_REAL_MARKET_QUOTE_ONLY',
    source_asset:'SOL',destination_asset:'XRP',
    source_network:'solana-mainnet',destination_network:'xrpl-mainnet',
    input_amount:'1.25',estimated_output_amount:'30.01',
    quote_provider:'THORCHAIN_PUBLIC_MCP',pool_route_quoted:true,
    quoted_price_impact_bps:34,quote_checked_at:now/1000,
    quote_expires_at:null,
    execution_enabled:false,can_submit:false,
    transaction_built:false,signing_authorized:false,
    deposit_address_provided:false,fee_breakdown_verified:false,
    minimum_received_guaranteed:false,
  };
  assert.equal(validManualSwapQuote(good,req),true);
  assert.equal(manualSwapQuoteExpiryMs(good,req,now),now+30_000);
  assert.equal(manualSwapQuoteExpiryMs(good,req,now+30_001),null);
  const shortLived={...good,quote_expires_at:now/1000+10};
  assert.equal(validManualSwapQuote(shortLived,req),true);
  assert.equal(manualSwapQuoteExpiryMs(shortLived,req,now),now+10_000);
  assert.equal(manualSwapQuoteExpiryMs(shortLived,req,now+10_001),null);
  assert.equal(validManualSwapQuote({...good,quote_expires_at:now/1000-1},req),false);
  assert.equal(validManualSwapQuote({...good,quote_expires_at:now/1000+3601},req),false);
  assert.equal(validManualSwapQuote({...good,quote_expires_at:'tomorrow'},req),false);
  assert.equal(validManualSwapQuote({...good,quote_expires_at:undefined},req),false);
  for(const change of [
    {source_asset:'ETH'},{destination_asset:'DOGE'},
    {source_network:'ethereum-mainnet'},
    {destination_network:'bitcoin-mainnet'},
    {quote_provider:'UNKNOWN'}, {pool_route_quoted:false},
    {estimated_output_amount:'0'}, {estimated_output_amount:'no quote'},
    {quoted_price_impact_bps:501},{quoted_price_impact_bps:'25'},
    {execution_enabled:true},{can_submit:true},
    {transaction_built:true},{signing_authorized:true},
    {deposit_address_provided:true},{fee_breakdown_verified:true},
    {minimum_received_guaranteed:true},
    {input_amount:'1.3'},{quote_checked_at:'now'},
    {memo:'=:'},{inbound_address:'bc1q...'},
  ]) assert.equal(validManualSwapQuote({...good,...change},req),false,
    JSON.stringify(change));
  assert.match(component,/quote\.request\.coin==='MULTICHAIN'/);
  assert.match(component,/walletSwapNativeLiquidityNotice/);
  assert.match(labels,/walletSwapNativeProvider: 'Live-Marktkurs von THORChain'/);
  assert.doesNotMatch(component,/setInterval\s*\(/);
  assert.doesNotMatch(component,/\b(?:sendTransaction|signTransaction|approve|broadcast)\s*\(/);
});

test('privy wallet record readiness is independent, authenticated and never an execution grant',()=>{
  const good={
    mode:'MANUAL_SWAP_WALLET_READINESS_ONLY',
    source:{asset:'SOL',network:'solana-mainnet',record_status:'PRESENT'},
    destination:{asset:'USDT',network:'ethereum-mainnet',record_status:'MISSING'},
    route_class:'CROSS_CHAIN',
    ada_in_progress:false,
    linked_wallet_records_checked:true,
    ownership_proof_verified:false,
    native_signer_verified:false,
    network_fees_verified:false,
    minimum_received_guaranteed:false,
    refund_path_verified:false,
    signing_authorized:false,
    execution_ready:false,
    can_submit:false,
  };
  assert.equal(validManualSwapWalletReadiness(good,'SOL','USDT'),true);
  for (const change of [
    {mode:'SWAP_EXECUTION_READY'},
    {signing_authorized:true},{execution_ready:true},{can_submit:true},
    {native_signer_verified:true},{ownership_proof_verified:true},
    {network_fees_verified:true},{refund_path_verified:true},
    {source:{...good.source,network:'ethereum-mainnet'}},
    {source:{...good.source,record_status:'READY'}},
    {destination:{...good.destination,record_status:'PRESENT',address:'0xsecret'}},
    {destination:{...good.destination,asset:'USDC'}},
    {route_class:'SAME_CHAIN'},
    {ada_in_progress:true},
    {memo:'=:BTC'},{address:'0x'+'a'.repeat(40)},
  ]) assert.equal(validManualSwapWalletReadiness({...good,...change},'SOL','USDT'),false,
      JSON.stringify(change));
  assert.equal(validManualSwapWalletReadiness({
    ...good,source:{asset:'ETH',network:'ethereum-mainnet',record_status:'PRESENT'},
    destination:{asset:'LINK',network:'ethereum-mainnet',record_status:'PRESENT'},
    route_class:'SAME_CHAIN',
  },'ETH','LINK'),true);
  assert.equal(validManualSwapWalletReadiness({
    ...good,source:{asset:'ADA',network:'cardano-mainnet',record_status:'MISSING'},
    ada_in_progress:true,
  },'ADA','USDT'),true);
});

test('manual quote UI is compact and preserves 12 coins without diagnostic buttons',()=>{
  assert.match(component,/onSubmit=\{checkQuote\}/);
  assert.match(component,/className="mininex-swap-summary"/);
  assert.match(component,/walletSwapAdaPending/);
  assert.match(component,/walletSwapNotEnabled/);
  for(const name of ['checkWalletReadiness','checkRoute','checkFee','checkReceipt'])
    assert.doesNotMatch(component,new RegExp('onClick=\\\\{'+name+'\\\\}'));
  assert.doesNotMatch(component,/setInterval\s*\(/);
  assert.doesNotMatch(component,/\b(?:sendTransaction|signTransaction|approve|broadcast)\s*\(/);
});
