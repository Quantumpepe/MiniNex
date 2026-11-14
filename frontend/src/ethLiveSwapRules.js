// Ethereum mainnet Router02 ERC20/WETH swap transaction restricted to own Privy EVM wallet.
export const ROUTER02='0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45';
export const WETH9='0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2';
export const STABLES={
  USDC:'0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48',
  USDT:'0xdac17f958d2ee523a2206206994597c13d831ec7',
};
const ADDRESS=/^0x[0-9a-fA-F]{40}$/;
const HASH=/^0x[0-9a-fA-F]{64}$/;
const CALL=/^0x[0-9a-fA-F]{456}$/;
const hexWord=(text)=>BigInt('0x'+text);
const addressWord=(text)=>{
  const n=hexWord(text);
  if(n>=1n<<160n)throw Error('non-canonical address');
  return '0x'+n.toString(16).padStart(40,'0');
};
export function validateOwnerRouter02Call(quote,{wallet,side,stable,amount}){
  if(!ADDRESS.test(wallet)||!['BUY','SELL'].includes(side)||!STABLES[stable]||
     !quote||quote.wallet_identity_verified!==true||
     quote.unsigned_swap_validated!==true||!Number.isSafeInteger(quote.transaction_expires_at)||
     quote.transaction_expires_at<=Math.floor(Date.now()/1000)||
     quote.transaction_expires_at>Math.floor(Date.now()/1000)+20)throw Error('expired quote');
  const tx=quote.transaction;
  if(!tx||tx.chainId!==1||tx.from?.toLowerCase()!==wallet.toLowerCase()||
     tx.to?.toLowerCase()!==ROUTER02||tx.value!=='0x0'||
     typeof tx.data!=='string'||!CALL.test(tx.data)||
     tx.data.slice(0,10).toLowerCase()!=='0x04e45aaf')throw Error('unexpected transaction');
  const words=Array.from({length:7},(_,i)=>tx.data.slice(10+i*64,10+(i+1)*64));
  const source=addressWord(words[0]),dest=addressWord(words[1]);
  const fee=hexWord(words[2]),recipient=addressWord(words[3]);
  const units=hexWord(words[4]),minOut=hexWord(words[5]);
  const zero=hexWord(words[6]);
  const wantSource=side==='BUY'?STABLES[stable]:WETH9;
  const wantDest=side==='BUY'?WETH9:STABLES[stable];
  const decimals=side==='BUY'?6:18;
  if(source!==wantSource||dest!==wantDest||
     recipient!==wallet.toLowerCase()||![500n,3000n].includes(fee)||
     zero!==0n||units<=0n||minOut<=0n||units>=10n**45n||
     !/^(0|[1-9][0-9]*)(\\.[0-9]+)?$/.test(amount))throw Error('route mismatch');
  const [whole,fraction='']=amount.split('.');
  if(fraction.length>decimals)throw Error('precision');
  const expected=BigInt(whole)*10n**BigInt(decimals)+
    BigInt((fraction||'').padEnd(decimals,'0')||'0');
  if(expected!==units)throw Error('amount mismatch');
  return {from:wallet,to:ROUTER02,data:tx.data,value:'0x0',
    sourceToken:source,amountAtomic:units,minimumAtomic:minOut,
    expiresAt:quote.transaction_expires_at*1000};
}
export function abiAllowance(owner,spender){
  if(!ADDRESS.test(owner)||!ADDRESS.test(spender))throw Error('address');
  return '0xdd62ed3e'+owner.slice(2).toLowerCase().padStart(64,'0')+
    spender.slice(2).toLowerCase().padStart(64,'0');
}
export function abiBalance(owner){
  if(!ADDRESS.test(owner))throw Error('address');
  return '0x70a08231'+owner.slice(2).toLowerCase().padStart(64,'0');
}
export function abiApprove(spender,units){
  if(!ADDRESS.test(spender)||typeof units!=='bigint'||units<=0n)throw Error('approve');
  return '0x095ea7b3'+spender.slice(2).toLowerCase().padStart(64,'0')+
    units.toString(16).padStart(64,'0');
}
export function decodeUint(raw){
  if(typeof raw!=='string'||!/^0x[0-9a-fA-F]{64}$/.test(raw))throw Error('erc20 rpc');
  return BigInt(raw);
}
export function ownerAccounts(accounts,wallet){
  return Array.isArray(accounts)&&accounts.some(a=>
    typeof a==='string'&&a.toLowerCase()===wallet.toLowerCase());
}
export const validHash=x=>typeof x==='string'&&HASH.test(x);
