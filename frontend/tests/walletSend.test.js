import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {
  SEND_ASSETS,SEND_EVM,sendAssetSpec,validSendAddress,exactSendUnits,
  formatSendUnits,rpcHex,readRpcQuantity,buildEvmSendTransaction,
  maxSendUnits,reviewEvmSend,verifiedSendReceipt,
  providerOwnsAddress,verifiedSendTransaction,sendAddressPlaceholder,
} from '../src/walletSendRules.js';

const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');
const sender=read('../src/WalletSend.jsx');
const panel=read('../src/PrivyWalletPanel.jsx');
const labels=read('../src/i18n.js');

test('12 listed coins plus Ethereum USDC/USDT; only proven EVM routes claim a signer',()=>{
  assert.deepEqual(SEND_ASSETS,['BTC','ETH','BNB','SOL','XRP','TRX','POL',
    'AVAX','LINK','DOGE','UNI','ADA','USDC','USDT']);
  assert.deepEqual(Object.keys(SEND_EVM).sort(),
    ['ETH','BNB','POL','AVAX','LINK','UNI','USDC','USDT'].sort());
  for(const symbol of ['BTC','SOL','XRP','TRX','DOGE','ADA'])assert.equal(sendAssetSpec(symbol),null);
  assert.equal(sendAssetSpec('ETH').chainId,1);
  assert.equal(sendAssetSpec('BNB').chainId,56);
  assert.equal(sendAssetSpec('POL').chainId,137);
  assert.equal(sendAssetSpec('AVAX').chainId,43114);
  assert.equal(sendAssetSpec('LINK').chainId,1);
  assert.equal(sendAssetSpec('USDT').decimals,6);
  assert.equal(sendAssetSpec('USDC').decimals,6);
  assert.match(sender,/coin==='ADA'\?t\.walletSwapAdaPending/);
  assert.match(sender,/walletSendChainPending/);
});

test('send amounts use exact BigInt instead of rounding user money',()=>{
  assert.equal(exactSendUnits('0.000000000000000001',18),1n);
  assert.equal(exactSendUnits('100.000001',6),100000001n);
  assert.equal(exactSendUnits('0.000001',6),1n);
  assert.equal(exactSendUnits('0.5',18),500000000000000000n);
  assert.equal(formatSendUnits(123456789n,6),'123.456789');
  assert.equal(formatSendUnits(1000000000000000000n,18),'1');
  for(const value of ['-1','0','0.0','NaN','Infinity','1e3','1.2.3',
                      '01','1.', ' 3','1,5',0,undefined]) {
    assert.equal(exactSendUnits(value,18),null,String(value));
  }
  assert.equal(exactSendUnits('0.0000001',6),null);
  assert.equal(exactSendUnits('0.0000000000000000001',18),null);
  assert.equal(exactSendUnits('1',undefined),null);
});

test('EVM destination rejects zero address, self-transfer and other chain formats',()=>{
  const own='0x'+'a'.repeat(40);
  const to='0x'+'b'.repeat(40);
  assert.equal(validSendAddress(to,own),true);
  for(const address of ['0x'+'0'.repeat(40),own,own.toUpperCase().replace('0X','0x'),
                        'bc1q'+'a'.repeat(38),'T'+'a'.repeat(33),
                        'r'+'a'.repeat(31),'addr1'+'a'.repeat(56),'0x123',null]){
    assert.equal(validSendAddress(address,own),false,String(address));
  }
  assert.equal(validSendAddress(to,'0x123'),false);
});

test('native transfers and allowlisted ERC-20 transfer calldata are exact',()=>{
  const from='0x'+'a'.repeat(40);
  const to='0x'+'b'.repeat(40);
  const native=buildEvmSendTransaction('BNB',from,to,123n);
  assert.deepEqual(native,{from,to,value:'0x7b'});
  assert.equal(readRpcQuantity(native.value),123n);
  assert.equal(readRpcQuantity('0x0'),0n);
  assert.equal(rpcHex(0n),'0x0');
  for(const bad of ['0x01','0x','0X01','1','0xgg',1,null]){
    assert.throws(()=>readRpcQuantity(bad));
  }
  for(const asset of ['USDC','USDT','UNI','LINK']){
    const tx=buildEvmSendTransaction(asset,from,to,12345n);
    assert.equal(tx.to,sendAssetSpec(asset).contract);
    assert.equal(tx.from,from);
    assert.equal(tx.value,'0x0');
    assert.equal(tx.data,'0xa9059cbb'+('b'.repeat(40)).padStart(64,'0')+
      (12345n).toString(16).padStart(64,'0'));
    assert.equal(tx.data.length,138);
    assert.doesNotMatch(tx.data,/approve|permit/i);
  }
  assert.throws(()=>buildEvmSendTransaction('BTC',from,to,1n));
  assert.throws(()=>buildEvmSendTransaction('ETH',from,from,1n));
  assert.throws(()=>buildEvmSendTransaction('ETH',from,to,0n));
});

test('max sends preserve worst-case gas fee; ERC20 max uses full token balance',()=>{
  const gas=25000n,price=2_000_000_000n;
  assert.equal(maxSendUnits(1000000000000000000n,gas,price,true),
    999950000000000000n);
  assert.equal(maxSendUnits(30n,gas,price,true),0n);
  assert.equal(maxSendUnits(30n,gas,price,false),30n);
  assert.equal(maxSendUnits(-1n,gas,price,true),null);
  assert.equal(maxSendUnits(100n,-1n,price,true),null);
});

test('fee and amount review is tied to one wallet/network for 90 seconds',()=>{
  const from='0x'+'a'.repeat(40),to='0x'+'b'.repeat(40);
  const base={symbol:'ETH',from,to,amount:'0.5',
    gasLimit:21000n,gasPrice:20_000_000_000n,
    nativeBalance:1_000_000_000_000_000_000n,tokenBalance:null,
    now:1_800_000_000_000};
  const good=reviewEvmSend(base);
  assert.ok(good);
  assert.equal(good.expiresAt,base.now+90000);
  assert.equal(good.gasCost,420000000000000n);
  assert.equal(good.transaction.value,'0x'+(5n*10n**17n).toString(16));
  for(const broken of [
    {...base,amount:'0'},
    {...base,amount:'1'},
    {...base,to:from},
    {...base,gasLimit:1500001n},
    {...base,gasLimit:-1n},
    {...base,gasPrice:0n},
    {...base,nativeBalance:1n},
  ]) assert.equal(reviewEvmSend(broken),null);
  const erc={...base,symbol:'USDC',amount:'100',nativeBalance:10n**16n,
    tokenBalance:101n*10n**6n};
  assert.ok(reviewEvmSend({...base,gasLimit:400001n})); // legitimate complex recipients
  assert.ok(reviewEvmSend(erc));
  assert.equal(reviewEvmSend({...erc,tokenBalance:99n*10n**6n}),null);
  assert.equal(reviewEvmSend({...erc,nativeBalance:1n}),null);
});

test('send UI uses current Privy-owned wallet, explicit send tap and no automatic transfers',()=>{
  assert.match(panel,/import WalletSend from '\.\/WalletSend\.jsx'/);
  assert.ok(panel.indexOf('<WalletSend')>panel.indexOf('<WalletBalances'));
  assert.ok(panel.indexOf('<WalletSend')<panel.indexOf('<ManualSwapPreview'));
  assert.match(sender,/useWallets\(\)/);
  assert.match(sender,/walletClientType==='privy'/);
  assert.match(sender,/getEthereumProvider/);
  assert.match(sender,/switchChain\(spec\.chainId\)/);
  assert.match(sender,/if\(chain!==BigInt\(spec\.chainId\)\)/);
  assert.match(sender,/providerOwnsAddress\(accounts,embedded\.address\)/);
  assert.match(sender,/method:'eth_chainId'/);
  assert.match(sender,/method:'eth_accounts'/);
  assert.match(sender,/method:'eth_estimateGas'/);
  assert.match(sender,/method:'eth_gasPrice'/);
  assert.match(sender,/method:'eth_getBalance'/);
  assert.match(sender,/method:'eth_call'/);
  assert.match(sender,/method:'eth_sendTransaction',params:\[tx\]/);
  assert.match(sender,/onClick=\{send\}/);
  assert.match(sender,/sending\.current=true/);
  assert.match(sender,/Date\.now\(\)>=review\.expiresAt/);
  assert.match(sender,/review\.from\.toLowerCase\(\)!==embedded/);
  assert.match(sender,/walletSendMax/);
  assert.match(sender,/walletSendFeeMax/);
  assert.match(sender,/walletSendUncertain/);
  assert.match(sender,/method:'eth_getTransactionReceipt'/);
  assert.match(sender,/method:'eth_getTransactionByHash'/);
  assert.match(sender,/verifiedSendTransaction\(review,onChain,txHash\)/);
  assert.match(sender,/receiptAttempts\.current>=3/);
  assert.match(sender,/\},12000\)/);
  assert.match(sender,/walletSendAutoReceipt/);
  assert.doesNotMatch(sender,/onClick=\{verifyReceipt\}/);
  assert.match(sender,/let broadcastAttempted=false/);
  assert.match(sender,/else if\(!broadcastAttempted\)/);
  assert.doesNotMatch(sender,/setInterval\s*\(/);
  assert.doesNotMatch(sender,/delegateWallet|privateKey|walletSecret|setApprovalForAll|unlimitedApproval/);
  assert.equal((sender.match(/method:'eth_sendTransaction'/g)||[]).length,1);
  assert.match(labels,/walletSendTitle: 'Senden'/);
  assert.match(labels,/walletSendTitle: 'Send'/);
});

test('receipt verifies real ERC20 transfer logs, not just an apparent success',()=>{
  const from='0x'+'a'.repeat(40),to='0x'+'b'.repeat(40);
  const plan=reviewEvmSend({
    symbol:'USDC',from,to,amount:'1.5',
    gasLimit:65000n,gasPrice:2000000000n,
    nativeBalance:1000000000000000000n,tokenBalance:2000000n,
    now:1800000000000,
  });
  assert.ok(plan);
  const event='0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef';
  const receipt={transactionHash:'0x'+'c'.repeat(64),from,to:plan.transaction.to,status:'0x1',
    logs:[{address:plan.transaction.to,topics:[
      event,
      '0x'+from.slice(2).padStart(64,'0'),
      '0x'+to.slice(2).padStart(64,'0'),
    ],data:'0x'+plan.amountUnits.toString(16).padStart(64,'0')}]};
  assert.equal(verifiedSendReceipt(plan,receipt),true);
  for(const changed of [
    {status:'0x0'},{from:to},{to:to},
    {logs:[]},{logs:null},
    {logs:[{...receipt.logs[0],data:'0x'+(plan.amountUnits+1n).toString(16).padStart(64,'0')}]},
    {logs:[{...receipt.logs[0],topics:[event,null,null]}]},
    {logs:[{...receipt.logs[0],address:to}]},
  ]) assert.equal(verifiedSendReceipt(plan,{...receipt,...changed}),false);
  const native=reviewEvmSend({...{
    symbol:'ETH',from,to,amount:'0.25',gasLimit:21000n,
    gasPrice:2000000000n,nativeBalance:1000000000000000000n,
    tokenBalance:null,now:1800000000000,
  }});
  assert.equal(verifiedSendReceipt(native,{from,to,status:'0x1',
    transactionHash:receipt.transactionHash}),true);
});

test('Privy EVM provider can expose multiple accounts without extra wallet prompts',()=>{
  const own='0x'+'a'.repeat(40);
  const other='0x'+'b'.repeat(40);
  assert.equal(providerOwnsAddress([own],own),true);
  assert.equal(providerOwnsAddress([other,own],own),true);
  assert.equal(providerOwnsAddress([other,own.toUpperCase().replace('0X','0x')],own),true);
  assert.equal(providerOwnsAddress([other],own),false);
  assert.equal(providerOwnsAddress([],own),false);
  assert.equal(providerOwnsAddress('wrong',own),false);
  assert.equal(providerOwnsAddress([own],'0x123'),false);
  assert.equal(providerOwnsAddress([own,null],own),true);
  assert.equal(providerOwnsAddress(Array(25).fill(own),own),true);
});

test('on-chain native send verification requires the exact reviewed amount',()=>{
  const from='0x'+'a'.repeat(40),to='0x'+'b'.repeat(40);
  const plan=reviewEvmSend({symbol:'ETH',from,to,amount:'0.5',
    gasLimit:25000n,gasPrice:2000000000n,
    nativeBalance:1000000000000000000n,now:1800000000000});
  assert.ok(plan);
  const hash='0x'+'e'.repeat(64);
  const tx={hash,from,to,value:plan.transaction.value,input:'0x'};
  assert.equal(verifiedSendTransaction(plan,tx,hash),true);
  assert.equal(verifiedSendTransaction(plan,{...tx,input:'0x1234'},hash),false);
  assert.equal(verifiedSendTransaction(plan,{...tx,value:'0x1'},hash),false);
  assert.equal(verifiedSendTransaction(plan,{...tx,from:to},hash),false);
  assert.equal(verifiedSendTransaction(plan,{...tx,to:from},hash),false);
  assert.equal(verifiedSendTransaction(plan,{...tx,hash:'0x'+'f'.repeat(64)},hash),false);
  assert.equal(verifiedSendTransaction(plan,{...tx,value:'-1'},hash),false);
  assert.equal(verifiedSendTransaction(plan,null,hash),false);
});

test('on-chain ERC20 verification requires exact token calldata and zero ETH',()=>{
  const from='0x'+'a'.repeat(40),to='0x'+'b'.repeat(40);
  const plan=reviewEvmSend({symbol:'USDT',from,to,amount:'10',
    gasLimit:100000n,gasPrice:1500000000n,
    nativeBalance:1000000000000000000n,tokenBalance:50000000n,
    now:1800000000000});
  const hash='0x'+'e'.repeat(64);
  const tx={hash,from,to:plan.transaction.to,
    value:'0x0',input:plan.transaction.data};
  assert.equal(verifiedSendTransaction(plan,tx,hash),true);
  assert.equal(verifiedSendTransaction(plan,{...tx,value:'0x10'},hash),false);
  assert.equal(verifiedSendTransaction(plan,{...tx,to:to},hash),false);
  assert.equal(verifiedSendTransaction(plan,{...tx,input:'0xa9059cbb'},hash),false);
  assert.equal(verifiedSendTransaction(plan,{...tx,input:plan.transaction.data.slice(0,-1)+'1'},hash),false);
});

test('gas fee overflow and absurd gas limits cannot be quoted',()=>{
  const own='0x'+'a'.repeat(40),to='0x'+'b'.repeat(40);
  const base={symbol:'ETH',from:own,to,amount:'1',gasLimit:21000n,
    gasPrice:2000000000n,nativeBalance:10n**30n,now:1800000000000};
  assert.ok(reviewEvmSend(base));
  assert.equal(reviewEvmSend({...base,gasLimit:1500001n}),null);
  assert.equal(reviewEvmSend({...base,gasPrice:2n**256n}),null);
});

test('send recipient hints follow native address families, never mislabel all as EVM',()=>{
  for(const [coin,hint] of Object.entries({
    BTC:'bc1…',SOL:'Base58…',XRP:'r…',TRX:'T…',
    DOGE:'D…',ADA:'addr1…',ETH:'0x…',BNB:'0x…',
    POL:'0x…',AVAX:'0x…',LINK:'0x…',UNI:'0x…',
    USDC:'0x…',USDT:'0x…',
  }))assert.equal(sendAddressPlaceholder(coin),hint);
  assert.match(sender,/placeholder=\{sendAddressPlaceholder\(coin\)\}/);
  assert.match(sender,/disabled=\{busy\|\|sending.current\|\|!spec\}/);
});
