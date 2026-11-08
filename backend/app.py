"""MiniNex FastAPI V0.3 -- verified prices and PAPER-only AUTO strategy."""
from contextlib import asynccontextmanager
import asyncio
import os
import time

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, StrictInt
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from .market_data import MarketDataService
from .auto_engine import PaperAutoEngine
from .auth import verify_privy_bearer
from .storage import get_account_summary
from .budgets import get_budget_preferences, save_budget_preference
from .trade_preferences import get_trade_preferences, save_trade_preference
from .tax_reports import list_verified_activity, make_csv, make_pdf, validate_year
from .blockchain_preview import fetch_preview, check_address
from .blockchain_history import fetch_history, validate_request
from .verified_sync import synchronize_one_page
from .user_paper import read_user_paper, start_user_paper, set_user_paper_status, step_user_paper
from .personal_paper_worker import PersonalPaperWorker

market = MarketDataService()
paper = PaperAutoEngine(market)
personal_paper = PersonalPaperWorker(market)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await market.start()
    await paper.start()
    await personal_paper.start()
    try:
        yield
    finally:
        await personal_paper.stop()
        await paper.stop()
        await market.stop()


app = FastAPI(title="MiniNex API", version="0.8.6", lifespan=lifespan)

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
    allow_methods=["GET", "PUT", "POST"],
    allow_headers=["Accept", "Content-Type", "Authorization"],
)


@app.get("/")
def home():
    return {"app": "MiniNex", "version": "0.8.6", "status": "online",
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


@app.get("/api/account")
def account_status(authorization: str | None = Header(default=None)):
    """Create/read only the account proven by a Privy token; no wallet authority."""
    did = verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_account_summary(did), headers={"Cache-Control":"no-store"})


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


@app.get("/api/trading/capabilities")
def trading_capabilities(authorization: str | None = Header(default=None)):
    """Authenticated, read-only capability contract. Never authorizes trading."""
    verify_privy_bearer(authorization)
    return JSONResponse({
        "mode": "PREPARATION_ONLY",
        "real_trading_enabled": False,
        "automated_signing_enabled": False,
        "deposits_enabled": False,
        "withdrawals_enabled": False,
        "routes": {
            "BTC": {
                "wallet_chain": "bitcoin",
                "native_asset": "BTC",
                "settlement_options": ["USDC", "USDT"],
                "cross_chain_settlement_required": True,
                "execution_provider_configured": False,
                "authorization_configured": False,
                "ready": False
            },
            "ETH": {
                "wallet_chain": "ethereum",
                "native_asset": "ETH",
                "settlement_options": ["USDC", "USDT"],
                "cross_chain_settlement_required": False,
                "execution_provider_configured": False,
                "authorization_configured": False,
                "ready": False
            }
        }
    }, headers={"Cache-Control": "no-store"})


class BudgetPreferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    percent: StrictInt
    expected_revision: StrictInt


@app.get("/api/budgets")
def budgets_status(authorization: str | None = Header(default=None)):
    """User-owned preferences. Never grant signer or trading permissions."""
    did = verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_budget_preferences(did), headers={"Cache-Control": "no-store"})


@app.put("/api/budgets/{asset}")
def budgets_update(
    asset: str, body: BudgetPreferenceRequest,
    authorization: str | None = Header(default=None)
):
    """Save a setting for the authenticated DID, not a trade authorization."""
    did = verify_privy_bearer(authorization)["user_id"]
    if asset not in ("BTC", "ETH"):
        raise HTTPException(404, "Only BTC and ETH are supported")
    if not 0 <= body.percent <= 100 or not 0 <= body.expected_revision < 2_000_000_000:
        raise HTTPException(422, "Invalid budget preference")
    return JSONResponse(
        save_budget_preference(did, asset, body.percent, body.expected_revision),
        headers={"Cache-Control": "no-store"},
    )


class TradePreferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str
    stable: str
    horizon: str
    expected_revision: StrictInt


@app.get("/api/trade-preferences")
def trade_preferences_status(authorization: str | None = Header(default=None)):
    """Persisted per-user options; never grants live order or wallet permissions."""
    did = verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_trade_preferences(did), headers={"Cache-Control": "no-store"})


@app.put("/api/trade-preferences/{asset}")
def trade_preferences_update(
    asset: str, body: TradePreferenceRequest,
    authorization: str | None = Header(default=None),
):
    did = verify_privy_bearer(authorization)["user_id"]
    if asset not in ("BTC", "ETH"):
        raise HTTPException(404, "Only BTC and ETH are supported")
    if (body.mode not in ("LONG", "SPOT_SHORT")
            or body.stable not in ("USDC", "USDT")
            or body.horizon not in ("5m", "15m", "1h", "24h")
            or not 0 <= body.expected_revision < 2_000_000_000):
        raise HTTPException(422, "Invalid trade preferences")
    return JSONResponse(
        save_trade_preference(
            did, asset, body.mode, body.stable, body.horizon, body.expected_revision
        ),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/activity/{year}")
def annual_verified_activity(year: int, authorization: str | None = Header(default=None)):
    """Read only confirmed account-bound activity; never paper trading history."""
    did = verify_privy_bearer(authorization)["user_id"]
    try:
        validate_year(year)
    except ValueError:
        raise HTTPException(422, "Invalid year") from None
    rows = list_verified_activity(did, year)
    return JSONResponse({
        "year": year, "rows": rows, "count": len(rows), "verified_only": True,
        "paper_excluded": True, "tax_complete": False,
    }, headers={"Cache-Control": "private, no-store"})


@app.get("/api/activity/{year}/export/{format}")
def annual_verified_activity_export(
    year: int, format: str, authorization: str | None = Header(default=None),
):
    did = verify_privy_bearer(authorization)["user_id"]
    try:
        validate_year(year)
    except ValueError:
        raise HTTPException(422, "Invalid year") from None
    if format not in ("csv", "pdf"):
        raise HTTPException(404, "Unsupported export")
    rows = list_verified_activity(did, year)
    blob = make_csv(rows, year) if format == "csv" else make_pdf(rows, year)
    return Response(
        content=blob,
        media_type="text/csv; charset=utf-8" if format == "csv" else "application/pdf",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="MiniNex_Activity_{year}.{format}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/blockchain/preview/{asset}")
async def blockchain_preview(
    asset: str, address: str, authorization: str | None = Header(default=None),
):
    """Read-only public explorer view; query address does not prove wallet ownership."""
    verify_privy_bearer(authorization)
    check_address(asset, address)
    return JSONResponse(
        await fetch_preview(asset, address),
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/blockchain/history/{asset}")
async def blockchain_history(
    asset: str, address: str, cursor: str | None = None,
    authorization: str | None = Header(default=None),
):
    """Paginated public read-only activity; wallet ownership remains UNVERIFIED."""
    verify_privy_bearer(authorization)
    validate_request(asset,address,cursor)
    return JSONResponse(
        await fetch_history(asset,address,cursor),
        headers={"Cache-Control":"private, no-store"},
    )


@app.post("/api/blockchain/sync/{asset}")
async def blockchain_import_verified_page(
    asset: str, address: str, cursor: str | None = None,
    authorization: str | None = Header(default=None),
):
    """Explicit opt-in archive; server independently verifies address at Privy."""
    did=verify_privy_bearer(authorization)["user_id"]
    validate_request(asset,address,cursor)
    return JSONResponse(
        await synchronize_one_page(did,asset,address,cursor),
        headers={"Cache-Control":"private, no-store"},
    )


@app.get("/api/user-paper/{asset}")
def user_paper_status(asset: str, authorization: str | None = Header(default=None)):
    """Authenticated, owner-isolated, persistent simulation status and fills."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(read_user_paper(did,asset),headers={"Cache-Control":"private, no-store"})


@app.post("/api/user-paper/{asset}/start")
def user_paper_start(asset: str, authorization: str | None = Header(default=None)):
    """Explicitly start a demo session; saved percent applies to virtual 1000 USD."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(start_user_paper(did,asset,market),headers={"Cache-Control":"private, no-store"})


@app.post("/api/user-paper/{asset}/evaluate")
def user_paper_evaluate(asset: str, authorization: str | None = Header(default=None)):
    """Single manual evaluation against verified shared market data, not background live trading."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(step_user_paper(did,asset,market,time.time()),
                        headers={"Cache-Control":"private, no-store"})

@app.post("/api/user-paper/{asset}/{action}")
def user_paper_action(asset: str, action: str,
                      authorization: str | None = Header(default=None)):
    """Only pause or resume an existing simulation; never wallet execution."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(set_user_paper_status(did,asset,action),
                        headers={"Cache-Control":"private, no-store"})

