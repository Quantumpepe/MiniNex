# MiniNex V0.4 — BTC/ETH paper AUTO + mobile DE/EN preview

MiniNex is an independent BTC/ETH trading application **under development**. V0.4
contains **no real-money trading, wallets, private keys, orders or signatures**.
It is a simulation with imaginary balances, and is *not* a profitable strategy
or evidence that real fills, fees, execution quality or availability are assured.

## Current architecture

- FastAPI service on Render, Python 3, one process.
- BTC/ETH Binance Spot WebSocket miniTicker (~1-second updates) + Kraken USD
  reference check (~25-second polling). Invalid, stale, divergent prices block
  new simulation trades; a REST health check alone does not imply market health.
- Independent paper AUTO portfolios: **BTC starts with imaginary 1,000 USDT**,
  **ETH starts with imaginary 0.25 ETH**. The engine never mixes BTC and ETH.
- Paper AUTO logic: 1/3/10-minute history, confirmation of rebound or breakout;
  profit trailing from position highs; explicitly labelled risk exit when a
  decline breaches a risk threshold (can create actual simulated losses).
- Fees are *paper estimates*: 0.10% venue, 0.05% spread, 0.08% slippage per side;
  $2.50 BTC / $1.50 ETH transaction/network estimate. Budget includes entry
  costs and reserves estimated later network cost. Actual on-chain BTC/ETH gas,
  stablecoin conversion, pool liquidity and execution costs are not modelled.
- A simulated USDC equals a simulated USDT/USD reference dollar for arithmetic
  only. **This is NOT safe for live USDC execution or a source of executable quotes.**
- Paper cycle: stable -> coin -> stable, or existing coin -> stable -> coin.
- DEMO account state and 1-hour price data are held only in memory. Render restarts,
  worker replacements and deployments **reset demo balances and history**.
- GET-only paper API: anonymous visitors cannot submit new orders or trades.

## Endpoints

- `/health` — FastAPI running (not a market/worker guarantee)
- `/api/market/prices` — BTC & ETH price snapshots, provenance and validity
- `/api/market/status` — primary/reference feed status and error reasons
- `/api/market/history/BTC?limit=120` and `/ETH` — most recent 1s samples
- `/ws/market` — prices over WebSocket (1 snapshot/s)
- `/api/paper/status` — paper worker status, demo balances, net P&L, decisions
- `/api/paper/status/BTC` or `/ETH` — one demo asset
- `/api/paper/trades/BTC` or `/ETH` — fictitious trades with estimated costs

The `/api/paper/*` responses ALWAYS say `real_trading_enabled: false`, and
are **public simulated data**. No POST endpoint starts an order.

## Render

Root Directory: blank; Language: Python 3; Build command:

`pip install -r requirements.txt`

Start command:

`uvicorn backend.app:app --host 0.0.0.0 --port $PORT`

Stay with **one Uvicorn worker** for now. Paper trading runs as a 3-second
async task in the web process. This is suitable for development, **not**
production 24/7 trading. Later: durable position database, dedicated worker,
atomic execution ledger, venue quotes, safe wallet authorisation, trade limits,
independent network gas and swap liquidity checks, per-account isolation,
authentication, and audited risk handling.

## DE / EN

The mobile-first web interface now provides **Deutsch (DE)** and **English (EN)**,
with a visible switch and locally saved preference. It is a web preview and not
yet a native Android/iOS package. Backend reason codes are language-neutral.

## Frontend / Render static site

The React/Vite interface lives in `frontend/`, separate from the Python web
service. To deploy, use Render **New → Static Site**, repository
`Quantumpepe/MiniNex`, root directory `frontend`, build command
`npm install && npm run build`, publish directory `dist`, optionally
`VITE_API_BASE_URL=https://mininex-backend.onrender.com`.

The default static-site name `mininex-ui` is allowed in the backend CORS
configuration. If another domain is chosen, set `MININEX_ALLOWED_ORIGINS` in the
**backend** Render environment to its full HTTPS origin (no trailing slash).
Do not use secrets in `VITE_` variables. The frontend shows actual public
market data and demo engine values, never real wallet holdings. Charts show
15-minute or 1-hour history only; 24 hours is disabled pending storage support.

See `frontend/README.md` for details.

## Tests

`pip install -r requirements.txt pytest`

`python -m pytest -q tests/`

All tests run offline; they do not verify WebSocket provider availability,
real trading, Render background task persistence or any blockchain settlement.
