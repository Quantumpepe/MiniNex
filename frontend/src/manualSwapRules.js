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
  const legacy = ['BTC','ETH'].includes(coin) && STABLES.has(stable);
  // The real provider uses 1e8 precision. Native XRP/TRX/ADA and Ethereum
  // stables have only 6 decimal places; never round any entered amount.
  const decimals = legacy
    ? (from === 'BTC' ? 8 : from === 'ETH' ? 18 : 6)
    : (STABLES.has(from) || ['XRP','TRX','ADA'].includes(from) ? 6 : 8);
  if (typeof amount !== 'string' || amount.length > 48 ||
      !RE_AMOUNT.test(amount) || !/^[0-9]+$/.test(amount.replace('.','')) ||
      amount.split('.')[1]?.length > decimals || !Number.isFinite(Number(amount)) ||
      Number(amount) <= 0) return null;
  if (!legacy) return {
    path:'/api/wallet/swap/live-quote',
    body:{source:from,target:to,amount},
    coin:'MULTICHAIN',from,to,
  };
  const side = from === stable ? 'BUY' : 'SELL';
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
  if(request?.coin==='MULTICHAIN') {
    const network=asset=>STABLES.has(asset)
      ? 'ethereum-mainnet':coinBySymbol(asset)?.network;
    return !!data &&
      data.mode==='NATIVE_MULTICHAIN_REAL_MARKET_QUOTE_ONLY' &&
      data.source_asset===request.from &&
      data.destination_asset===request.to &&
      data.source_network===network(request.from) &&
      data.destination_network===network(request.to) &&
      data.input_amount===request.body.amount &&
      data.quote_provider==='THORCHAIN_PUBLIC_MCP' &&
      data.pool_route_quoted===true &&
      validPositiveQuoteAmount(data.estimated_output_amount) &&
      data.execution_enabled===false && data.can_submit===false &&
      data.transaction_built===false && data.signing_authorized===false &&
      data.deposit_address_provided===false &&
      data.fee_breakdown_verified===false &&
      data.minimum_received_guaranteed===false &&
      Number.isInteger(data.quoted_price_impact_bps) &&
      data.quoted_price_impact_bps>=0 &&
      data.quoted_price_impact_bps<=500 &&
      Number.isSafeInteger(data.quote_checked_at) &&
      (data.quote_expires_at===null ||
       (Number.isSafeInteger(data.quote_expires_at) &&
        data.quote_expires_at > data.quote_checked_at &&
        data.quote_expires_at <= data.quote_checked_at + 3600)) &&
      !('memo' in data) && !('inbound_address' in data);
  }
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


// On-demand Privy-linked wallet RECORD inspection only. It never confirms
// spend authority or permits a transfer; missing/ambiguous wallets fail closed.
export function validManualSwapWalletReadiness(data, from, to) {
  if (!MANUAL_SWAP_ASSETS.includes(from) || !MANUAL_SWAP_ASSETS.includes(to) ||
      from===to || !data || data.mode!=='MANUAL_SWAP_WALLET_READINESS_ONLY')
    return false;
  const network=asset=>STABLES.has(asset)
    ? 'ethereum-mainnet':coinBySymbol(asset)?.network;
  const src=network(from), dst=network(to);
  const states=['PRESENT','MISSING','AMBIGUOUS','INVALID_FORMAT'];
  const exactKeys=(object,keys)=>object && typeof object==='object' &&
    !Array.isArray(object) &&
    Object.keys(object).sort().join(',')===keys.slice().sort().join(',');
  return !!src && !!dst &&
    exactKeys(data.source,['asset','network','record_status']) &&
    exactKeys(data.destination,['asset','network','record_status']) &&
    data.source.asset===from && data.destination.asset===to &&
    data.source.network===src && data.destination.network===dst &&
    states.includes(data.source.record_status) &&
    states.includes(data.destination.record_status) &&
    data.route_class===(src===dst?'SAME_CHAIN':'CROSS_CHAIN') &&
    data.ada_in_progress===(from==='ADA'||to==='ADA') &&
    data.linked_wallet_records_checked===true &&
    data.ownership_proof_verified===false &&
    data.native_signer_verified===false &&
    data.network_fees_verified===false &&
    data.minimum_received_guaranteed===false &&
    data.refund_path_verified===false &&
    data.signing_authorized===false &&
    data.execution_ready===false && data.can_submit===false &&
    !('address' in data) && !('signing_payload' in data) &&
    !('inbound_address' in data) && !('memo' in data);
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
  // BTC and extended native cross-chain responses are only live snapshots.
  // BTC cross-chain response has only quote_checked_at, not a guaranteed
  // expiry. UI uses an intentionally short 30-second preview window.
  if (stampMs - nowMs > 5_000) return null;
  const snapshotExpiry = stampMs + 30_000;
  // A native quote with an earlier provider expiry must never outlive it.
  const providerExpiry = request.coin === 'MULTICHAIN'
    ? data.quote_expires_at : null;
  const expiry = providerExpiry === null ? snapshotExpiry
    : Math.min(snapshotExpiry, providerExpiry * 1000);
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


// The backend reports which NATIVE networks actually need connecting.
// This is route discovery only: it may not authorize signatures, swaps or
// pretend any missing ERC-20 stablecoin exists on a foreign blockchain.
import {coinBySymbol} from './coins.js';

export function validManualSwapRoute(data, from, to) {
  if (!MANUAL_SWAP_ASSETS.includes(from) || !MANUAL_SWAP_ASSETS.includes(to) ||
      from===to || !data || data.mode!=='MANUAL_SWAP_ROUTE_DISCOVERY_ONLY') return false;
  const expectedNetwork=asset=>STABLES.has(asset)
    ? 'ethereum-mainnet' : coinBySymbol(asset)?.network;
  const incoming=expectedNetwork(from), outgoing=expectedNetwork(to);
  const quote=manualSwapQuoteRequest(from,to,'1');
  return !!incoming && !!outgoing &&
    data.source?.symbol===from && data.destination?.symbol===to &&
    data.source.network===incoming && data.destination.network===outgoing &&
    data.route_class===(incoming===outgoing?'SAME_CHAIN':'CROSS_CHAIN') &&
    data.quote_preview_implemented===!!quote &&
    data.quote_asset===(quote?.coin||null) &&
    data.quote_endpoint===(quote?.path||null) &&
    data.execution_ready===false && data.signing_authorized===false &&
    data.transaction_built===false && data.can_submit===false &&
    data.network_fee_verified===false &&
    data.guaranteed_minimum_received===false &&
    Array.isArray(data.required_checks) && data.required_checks.length>=5 &&
    data.required_checks.every(item=>typeof item==='string'&&item.length>3);
}


// A REAL Ethereum receipt is not proof of swap output, and must never turn
// into client-side authorization to send/sign a new transaction.
export function validEthReceiptStatus(data, requestedHash) {
  if(!/^0x[a-fA-F0-9]{64}$/.test(requestedHash||'') ||
     !data || data.mode!=='ETH_MAINNET_RECEIPT_CHECK' ||
     data.network!=='ethereum-mainnet' ||
     data.tx_hash!==requestedHash.toLowerCase() ||
     data.execution_enabled!==false || data.can_submit!==false ||
     data.swap_outputs_verified!==false ||
     data.live_order_recorded!==false ||
     !Number.isSafeInteger(data.confirmations) ||
     data.confirmations<0 || data.confirmations>1000000) return false;
  const states={
    NOT_FOUND:[false,false,null],
    PENDING:[true,false,null],
    INCLUDED_SUCCESS:[true,false,true],
    INCLUDED_REVERTED:[true,false,false],
    FINALIZED_SUCCESS:[true,true,true],
    FINALIZED_REVERTED:[true,true,false],
  };
  const s=states[data.state];
  if(!s || data.transaction_seen!==s[0] ||
     data.transaction_finalized!==s[1] ||
     data.transaction_success!==s[2])return false;
  if(data.state==='NOT_FOUND'||data.state==='PENDING')
    return data.confirmations===0 && data.network_fee_eth===null;
  const gas=data.network_fee_eth;
  return typeof gas==='string' &&
    /^(?:0|[1-9]\d*)(?:\.\d{1,18})?$/.test(gas) &&
    Number.isFinite(Number(gas)) && Number(gas)>0 &&
    data.confirmations>=1;
}
