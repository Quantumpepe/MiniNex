"""MiniNex FastAPI V0.3 -- verified prices and PAPER-only AUTO strategy."""
from contextlib import asynccontextmanager
import asyncio
import os
import time

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, StrictInt
from typing import Literal
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
from .wallet_balances import get_wallet_balances
from .user_paper import read_user_paper, start_user_paper, set_user_paper_status, step_user_paper, add_paper_mark_to_market
from .personal_paper_worker import PersonalPaperWorker
from .treasury import treasury_status
from .ota_proxy import ota_release_cache, ReleaseUnavailable
from .strategy_replay import replay_strategy, WINDOWS as REPLAY_WINDOWS, MODES as REPLAY_MODES
from .live_eth_quotes import quote_eth_mainnet, quote_readiness
from .live_eth_controls import get_live_eth_limits, save_live_eth_limits, stop_live_eth
from .privy_live_signer import get_eth_signer_status

market = MarketDataService()
paper = PaperAutoEngine(market)
personal_paper = PersonalPaperWorker(market)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await market.start()
    if _paper_visible():
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


@app.get("/api/updates/latest")
async def latest_verified_ota_release():
    """Public read-only release metadata cached centrally on Render.

    Phones never call GitHub REST directly. The client still strictly verifies
    native ABI, source commit, release ZIP URL and SHA-256 before downloading.
    """
    try:
        release, stale = await ota_release_cache.latest()
    except ReleaseUnavailable:
        return JSONResponse({"detail": "Update service temporarily unavailable"},
                            status_code=503, headers={"Cache-Control": "no-store",
                                                      "X-Content-Type-Options": "nosniff"})
    return JSONResponse(release, headers={
        "Cache-Control": "public, max-age=60",
        "X-Content-Type-Options": "nosniff",
        "X-MiniNex-OTA-Cache": "stale" if stale else "fresh",
    })


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


@app.get("/api/treasury/status")
def configured_treasury_status(authorization: str | None = Header(default=None)):
    """Authenticated config readiness ONLY; never expose owner address or execute."""
    verify_privy_bearer(authorization)
    return JSONResponse(treasury_status(),headers={"Cache-Control":"private, no-store"})


@app.get("/api/account")
def account_status(authorization: str | None = Header(default=None)):
    """Create/read only the account proven by a Privy token; no wallet authority."""
    did = verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_account_summary(did), headers={"Cache-Control":"no-store"})


@app.get("/api/wallet/balances")
async def verified_read_only_wallet_balances(
    authorization: str | None = Header(default=None),
):
    """Fetch public mainnet balances only for server-verified Privy wallets.

    No address or chain is taken from a client parameter. Never signs or
    authorizes a trade, never reads a PAPER position, never creates a deposit.
    """
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(await get_wallet_balances(did),
                        headers={"Cache-Control":"private, no-store",
                                 "X-Content-Type-Options":"nosniff"})


@app.get("/api/market/prices")
def prices():
    return JSONResponse(market.snapshot(), headers={"Cache-Control": "no-store"})


@app.get("/api/market/status")
def market_status():
    snap = market.snapshot()
    return {
        "version": snap["version"],
        "trading_enabled": False,
        "historical_samples":snap["historical_samples"],
        "history_loaded_at":snap["history_loaded_at"],
        "history_preload_error":market.last_history_error,
        "history_errors":snap["history_errors"],
        "primary_ws_connected": snap["primary_ws_connected"],
        "rest_backup_active":snap["rest_backup_active"],
        "rest_backup_error":market.last_rest_error,
        "primary_sources":{k:v["primary_source"] for k,v in snap["markets"].items()},
        "reference_sources":{k:v["reference_source"] for k,v in snap["markets"].items()},
        "reference_fallback_active":{
            k:v["reference_source"]=="Bitstamp Spot" and v["market_data_valid"]
            for k,v in snap["markets"].items()
        },
        "primary_sample_ages_sec":{
            k:round(max(0.0,time.time()-market._primary[k]["received_at"]),1)
              if k in market._primary else None
            for k in snap["markets"]
        },
        "market_data_valid": {k: v["market_data_valid"] for k, v in snap["markets"].items()},
        "market_reasons": {k: v["reason"] for k, v in snap["markets"].items()},
        "primary_error": market.last_primary_error,
        "reference_error": market.last_reference_error,
    }


@app.get("/api/strategy/replay/{asset}")
def historical_strategy_replay(asset: str, mode: str = "LONG", horizon: str = "15m"):
    """Non-executing historical replay, separate from all user accounts."""
    asset=asset.upper()
    if asset not in ("BTC","ETH") or mode not in REPLAY_MODES or horizon not in REPLAY_WINDOWS:
        raise HTTPException(422,"Invalid replay selection")
    samples=market.history(asset,limit=3600)
    result=replay_strategy(asset,mode,horizon,samples)
    return JSONResponse(result,headers={"Cache-Control":"no-store"})


@app.get("/api/market/history/{asset}")
def price_history(asset: str, limit: int = 120):
    asset = asset.upper()
    if asset not in ("BTC", "ETH"):
        raise HTTPException(404, "Only BTC and ETH are supported")
    return {"asset": asset, "currency": "USDT", "points": market.history(asset, limit=limit)}


@app.get("/api/paper/status")
def paper_status():
    _require_paper_visible()
    return JSONResponse(paper.status(), headers={"Cache-Control": "no-store"})


@app.get("/api/paper/status/{asset}")
def paper_asset_status(asset: str):
    _require_paper_visible()
    asset = asset.upper()
    if asset not in ("BTC", "ETH"):
        raise HTTPException(404, "Only BTC and ETH are supported")
    return JSONResponse(paper.status(asset), headers={"Cache-Control": "no-store"})


@app.get("/api/paper/trades/{asset}")
def paper_trade_history(asset: str, limit: int = 50):
    _require_paper_visible()
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


class EthLiveQuoteRequest(BaseModel):
    """Strict input: no client-selected wallet, recipient, chain or router."""
    model_config = ConfigDict(extra="forbid")
    side: Literal["BUY", "SELL"]
    stable: Literal["USDC", "USDT"]
    amount: str


@app.get("/api/live/eth/readiness")
def eth_quote_readiness(authorization: str | None = Header(default=None)):
    """Check independent quote preparation status; never grants live authority."""
    verify_privy_bearer(authorization)
    return JSONResponse(quote_readiness(), headers={"Cache-Control": "private, no-store"})


@app.post("/api/live/eth/quote")
async def eth_mainnet_quote(request: EthLiveQuoteRequest,
                            authorization: str | None = Header(default=None)):
    """Fetch a REAL market quote for user's independently verified Privy wallet.

    No transactions, approvals, order construction or signing can occur here.
    """
    did = verify_privy_bearer(authorization)["user_id"]
    quote = await quote_eth_mainnet(
        did, side=request.side, stable=request.stable, amount=request.amount)
    return JSONResponse(quote, headers={"Cache-Control": "private, no-store",
                                       "X-Content-Type-Options": "nosniff"})


class LiveEthLimitsRequest(BaseModel):
    """Explicit LIVE risk-limit preferences, never a delegated wallet grant."""
    model_config = ConfigDict(extra="forbid")
    planned_capital_usd: str
    max_order_usd: str
    max_daily_turnover_usd: str
    expected_revision: StrictInt


@app.get("/api/live/eth/authorization")
async def eth_signer_authorization_status(authorization: str | None = Header(default=None)):
    """Independently inspect Privy signer + policy; never grant signer access."""
    did = verify_privy_bearer(authorization)["user_id"]
    status = await get_eth_signer_status(did)
    return JSONResponse(status, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/eth/limits")
def eth_live_limits_status(authorization: str | None = Header(default=None)):
    did = verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_live_eth_limits(did), headers={"Cache-Control": "private, no-store"})


@app.put("/api/live/eth/limits")
def eth_live_limits_update(body: LiveEthLimitsRequest,
                           authorization: str | None = Header(default=None)):
    """Set spending limits, but keep trading PAUSED and signer unauthorized."""
    did = verify_privy_bearer(authorization)["user_id"]
    limits = save_live_eth_limits(
        did,
        capital=body.planned_capital_usd,
        order=body.max_order_usd,
        daily=body.max_daily_turnover_usd,
        expected_revision=body.expected_revision,
    )
    return JSONResponse(limits, headers={"Cache-Control": "private, no-store"})


@app.post("/api/live/eth/stop")
def eth_live_stop(authorization: str | None = Header(default=None)):
    """Stop future ETH LIVE sends; does not cancel any on-chain transaction."""
    did = verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(stop_live_eth(did), headers={"Cache-Control": "private, no-store"})


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


def _paper_visible() -> bool:
    # Future live-mode cutover: NEVER display or execute simulated sessions.
    # This flag only DISABLES paper mode; it cannot enable real trading.
    return os.getenv("MININEX_LIVE_TRADING_ENABLED","false").lower() != "true"


def _require_paper_visible() -> None:
    if not _paper_visible():
        raise HTTPException(404,"Paper simulation unavailable in live mode")


@app.get("/api/user-paper/availability")
def user_paper_availability():
    return JSONResponse({
        "paper_available":_paper_visible(),
        "live_trading_enabled":False,
    },headers={"Cache-Control":"no-store"})


@app.get("/api/user-paper/{asset}")
def user_paper_status(asset: str, authorization: str | None = Header(default=None)):
    """Authenticated, owner-isolated, persistent simulation status and fills."""
    _require_paper_visible()
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(add_paper_mark_to_market(read_user_paper(did,asset),market),
                        headers={"Cache-Control":"private, no-store"})


@app.post("/api/user-paper/{asset}/start")
def user_paper_start(asset: str, authorization: str | None = Header(default=None)):
    """Explicitly start a demo session; saved percent applies to virtual 1000 USD."""
    _require_paper_visible()
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(start_user_paper(did,asset,market),headers={"Cache-Control":"private, no-store"})


@app.post("/api/user-paper/{asset}/evaluate")
def user_paper_evaluate(asset: str, authorization: str | None = Header(default=None)):
    """Single manual evaluation against verified shared market data, not background live trading."""
    _require_paper_visible()
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(step_user_paper(did,asset,market,time.time()),
                        headers={"Cache-Control":"private, no-store"})

@app.post("/api/user-paper/{asset}/{action}")
def user_paper_action(asset: str, action: str,
                      authorization: str | None = Header(default=None)):
    """Pause PAPER immediately and settle simulated coins to USDC/USDT; no real swaps."""
    _require_paper_visible()
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(set_user_paper_status(did,asset,action,market,time.time()),
                        headers={"Cache-Control":"private, no-store"})

