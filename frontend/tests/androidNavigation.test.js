import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const app=readFileSync(new URL('../src/App.jsx',import.meta.url),'utf8');
const pkg=JSON.parse(readFileSync(new URL('../package.json',import.meta.url),'utf8'));

test('Capacitor native hardware back navigates inside MiniNex',()=>{
  assert.equal(pkg.dependencies['@capacitor/app'].startsWith('^8.'),true);
  assert.match(app,/CapacitorApp\.addListener\('backButton'/);
  assert.match(app,/previousPages\.current\.push\(pageRef\.current\)/);
  assert.match(app,/previousPages\.current\.pop\(\)/);
  assert.match(app,/setPage\(target\)/);
  assert.match(app,/goTo=\{navigate\}/);
  assert.match(app,/setPage=\{navigate\}/);
});

test('Back closes Settings help first and will not accidentally exit from home',()=>{
  assert.match(app,/if\(helpRef\.current\)/);
  assert.match(app,/setHelpOpen\(false\)/);
  assert.match(app,/const target=previous\|\|'overview'/);
  assert.doesNotMatch(app,/CapacitorApp\.exitApp\(/);
});
