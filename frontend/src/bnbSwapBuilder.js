// BNB (PancakeSwap) swap transaction builder.
// Builds calldata for swapExactETHForTokens without external API.

const PANCAKE_ROUTER = '0x10ED43C718714eb63d5aA57B78B54704E256024E';
const WBNB = '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c';
const USDT_BSC = '0x55d398326f99059fF775485246999027B3197955';
const USDC_BSC = '0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d';

// Function selectors
const SWAP_EXACT_ETH_FOR_TOKENS = '0x7ff36ab5';
const SWAP_EXACT_TOKENS_FOR_ETH = '0x18cbafe5';
const SWAP_EXACT_TOKENS_FOR_TOKENS = '0x38ed1739';

function padAddress(addr) {
  return addr.toLowerCase().replace('0x', '').padStart(64, '0');
}

function padUint(value) {
  return BigInt(value).toString(16).padStart(64, '0');
}

function encodePath(path) {
  const offset = padUint(128); // path starts at byte 128
  const length = padUint(path.length);
  const addresses = path.map(padAddress).join('');
  return offset + length + addresses;
}

export function buildBnbSwapCalldata({
  side, // 'BUY' or 'SELL'
  amountIn,
  amountOutMin,
  tokenOut, // for BUY: USDT/USDC address
  tokenIn,  // for SELL: USDT/USDC address
  recipient,
  deadline,
}) {
  const to = padAddress(recipient);
  const deadlineHex = padUint(deadline);
  const minOut = padUint(amountOutMin);

  if (side === 'BUY') {
    // BNB -> Token: swapExactETHForTokens
    const path = [WBNB, tokenOut];
    const pathEncoded = encodePath(path);
    return SWAP_EXACT_ETH_FOR_TOKENS + minOut + pathEncoded + to + deadlineHex;
  } else {
    // Token -> BNB: swapExactTokensForETH
    const path = [tokenIn, WBNB];
    const pathEncoded = encodePath(path);
    const amountInHex = padUint(amountIn);
    return SWAP_EXACT_TOKENS_FOR_ETH + amountInHex + minOut + pathEncoded + to + deadlineHex;
  }
}

export function buildBnbSwapTransaction({
  side,
  amountIn,
  amountOutMin,
  stable, // 'USDT' or 'USDC'
  recipient,
  deadline,
  gasLimit = 300000,
  gasPrice = '5000000000', // 5 gwei
}) {
  const token = stable === 'USDT' ? USDT_BSC : USDC_BSC;
  const calldata = buildBnbSwapCalldata({
    side,
    amountIn,
    amountOutMin,
    tokenOut: token,
    tokenIn: token,
    recipient,
    deadline,
  });

  return {
    to: PANCAKE_ROUTER,
    data: '0x' + calldata,
    value: side === 'BUY' ? '0x' + BigInt(amountIn).toString(16) : '0x0',
    gasLimit: '0x' + gasLimit.toString(16),
    gasPrice,
    chainId: 56,
  };
}
