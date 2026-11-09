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
 assert.match(compact,/\{asset\}\/USDT/);
 assert.match(compact,/price_usdt/);
});
test('Compact buttons lead to actual Wallet/Trade sections, not fake live orders',()=>{
 assert.match(compact,/goTo\('activity'\)/);
 assert.match(compact,/goTo\('wallet'\)/);
 assert.doesNotMatch(compact,/onClick\s*=\s*\{[^}]+trade/i);
});
test('Native build stays manual and distributes only a temporary debug artifact',()=>{
 assert.match(workflow,/workflow_dispatch:/);
 assert.doesNotMatch(workflow,/gh release|branches: \[main\]/);
 assert.match(workflow,/uses: actions\/upload-artifact@v4/);
 assert.match(workflow,/retention-days: 7/);
 assert.match(workflow,/app-debug\.apk/);
});

test('Mobile home aligns logo, language switch and settings in one row',()=>{
  assert.match(compact,/className="compact-welcome"/);
  assert.match(compact,/className="compact-home-brand"/);
  assert.match(compact,/className="compact-home-actions"/);
  assert.match(compact,/onClick=\{\(\)=>chooseLang\('de'\)\}/);
  assert.match(compact,/onClick=\{\(\)=>chooseLang\('en'\)\}/);
  assert.match(compact,/onClick=\{\(\)=>goTo\('settings'\)\}/);
  assert.match(app,/chooseLang=\{chooseLang\}/);
  assert.match(css,/\.app-shell--home \.topbar \.header-actions\{display:none\}/);
  assert.match(css,/\.compact-home-actions\{display:flex;align-items:center/);
});

test('mobile Home shows private paper portfolio before BTC and ETH cards',()=>{
  assert.match(compact,/function PaperPortfolioCard\(/);
  assert.match(compact,/portfolioSummary=\{null\}|portfolioSummary=null/);
  assert.match(compact,/<PaperPortfolioCard t=\{t\}/);
  assert.ok(compact.indexOf('<PaperPortfolioCard') < compact.indexOf('className="compact-price-cards"'));
  assert.match(compact,/compact-wallet-curve/);
  assert.match(app,/usePaperPortfolioHome\(paperVisible,refreshNonce\)/);
  assert.match(app,/portfolioSummary=\{paperHome.summary\}/);
  assert.match(css,/\.compact-wallet-portfolio\{/);
  assert.match(css,/\.compact-chart-large,\.compact-chart-empty\{height:120px/);
});

test('wallet card does not imply real wallet trading or fabricate a history',()=>{
  assert.match(compact,/paperAvailable\?'PAPER':'LIVE/);
  assert.match(compact,/aria-disabled="true"/);
  assert.match(compact,/compact-market-modes/);
  assert.match(compact,/summary&&chart/);
  assert.match(compact,/paperSparkline\(samples\)/);
  assert.doesNotMatch(compact,/LIVE.*\d{3,}/);
});
