// Generic EVM swap builder for all chains.
// Same logic, different router/token addresses.

import {EVM_CHAINS} from './evmSwapRules.js';

const SWAP_EXACT_ETH_FOR_TOKENS = '0x7ff36ab5';
const SWAP_EXACT_TOKENS_FOR_ETH = '0x18cbafe5';

function padAddress(addr) {
  return addr.toLowerCase().replace('0x', '').padStart(64, '0');
}

function padUint(value) {
  return BigInt(value).toString(16).padStart(64, '0');
}

function encodePath(path) {
  const offset = padUint(128);
  const length = padUint(path.length);
  const addresses = path.map(padAddress).join('');
  return offset + length + addresses;
}

export function buildEvmSwapCalldata(chain, {side, amountIn, amountOutMin, stable, recipient, deadline}) {
  const config = EVM_CHAINS[chain];
  if (!config) throw Error('Unsupported chain');
  const token = config.stables[stable];
  if (!token) throw Error('Unsupported stable');
  const to = padAddress(recipient);
  const deadlineHex = padUint(deadline);
  const minOut = padUint(amountOutMin);

  if (side === 'BUY') {
    const path = [config.wrapped, token];
    const pathEncoded = encodePath(path);
    return SWAP_EXACT_ETH_FOR_TOKENS + minOut + pathEncoded + to + deadlineHex;
  } else {
    const path = [token, config.wrapped];
    const pathEncoded = encodePath(path);
    const amountInHex = padUint(amountIn);
    return SWAP_EXACT_TOKENS_FOR_ETH + amountInHex + minOut + pathEncoded + to + deadlineHex;
  }
}

export function buildEvmSwapTransaction(chain, {side, amountIn, amountOutMin, stable, recipient, deadline, gasLimit = 300000, gasPrice = '5000000000'}) {
  const config = EVM_CHAINS[chain];
  if (!config) throw Error('Unsupported chain');
  const calldata = buildEvmSwapCalldata(chain, {side, amountIn, amountOutMin, stable, recipient, deadline});
  return {
    to: config.router,
    data: '0x' + calldata,
    value: side === 'BUY' ? '0x' + BigInt(amountIn).toString(16) : '0x0',
    gasLimit: '0x' + gasLimit.toString(16),
    gasPrice,
    chainId: config.chainId,
  };
}
