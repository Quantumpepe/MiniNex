import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const app=readFileSync(new URL('../src/App.jsx',import.meta.url),'utf8');
const compact=readFileSync(new URL('../src/CompactOverview.jsx',import.meta.url),'utf8');
const css=readFileSync(new URL('../src/styles.css',import.meta.url),'utf8');
test('mobile bottom navigation is not rendered',()=>{assert.doesNotMatch(app,/<Navigation page={page} setPage={setPage} t={t} mobile\/>/);});
test('portfolio wallet shortcut remains, trade history moves to Settings',()=>{
  assert.match(compact,/goTo\('wallet'\)/);
  assert.doesNotMatch(compact,/goTo\('activity'\)/);
  assert.match(app,/className="settings-activity-link"[^\n]*navigate\('activity'\)/);
});
test('home logo and settings icon navigate without a footer',()=>{assert.match(app,/className="mobile-brand"[^\n]*navigate\('overview'\)/);assert.match(app,/className="icon-btn mobile-settings-btn"[^\n]*navigate\('settings'\)/);assert.match(css,/\.mobile-navigation\{display:none!important\}/);});
