"""MiniNex FastAPI V0.3 -- verified prices and PAPER-only AUTO strategy."""
from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .market_data import MarketDataService
from .auto_engine import PaperAutoEngine

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


app = FastAPI(title="MiniNex API", version="0.3.0", lifespan=lifespan)


@app.get("/")
def home():
    return {"app": "MiniNex", "version": "0.3.0", "status": "online",
            "trading": "disabled", "paper_auto": "enabled", "real_orders": False}


@app.get("/health")
def health():
    # Deployment health != independently verified market data.
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
