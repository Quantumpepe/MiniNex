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

const validPositiveQuoteAmount=value=>
  typeof value==='string' && /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value) &&
  Number.isFinite(Number(value)) && Number(value)>0;

// Read-only quote responses must match both the asset and the physical
// settlement network. Unrecognized fields/providers fail closed; a plausible
// number alone is not a trusted quote and NEVER authorizes execution.
export function validManualSwapQuote(data, request) {
  if (!data || !request || data.asset !== request.coin ||
      data.side !== request.body.side || data.execution_enabled!==false ||
      !validPositiveQuoteAmount(
        request.coin==='ETH'?data.expected_output_amount:data.estimated_output_amount
      )) return false;

  if (request.coin==='ETH') {
    if (data.mode!=='LIVE_QUOTE_ONLY' || data.chain_id!==1 ||
        data.route!=='UNISWAP_V3_CLASSIC' ||
        data.signing_authorized!==false || data.transaction_submitted!==false ||
        data.input_asset!==request.from || data.output_asset!==request.to ||
        data.input_amount!==request.body.amount ||
        typeof data.slippage_tolerance_pct!=='number' ||
        !Number.isFinite(data.slippage_tolerance_pct) ||
        data.slippage_tolerance_pct<0 || data.slippage_tolerance_pct>0.5 ||
        !validPositiveQuoteAmount(data.minimum_output_amount)) return false;
    const expected=Number(data.expected_output_amount);
    const minimum=Number(data.minimum_output_amount);
    return minimum<=expected;
  }

  // A native BTC <-> ERC-20 stablecoin route crosses two different chains;
  // never label an address/quote as native stablecoin on Bitcoin.
  const expectedNetwork=request.body.side==='BUY'?'bitcoin-mainnet':'ethereum-mainnet';
  return data.mode==='BTC_CROSSCHAIN_INDICATIVE_ONLY' &&
    data.destination_network===expectedNetwork &&
    data.route_provider==='THORCHAIN_PUBLIC_INDICATIVE' &&
    data.source_asset===request.from && data.destination_asset===request.to &&
    data.input_amount===request.body.amount &&
    data.can_submit===false && data.transaction_built===false &&
    data.automated_signing_enabled===false &&
    data.deposit_address_provided===false &&
    data.minimum_received_guaranteed===false &&
    data.fee_breakdown_verified===false &&
    Number.isInteger(data.quoted_price_impact_bps) &&
    data.quoted_price_impact_bps>=0 && data.quoted_price_impact_bps<=500;
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


// This only validates a read-only, unsigned Uniswap fee preflight. It NEVER
// grants transaction authority. BigInt ensures exact max-fee arithmetic
// without float rounding, EVM SDK imports, polling or a second quote.
function decimalToBaseUnits(raw, decimals) {
  if (typeof raw!=='string'||raw.length>40||
      !/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(raw)) return null;
  const [whole,fraction='']=raw.split('.');
  if(fraction.length>decimals)return null;
  try {return BigInt(whole)*10n**BigInt(decimals)+
    BigInt((fraction.padEnd(decimals,'0'))||'0');}
  catch{return null;}
}

export function validEthSwapFeeReview(data,request) {
  if(!data||!request||request.coin!=='ETH'||!['BUY','SELL'].includes(request.body.side)||
      data.mode!=='LIVE_ETH_SWAP_PLAN_ONLY'||data.asset!=='ETH'||data.chain_id!==1||
      data.side!==request.body.side||
      data.input_asset!==request.from||data.output_asset!==request.to||
      data.input_amount!==request.body.amount||
      data.execution_enabled!==false||data.automatic_signing_enabled!==false||
      data.can_submit!==false||
      !validPositiveQuoteAmount(data.expected_output_amount)||
      !validPositiveQuoteAmount(data.minimum_output_amount)||
      Number(data.minimum_output_amount)>Number(data.expected_output_amount)||
      typeof data.erc20_approval_required!=='boolean'||
      typeof data.permit_signature_required!=='boolean')return false;

  if(data.state==='APPROVAL_OR_PERMIT_REQUIRED')
    return data.transaction_built===false &&
      (data.erc20_approval_required||data.permit_signature_required) &&
      !('safe_to_sign' in data) && !('max_possible_gas_eth' in data);

  if(data.state!=='UNSIGNED_TX_REVIEW_REQUIRED'||
      data.transaction_built!==true||data.safe_to_sign!==false||
      data.router_commands_decoded!==false||
      data.calldata_security_review_complete!==false||
      data.tx_outer_fields_verified!==true||
      data.erc20_approval_required!==false||data.permit_signature_required!==false||
      data.router?.toLowerCase()!=='0x23617e59a5925b2a4bf75d73ff6711cd0b29de85'||
      data.router_version!=='2.1.2'||
      !Number.isSafeInteger(data.max_gas_limit)||data.max_gas_limit<=0||
      data.max_gas_limit>2_000_000) return false;

  // Ignore claims of fees unless limit x max EIP-1559 price exactly matches
  // the stated worst-case ETH liability and the backend's 0.004 ETH cap.
  const feeWei=decimalToBaseUnits(data.max_fee_per_gas_gwei,9);
  const liabilityWei=decimalToBaseUnits(data.max_possible_gas_eth,18);
  return feeWei!==null&&feeWei>0n&&feeWei<=300_000_000_000n&&
    liabilityWei!==null&&liabilityWei>0n&&liabilityWei<=4_000_000_000_000_000n&&
    BigInt(data.max_gas_limit)*feeWei===liabilityWei &&
    !('calldata' in data) && !('data' in data) && !('signature' in data);
}
