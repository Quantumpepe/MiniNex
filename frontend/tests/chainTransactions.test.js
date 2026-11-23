import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read=name=>readFileSync(new URL(name,import.meta.url),'utf8');
const chain=read('../src/ChainTransactions.jsx');
const app=read('../src/App.jsx');
const i18n=read('../src/i18n.js');
const quote=read('../src/ManualSwapPreview.jsx');

test('history retains tax exports and adds per-chain transaction explorer in activity',()=>{
  assert.match(app,/<BlockchainPreview t=\{t\}\/>/);
  assert.match(app,/<ChainTransactions t=\{t\} lang=\{language\}\/>/);
  assert.match(app,/<ActivityReport t=\{t\}\/>/);
  assert.match(chain,/const NETWORKS=\['BTC','ETH','BNB','POL','AVAX','ARB','SOL','XRP','TRX'\]/);
  assert.match(chain,/\/api\/blockchain\/chain-transactions\//);
  assert.match(chain,/getAccessToken/);
  assert.match(chain,/Authorization:'Bearer '\+token/);
  assert.match(chain,/wallet_ownership_verified!==true/);
  assert.match(chain,/preview_only!==true/);
  assert.match(chain,/history_complete!==false/);
  assert.match(chain,/target="_blank" rel="noopener noreferrer"/);
  assert.match(chain,/verifiedExplorerUrl\(chain,tx.explorer_url\)/);
  assert.match(chain,/HOSTS=\{/);
  assert.match(chain,/u.hostname!==HOSTS\[chain\]/);
  assert.doesNotMatch(chain,/eth_sendTransaction|signTransaction|wallet_sendTransaction/);
});
test('both languages provide concise titles and wallet-specific status',()=>{
  for(const value of [
    'Transaktionen pro Chain','Transactions by blockchain',
    'Blockchain auswählen','Choose blockchain',
    'Für dieses Netzwerk ist noch keine Wallet verknüpft.',
    'No linked wallet for this network.',
    'Transaktion öffnen','Open transaction',
  ])assert.ok(i18n.includes(value),value);
});
test('native XRP shows compact quote and Swap control without pretending execution exists',()=>{
  assert.match(quote,/walletSwapRate/);
  assert.match(quote,/maximumFractionDigits:8/);
  assert.match(quote,/walletSwapFrom/);
  assert.match(quote,/walletSwapTo/);
  assert.match(quote,/quote.request.coin==='MULTICHAIN'&&<>/);
  assert.match(quote,/disabled aria-disabled="true">\{t.walletSwapAction\}/);
  assert.match(quote,/walletSwapPendingShort/);
  assert.doesNotMatch(quote,/\{t.walletSwapNativeLiquidityNotice\}/);
  assert.doesNotMatch(quote,/\{t.walletSwapFeesUnknown\}/);
  assert.doesNotMatch(quote,/\{t.walletSwapManualOnly\}/);
});
