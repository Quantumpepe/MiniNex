// Version-specific boundary for @privy-io/react-auth/extended-chains
// SignRawHashInput {address, chainType, hash}; output {signature}.
// Unused by the MiniNex runtime. Calling this never broadcasts a Bitcoin tx.
import {parseBitcoinRawSignature} from './btcSignerAdapter.js';

const BTC_CHAIN='bitcoin-segwit';
const HASH=/^[a-fA-F0-9]{64}$/;

export function createPrivyBtcHashSigner(signRawHash) {
  if(typeof signRawHash!=='function')return null;
  return async function signReviewedBtcHash(intent) {
    if(!intent||intent.chainType!==BTC_CHAIN||
       typeof intent.address!=='string'||!/^bc1q[ac-hj-np-z02-9]{38}$/.test(intent.address)||
       typeof intent.hashHex!=='string'||!HASH.test(intent.hashHex))
      throw new Error('Invalid reviewed Bitcoin signing input');
    // These keys are defined by the installed Privy extended-chain SDK;
    // there is no walletId, tx bytes, or undocumented provider RPC method.
    const reply=await signRawHash({
      address:intent.address,
      chainType:BTC_CHAIN,
      hash:'0x'+intent.hashHex.toLowerCase()
    });
    const result=parseBitcoinRawSignature(reply?.signature);
    if(result===null)throw new Error('Privy returned an unsupported BTC signature');
    return result;
  };
}
