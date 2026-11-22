// Multi-chain EVM swap routers for MiniNex.
// Each chain has its own DEX router and wrapped native token.

export const EVM_CHAINS = Object.freeze({
  ETH: Object.freeze({
    chainId: 1,
    name: 'Ethereum',
    router: '0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45', // Uniswap Router02
    wrapped: '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2', // WETH
    stables: Object.freeze({
      USDC: '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48',
      USDT: '0xdac17f958d2ee523a2206206994597c13d831ec7',
    }),
  }),
  BNB: Object.freeze({
    chainId: 56,
    name: 'BNB Smart Chain',
    router: '0x10ED43C718714eb63d5aA57B78B54704E256024E', // PancakeSwap Router
    wrapped: '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c', // WBNB
    stables: Object.freeze({
      USDT: '0x55d398326f99059fF775485246999027B3197955',
      USDC: '0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d',
    }),
  }),
  POL: Object.freeze({
    chainId: 137,
    name: 'Polygon',
    router: '0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff', // QuickSwap Router
    wrapped: '0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270', // WMATIC
    stables: Object.freeze({
      USDC: '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174',
      USDT: '0xc2132D05D31c914a87C6611C10748AEb04B58e8F',
    }),
  }),
  AVAX: Object.freeze({
    chainId: 43114,
    name: 'Avalanche',
    router: '0x60aE616a2155Ee3d9A68541Ba4544862310933d4', // TraderJoe Router
    wrapped: '0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7', // WAVAX
    stables: Object.freeze({
      USDC: '0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E',
      USDT: '0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7',
    }),
  }),
  ARB: Object.freeze({
    chainId: 42161,
    name: 'Arbitrum',
    router: '0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45', // Uniswap Router02
    wrapped: '0x82aF49447D8a07e3bd95BD0d56f35241523fBab1', // WETH
    stables: Object.freeze({
      USDC: '0xaf88d065e77c8cC2239327C5EDb3A432268e5831',
      USDT: '0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9',
    }),
  }),
});

export function getChainConfig(symbol) {
  return EVM_CHAINS[symbol] || null;
}

export function getRouter(symbol) {
  return EVM_CHAINS[symbol]?.router || null;
}

export function getWrappedNative(symbol) {
  return EVM_CHAINS[symbol]?.wrapped || null;
}

export function getStables(symbol) {
  return EVM_CHAINS[symbol]?.stables || null;
}
