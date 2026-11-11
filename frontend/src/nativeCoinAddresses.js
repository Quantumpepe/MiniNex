// Public-address verification for Privy-owned additional native wallets.
// No keys, wallet signing, deposits, balances or third-party address imports.
const EVM_RE = /^0x[0-9a-fA-F]{40}$/;
const SOL_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const TRX_RE = /^T[1-9A-HJ-NP-Za-km-z]{33}$/;

export function validEvmAddress(address) {
  return typeof address === 'string' && EVM_RE.test(address);
}

export function validNativePreviewAddress(chainType, address) {
  if (typeof address !== 'string') return false;
  if (chainType === 'solana') return SOL_RE.test(address);
  if (chainType === 'tron') return TRX_RE.test(address);
  return false; // XRP and Cardano are NOT interchangeable with EVM or Solana.
}

// Never borrow an external, another chain's or another user's linked wallet.
export function findPrivyNativeWallet(user, chainType) {
  if (!user?.id || !Array.isArray(user.linkedAccounts)) return null;
  return user.linkedAccounts.find(account =>
    account?.type === 'wallet' &&
    account?.walletClientType === 'privy' &&
    account?.chainType === chainType &&
    validNativePreviewAddress(chainType, account.address)
  ) || null;
}

// EVM-compatible chains use the same key/address on DIFFERENT networks.
// This does not create a second wallet or imply that any deposit is enabled.
export function evmNetworkAddress(network, embeddedAddress) {
  if (network !== 'bsc-mainnet' && network !== 'polygon-pos-mainnet') return null;
  return validEvmAddress(embeddedAddress) ? embeddedAddress : null;
}
