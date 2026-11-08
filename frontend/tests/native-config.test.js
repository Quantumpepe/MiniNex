import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const conf = JSON.parse(fs.readFileSync(new URL('../capacitor.config.json', import.meta.url),'utf8'));
const pkg = JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url),'utf8'));
const app = fs.readFileSync(new URL('../src/App.jsx', import.meta.url),'utf8');
const vite = fs.readFileSync(new URL('../vite.config.js', import.meta.url),'utf8');

test('native app bundles local web assets, not a remote static site',()=>{
  assert.equal(conf.webDir,'dist');
  assert.equal(conf.server?.url,undefined);
  assert.equal(conf.appName,'MiniNex');
  assert.match(vite,/base:\s*['"]\.\//);
});
test('Capacitor Android and iOS dependencies share major version 8',()=>{
  for(const dep of ['@capacitor/core','@capacitor/android','@capacitor/ios']) assert.match(pkg.dependencies[dep],/\^8\./);
  assert.match(pkg.devDependencies['@capacitor/cli'],/\^8\./);
});
test('native WebView uses secure Android and Capacitor iOS schemes',()=>{
  assert.equal(conf.server.hostname,'localhost');
  assert.equal(conf.server.androidScheme,'https');
  assert.equal(conf.server.iosScheme,'capacitor');
});
test('no clickable Render/health URL in the in-app Settings screen',()=>{
  assert.doesNotMatch(app,/href=\{\`\$\{API_BASE\}\/health\`\}/);
});
