# MiniNex UI V0.4 – DE / EN

A fast, mobile-first React/Vite preview that consumes the existing **PAPER-ONLY** MiniNex backend.

## Features

- German / English manual switch in the header and Settings (locale remembered locally).
- Live BTC/ETH public prices via `/ws/market`, backup REST polling, cross-provider verification status.
- Real backend history: 15-min and up to 1-hour charts. **24-hour history is not yet stored** and the 24h button is disabled.
- Buy/sell paper markers on charts when demo trades exist. Paper AUTO decisions, demo holdings, estimated realized/unrealized net PnL, estimated fees and trade history.
- Wallet is explicitly a **preview**; it cannot connect Privy or perform deposits/transfers. No real orders or user balances.
- No stored wallet credentials, signing keys or backend credentials.

## Development

```bash
cd frontend
npm install
npm run dev
npm run build
npm test
```

For testing against a specific backend, set `VITE_API_BASE_URL` in `frontend/.env.local` (public API URL only).

## Render static-site deployment

Create a **new Render Static Site** from repository `Quantumpepe/MiniNex` (leave the Python `mininex-backend` web service running separately).

- Site name: `mininex-ui` (or another name; see CORS below)
- Branch: `main`
- Root directory: `frontend`
- Build command: `npm ci && npm run build` (or `npm install && npm run build` if lockfile not yet committed)
- Publish directory: `dist`
- Public environment variable: `VITE_API_BASE_URL=https://mininex-backend.onrender.com`

The Python backend explicitly allows `https://mininex-ui.onrender.com`, `http://localhost:5173` and `http://127.0.0.1:5173` as browser origins. If the actual static-site URL is different, add that full HTTPS origin (without a trailing slash) to the Python backend environment variable `MININEX_ALLOWED_ORIGINS`, then redeploy the backend. No wildcard origins with credentials.

This is a responsive web preview, **not yet a native Android/iOS package**.

## Safety / correctness

Paper status must explicitly assert `mode: PAPER_AUTO_DEMO`, `real_trading_enabled: false`, and `wallet_connected: false` before the UI displays portfolio data. User-facing money metrics must come from the server, never fabricated demo chart values. Unavailable/stale verified prices are shown as unavailable rather than zero. Trades in `Activity` are **simulated only**.
