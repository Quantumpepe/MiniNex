import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {MANUAL_SWAP_ASSETS,manualSwapQuoteRequest,validManualSwapQuote}
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

test('only BTC/ETH to/from stablecoins can request current read-only quotes',()=>{
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
  assert.equal(manualSwapQuoteRequest('ETH','BTC','2'),null);
  assert.equal(manualSwapQuoteRequest('LINK','USDC','10'),null);
  assert.equal(manualSwapQuoteRequest('ADA','USDC','20'),null);
  assert.equal(manualSwapQuoteRequest('DOGE','USDT','100'),null);
  assert.equal(manualSwapQuoteRequest('BTC','BTC','1'),null);
  assert.equal(manualSwapQuoteRequest('USDC','USDT','1'),null);
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
    expected_output_amount:'3000',execution_enabled:false,
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
  ]) assert.equal(validManualSwapQuote(bad,request),false);
  const btcReq=manualSwapQuoteRequest('BTC','USDT','0.1');
  const btc={
    mode:'BTC_CROSSCHAIN_INDICATIVE_ONLY',asset:'BTC',side:'SELL',
    source_asset:'BTC',destination_asset:'USDT',input_amount:'0.1',
    estimated_output_amount:'100',execution_enabled:false,
    can_submit:false,transaction_built:false,minimum_received_guaranteed:false,
  };
  assert(validManualSwapQuote(btc,btcReq));
  assert.equal(validManualSwapQuote({...btc,can_submit:true},btcReq),false);
  assert.equal(validManualSwapQuote({...btc,minimum_received_guaranteed:true},btcReq),false);
});
