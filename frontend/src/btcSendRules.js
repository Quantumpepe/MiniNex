import {Buffer} from 'buffer';
import * as bitcoin from 'bitcoinjs-lib';
import {secp256k1} from '@noble/curves/secp256k1';

// Native Bitcoin P2WPKH only. Never treat a TEE-created address as proof
// of signing authority; the user must approve the Privy signature.
const API='https://mempool.space/api';
const SATS=100000000n;
const DUST=546n;
export const BTC_REVIEW_MAX_AGE_MS=90000;

export function btcSatoshis(amount) {
  if(typeof amount!=='string'||!/^(?:0|[1-9]\d*)(?:\.\d{1,8})?$/.test(amount))return null;
  const [whole,fraction='']=amount.split('.');
  const value=BigInt(whole)*SATS+BigInt((fraction+'00000000').slice(0,8));
  return value>0n?value:null;
}
export function btcDisplay(sats) {
  return (Number(sats)/1e8).toFixed(8).replace(/0+$/,'').replace(/\.$/,'');
}
export function btcOwnedPublicKey(wallet) {
  const key=String(wallet?.publicKey||wallet?.public_key||'').replace(/^0x/,'');
  if(!/^(?:[0-9a-fA-F]{66}|[0-9a-fA-F]{130})$/.test(key))return null;
  const bytes=Buffer.from(key,'hex');
  try {return Buffer.from(secp256k1.ProjectivePoint.fromHex(bytes).toRawBytes(true));}
  catch{return null;}
}
export function btcOwnerScript(address,pubkey) {
  if(!address||!pubkey||!/^bc1q[ac-hj-np-z02-9]{38,59}$/.test(address))return null;
  try {
    const payment=bitcoin.payments.p2wpkh({pubkey,network:bitcoin.networks.bitcoin});
    return payment.address===address?payment.output:null;
  }catch{return null;}
}
export function btcRecipientScript(destination) {
  if(typeof destination!=='string'||destination.trim()!==destination)return null;
  try {return bitcoin.address.toOutputScript(destination,bitcoin.networks.bitcoin);}
  catch{return null;}
}
export function btcFeeRate(fees) {
  const rate=Number(fees?.halfHourFee);
  return Number.isInteger(rate)&&rate>0&&rate<=100000?rate:null;
}
export function btcValidatedUtxos(data) {
  if(!Array.isArray(data))return null;
  // Unconfirmed outputs are not spendable yet; they must not block already
  // confirmed funds from being sent.
  const confirmed=data.filter(u=>u?.status?.confirmed===true);
  if(!confirmed.length)return null;
  const used=new Set(),utxos=[];
  for(const u of confirmed){
    if(!/^[a-fA-F0-9]{64}$/.test(u?.txid||'')||
      !Number.isSafeInteger(u?.vout)||u.vout<0||
      !Number.isSafeInteger(u?.value)||u.value<=0)return null;
    const key=u.txid+':'+u.vout;
    if(used.has(key))return null;
    used.add(key);
    utxos.push({txid:u.txid,vout:u.vout,value:BigInt(u.value)});
  }
  return utxos;
}
function outputVbytes(script) {return 8+(script.length<253?1:3)+script.length;}
export function btcPlan({destination,amount,utxos,feeRate,sourceScript}) {
  const sats=btcSatoshis(amount);
  const recipientScript=btcRecipientScript(destination);
  if(!sats||!recipientScript||!sourceScript||!Array.isArray(utxos)||!utxos.length||
    !Number.isSafeInteger(feeRate)||feeRate<=0)return null;
  const chosen=[];
  let total=0n;
  // Prefer fewer inputs and therefore lower miner fees; do not invent any
  // trading limit or send limit. Each selected UTXO is verified before signing.
  for(const utxo of [...utxos].sort((a,b)=>a.value>b.value?-1:a.value<b.value?1:0)){
    chosen.push(utxo);total+=utxo.value;
    const baseSize=11+69*chosen.length+outputVbytes(recipientScript);
    const withoutChange=BigInt(baseSize)*BigInt(feeRate);
    const withChange=BigInt(baseSize+outputVbytes(sourceScript))*BigInt(feeRate);
    if(total>=sats+withChange+DUST){
      const change=total-sats-withChange;
      return {utxos:chosen,sourceScript,recipientScript,destination,amount,
        sats,fee:withChange,change,total,feeRate,createdAt:Date.now()};
    }
    // An output below dust threshold is not relayable. Only the dust-size
    // remainder may become fee; never silently donate a large change amount.
    if(total>=sats+withoutChange&&total-sats-withoutChange<DUST){
      return {utxos:chosen,sourceScript,recipientScript,destination,amount,
        sats,fee:total-sats,change:0n,total,feeRate,createdAt:Date.now()};
    }
  }
  return null;
}
export async function btcFetch(path,{method='GET',body,signal}={}) {
  const response=await fetch(API+path,{method,body,signal,cache:'no-store',
    headers:method==='POST'?{'Content-Type':'text/plain'}:undefined});
  if(!response.ok)throw new Error('Bitcoin network provider unavailable');
  return method==='POST'?response.text():response.json();
}
export async function btcVerifyUtxo(utxo,sourceScript,signal) {
  const response=await fetch(API+'/tx/'+utxo.txid+'/hex',{signal,cache:'no-store'});
  if(!response.ok)throw new Error('Bitcoin UTXO verification failed');
  const hex=(await response.text()).trim();
  if(!/^[0-9a-fA-F]+$/.test(hex))throw new Error('Bitcoin transaction invalid');
  const tx=bitcoin.Transaction.fromHex(hex);
  const out=tx.outs[utxo.vout];
  if(tx.getId().toLowerCase()!==utxo.txid.toLowerCase()||!out||
     BigInt(out.value)!==utxo.value||
     !Buffer.from(out.script).equals(sourceScript))
     throw new Error('Bitcoin UTXO ownership mismatch');
}
export async function btcSignAndBroadcast(plan,{address,pubkey,signRawHash,signal}) {
  if(Date.now()-plan.createdAt>BTC_REVIEW_MAX_AGE_MS)throw Error('Fee review expired');
  if(!btcOwnerScript(address,pubkey)?.equals(plan.sourceScript))throw Error('Wallet mismatch');
  for(const utxo of plan.utxos)await btcVerifyUtxo(utxo,plan.sourceScript,signal);
  if(signal?.aborted)throw Error('Review cancelled');
  const psbt=new bitcoin.Psbt({network:bitcoin.networks.bitcoin});
  for(const u of plan.utxos)psbt.addInput({
    hash:u.txid,index:u.vout,sequence:0xfffffffd,
    witnessUtxo:{script:plan.sourceScript,value:Number(u.value)}
  });
  psbt.addOutput({script:plan.recipientScript,value:Number(plan.sats)});
  if(plan.change>0n)psbt.addOutput({script:plan.sourceScript,value:Number(plan.change)});
  const walletSigner={publicKey:pubkey,sign:async hash=>{
    if(signal?.aborted)throw Error('Review cancelled');
    const signed=await signRawHash({address,chainType:'bitcoin-segwit',
      hash:'0x'+Buffer.from(hash).toString('hex')});
    const text=String(signed?.signature||'').replace(/^0x/,'');
    // Privy signatures may include a single recovery byte.
    if(!/^[0-9a-fA-F]{128}(?:[0-9a-fA-F]{2})?$/.test(text))
      throw Error('Unexpected Privy signature format');
    const bytes=Buffer.from(text.slice(0,128),'hex');
    if(!secp256k1.verify(bytes,Buffer.from(hash),pubkey))
      throw Error('Privy signature did not verify');
    return bytes;
  }};
  for(let i=0;i<plan.utxos.length;i++)await psbt.signInputAsync(i,walletSigner);
  if(!psbt.validateSignaturesOfAllInputs((key,hash,sig)=>
     secp256k1.verify(Buffer.from(sig),Buffer.from(hash),Buffer.from(key))))
    throw Error('Bitcoin signatures invalid');
  psbt.finalizeAllInputs();
  const tx=psbt.extractTransaction();
  const size=BigInt(tx.virtualSize());
  const actual=plan.fee;
  if(actual<size*BigInt(plan.feeRate))throw Error('Network fee below reviewed rate');
  if(signal?.aborted)throw Error('Review cancelled');
  const txid=tx.getId();
  const broadcast=(await btcFetch('/tx',{method:'POST',body:tx.toHex(),signal})).trim();
  if(broadcast.toLowerCase()!==txid.toLowerCase())throw Error('Broadcast result not verified');
  return txid;
}
