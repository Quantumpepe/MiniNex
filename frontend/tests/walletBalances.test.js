import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {validateWalletBalanceResponse,ASSETS} from '../src/walletBalanceValidation.js';

function valid(){
  return {
    mode:'VERIFIED_READ_ONLY',wallet_ownership_verified:true,
    real_trading_enabled:false,automatic_signing_enabled:false,
    wallet_funds_used_for_paper:false,pricing:'NATIVE_UNITS_ONLY',
    wallets:{BTC:'bc1q'+'a'.repeat(38),ETH:'0x'+'b'.repeat(40)},
    balances:{
      BTC:{balance:'0.00000123',status:'verified',network:'bitcoin-mainnet'},
      ETH:{balance:'0.50',status:'verified',network:'ethereum-mainnet'},
      USDC:{balance:'125.12',status:'verified',network:'ethereum-mainnet'},
      USDT:{balance:'0',status:'verified',network:'ethereum-mainnet'},
    }
  };
}

test('BTC, ETH, USDC and USDT each have a validated mainnet balance',()=>{
  assert.deepEqual(ASSETS,['BTC','ETH','USDC','USDT']);
  assert.equal(validateWalletBalanceResponse(valid()),true);
  const missing=valid();
  missing.balances.BTC.balance=null;
  missing.balances.BTC.status='wallet_missing';
  assert.equal(validateWalletBalanceResponse(missing),true);
});

test('never equate missing verification or live signing authority with read-only',()=>{
  const variations=[
    value=>{value.mode='PAPER_AUTO_DEMO';},
    value=>{value.wallet_ownership_verified=false;},
    value=>{value.real_trading_enabled=true;},
    value=>{value.automatic_signing_enabled=true;},
    value=>{value.wallet_funds_used_for_paper=true;},
    value=>{value.pricing='FIAT_ESTIMATE';},
    value=>{value.balances.BTC.network='ethereum-mainnet';},
    value=>{value.balances.USDC.network='polygon-mainnet';},
    value=>{value.balances.USDT.status='unverified';},
    value=>{value.balances.ETH.balance='NaN';},
    value=>{value.balances.ETH.balance='1e100';},
    value=>{value.balances.USDC.balance='-25';},
    value=>{value.balances.USDC.balance=null;},
    value=>{delete value.balances.USDC;},
    value=>{value.wallets=null;},
  ];
  for(const change of variations){
    const result=valid();change(result);
    assert.equal(validateWalletBalanceResponse(result),false,JSON.stringify(result));
  }
});

test('wallet balances are requested only with bearer auth and never sent to trade flow',()=>{
  const wallet=readFileSync(new URL('../src/WalletBalances.jsx',import.meta.url),'utf8');
  const panel=readFileSync(new URL('../src/PrivyWalletPanel.jsx',import.meta.url),'utf8');
  const backend=readFileSync(new URL('../../backend/app.py',import.meta.url),'utf8');
  const i18n=readFileSync(new URL('../src/i18n.js',import.meta.url),'utf8');
  assert.match(wallet,/API_BASE\+'\/api\/wallet\/balances'/);
  assert.match(wallet,/Authorization:'Bearer '\+token/);
  assert.match(wallet,/method:'GET'/);
  assert.match(wallet,/cache:'no-store'/);
  assert.match(wallet,/setBalances\(null\)/);
  assert.match(wallet,/validateWalletBalanceResponse\(data\)/);
  assert.match(wallet,/current=false/);
  assert.match(panel,/identityStatus==='verified' && databaseStatus==='stored'/);
  assert.match(panel,/<WalletBalances t=\{t\} getAccessToken=\{getAccessToken\} userId=\{user\?\.id\}\/>/);
  assert.match(backend,/@app\.get\("\/api\/wallet\/balances"\)/);
  assert.match(backend,/get_wallet_balances\(did\)/);
  assert.doesNotMatch(wallet,/sendTransaction|signMessage|delegateWallet|privateKey|method:'POST'/);
  assert.match(i18n,/Echte Wallet-Guthaben · nur lesen/);
  assert.match(i18n,/Real wallet balances · read-only/);
  assert.match(i18n,/NICHT dein PAPER-Budget/);
});
