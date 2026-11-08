"""MiniNex FastAPI V0.3 -- verified prices and PAPER-only AUTO strategy."""
from contextlib import asynccontextmanager
import asyncio
import os

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .market_data import MarketDataService
from .auto_engine import PaperAutoEngine
from .auth import verify_privy_bearer

market = MarketDataService()
paper = PaperAutoEngine(market)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await market.start()
    await paper.start()
    try:
        yield
    finally:
        await paper.stop()
        await market.stop()


app = FastAPI(title="MiniNex API", version="0.5.0", lifespan=lifespan)

# Only known public browser origins can access read-only market/paper API.
# Extra frontends: MININEX_ALLOWED_ORIGINS=https://my-mininex.example
_allowed_origins = [
    "https://mininex-ui.onrender.com",
    "https://localhost",  # Capacitor Android bundled WebView
    "capacitor://localhost",  # Capacitor iOS bundled WebView
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
_allowed_origins.extend(
    origin.strip().rstrip("/")
    for origin in os.getenv("MININEX_ALLOWED_ORIGINS", "").split(",")
    if origin.strip().startswith(("https://", "http://"))
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys(_allowed_origins)),
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["Accept", "Content-Type", "Authorization"],
)


@app.get("/")
def home():
    return {"app": "MiniNex", "version": "0.5.0", "status": "online",
            "trading": "disabled", "paper_auto": "enabled", "real_orders": False}


@app.get("/health")
def health():
    # Deployment health != independently verified market data.
    return {"status": "ok"}


@app.get("/api/auth/me")
def authenticated_identity(authorization: str | None = Header(default=None)):
    """Verify Privy access token. Never assert wallet ownership or permission to trade."""
    identity = verify_privy_bearer(authorization)
    return JSONResponse({
        "authenticated": True,
        "privy_user_id": identity["user_id"],
        "wallet_connected": False,
        "trading_enabled": False,
        "mode": "IDENTITY_ONLY",
    }, headers={"Cache-Control": "no-store"})


@app.get("/api/market/prices")
def prices():
    return JSONResponse(market.snapshot(), headers={"Cache-Control": "no-store"})


@app.get("/api/market/status")
def market_status():
    snap = market.snapshot()
    return {
        "version": snap["version"],
        "trading_enabled": False,
        "primary_ws_connected": snap["primary_ws_connected"],
        "market_data_valid": {k: v["market_data_valid"] for k, v in snap["markets"].items()},
        "market_reasons": {k: v["reason"] for k, v in snap["markets"].items()},
        "primary_error": market.last_primary_error,
        "reference_error": market.last_reference_error,
    }


@app.get("/api/market/history/{asset}")
def price_history(asset: str, limit: int = 120):
    asset = asset.upper()
    if asset not in ("BTC", "ETH"):
        raise HTTPException(404, "Only BTC and ETH are supported")
    return {"asset": asset, "currency": "USDT", "points": market.history(asset, limit=limit)}


@app.get("/api/paper/status")
def paper_status():
    return JSONResponse(paper.status(), headers={"Cache-Control": "no-store"})


@app.get("/api/paper/status/{asset}")
def paper_asset_status(asset: str):
    asset = asset.upper()
    if asset not in ("BTC", "ETH"):
        raise HTTPException(404, "Only BTC and ETH are supported")
    return JSONResponse(paper.status(asset), headers={"Cache-Control": "no-store"})


@app.get("/api/paper/trades/{asset}")
def paper_trade_history(asset: str, limit: int = 50):
    asset = asset.upper()
    if asset not in ("BTC", "ETH"):
        raise HTTPException(404, "Only BTC and ETH are supported")
    return JSONResponse(paper.trades(asset, limit), headers={"Cache-Control": "no-store"})


@app.websocket("/ws/market")
async def websocket_market(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(market.snapshot())
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
