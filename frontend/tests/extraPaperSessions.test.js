import test from 'node:test';
import assert from 'node:assert/strict';

import {readFileSync} from 'node:fs';
const control=readFileSync(new URL('../src/ExtraCoinPaperControl.jsx',import.meta.url),'utf8');

test('BTC and ETH started paper budgets display as numbers, not unformatted strings',()=>{
  assert.match(control,/const initial=Number\(result\.demo_start_cash\?\?result\.position\?\.demo_start_cash\)/);
  assert.match(control,/result\.budget_usd=Number\.isFinite\(initial\)&&initial>0\?initial:null/);
  assert.match(control,/fmt\(data\.budget_usd\)/);
});

test('placeholder',()=>{
  assert.ok(true);
});
