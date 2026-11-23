import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read=name=>readFileSync(new URL(name,import.meta.url),'utf8');
const chain=read('../src/ChainTransactions.jsx');
const app=read('../src/App.jsx');
const i18n=read('../src/i18n.js');
const quote=read('../src/ManualSwapPreview.jsx');
import {COINS} from '../src/coins.js';
import {COIN_CHAIN,TOKEN_FILTERS,historyRowsForCoin} from '../src/chainHistoryCoinRules.js';

test('history retains tax exports and adds per-chain transaction explorer in activity',()=>{
  assert.match(app,/<BlockchainPreview t=\{t\}\/>/);
  assert.match(app,/<ChainTransactions t=\{t\} lang=\{language\}\/>/);
  assert.match(app,/<ActivityReport t=\{t\}\/>/);
  assert.match(chain,/COINS\.map\(c=><option/);
  assert.match(chain,/COIN_CHAIN\[coin\]/);
  assert.match(chain,/historyRowsForCoin\(data\?\.items,coin\)/);
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

test('all twelve approved coins appear in history, with tokens on their actual chain',()=>{
  const expected=['BTC','ETH','BNB','SOL','XRP','TRX','POL','AVAX',
    'LINK','QNT','UNI','ARB'];
  assert.deepEqual(COINS.map(c=>c.symbol),expected);
  assert.equal(COINS.length,12);
  for(const coin of COINS)assert.ok(COIN_CHAIN[coin.symbol],
    'Missing chain mapping for '+coin.symbol);
  for(const token of ['LINK','QNT','UNI']){
    assert.equal(COIN_CHAIN[token],'ETH');
    assert.equal(TOKEN_FILTERS.has(token),true);
  }
  assert.equal(COIN_CHAIN.ARB,'ARB');
  assert.equal(COIN_CHAIN.POL,'POL');
});

test('Ethereum token history includes only related transfers, with same explorer hashes',()=>{
  const rows=[
    {hash:'link1',chain:'ETH',asset:'ETH',tokens:[{asset:'LINK',direction:'IN'}]},
    {hash:'qnt1',chain:'ETH',asset:'ETH',tokens:[{asset:'QNT',direction:'OUT'}]},
    {hash:'uni1',chain:'ETH',asset:'ETH',tokens:[{asset:'UNI',direction:'IN'}]},
    {hash:'eth1',chain:'ETH',asset:'ETH',tokens:[]},
    {hash:'link2',chain:'ETH',asset:'LINK',tokens:[]},
    {hash:'usdc1',chain:'ETH',asset:'USDC',tokens:[]},
  ];
  assert.deepEqual(historyRowsForCoin(rows,'LINK').map(x=>x.hash),['link1','link2']);
  assert.deepEqual(historyRowsForCoin(rows,'QNT').map(x=>x.hash),['qnt1']);
  assert.deepEqual(historyRowsForCoin(rows,'UNI').map(x=>x.hash),['uni1']);
  assert.deepEqual(historyRowsForCoin(rows,'ETH'),rows);
  assert.deepEqual(historyRowsForCoin(rows,'ARB'),rows);
  assert.deepEqual(historyRowsForCoin(null,'LINK'),[]);
});
