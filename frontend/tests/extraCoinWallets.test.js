import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {
  validEvmAddress,validNativePreviewAddress,findPrivyNativeWallet,evmNetworkAddress,
} from '../src/nativeCoinAddresses.js';

const sol='11111111111111111111111111111111';
const trx='T'+'A'.repeat(33);
const evm='0x'+'a'.repeat(40);
const wallet=(chainType,address,walletClientType='privy')=>({
  type:'wallet',chainType,address,walletClientType,
});
const user={id:'did:privy:user-A',linkedAccounts:[
  wallet('ethereum',evm),
  wallet('solana',sol),
  wallet('tron',trx),
]};

test('separate verified wallet chains cannot borrow one another\'s address',()=>{
  assert.equal(findPrivyNativeWallet(user,'solana').address,sol);
  assert.equal(findPrivyNativeWallet(user,'tron').address,trx);
  assert.equal(findPrivyNativeWallet(user,'xrpl'),null);
  assert.equal(findPrivyNativeWallet(user,'cardano'),null);
  assert.equal(findPrivyNativeWallet({linkedAccounts:user.linkedAccounts},'solana'),null);
  assert.equal(findPrivyNativeWallet({id:'other',linkedAccounts:[]},'solana'),null);
  assert.equal(findPrivyNativeWallet({id:'other',linkedAccounts:[
    wallet('solana',sol,'phantom'),wallet('tron',trx,'tronlink')
  ]},'solana'),null);
  assert.equal(findPrivyNativeWallet({id:'other',linkedAccounts:[
    wallet('solana',evm),wallet('tron',sol)
  ]},'solana'),null);
});

test('EVM-compatible BSC and Polygon use one address on distinct networks only',()=>{
  assert(validEvmAddress(evm));
  assert.equal(evmNetworkAddress('bsc-mainnet',evm),evm);
  assert.equal(evmNetworkAddress('polygon-pos-mainnet',evm),evm);
  assert.equal(evmNetworkAddress('solana-mainnet',evm),null);
  assert.equal(evmNetworkAddress('ethereum-mainnet',evm),null);
  for(const invalid of [null,'','0x123',sol,trx]) {
    assert.equal(evmNetworkAddress('bsc-mainnet',invalid),null);
  }
});

test('address prefixes and ranges cannot cross native chains',()=>{
  assert(validNativePreviewAddress('solana',sol));
  assert(validNativePreviewAddress('tron',trx));
  assert.equal(validNativePreviewAddress('solana',trx),false);
  assert.equal(validNativePreviewAddress('tron',sol),false);
  assert.equal(validNativePreviewAddress('tron',evm),false);
  assert.equal(validNativePreviewAddress('xrpl','r'+'a'.repeat(33)),false);
  assert.equal(validNativePreviewAddress('cardano','addr1'+'q'.repeat(50)),false);
});

const src=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const walletUI=src('../src/PrivyWalletPanel.jsx');
const hooks=src('../src/useAdditionalCoinWallets.js');
const translations=src('../src/i18n.js');

test('SOL and TRX creation only after Privy authentication and database verification',()=>{
  assert.match(walletUI,/useAdditionalCoinWallets\(/);
  assert.match(walletUI,/user,ready&&authenticated&&identityStatus==='verified'&&databaseStatus==='stored'/);
  assert.match(hooks,/@privy-io\/react-auth\/solana/);
  assert.match(hooks,/@privy-io\/react-auth\/extended-chains/);
  assert.match(hooks,/createSolanaWallet\(\)/);
  assert.match(hooks,/createExtendedWallet\(\{chainType:'tron'\}\)/);
  assert.match(hooks,/currentUserId\.current !== userId/);
  assert.match(hooks,/created\?\.userId === user\?\.id/);
  assert.match(hooks,/findPrivyNativeWallet\(user, chainType\)/);
  assert.doesNotMatch(hooks,/localStorage|privateKey|signTransaction|sendTransaction/);
});

test('each coin panel displays only its own network address or remains unavailable',()=>{
  for(const symbol of ['BNB','POL','SOL','TRX']) {
    assert.match(walletUI,new RegExp("asset==='"+symbol+"'"));
  }
  assert.match(walletUI,/evmNetworkAddress\(chain,address\)/);
  assert.match(walletUI,/extraWallets\.solana/);
  assert.match(walletUI,/extraWallets\.tron/);
  assert.match(walletUI,/<CopyWalletAddress address=\{evmAddress\} network=\{network\} t=\{t\}\/>/);
  assert.match(walletUI,/<CopyWalletAddress address=\{nativeWallet\.address\} network=\{network\} t=\{t\}\/>/);
  assert.match(walletUI,/t\.coinAddressPending/);
  assert.match(translations,/coinAddressReadOnly: 'Nur Wallet-Vorschau/);
  assert.match(translations,/coinEvmSharedAddress:/);
  assert.doesNotMatch(walletUI+hooks,/sendTransaction\s*\(|sendBitcoin\s*\(|delegateWallet\s*\(|privateKey/);
});
