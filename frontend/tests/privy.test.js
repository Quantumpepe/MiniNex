import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const main=read('../src/main.jsx');
const auth=read('../src/PrivyWalletPanel.jsx');
const app=read('../src/App.jsx');
const cfg=read('../src/privyConfig.js');
const workflow=read('../../.github/workflows/frontend-check.yml');
test('official Privy provider is gated on a public app ID',()=>{
  assert.match(main,/PRIVY_APP_ID\s*\?/);
  assert.match(main,/<PrivyProvider/);
  assert.match(cfg,/VITE_PRIVY_APP_ID/);
  assert.match(cfg,/cmw7sb7ty013o0gl55aqyzi3y/);
  assert.match(cfg,/createOnLogin:\s*'all-users'/);
});
test('wallet uses embedded EVM address, not the demo wallet',()=>{
  assert.match(auth,/usePrivy/);
  assert.match(auth,/useWallets/);
  assert.match(auth,/walletClientType==='privy'/);
  assert.match(auth,/wallet\.address/);
  assert.match(app,/<PrivyWalletPanel t=\{t\}/);
  assert.match(app,/ensurePaperOnly/);
  assert.doesNotMatch(app,/<MoneyInfo portfolio=|<FeeDetails paper=/);
  assert.match(app,/<PrivyWalletPanel t=\{t\}/);
});
test('no transfers/delegations or private credentials in client wallet',()=>{
  assert.doesNotMatch(auth,/sendTransaction\s*\(|transfer\s*\(|privateKey|appSecret|delegateWallet|signMessage\s*\(/);
  assert.doesNotMatch(cfg,/VITE_PRIVY_APP_SECRET|privateKey|appSecret/);
});
test('frontend CI publishes only tested web bundles, never a signed APK',()=>{
  assert.match(workflow,/npm run build/);
  assert.match(workflow,/needs: frontend-check/);
  assert.match(workflow,/gh release create/);
  assert.match(workflow,/mininex-web\.zip/);
  assert.doesNotMatch(workflow,/upload-artifact|assembleDebug|\.apk/);
});

test('native BTC placeholder cannot be mistaken for an EVM receiving address',()=>{
  assert.match(auth,/privyBtcTitle/);
  assert.match(auth,/privyBtcPending/);
  assert.match(read('../src/HelpCenter.jsx'),/helpSecurityText/);
  assert.doesNotMatch(auth,/bc1[a-z0-9]{20,}/i);
  assert.doesNotMatch(auth,/sendBitcoin\s*\(/);
});

test('verified account automatically provisions a separate BTC wallet with no signing rights',()=>{
  assert.match(auth, /useCreateWallet as useCreateExtendedWallet/);
  assert.match(auth, /@privy-io\/react-auth\/extended-chains/);
  assert.match(auth, /createWallet\(\{chainType: 'bitcoin-segwit'\}\)/);
  assert.match(auth, /identityStatus==='verified'&&databaseStatus==='stored'/);
  assert.match(auth, /findNativeBtcWallet\(user\)/);
  assert.match(auth, /isNativeBtcAddress\(wallet.address\)/);
  assert.match(auth, /const btcWallet = useNativeBtcWallet/);
  assert.match(auth, /createdWallet\?\.userId===user\?\.id/);
  assert.doesNotMatch(auth, /sendBitcoin\s*\(|signRaw\s*\(|sendTransaction\s*\(/);
});

test('ETH and native BTC can copy their full public address without signing',()=>{
  assert.match(auth,/function CopyWalletAddress\(\{address, network, t\}\)/);
  assert.match(auth,/navigator\.clipboard\.writeText\(address\)/);
  assert.match(auth,/<CopyWalletAddress address=\{btcWallet.address\} network="BTC" t=\{t\}\/>/);
  assert.match(auth,/<CopyWalletAddress address=\{address\} network="ETH" t=\{t\}\/>/);
  assert.match(auth,/privyAddressCopied/);
  assert.match(auth,/privyCopyFailed/);
  assert.match(auth,/renderCoinAddress=\{renderCoinAddress\}/);
  assert.doesNotMatch(auth,/privy-addresses-section/);
  assert.match(auth,/coinAddressPending/);
  assert.doesNotMatch(auth,/sendTransaction\s*\(|sendBitcoin\s*\(|signMessage\s*\(/);
});

test('full wallet addresses can be revealed per network without signing or sending',()=>{
  assert.match(auth,/setShowFull\(v=>!v\)/);
  assert.match(auth,/aria-expanded=\{showFull\}/);
  assert.match(auth,/overflowWrap:'anywhere'/);
  assert.match(auth,/<strong className="privy-wallet-network">\{network\}<\/strong>/);
  assert.doesNotMatch(auth,/sendTransaction\s*\(|sendBitcoin\s*\(|delegateWallet\s*\(/);
});

test('native BTC uses Privy bitcoin-segwit consistently for create and recovery',()=>{
  assert.match(auth,/account\?\.chainType === 'bitcoin-segwit'/);
  assert.match(auth,/wallet\.chainType !== 'bitcoin-segwit'/);
  assert.match(auth,/createWallet\(\{chainType: 'bitcoin-segwit'\}\)/);
  assert.doesNotMatch(auth,/chainType: 'bitcoin'|chainType === 'bitcoin'/);
});
