// Native Dogecoin mainnet P2PKH address validation ONLY.
// This module does not generate keys, authorize deposits or sign transactions.
// DOGE uses a different UTXO chain than Bitcoin: BTC/ETH/SOL keys and wallet
// addresses must NEVER be mapped to DOGE. P2SH is deliberately unsupported
// until a native signer provides spendability proofs.
const ALPHABET='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
const DOGE_P2PKH_VERSION=0x1e;

function decodeBase58(value) {
  let number=0n;
  for(const char of value) {
    const position=ALPHABET.indexOf(char);
    if(position<0)return null;
    number=number*58n+BigInt(position);
  }
  const zeros=(value.match(/^1+/)?.[0]||'').length;
  let hex=number===0n?'':number.toString(16);
  if(hex.length%2)hex='0'+hex;
  const result=new Uint8Array(zeros+hex.length/2);
  for(let i=0;i<hex.length;i+=2)
    result[zeros+i/2]=Number.parseInt(hex.slice(i,i+2),16);
  return result;
}

async function hash256(payload) {
  if(!globalThis.crypto?.subtle)return null;
  const first=await globalThis.crypto.subtle.digest('SHA-256',payload);
  return new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256',first));
}

export async function validNativeDogeMainnetAddress(address) {
  if(typeof address!=='string'||!/^[D][1-9A-HJ-NP-Za-km-z]{25,34}$/.test(address))
    return false;
  const decoded=decodeBase58(address);
  if(decoded?.length!==25 || decoded[0]!==DOGE_P2PKH_VERSION)return false;
  try {
    const checksum=await hash256(decoded.slice(0,21));
    return !!checksum && [0,1,2,3].every(i=>decoded[21+i]===checksum[i]);
  }catch {return false;}
}

// Even a correctly checksummed address is insufficient: it must originate
// from the CURRENT authenticated user and Privy's own linked-wallet record.
// Privy does not presently advertise an in-app DOGE creator, so this remains
// null until equivalent independently verified custody is integrated.
export async function linkedVerifiedDogecoinAddress(user) {
  if(typeof user?.id!=='string'||!user.id.startsWith('did:privy:')||
    !Array.isArray(user.linkedAccounts))return null;
  for(const account of user.linkedAccounts) {
    if(account?.type!=='wallet'||account.chainType!=='dogecoin'||
       account.walletClientType!=='privy')continue;
    if(await validNativeDogeMainnetAddress(account.address))return account.address;
  }
  return null;
}
