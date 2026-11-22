import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {EXTRA_REPLAY_COINS} from '../src/extraReplayRules.js';
import {validSavedExtraCoinPreferences,savedExtraOptions,coinPreferenceChanged}
  from '../src/extraCoinPreferenceRules.js';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const settingsUI=read('../src/TradingSetupPreview.jsx');
const extraUI=read('../src/ExtraCoinStrategyReplay.jsx');

const data=()=>({
  mode:'INDEPENDENT_COIN_PREFERENCES',
  assets:[...EXTRA_REPLAY_COINS],
  independent_budgets:true,active_sessions_unchanged:true,
  wallet_authorized:false,live_trading_enabled:false,
  settings:Object.fromEntries(EXTRA_REPLAY_COINS.map((coin,i)=>[
    coin,{budget_usd:500+i*100,mode:i%2?'AUTO':'LONG',
      stable:i%2?'USDT':'USDC',horizon:i%2?'1h':'15m',revision:1},
  ])),
});

test('ten saved settings remain separate and include ARB without granting trading',()=>{
  const snapshot=data();
  assert.equal(EXTRA_REPLAY_COINS.length,10);
  assert.equal(EXTRA_REPLAY_COINS.includes('ARB'),true);
  assert.equal(EXTRA_REPLAY_COINS.includes('QNT'),true);
  assert.equal(EXTRA_REPLAY_COINS.includes('DOGE'),false);
  assert.equal(validSavedExtraCoinPreferences(snapshot),true);
  const result=savedExtraOptions(snapshot);
  for(const coin of EXTRA_REPLAY_COINS){
    assert.equal(result[coin].budget,String(snapshot.settings[coin].budget_usd));
    assert.equal(result[coin].mode,snapshot.settings[coin].mode);
    assert.equal(result[coin].stable,snapshot.settings[coin].stable);
    assert.equal(result[coin].horizon,snapshot.settings[coin].horizon);
  }
});

test('QNT budget input accepts independent user capital above 5000',()=>{
  const saved=data(), options=savedExtraOptions(saved);
  const unchanged=structuredClone(options.UNI);
  options.QNT={...options.QNT,budget:'25000',mode:'AUTO'};
  assert.equal(coinPreferenceChanged(options.QNT,saved.settings.QNT),true);
  assert.equal(coinPreferenceChanged(options.UNI,saved.settings.UNI),false);
  assert.deepEqual(options.UNI,unchanged);
  assert.match(settingsUI,/validExtraReplayBudget\(replayOptions\[coin\]\?\.budget\)/);
  assert.match(settingsUI,/setTimeout\(\(\)=>\{void saveExtra\(asset,value\);\},400\)/);
});

test('editing AUTO budget for one coin cannot silently overwrite another',()=>{
  const snapshot=data(), opts=savedExtraOptions(snapshot);
  const previous=structuredClone(opts);
  opts.SOL={...opts.SOL,budget:'2500',mode:'AUTO'};
  assert.equal(coinPreferenceChanged(opts.SOL,snapshot.settings.SOL),true);
  assert.equal(coinPreferenceChanged(opts.BNB,snapshot.settings.BNB),false);
  assert.deepEqual(opts.BNB,previous.BNB);
  assert.equal(snapshot.settings.SOL.budget_usd,600);
});

test('malformed or permission-granting server responses never replace settings',()=>{
  for(const mutation of [
    d=>{d.live_trading_enabled=true;},
    d=>{d.wallet_authorized=true;},
    d=>{d.assets.reverse();},
    d=>{delete d.settings.UNI;},
    d=>{d.settings.SOL.mode='LEVERAGE';},
    d=>{d.settings.BNB.budget_usd=9007199254740992;},
    d=>{d.settings.TRX.revision='0';},
  ]){
    const d=data();mutation(d);
    assert.equal(validSavedExtraCoinPreferences(d),false);
    assert.throws(()=>savedExtraOptions(d));
  }
});

test("skip",()=>{});


test('selecting ARB always has independent form defaults, preventing wallet black screen',()=>{
  const saved=data(), options=savedExtraOptions(saved);
  assert.deepEqual(Object.keys(options),EXTRA_REPLAY_COINS);
  assert.equal(options.ARB.budget,String(saved.settings.ARB.budget_usd));
  assert.equal(options.ARB.mode,saved.settings.ARB.mode);
  assert.equal(typeof options.ARB.stable,'string');
  assert.equal(validSavedExtraCoinPreferences(saved),true);
  const withoutArb=structuredClone(saved);
  delete withoutArb.settings.ARB;
  assert.equal(validSavedExtraCoinPreferences(withoutArb),false);
  assert.match(settingsUI,/options=\{replayOptions\[opened\]\|\|defaultReplayOption\(\)\}/);
});
