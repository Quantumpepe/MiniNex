"""MiniNex FastAPI V0.2 -- market monitoring only; orders are disabled."""
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from .market_data import MarketDataService
import asyncio

market = MarketDataService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await market.start()
    try:
        yield
    finally:
        await market.stop()


app = FastAPI(title="MiniNex API", version="0.2.0", lifespan=lifespan)


@app.get("/")
def home():
    return {"app": "MiniNex", "version": "0.2.0", "status": "online", "trading": "disabled"}


@app.get("/health")
def health():
    # Deployment health != independently verified live prices. Check /api/market/status.
    return {"status": "ok"}


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


@app.websocket("/ws/market")
async def websocket_market(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(market.snapshot())
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass