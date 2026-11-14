// Verify ETH's two actual on-chain read-only prerequisites before any
// Privy policy adjustment: one V3 exactInputSingle quote and ERC20 allowance.
// Neither preview is an executable transaction or a wallet authorization.
const QUOTE_POS=/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
const HEX64=/^[0-9a-fA-F]{64}$/;
const ROUTER='0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45';
const MAX_QUOTE_AGE_MS=20000;

const positive=(value)=>typeof value==='string'&&value.length<=85&&
  QUOTE_POS.test(value)&&/[1-9]/.test(value);

export function validEthPolicyQuote(data,request){
  if(!request||request.asset!=='ETH'||!data||typeof data!=='object'||
     Array.isArray(data)||data.mode!=='ETH_ROUTER02_POLICY_ABI_QUOTE_ONLY'||
     data.asset!=='ETH'||data.network!=='ethereum-mainnet'||
     data.side!==request.side||data.stable!==request.stable||
     data.input_amount!==request.amount||
     data.route!=='UNISWAP_V3_SINGLE_POOL'||
     ![500,3000].includes(data.quote_fee_tier)||
     data.swap_call_abi!=='exactInputSingle'||
     data.router?.toLowerCase()!==ROUTER.toLowerCase()||
     data.swap_output_token!==(request.side==='BUY'?'WETH':request.stable)||
     data.native_eth_cycle_non_atomic!==true||
     !Array.isArray(data.native_eth_cycle_stages)||
     data.native_eth_cycle_stages.length!==2||
     data.wallet_identity_verified!==true||
     data.onchain_swap_quote!==true||
     data.unsigned_swap_validated!==true||
     !HEX64.test(data.unsigned_swap_hash||'')||
     data.approval_required_not_yet_checked!==true||
     data.native_wrap_unwrap_not_yet_settled!==true||
     data.privy_policy_attached!==false||
     data.privy_signer_authorized!==false||
     data.execution_enabled!==false||
     data.can_submit!==false||
     data.wallet_signed!==false||
     data.funds_transferred!==false||
     data.quote_ttl_seconds!==15||
     !Number.isSafeInteger(data.quoted_at)||
     data.quoted_at<=0||
     !positive(data.expected_swap_output_amount)||
     !positive(data.minimum_swap_output_amount)||
     Number(data.minimum_swap_output_amount)>Number(data.expected_swap_output_amount))return false;
  const stages=data.native_eth_cycle_stages.map(stage=>stage?.name);
  return request.side==='BUY'
    ? stages[0]==='SWAP_STABLE_TO_WETH'&&stages[1]==='UNWRAP_RECEIVED_WETH'
    : stages[0]==='WRAP_NATIVE_ETH'&&stages[1]==='SWAP_CONFIRMED_WETH_TO_STABLE';
}

export function checkEthPolicyStatus(quote,allowance,request){
  if(!validEthPolicyQuote(quote,request))return 'INVALID_QUOTE';
  if(!validEthPolicyAllowance(allowance,request))return 'INVALID_ALLOWANCE';
  const expiry=quote.quoted_at*1000+quote.quote_ttl_seconds*1000;
  if(Date.now()>expiry+5000)return 'QUOTE_EXPIRED';
  if(!allowance.source_funds_sufficient)return 'SOURCE_BALANCE_MISSING';
  if(!allowance.router_allowance_sufficient)return 'ALLOWANCE_MISSING';
  if(!allowance.conservative_native_gas_reserve_met)return 'GAS_RESERVE_MISSING';
  return 'PREVIEW_ONLY';
}

export function validEthPolicyAllowance(data,request){
  if(!request||request.asset!=='ETH'||!data||typeof data!=='object'||
     Array.isArray(data)||data.mode!=='ETH_ROUTER02_REAL_ALLOWANCE_READ_ONLY'||
     data.asset!=='ETH'||data.network!=='ethereum-mainnet'||
     data.side!==request.side||data.stable!==request.stable||
     data.input_amount!==request.amount||
     data.source_token!==(request.side==='BUY'?request.stable:'WETH')||
     data.spending_router?.toLowerCase()!==ROUTER.toLowerCase()||
     data.wallet_identity_verified!==true||
     data.signed_erc20_approval_created!==false||
     data.privy_grant_verified!==false||
     data.native_eth_unwrap_or_wrap_settled!==false||
     data.allowance_not_spend_authority!==true||
     data.execution_enabled!==false||data.can_submit!==false||
     typeof data.source_funds_sufficient!=='boolean'||
     typeof data.router_allowance_sufficient!=='boolean'||
     typeof data.manual_token_approval_would_be_required!=='boolean'||
     data.manual_token_approval_would_be_required===data.router_allowance_sufficient||
     typeof data.conservative_native_gas_reserve_met!=='boolean')return false;
  const balances=[data.verified_source_balance,
    data.existing_router_allowance,data.native_eth_gas_balance];
  return balances.every(v=>typeof v==='string'&&v.length<=85&&QUOTE_POS.test(v))&&
    [data.verified_source_balance_atomic,
      data.existing_router_allowance_atomic].every(v=>typeof v==='string'&&
      /^\d+$/.test(v)&&v.length<=78);
}
