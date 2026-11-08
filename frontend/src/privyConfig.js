// Only the public Privy application ID belongs in client-side code.
// An app secret and any trading/delegation signer must stay on a secured server.
export const PRIVY_APP_ID = String(import.meta.env?.VITE_PRIVY_APP_ID || '').trim();

export const PRIVY_PROVIDER_CONFIG = {
  loginMethods: ['email'],
  appearance: {
    theme: 'dark',
    accentColor: '#51e4b0',
    walletChainType: 'ethereum-only',
    showWalletLoginFirst: false,
  },
  embeddedWallets: {
    ethereum: {createOnLogin: 'all-users'},
  },
};
