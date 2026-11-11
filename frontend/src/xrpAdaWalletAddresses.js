// XRP Ledger / Cardano read-only native wallet address verification.
// Never derive addresses from ETH/SOL keys, accept an address typed by a user,
// generate secrets, or confuse a verified public address with custody readiness.
// The only trusted source is the authenticated user's Privy linked wallet.
const XRPL_ALPHABET = 'rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz';
const BECH32_ALPHABET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l';

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

function polymod(data) {
  let check = 1;
  for (const v of data) {
    const high = check >>> 25;
    check = ((check & 0x1ffffff) << 5) ^ v;
    for (const [index,generator] of [
      [0,0x3b6a57b2],[1,0x26508e6d],[2,0x1ea119fa],
      [3,0x3d4233dd],[4,0x2a1462b3],
    ]) if ((high >>> index)&1) check ^= generator;
  }
  return check >>> 0;
}

function bech32Decode(value) {
  if (typeof value !== 'string' || value.length > 140 || !/^addr1[02-9ac-hj-np-z]+$/.test(value)) return null;
  const separator = value.lastIndexOf('1');
  const prefix = value.slice(0, separator);
  if (prefix !== 'addr') return null; // never accept testnet, rewards or stake addresses
  const symbols = [...value.slice(separator+1)].map(char=>BECH32_ALPHABET.indexOf(char));
  if (symbols.some(s=>s<0) || symbols.length<7) return null;
  const expanded = [...prefix].map(c=>c.charCodeAt(0)>>>5).concat([0],
    [...prefix].map(c=>c.charCodeAt(0)&31),symbols);
  if (polymod(expanded)!==1) return null;
  const payload = symbols.slice(0,-6);
  const bytes=[];
  let accumulator=0, bits=0;
  for (const symbol of payload) {
    accumulator=(accumulator<<5)|symbol;
    bits+=5;
    while (bits>=8) {
      bits-=8;
      bytes.push((accumulator>>>bits)&255);
      accumulator &= (1<<bits)-1;
    }
  }
  if (bits>=5 || (accumulator & ((1<<bits)-1))!==0) return null;
  return bytes;
}

export function validCardanoMainnetPaymentAddress(address) {
  const bytes=bech32Decode(address);
  if (!bytes?.length || (bytes[0]&15)!==1) return false;
  const type=bytes[0]>>4;
  // Single-key base (payment+stake) or key-based enterprise (payment only).
  // Reject script, reward, pointer, testnet, Byron and malformed length.
  return (type===0 && bytes.length===57) || (type===6 && bytes.length===29);
}

export async function linkedVerifiedNativeAddress(user,chain) {
  if (!user?.id || !Array.isArray(user.linkedAccounts) ||
      (chain!=='xrpl' && chain!=='cardano')) return null;
  for (const account of user.linkedAccounts) {
    if (account?.type!=='wallet'||account.chainType!==chain ||
        account.walletClientType!=='privy') continue;
    const address=account.address;
    const valid=chain==='xrpl'
      ? await validXrplMainnetAddress(address)
      : validCardanoMainnetPaymentAddress(address);
    if (valid) return address;
  }
  return null;
}
