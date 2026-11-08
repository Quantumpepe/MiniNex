// Pure UI presentation helpers. No execution, wallets or mock account data.
export function compactDecision(raw, lang='en') {
  const v=String(raw||'WAIT').trim().toUpperCase();
  if (['BUY','PAPER_BUY','ENTRY','ADD'].includes(v)) return lang==='de'?'Kaufen':'Buy';
  if (['SELL','PAPER_SELL','EXIT','CLOSE','RISK_EXIT'].includes(v)) return lang==='de'?'Verkaufen':'Sell';
  if (['HOLD','HOLD_COIN','HOLD_STABLE','TRAILING'].includes(v)) return lang==='de'?'Halten':'Hold';
  return lang==='de'?'Warten':'Wait';
}
export function compactNetPnL(portfolio) {
  if (!portfolio) return null;
  const realized=Number(portfolio.realized_net_pnl);
  if (!Number.isFinite(realized)) return null;
  if (portfolio.position === 'COIN') {
    if (portfolio.estimated_unrealized_net_pnl === null || portfolio.estimated_unrealized_net_pnl === undefined) return null;
    const pending=Number(portfolio.estimated_unrealized_net_pnl);
    return Number.isFinite(pending)?realized+pending:null;
  }
  return realized;
}
export function compactChartData(points, seconds=900) {
  if (!Array.isArray(points)) return [];
  const recent=points.filter(p=>Number.isFinite(Number(p?.timestamp))&&Number.isFinite(Number(p?.price_usdt))&&Number(p.price_usdt)>0)
    .sort((a,b)=>a.timestamp-b.timestamp);
  if (!recent.length) return [];
  const cutoff=recent.at(-1).timestamp-seconds;
  const filtered=recent.filter(p=>p.timestamp>=cutoff);
  const stride=Math.max(1,Math.ceil(filtered.length/160));
  const result=filtered.filter((_,i)=>i%stride===0);
  if(result.at(-1)!==filtered.at(-1)) result.push(filtered.at(-1));
  return result;
}
