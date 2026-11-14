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

test('nine saved settings remain separate; ADA is not tradable',()=>{
  const snapshot=data();
  assert.equal(EXTRA_REPLAY_COINS.length,9);
  assert.equal(EXTRA_REPLAY_COINS.includes('ADA'),false);
  assert.equal(validSavedExtraCoinPreferences(snapshot),true);
  const result=savedExtraOptions(snapshot);
  for(const coin of EXTRA_REPLAY_COINS){
    assert.equal(result[coin].budget,String(snapshot.settings[coin].budget_usd));
    assert.equal(result[coin].mode,snapshot.settings[coin].mode);
    assert.equal(result[coin].stable,snapshot.settings[coin].stable);
    assert.equal(result[coin].horizon,snapshot.settings[coin].horizon);
  }
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
    d=>{d.settings.BNB.budget_usd=5001;},
    d=>{d.settings.TRX.revision='0';},
  ]){
    const d=data();mutation(d);
    assert.equal(validSavedExtraCoinPreferences(d),false);
    assert.throws(()=>savedExtraOptions(d));
  }
});

test('per-coin save and retry replace duplicate global control, not trade controls',()=>{
  assert.match(settingsUI,/\/api\/coin-preferences/);
  assert.match(settingsUI,/expected_revision:prior.revision/);
  assert.match(settingsUI,/setSavedExtra\(next.settings\)/);
  assert.match(settingsUI,/onSavePreference=\{saveExtra\}/);
  assert.match(settingsUI,/setReplayOptions\(old=>\(\{/);
  assert.match(settingsUI,/userId=\{userId\}/);
  assert.doesNotMatch(settingsUI,/Für alle 11 Coins übernehmen|applyGlobal/);
  assert.match(extraUI,/onSavePreference\?\.\(asset\)/);
  assert.match(extraUI,/<ExtraCoinPaperControl/);
  assert.match(extraUI,/preferenceStatus==='failed'/);
  assert.match(extraUI,/<option value="AUTO">/);
});
