import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const app=read('../src/App.jsx');
const report=read('../src/ActivityReport.jsx');
const i18n=read('../src/i18n.js');

test('yearly PDF and CSV exports are available from the Activity screen',()=>{
  assert.match(app,/<ActivityReport t=\{t\}\/>/);
  assert.match(report,/\/api\/activity\/\x27\+year\+\x27\/export\/\x27\+format/);
  assert.match(report,/\['csv','pdf'\]/);
  assert.match(report,/\.download='MiniNex_Activity_'/);
});

test('only Privy-authenticated verified account rows may be shown or exported',()=>{
  assert.match(report,/usePrivy\(\)/);
  assert.match(report,/getAccessToken/);
  assert.match(report,/Authorization:'Bearer '\+token/);
  assert.match(report,/data\?\.verified_only!==true/);
  assert.match(report,/data\?\.paper_excluded!==true/);
  assert.match(report,/data\?\.tax_complete!==false/);
  assert.doesNotMatch(report,/PAPER_BUY|PAPER_SELL|\/api\/paper\/trades/);
});

test('exports disclose incomplete blockchain sync and tax valuation status in both languages',()=>{
  assert.match(i18n,/taxDownloadPdf: 'Download PDF'/);
  assert.match(i18n,/taxDownloadCsv: 'Download CSV'/);
  assert.match(i18n,/taxDownloadPdf: 'PDF herunterladen'/);
  assert.match(i18n,/taxDownloadCsv: 'CSV herunterladen'/);
  assert.match(i18n,/Blockchain synchronization and CHF year-end valuations are not yet available/);
  assert.match(i18n,/Blockchain-Synchronisierung und CHF-Jahresendbewertungen fehlen noch/);
});
