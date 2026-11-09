/** Identity-scoped PAPER valuation. Never conflates virtual equity with a real wallet. */
const ASSETS = ['BTC','ETH'];

const finite = value => {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};

/** Only verified estimated liquidation values count towards displayed equity. */
export function summarizePaperSessions(sessions) {
  if (!sessions || typeof sessions !== 'object') return null;
  let equity = 0;
  let startingCapital = 0;
  let activeAssets = 0;
  let runningAssets = 0;
  for (const asset of ASSETS) {
    const row = sessions[asset];
    if (!row || row.status === 'NOT_STARTED') continue;
    if (row.asset !== asset || row.paper_only !== true ||
        row.real_trading_enabled !== false || row.wallet_funds_used !== false ||
        !['ACTIVE','PAUSED'].includes(row.status)) return null;
    const initial = finite(row.demo_start_cash);
    const valuation = row.position?.valuation;
    const cash = finite(row.position?.stable_cash);
    const coin = finite(row.position?.coin_quantity);
    const estimated = finite(valuation?.estimated_total_equity_usd);
    if (initial === null || initial <= 0 ||
        cash === null || cash < 0 || coin === null || coin < 0 ||
        valuation?.quote_valid !== true || estimated === null || estimated < 0) return null;
    // A read-only valuation already accounts for estimated exit costs; do not add
    // coin value or subtract acquisition fees again.
    equity += estimated;
    startingCapital += initial;
    activeAssets++;
    if (row.status === 'ACTIVE') runningAssets++;
  }
  if (!activeAssets) return null;
  const pnl = equity - startingCapital;
  return {
    equity: Math.round(equity * 100) / 100,
    startingCapital: Math.round(startingCapital * 100) / 100,
    pnl: Math.round(pnl * 100) / 100,
    pnlPct: startingCapital > 0 ? pnl / startingCapital * 100 : 0,
    assetCount: activeAssets,
    runningAssets,
    mode: 'PAPER'
  };
}

/** Show only observed portfolio snapshots; never fabricate a historical curve. */
export function paperSparkline(samples) {
  if (!Array.isArray(samples) || samples.length < 2) return null;
  const points = samples.filter(p => p && Number.isFinite(p.at) && Number.isFinite(p.value) && p.value >= 0);
  if (points.length < 2) return null;
  const values = points.map(p=>p.value), lo=Math.min(...values), hi=Math.max(...values);
  const spread=Math.max(hi-lo,Math.max(1,Math.abs(hi)) * 0.0002);
  const first=points[0].at, last=points[points.length-1].at;
  const x=(p,i)=>8 + (last > first ? (p.at-first)/(last-first) : i/(points.length-1))*184;
  const y=p=>36 - ((p.value-lo+(spread-(hi-lo))/2)/spread)*24;
  const line=points.map((p,i)=>(i?'L':'M')+x(p,i).toFixed(1)+' '+y(p).toFixed(1)).join(' ');
  return {line, area:line+' L192 45 L8 45 Z', lastY:y(points[points.length-1]),
          positive:values[values.length-1]>=values[0]};
}
