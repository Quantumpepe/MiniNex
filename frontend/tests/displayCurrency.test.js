import test from 'node:test';
import assert from 'node:assert/strict';
import {setDisplayCurrency,setDisplayRates} from '../src/displayCurrency.js';
import {formatMoney,formatAssetMoney,formatSigned} from '../src/data.js';

test('global fiat display uses a real supplied rate, without relabeling raw USD',()=>{
  try{
    setDisplayCurrency('CHF',{persist:false});
    assert.equal(formatMoney(100),'—');
    assert.equal(setDisplayRates({base:'USD',reference_only:true,rates:{USD:1,CHF:.8,EUR:.9,GBP:.75,JPY:150},dates:{CHF:'2026-11-23',EUR:'2026-11-23',GBP:'2026-11-23',JPY:'2026-11-23'}}),true);
    assert.equal(formatMoney(100),'80.00 CHF');
    assert.equal(formatAssetMoney(.1,'POL'),'0.080 CHF');
    assert.equal(formatSigned(-25),'−20.00 CHF');
    assert.equal(formatSigned(25,'en','USDC'),'+25.00 USDC');
  }finally{setDisplayCurrency('USD',{persist:false});}
  assert.equal(formatMoney(100),'100.00 $');
});
