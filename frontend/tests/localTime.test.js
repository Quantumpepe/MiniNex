import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');

test('personal paper and activity timelines use the device local time formatter',()=>{
  for(const path of ['../src/UserPaperSessions.jsx','../src/BlockchainPreview.jsx','../src/ActivityReport.jsx']){
    const source=read(path);
    assert.match(source,/formatTimestamp/);
    assert.doesNotMatch(source,/\.slice\(0,16\)\.replace\('T',' '\).*UTC/);
  }
  const ui=read('../src/UserPaperSessions.jsx');
  assert.match(ui,/formatTimestamp\(position\.last_evaluated_at/);
  assert.match(ui,/formatTimestamp\(trade\.at/);
});

test('formatter uses local device zone, including daylight saving',()=>{
  const data=read('../src/data.js');
  assert.match(data,/new Intl.DateTimeFormat/);
  assert.doesNotMatch(data,/timeZone:\s*['"]UTC['"]/);
  assert.doesNotMatch(data,/toISOString\(\)\.slice/);
});
