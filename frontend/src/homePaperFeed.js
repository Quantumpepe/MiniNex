/** Strict client boundary for the account's newest saved PAPER events. */
export const HOME_PAPER_COINS=Object.freeze([
  'BTC','ETH','BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI',
]);

const positive=n=>typeof n==='number'&&Number.isFinite(n)&&n>0;
const finite=n=>typeof n==='number'&&Number.isFinite(n);
const VALID_KINDS=new Set(['PAPER_BUY','PAPER_SELL']);

export function validHomePaperFeed(data,expectedAsset='ALL') {
  if(expectedAsset!=='ALL'&&!HOME_PAPER_COINS.includes(expectedAsset))return false;
  if(!data||typeof data!=='object'||Array.isArray(data)||
     data.paper_only!==true||data.real_trading_enabled!==false||
     data.wallet_funds_used!==false||data.scope!=='PRIVATE_ACCOUNT'||
     data.sort!=='NEWEST_FIRST'||
     data.selected_asset!==expectedAsset||
     !Array.isArray(data.assets)||
     data.assets.length!==HOME_PAPER_COINS.length||
     !data.assets.every((coin,i)=>coin===HOME_PAPER_COINS[i])||
     !Array.isArray(data.trades)||data.trades.length>60)return false;
  const ids=new Set();
  let last=Infinity;
  for(const trade of data.trades){
    if(!trade||typeof trade!=='object'||Array.isArray(trade)||
       !HOME_PAPER_COINS.includes(trade.asset)||
       (expectedAsset!=='ALL'&&trade.asset!==expectedAsset)||
       trade.paper_only!==true||trade.real_trade!==false||
       !VALID_KINDS.has(trade.kind)||
       typeof trade.id!=='string'||
       !/^[A-Z]+:[1-9][0-9]*$/.test(trade.id)||
       !trade.id.startsWith(trade.asset+':')||ids.has(trade.id)||
       !['USDC','USDT'].includes(trade.settlement)||
       !positive(trade.price_reference)||!positive(trade.quantity)||
       !positive(trade.amount_usd)||
       typeof trade.at!=='string'||
       !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d/.test(trade.at)||
       typeof trade.reason!=='string'||trade.reason.length>120)return false;
    const timestamp=Date.parse(trade.at);
    if(!Number.isFinite(timestamp)||timestamp>last)return false;
    if(trade.kind==='PAPER_SELL'&&!finite(trade.realized_net_profit))
      return false;
    if(trade.kind==='PAPER_BUY'&&trade.realized_net_profit!==null)
      return false;
    ids.add(trade.id);
    last=timestamp;
  }
  return true;
}
