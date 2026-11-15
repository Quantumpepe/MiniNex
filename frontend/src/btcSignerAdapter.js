// Privy Bitcoin signer boundary, intentionally NOT imported by any MiniNex
// screen. The working 423 UI must remain intact until a device test passes.
//
// This layer does not assume a specific Privy React hook or RPC call signature.
// A supported SDK adapter must be supplied explicitly after matching its
// installed version against the actual Privy documentation/type declarations.
// No live signature, broadcast, delegation or wallet creation happens here.

export const BTC_SIGNER_STATES=Object.freeze({
  ready:'ready',missing:'wallet_missing',unsupported:'unsupported_signer',
  mismatch:'wallet_mismatch'
});
const MAINNET_P2WPKH=/^bc1q[ac-hj-np-z02-9]{38}$/;
const COMPRESSED_PUBLIC_KEY=/^(02|03)[0-9a-f]{64}$/i;
const SHA256=/^[a-f0-9]{64}$/i;
const ECDSA_COMPACT=/^[a-f0-9]{128}$/i;
const confirmedIntents=new WeakSet();
const usedIntents=new WeakSet();

export function bitcoinSignerPreflight({wallet,address,publicKey,approve}={}){
  // A linked BTC address is necessary but never sufficient for signing.
  if(typeof wallet?.address!=='string'||!MAINNET_P2WPKH.test(wallet.address))
    return {state:BTC_SIGNER_STATES.missing};
  if(wallet.chainType!=='bitcoin-segwit'||wallet.address!==address)
    return {state:BTC_SIGNER_STATES.mismatch};
  if(typeof publicKey!=='string'||!COMPRESSED_PUBLIC_KEY.test(publicKey))
    return {state:BTC_SIGNER_STATES.unsupported};
  if(typeof approve!=='function')
    return {state:BTC_SIGNER_STATES.unsupported};
  return {state:BTC_SIGNER_STATES.ready,address,publicKey:publicKey.toLowerCase()};
}
export function bitcoinSigningIntent({walletId,address,hashHex,reviewId,confirmedByUser}={}){
  if(typeof walletId!=='string'||!walletId.trim()||
     typeof address!=='string'||!MAINNET_P2WPKH.test(address)||
     typeof hashHex!=='string'||!SHA256.test(hashHex)||
     typeof reviewId!=='string'||!reviewId.trim()||
     confirmedByUser!==true)return null;
  const intent=Object.freeze({
    chainType:'bitcoin-segwit',walletId,address,
    hashHex:hashHex.toLowerCase(),reviewId
  });
  confirmedIntents.add(intent);
  return intent;
}
export function parseBitcoinRawSignature(value){
  // Privy SDK response mapping must be explicit, not guessed from arbitrary
  // envelopes or numeric/string coercions. Allow just verified compact hex.
  if(typeof value!=='string')return null;
  const hex=value.startsWith('0x')?value.slice(2):value;
  return ECDSA_COMPACT.test(hex)?hex.toLowerCase():null;
}
export async function requestReviewedBitcoinSignature({
  preflight,intent,approve,verifySignature
}={}){
  if(preflight?.state!==BTC_SIGNER_STATES.ready||
     !intent||!confirmedIntents.has(intent)||usedIntents.has(intent)||intent.address!==preflight.address||
     typeof approve!=='function'||typeof verifySignature!=='function')
    return {ok:false,reason:'signer_not_ready'};
  // Only invoke the injected, SDK-version-verified adapter after the caller
  // completed an explicit confirmation. The adapter cannot broadcast here.
  // Consume before the asynchronous call, preventing duplicate signatures
  // from double taps or simultaneous callbacks.
  usedIntents.add(intent);
  const result=await approve(intent);
  const signature=parseBitcoinRawSignature(result);
  if(!signature)return {ok:false,reason:'invalid_signature_format'};
  const verified=await verifySignature({signature,hashHex:intent.hashHex,
    publicKey:preflight.publicKey,address:intent.address});
  if(verified!==true)return {ok:false,reason:'invalid_signature'};
  return {ok:true,signature};
}
