// XRP Ledger read-only native wallet address verification.
// Never derive addresses from ETH/SOL keys, accept an address typed by a user,
// generate secrets, or confuse a verified public address with custody readiness.
// The only trusted source is the authenticated user's Privy linked wallet.
const XRPL_ALPHABET = 'rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz';

function decodeBase58(value, alphabet) {
  let numeric = 0n;
  for (const char of value) {
    const index = alphabet.indexOf(char);
    if (index < 0) return null;
    numeric = numeric * 58n + BigInt(index);
  }
  const leadingZeros = (value.match(new RegExp('^'+alphabet[0]+'*'))?.[0] || '').length;
  const hex = numeric === 0n ? '' : numeric.toString(16).padStart(Math.ceil(numeric.toString(16).length / 2) * 2,'0');
  const result = new Uint8Array(leadingZeros + hex.length/2);
  for (let i = 0; i < hex.length; i += 2) result[leadingZeros + i/2] = Number.parseInt(hex.slice(i,i+2),16);
  return result;
}

async function sha256(bytes) {
  if (!globalThis.crypto?.subtle) return null;
  return new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256',bytes));
}

// Classic accounts only (r...), with Ripple's unique alphabet and checksum.
// X-addresses include tags and are intentionally excluded from this wallet.
export async function validXrplMainnetAddress(address) {
  if (typeof address !== 'string' || !/^r[A-Za-z0-9]{24,34}$/.test(address)) return false;
  const bytes = decodeBase58(address,XRPL_ALPHABET);
  if (bytes?.length !== 25 || bytes[0] !== 0) return false;
  try {
    const first = await sha256(bytes.slice(0,21));
    if (!first) return false;
    const second = await sha256(first);
    return !!second && [0,1,2,3].every(i=>second[i]===bytes[21+i]);
  } catch { return false; }
}

export async function linkedVerifiedNativeAddress(user,chain) {
  if (!user?.id || !Array.isArray(user.linkedAccounts) || chain!=='xrpl') return null;
  for (const account of user.linkedAccounts) {
    if (account?.type!=='wallet'||account.chainType!==chain ||
        account.walletClientType!=='privy') continue;
    const address=account.address;
    if (await validXrplMainnetAddress(address)) return address;
  }
  return null;
}

/**
 * Validate a native XRPL wallet just returned by Privy's authenticated
 * user-scoped creation hook. A base58-like string alone never proves custody.
 */
export async function validatedCreatedXrplAddress(response,userId) {
  if(typeof userId!=='string'||!userId.startsWith('did:privy:')||
     response?.user?.id!==userId||response?.wallet?.chainType!=='xrpl')
    return null;
  const address=response.wallet.address;
  return await validXrplMainnetAddress(address)?address:null;
}
