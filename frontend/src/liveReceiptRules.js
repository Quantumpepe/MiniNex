// One BTC/ETH read-only transaction status form; no signer or spend authority.
// A confirmed source-chain receipt alone is never a filled cross-chain trade.
export function receiptRequest(asset,txid,type,side,stable){
  if(asset==='BTC'){
    if(typeof txid!=='string'||!/^[0-9a-fA-F]{64}$/.test(txid))return null;
    return {asset,hash:txid.toLowerCase(),
      path:'/api/live/btc/tx-receipt/'+txid.toLowerCase(),
      type:'BTC_CHAIN'};
  }
  if(asset!=='ETH'||typeof txid!=='string'||!/^0x[0-9a-fA-F]{64}$/.test(txid))
    return null;
  const hash=txid.toLowerCase();
  if(type==='ROUTER02'){
    if(!['BUY','SELL'].includes(side)||!['USDC','USDT'].includes(stable))return null;
    return {asset,hash,type,
      path:'/api/live/eth/router02-receipt/'+hash+
        '?side='+side+'&stable='+stable};
  }
  if(type==='DEPOSIT'||type==='WITHDRAW')
    return {asset,hash,type,
      path:'/api/live/eth/weth9-receipt/'+hash+'?operation='+type};
  return null;
}

const AMOUNT=/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;

export function validReceiptResponse(response,request){
  if(!response||typeof response!=='object'||Array.isArray(response)||
     !request||response.asset!==request.asset||
     response.network!==(request.asset==='BTC'?'bitcoin-mainnet':'ethereum-mainnet')||
     response.execution_enabled!==false||response.can_submit!==false)return false;
  if(request.asset==='BTC'){
    return response.mode==='BTC_MAINNET_WALLET_TX_OBSERVATION'&&
      response.txid===request.hash&&
      response.crosschain_swap_fill_verified===false&&
      response.ethereum_stablecoin_settlement_verified===false&&
      response.trade_order_recorded===false&&
      Number.isInteger(response.confirmations_observed)&&
      response.confirmations_observed>=0&&
      typeof response.net_wallet_movement_btc==='string'&&
      AMOUNT.test(response.net_wallet_movement_btc);
  }
  if(response.tx_hash!==request.hash||
     response.native_eth_trade_cycle_complete!==false)return false;
  if(request.type==='ROUTER02'){
    return response.mode==='ETH_ROUTER02_ERC20_FILL_OBSERVATION'&&
      response.side===request.path.match(/side=(BUY|SELL)/)?.[1]&&
      ['WETH','USDC','USDT'].includes(response.swap_output_asset)&&
      typeof response.swap_erc20_leg_verified==='boolean'&&
      response.real_order_recorded===false&&
      (response.destination_received_amount===null||
       (typeof response.destination_received_amount==='string'&&
        AMOUNT.test(response.destination_received_amount)));
  }
  return response.mode==='ETH_MAINNET_WETH9_CONVERSION_RECEIPT'&&
    response.operation===request.type&&
    response.swap_leg_linked===false&&
    typeof response.confirmed_native_conversion==='boolean'&&
    (response.weth9_event_amount===null||
     (typeof response.weth9_event_amount==='string'&&
      AMOUNT.test(response.weth9_event_amount)));
}
