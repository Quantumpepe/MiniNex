// Offline-only Bitcoin transfer preflight. Not imported by MiniNex App or Wallet.
// This module never signs, broadcasts or reads private wallet credentials.
// Keep the verified Android OTA 423 runtime untouched during BTC development.
const SATS=100000000n;
const DUST=546n;
export const BTC_MAINNET='bitcoin-mainnet';

export function btcToSats(text) {
  if(typeof text!=='string'||!/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?$/.test(text))
    return null;
  const [whole,part='']=text.split('.');
  const sats=BigInt(whole)*SATS+BigInt(part.padEnd(8,'0'));
  return sats>0n?sats:null;
}
export function btcFromSats(value) {
  if(typeof value!=='bigint'||value<0n)return null;
  const whole=value/SATS;
  const decimals=(value%SATS).toString().padStart(8,'0').replace(/0+$/,'');
  return whole.toString()+(decimals?'.'+decimals:'');
}
export function btcConfirmedUtxos(raw) {
  if(!Array.isArray(raw))return null;
  const out=[],seen=new Set();
  for(const record of raw){
    if(record?.status?.confirmed!==true)continue;
    const {txid,vout,value}=record;
    if(typeof txid!=='string'||!/^[0-9a-f]{64}$/i.test(txid)||
       !Number.isSafeInteger(vout)||vout<0||
       !Number.isSafeInteger(value)||value<=0)return null;
    const id=txid.toLowerCase()+':'+vout;
    if(seen.has(id))return null;
    seen.add(id);
    out.push({txid:txid.toLowerCase(),vout,value:BigInt(value)});
  }
  return out;
}
export function btcFeeRate(fees) {
  const rate=fees?.halfHourFee;
  return Number.isSafeInteger(rate)&&rate>0?rate:null;
}
export function btcReviewDraft({amount,utxos,feeRate,recipientOutputBytes=31,changeOutputBytes=31}){
  const sats=btcToSats(amount);
  if(!sats||!Array.isArray(utxos)||!utxos.length||
     !Number.isSafeInteger(feeRate)||feeRate<=0||
     !Number.isSafeInteger(recipientOutputBytes)||recipientOutputBytes<=0||
     !Number.isSafeInteger(changeOutputBytes)||changeOutputBytes<=0)return null;
  const ordered=[...utxos].sort((a,b)=>a.value>b.value?-1:a.value<b.value?1:0);
  const selected=[];
  let total=0n;
  for(const utxo of ordered){
    selected.push(utxo);
    total+=utxo.value;
    // Conservative P2WPKH sizing. Final PSBT signer must recheck the fee
    // against the actual transaction vsize before any broadcast.
    const estimate=11+69*selected.length+recipientOutputBytes;
    const withChange=BigInt(estimate+changeOutputBytes)*BigInt(feeRate);
    const noChange=BigInt(estimate)*BigInt(feeRate);
    if(total>=sats+withChange+DUST){
      return Object.freeze({selected,total,sats,
        estimatedFee:withChange,change:total-sats-withChange,feeRate,
        requiresFinalFeeVerification:true});
    }
    const remainder=total-sats-noChange;
    if(remainder>=0n&&remainder<DUST)
      return Object.freeze({selected,total,sats,
        estimatedFee:total-sats,change:0n,feeRate,
        requiresFinalFeeVerification:true});
  }
  return null;
}
