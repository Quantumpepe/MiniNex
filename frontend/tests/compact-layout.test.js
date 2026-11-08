import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const css=readFileSync(new URL('../src/styles.css',import.meta.url),'utf8');
const app=readFileSync(new URL('../src/App.jsx',import.meta.url),'utf8');
const compact=readFileSync(new URL('../src/CompactOverview.jsx',import.meta.url),'utf8');
const workflow=readFileSync(new URL('../../.github/workflows/android-debug.yml',import.meta.url),'utf8');
test('Compact phone Home occupies the viewport without scrolling, and desktop remains available',()=>{
 assert.match(css,/\.compact-home\{display:none\}/);
 assert.match(css,/@media\(max-width:780px\)/);
 assert.match(css,/\.app-shell--home.*height:100dvh;min-height:0;overflow:hidden/);
 assert.match(css,/\.desktop-overview\{display:none\}/);
 assert.match(app,/<CompactOverview/);
});
test('Market quote denomination is USDT, not demo settlement token',()=>{
 assert.match(compact,/\{asset\} \/ USDT/);
 assert.match(compact,/price_usdt/);
});
test('Compact buttons lead to actual Wallet/Trade sections, not fake live orders',()=>{
 assert.match(compact,/goTo\('activity'\)/);
 assert.match(compact,/goTo\('wallet'\)/);
 assert.doesNotMatch(compact,/onClick\s*=\s*\{[^}]+trade/i);
});
test('Native build stays manual and no APK is published publicly',()=>{
 assert.match(workflow,/workflow_dispatch:/);
 assert.doesNotMatch(workflow,/upload-artifact|gh release|branches: \[main\]/);
});
