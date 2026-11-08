export const API_BASE = (import.meta.env?.VITE_API_BASE_URL || 'https://mininex-backend.onrender.com').replace(/\/+$/, '');

export function ensurePaperOnly(data) {
  if (!data || data.mode !== 'PAPER_AUTO_DEMO' || data.real_trading_enabled !== false || data.wallet_connected !== false) {
    throw new Error('Unexpected API mode: not a verified paper-only response');
  }
  return data;
}

export function validMarket(market) {
  return !!(market && market.market_data_valid === true && Number.isFinite(Number(market.price_usdt)) && Number(market.price_usdt) > 0);
}

export function cleanNumber(value) {
  if (value === undefined || value === null || value === '' || typeof value === 'boolean') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function portfolioValue(portfolio) {
  if (!portfolio || portfolio.market_data_valid !== true) return null;
  return cleanNumber(portfolio.mark_after_estimated_exit_costs);
}

export function formatNumber(value, lang = 'en', digits = 2) {
  const v = cleanNumber(value);
  if (v === null) return '—';
  return new Intl.NumberFormat(lang === 'de' ? 'de-CH' : 'en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits}).format(v);
}

export function formatMoney(value, lang = 'en', digits = 2) {
  const v = cleanNumber(value);
  if (v === null) return '—';
  return `${formatNumber(v, lang, digits)} $`;
}

export function formatCoin(value, lang = 'en') {
  const v = cleanNumber(value);
  if (v === null) return '—';
  const digits = v !== 0 && Math.abs(v) < 0.001 ? 8 : 6;
  return formatNumber(v, lang, digits);
}

export function formatSigned(value, lang = 'en', suffix = '$') {
  const n = cleanNumber(value);
  if (n === null) return '—';
  return `${n > 0 ? '+' : n < 0 ? '−' : ''}${formatNumber(Math.abs(n), lang, 2)} ${suffix}`;
}

export function formatTimestamp(value, lang = 'en', options = {}) {
  if (!value) return '—';
  const parsed = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (!Number.isFinite(parsed.getTime())) return '—';
  return new Intl.DateTimeFormat(lang === 'de' ? 'de-CH' : 'en-GB', {hour:'2-digit',minute:'2-digit', ...options}).format(parsed);
}

export function formatRelative(iso, lang='en') {
  if (!iso) return '—';
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms)) return '—';
  if (ms < 5000) return lang === 'de' ? 'gerade eben' : 'just now';
  const s = Math.max(0,Math.floor(ms/1000));
  if (s < 60) return lang === 'de' ? `vor ${s} Sek.` : `${s}s ago`;
  const m = Math.floor(s/60);
  if(m < 60) return lang === 'de' ? `vor ${m} Min.` : `${m}m ago`;
  const h = Math.floor(m/60);
  return lang === 'de' ? `vor ${h} Std.` : `${h}h ago`;
}

export function limitHistory(points, windowSeconds = 900, now = Date.now()/1000) {
  if (!Array.isArray(points)) return [];
  const result = points.filter(p => cleanNumber(p?.timestamp) !== null && cleanNumber(p?.price_usdt) > 0 && p.timestamp >= now - windowSeconds).sort((a,b)=>a.timestamp-b.timestamp);
  // A stable bounded path, even when the server samples once per second.
  if(result.length <= 250) return result;
  const stride = Math.ceil(result.length / 250);
  const sampled = result.filter((_,i)=>i % stride === 0);
  if(sampled[sampled.length-1] !== result[result.length-1]) sampled.push(result[result.length-1]);
  return sampled;
}
