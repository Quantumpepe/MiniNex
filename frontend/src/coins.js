// Eight real native-coin networks in the exact order of the backend registry.
// UI availability must never be interpreted as permission to trade or deposit.
export const COINS=Object.freeze([
  Object.freeze({symbol:'BTC',name:'Bitcoin',glyph:'₿',network:'bitcoin-mainnet',paperSettingsReady:true}),
  Object.freeze({symbol:'ETH',name:'Ethereum',glyph:'Ξ',network:'ethereum-mainnet',paperSettingsReady:true}),
  Object.freeze({symbol:'BNB',name:'BNB',glyph:'⬡',network:'bsc-mainnet',paperSettingsReady:false}),
  Object.freeze({symbol:'POL',name:'Polygon',glyph:'⬡',network:'polygon-pos-mainnet',paperSettingsReady:false}),
  Object.freeze({symbol:'SOL',name:'Solana',glyph:'◎',network:'solana-mainnet',paperSettingsReady:false}),
  Object.freeze({symbol:'XRP',name:'XRP',glyph:'✕',network:'xrpl-mainnet',paperSettingsReady:false}),
  Object.freeze({symbol:'ADA',name:'Cardano',glyph:'₳',network:'cardano-mainnet',paperSettingsReady:false}),
  Object.freeze({symbol:'TRX',name:'TRON',glyph:'◈',network:'tron-mainnet',paperSettingsReady:false}),
]);

export function coinBySymbol(symbol) {
  return COINS.find(coin=>coin.symbol===symbol)||null;
}

// All eight coins may show independently checked read-only public prices.
// paperSettingsReady controls trading settings, not market quote visibility.
export function coinMarketSnapshot(symbol, markets, syncedAt, now=Date.now()) {
  const coin=coinBySymbol(symbol);
  if(!coin)return null;
  if(typeof syncedAt!=='number'||!Number.isFinite(syncedAt)||
     syncedAt<=0||now-syncedAt<0||now-syncedAt>=20000)return null;
  const quote=markets?.[symbol];
  if(!quote||quote.market_data_valid!==true||
     !Number.isFinite(Number(quote.price_usdt))||Number(quote.price_usdt)<=0)return null;
  return quote;
}
