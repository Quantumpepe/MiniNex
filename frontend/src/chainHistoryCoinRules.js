// MiniNex has 12 display coins but fewer independent blockchains.
// LINK, QNT and UNI are ERC-20 tokens on Ethereum, not new networks.
export const COIN_CHAIN=Object.freeze({
  BTC:'BTC',ETH:'ETH',BNB:'BNB',SOL:'SOL',XRP:'XRP',TRX:'TRX',
  POL:'POL',AVAX:'AVAX',LINK:'ETH',QNT:'ETH',UNI:'ETH',ARB:'ARB',
});
export const TOKEN_FILTERS=new Set(['LINK','QNT','UNI']);
export function historyRowsForCoin(items,coin){
  if(!Array.isArray(items))return [];
  if(!TOKEN_FILTERS.has(coin))return items;
  return items.filter(row=>row?.asset===coin||
    row?.tokens?.some(token=>token?.asset===coin));
}
