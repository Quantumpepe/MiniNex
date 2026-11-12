// Input gate for manual swap quote PREVIEWS only. No wallet operations.
export const MANUAL_SWAP_ASSETS = Object.freeze([
  'BTC','ETH','BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI','ADA','USDC','USDT',
]);
const STABLES = new Set(['USDC','USDT']);
const RE_AMOUNT = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

export function manualSwapQuoteRequest(from, to, amount) {
  if (!MANUAL_SWAP_ASSETS.includes(from) || !MANUAL_SWAP_ASSETS.includes(to) || from === to) return null;
  const coin = STABLES.has(from) ? to : from;
  const stable = STABLES.has(from) ? from : to;
  if (!['BTC','ETH'].includes(coin) || !STABLES.has(stable)) return null;
  const side = from === stable ? 'BUY' : 'SELL';
  const decimals = from === 'BTC' ? 8 : from === 'ETH' ? 18 : 6;
  if (typeof amount !== 'string' || amount.length > 48 ||
      !RE_AMOUNT.test(amount) || !/^[0-9]+$/.test(amount.replace('.','')) ||
      amount.split('.')[1]?.length > decimals || !Number.isFinite(Number(amount)) ||
      Number(amount) <= 0) return null;
  return {
    path: coin === 'ETH' ? '/api/live/eth/quote' : '/api/live/btc/quote',
    body: {side,stable,amount}, coin, from, to,
  };
}

export function validManualSwapQuote(data, request) {
  if (!data || !request || data.asset !== request.coin ||
      data.side !== request.body.side ||
      data.execution_enabled !== false ||
      typeof data.estimated_output_amount !== 'string' &&
      typeof data.expected_output_amount !== 'string') return false;
  const isEth=request.coin==='ETH';
  if (isEth && (data.mode!=='LIVE_QUOTE_ONLY' ||
      data.signing_authorized!==false || data.transaction_submitted!==false ||
      data.input_asset!==request.from || data.output_asset!==request.to ||
      data.input_amount!==request.body.amount)) return false;
  if (!isEth && (data.mode!=='BTC_CROSSCHAIN_INDICATIVE_ONLY' ||
      data.can_submit!==false || data.transaction_built!==false ||
      data.minimum_received_guaranteed!==false ||
      data.source_asset!==request.from || data.destination_asset!==request.to ||
      data.input_amount!==request.body.amount)) return false;
  const result=isEth?data.expected_output_amount:data.estimated_output_amount;
  return typeof result==='string' && /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(result) &&
    Number.isFinite(Number(result)) && Number(result)>0;
}


// No polling, network requests or background updates: one local expiry timer
// is enough for a displayed read-only quote. Uncertain/old timestamps fail
// closed. BTC quotes are only indicative and can never be executed.
export function manualSwapQuoteExpiryMs(data, request, nowMs = Date.now()) {
  if (!validManualSwapQuote(data, request) ||
      !Number.isFinite(nowMs) || nowMs <= 0) return null;

  const eth = request.coin === 'ETH';
  const timestamp = eth ? data.quote_expires_at : data.quote_checked_at;
  if (!Number.isSafeInteger(timestamp) || timestamp <= 0) return null;
  const stampMs = timestamp * 1000;
  if (!Number.isSafeInteger(stampMs)) return null;

  // An ETH quote is very short-lived (backend: 15 seconds), never accept
  // timestamps much farther in the future. This also fails closed on
  // gross device/server clock mismatch.
  if (eth) {
    return stampMs > nowMs && stampMs - nowMs <= 60_000 ? stampMs : null;
  }
  // BTC cross-chain response has only quote_checked_at, not a guaranteed
  // expiry. UI uses an intentionally short 30-second preview window.
  if (stampMs - nowMs > 5_000) return null;
  const expiry = stampMs + 30_000;
  return expiry > nowMs ? expiry : null;
}
