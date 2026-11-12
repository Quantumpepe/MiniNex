// Exact, chain-specific wallet send rules. No remote calls or signing here.
// A listed asset is NOT automatically an enabled sender: native UTXO,
// Solana, TRON, XRPL and Cardano still need independently supported signers.
const EVM_ADDRESS=/^0x[0-9a-fA-F]{40}$/;
const MAX_UINT256=(1n<<256n)-1n;
const ALL=Object.freeze([
  'BTC','ETH','BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI','ADA','USDC','USDT',
]);
const EVM=Object.freeze({
  ETH: Object.freeze({chainId:1,decimals:18,feeSymbol:'ETH',kind:'native'}),
  BNB: Object.freeze({chainId:56,decimals:18,feeSymbol:'BNB',kind:'native'}),
  POL: Object.freeze({chainId:137,decimals:18,feeSymbol:'POL',kind:'native'}),
  AVAX:Object.freeze({chainId:43114,decimals:18,feeSymbol:'AVAX',kind:'native'}),
  LINK:Object.freeze({chainId:1,decimals:18,feeSymbol:'ETH',kind:'erc20',
    contract:'0x514910771af9ca656af840dff83e8264ecf986ca'}),
  UNI:Object.freeze({chainId:1,decimals:18,feeSymbol:'ETH',kind:'erc20',
    contract:'0x1f9840a85d5af5bf1d1762f925bdaddc4201f984'}),
  USDC:Object.freeze({chainId:1,decimals:6,feeSymbol:'ETH',kind:'erc20',
    contract:'0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48'}),
  USDT:Object.freeze({chainId:1,decimals:6,feeSymbol:'ETH',kind:'erc20',
    contract:'0xdac17f958d2ee523a2206206994597c13d831ec7'}),
});
export const SEND_ASSETS=ALL;
export const SEND_EVM=EVM;
export const sendAssetSpec=asset=>EVM[asset]||null;
export const validSendAddress=(address,own)=>{
  return typeof address==='string'&&EVM_ADDRESS.test(address) &&
    !/^0x0{40}$/i.test(address) &&
    (!own || (EVM_ADDRESS.test(own) && own.toLowerCase()!==address.toLowerCase()));
};
export function exactSendUnits(value,decimals) {
  if(typeof value!=='string'||value.length>48||
     !/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(value)||
     !Number.isSafeInteger(decimals)||decimals<0||decimals>18)return null;
  const [whole,fraction='']=value.split('.');
  if(fraction.length>decimals)return null;
  try {
    const units=BigInt(whole)*10n**BigInt(decimals)+
      BigInt(fraction.padEnd(decimals,'0')||'0');
    return units>0n&&units<=MAX_UINT256?units:null;
  }catch{return null;}
}
export function formatSendUnits(units,decimals) {
  if(typeof units!=='bigint'||units<0n||!Number.isInteger(decimals)||decimals<0||decimals>18)return null;
  const scale=10n**BigInt(decimals);
  const fraction=(units%scale).toString().padStart(decimals,'0').replace(/0+$/,'');
  return (units/scale).toString()+(fraction?'.'+fraction:'');
}
export function rpcHex(value) {
  if(typeof value!=='bigint'||value<0n||value>MAX_UINT256)throw Error('Invalid chain quantity');
  return '0x'+value.toString(16);
}
export function readRpcQuantity(raw) {
  if(typeof raw!=='string'||!/^0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)$/.test(raw))
    throw Error('Invalid chain amount');
  const value=BigInt(raw);
  if(value>MAX_UINT256)throw Error('Chain amount overflow');
  return value;
}
export function buildEvmSendTransaction(symbol,from,to,amountUnits) {
  const spec=sendAssetSpec(symbol);
  if(!spec||!validSendAddress(from)||!validSendAddress(to,from)||
     typeof amountUnits!=='bigint'||amountUnits<=0n||amountUnits>MAX_UINT256)
    throw Error('Invalid wallet send');
  if(spec.kind==='native')return {from,to,value:rpcHex(amountUnits)};
  // ERC-20 transfer(address,uint256), no approve/permit/allowance involved.
  const addressWord=to.slice(2).toLowerCase().padStart(64,'0');
  const amountWord=amountUnits.toString(16).padStart(64,'0');
  return {from,to:spec.contract,value:'0x0',
    data:'0xa9059cbb'+addressWord+amountWord};
}
export function maxSendUnits(balance,gasLimit,gasPrice,native) {
  if(![balance,gasLimit,gasPrice].every(x=>typeof x==='bigint'&&x>=0n))
    return null;
  const worstFee=gasLimit*gasPrice;
  if(worstFee>MAX_UINT256)return null;
  return native?(balance>worstFee?balance-worstFee:0n):balance;
}
export function reviewEvmSend({symbol,from,to,amount,gasLimit,gasPrice,
                               nativeBalance,tokenBalance,now=Date.now()}) {
  const spec=sendAssetSpec(symbol);
  const units=exactSendUnits(amount,spec?.decimals);
  if(!spec||units===null||!validSendAddress(to,from)||
     typeof gasLimit!=='bigint'||gasLimit<=0n||gasLimit>1500000n||
     typeof gasPrice!=='bigint'||gasPrice<=0n||
     typeof nativeBalance!=='bigint'||nativeBalance<0n||
     !Number.isSafeInteger(now)||now<=0) return null;
  const gasCost=gasLimit*gasPrice;
  if(gasCost>MAX_UINT256)return null;
  const spend=spec.kind==='native'?units+gasCost:gasCost;
  if(spend>MAX_UINT256||spend>nativeBalance||
    (spec.kind==='erc20'&&(typeof tokenBalance!=='bigint'||units>tokenBalance)))
    return null;
  return {symbol,chainId:spec.chainId,from,to,amount,amountUnits:units,
    gasLimit,gasPrice,gasCost,feeSymbol:spec.feeSymbol,
    transaction:buildEvmSendTransaction(symbol,from,to,units),
    expiresAt:now+90000};
}

const TRANSFER_EVENT='0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef';
export function verifiedSendReceipt(review,receipt) {
  if(!review||!receipt||
     typeof receipt.transactionHash!=='string'||
     typeof receipt.from!=='string'||typeof receipt.to!=='string'||
     receipt.from.toLowerCase()!==review.from?.toLowerCase()||
     receipt.to.toLowerCase()!==review.transaction?.to?.toLowerCase()||
     receipt.status!=='0x1')return false;
  const spec=sendAssetSpec(review.symbol);
  if(!spec)return false;
  if(spec.kind==='native')return true;
  // Successful token transaction != delivered tokens unless the matching
  // ERC-20 Transfer event was emitted. No extra RPC query is needed.
  const sourceTopic='0x'+review.from.slice(2).toLowerCase().padStart(64,'0');
  const targetTopic='0x'+review.to.slice(2).toLowerCase().padStart(64,'0');
  return Array.isArray(receipt.logs)&&receipt.logs.some(log=>
    typeof log?.address==='string'&&log.address.toLowerCase()===spec.contract &&
    Array.isArray(log.topics)&&log.topics.length===3 &&
    log.topics.every(topic=>typeof topic==='string')&&
    log.topics[0].toLowerCase()===TRANSFER_EVENT&&
    log.topics[1].toLowerCase()===sourceTopic&&
    log.topics[2].toLowerCase()===targetTopic&&
    typeof log.data==='string'&&/^0x[0-9a-fA-F]{64}$/.test(log.data)&&
    BigInt(log.data)===review.amountUnits);
}

/** Some EIP-1193 providers expose multiple accounts. Only the exact Privy
 * embedded-wallet address is allowed; other accounts never grant authority. */
export function providerOwnsAddress(accounts,embeddedAddress) {
  if(!Array.isArray(accounts)||typeof embeddedAddress!=='string'||
     !validSendAddress(embeddedAddress))return false;
  const needle=embeddedAddress.toLowerCase();
  return accounts.some(a=>typeof a==='string'&&
    EVM_ADDRESS.test(a)&&a.toLowerCase()===needle);
}

/** A chain receipt alone cannot prove what a submitted transaction attempted.
 * Compare on-chain transaction fields against the exact reviewed calldata. */
export function verifiedSendTransaction(review,tx,hash) {
  if(!review||!tx||typeof hash!=='string'||!/^0x[0-9a-fA-F]{64}$/.test(hash))
    return false;
  const original=review.transaction;
  if(!original||typeof tx.hash!=='string'||tx.hash.toLowerCase()!==hash.toLowerCase()||
     typeof tx.from!=='string'||typeof tx.to!=='string'||
     tx.from.toLowerCase()!==review.from?.toLowerCase()||
     tx.to.toLowerCase()!==original.to?.toLowerCase()||
     typeof tx.value!=='string')return false;
  try {
    if(readRpcQuantity(tx.value)!==readRpcQuantity(original.value))return false;
    if(original.data){
      return typeof tx.input==='string'&&tx.input.toLowerCase()===original.data.toLowerCase();
    }
    return tx.input==null||tx.input==='0x';
  }catch{return false;}
}
