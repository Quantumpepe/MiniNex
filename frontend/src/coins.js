// Twelve approved MiniNex display assets. Only BTC/ETH have PAPER settings.
// Showing a coin never authorizes deposits, wallet transfers or live trades.
// LINK/UNI are ERC-20 tokens on Ethereum, not separate native networks.
export const COINS=Object.freeze([
  Object.freeze({symbol:'BTC',name:'Bitcoin',glyph:'₿',network:'bitcoin-mainnet',paperSettingsReady:true}),
  Object.freeze({symbol:'ETH',name:'Ethereum',glyph:'Ξ',network:'ethereum-mainnet',paperSettingsReady:true}),
  Object.freeze({symbol:'BNB',name:'BNB',glyph:'⬡',network:'bsc-mainnet',paperSettingsReady:false}),
  Object.freeze({symbol:'SOL',name:'Solana',glyph:'◎',network:'solana-mainnet',paperSettingsReady:false}),
  Object.freeze({symbol:'XRP',name:'XRP',glyph:'✕',network:'xrpl-mainnet',paperSettingsReady:false}),
  Object.freeze({symbol:'TRX',name:'TRON',glyph:'◈',network:'tron-mainnet',paperSettingsReady:false}),
  Object.freeze({symbol:'POL',name:'Polygon',glyph:'⬡',network:'polygon-pos-mainnet',paperSettingsReady:false}),
  Object.freeze({symbol:'AVAX',name:'Avalanche',glyph:'▲',network:'avalanche-c-mainnet',paperSettingsReady:false}),
  Object.freeze({symbol:'LINK',name:'Chainlink',glyph:'⬡',network:'ethereum-mainnet',paperSettingsReady:false,token:true}),
  Object.freeze({symbol:'DOGE',name:'Dogecoin',glyph:'Ð',network:'dogecoin-mainnet',paperSettingsReady:false}),
  Object.freeze({symbol:'UNI',name:'Uniswap',glyph:'🦄',network:'ethereum-mainnet',paperSettingsReady:false,token:true}),
  Object.freeze({symbol:'ADA',name:'Cardano',glyph:'₳',network:'cardano-mainnet',paperSettingsReady:false,inProgress:true}),
]);

export function coinBySymbol(symbol) {
  return COINS.find(coin=>coin.symbol===symbol)||null;
}

// Show ONLY independently verified, fresh read-only market prices.
// A quote does not mean a safe executable swap exists.
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
