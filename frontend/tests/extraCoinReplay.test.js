import test from 'node:test';
import assert from 'node:assert/strict';

import {readFileSync} from 'node:fs';
const src=readFileSync(new URL('../src/ExtraCoinStrategyReplay.jsx',import.meta.url),'utf8');

test('paper replay never creates unauthenticated real LIVE orders',()=>{
  assert.match(src,/if\(liveMode\)return/);
  assert.match(src,/\{!liveMode&&<button type="submit"/);
  assert.doesNotMatch(src,/\/api\/live\/evm\/auto-session\/.*\/start/);
});
