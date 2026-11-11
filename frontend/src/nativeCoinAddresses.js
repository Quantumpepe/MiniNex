// Public-address verification for Privy-owned additional native wallets.
// No keys, wallet signing, deposits, balances or third-party address imports.
const EVM_RE = /^0x[0-9a-fA-F]{40}$/;
const SOL_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const TRX_RE = /^T[1-9A-HJ-NP-Za-km-z]{33}$/;

const BASE58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
function base58DecodedLength(value) {
  let number = 0n;
  for (const character of value) {
    const digit = BASE58.indexOf(character);
    if (digit < 0) return -1;
    number = number * 58n + BigInt(digit);
  }
  const leadingZeroBytes = /^1*/.exec(value)[0].length;
  const nonzeroBytes = number === 0n ? 0 : Math.ceil(number.toString(16).length / 2);
  return leadingZeroBytes + nonzeroBytes;
}


export function validEvmAddress(address) {
  return typeof address === 'string' && EVM_RE.test(address);
}

export function validNativePreviewAddress(chainType, address) {
  if (typeof address !== 'string') return false;
  if (chainType === 'solana') return SOL_RE.test(address) && base58DecodedLength(address) === 32;
  if (chainType === 'tron') return TRX_RE.test(address);
  return false; // XRP and Cardano are NOT interchangeable with EVM or Solana.
}

// Never borrow an external, another chain's or another user's linked wallet.
export function findPrivyNativeWallet(user, chainType) {
  if (!user?.id || !Array.isArray(user.linkedAccounts)) return null;
  return user.linkedAccounts.find(account =>
    account?.type === 'wallet' &&
    // Tier-2 wallet records can omit walletClientType; reject explicitly external providers.
    (account.walletClientType == null || account.walletClientType === 'privy') &&
    account?.chainType === chainType &&
    validNativePreviewAddress(chainType, account.address)
  ) || null;
}

// EVM-compatible chains use the same key/address on DIFFERENT networks.
// This does not create a second wallet or imply that any deposit is enabled.
export function evmNetworkAddress(network, embeddedAddress) {
  if (!['bsc-mainnet','polygon-pos-mainnet','avalanche-c-mainnet'].includes(network)) return null;
  return validEvmAddress(embeddedAddress) ? embeddedAddress : null;
}
