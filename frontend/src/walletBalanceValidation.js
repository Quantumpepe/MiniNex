export const ASSETS=['BTC','ETH','USDC','USDT'];
const EXPECTED_NETWORK={BTC:'bitcoin-mainnet',ETH:'ethereum-mainnet',
                        USDC:'ethereum-mainnet',USDT:'ethereum-mainnet'};
const UNITS=/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;

export function validateWalletBalanceResponse(result) {
  if(!result||result.mode!=='VERIFIED_READ_ONLY'||
     result.wallet_ownership_verified!==true||
     result.real_trading_enabled!==false||
     result.automatic_signing_enabled!==false||
     result.wallet_funds_used_for_paper!==false||
     result.pricing!=='NATIVE_UNITS_ONLY'||!result.balances||
     !result.wallets||typeof result.wallets!=='object')return false;
  return ASSETS.every(asset=>{
    const entry=result.balances[asset];
    return entry&&entry.network===EXPECTED_NETWORK[asset]&&(
      (entry.status==='wallet_missing'&&entry.balance===null)||
      (entry.status==='verified'&&typeof entry.balance==='string'&&
       entry.balance.length<=85&&UNITS.test(entry.balance))
    );
  });
}

