"""MiniNex FastAPI V0.3 -- verified prices and PAPER-only AUTO strategy."""
from contextlib import asynccontextmanager
import asyncio
import math
import os
import time

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, StrictInt, Field
from typing import Literal
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from .market_data import MarketDataService
from .extra_market_data import ExtraMarketData, EXTRA_ASSETS
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
from .extra_paper import (
    read as read_extra_paper, start as start_extra_paper,
    set_status as set_extra_paper_status,
)
from .extra_paper_report import compare as compare_extra_paper
from .home_paper_trades import recent_home_trades
from .treasury import treasury_status
from .ota_proxy import ota_release_cache, ReleaseUnavailable
from .strategy_replay import (
    replay_strategy, WINDOWS as REPLAY_WINDOWS, MODES as REPLAY_MODES,
    REPLAY_ASSETS, EXTRA_REPLAY_ASSETS,
)
from .extended_paper_replay import compact_replay_observations
from .live_eth_quotes import quote_eth_mainnet, quote_readiness
from .live_eth_swap_plan import make_eth_swap_plan
from .live_eth_receipts import check_eth_swap_receipt
from .live_btc_quotes import quote_native_btc
from .live_eth_controls import get_live_eth_limits, save_live_eth_limits, stop_live_eth
from .privy_live_signer import get_eth_signer_status
from .privy_eth_policy import get_eth_policy_review
from .eth_auto_preflight import get_eth_automatic_preflight
from .eth_user_enrollment_preflight import get_eth_user_enrollment_preflight
from .eth_privy_policy_plan import get_eth_policy_candidate_preflight
from .eth_privy_policy_ledger import (
    prepare_eth_policy_record, get_eth_policy_ledger_status,
    get_eth_policy_remote_revocation_review,
)
from .eth_user_consent import (
    prepare_eth_consent_challenge, confirm_eth_consent_intent,
    revoke_eth_consent_intent,
)
from .live_orders import get_live_order_history
from .live_asset_registry import get_global_live_readiness
from .manual_swap_routes import build_manual_swap_route
from .manual_swap_live_quotes import get_native_swap_quote
from .manual_swap_wallet_readiness import get_manual_swap_wallet_readiness

market = MarketDataService()
extra_markets = ExtraMarketData()
paper = PaperAutoEngine(market)
personal_paper = PersonalPaperWorker(market,extra_markets)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await market.start()
    await extra_markets.start()
    if _paper_visible():
        await paper.start()
    await personal_paper.start()
    try:
        yield
    finally:
        await personal_paper.stop()
        await paper.stop()
        await extra_markets.stop()
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


def public_market_snapshot():
    """Add read-only previews without changing the BTC/ETH strategy engine."""
    snapshot = market.snapshot()
    snapshot["markets"].update(extra_markets.snapshot())
    return snapshot


@app.get("/api/market/prices")
def prices():
    return JSONResponse(public_market_snapshot(), headers={"Cache-Control": "no-store"})


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
        "extra_coin_markets": extra_markets.diagnostics(),
    }


@app.get("/api/strategy/replay/{asset}")
def historical_strategy_replay(asset: str, mode: str = "LONG",
                               horizon: str = "15m", initial_usd: float = 500.0):
    """One on-demand historical PAPER evaluation, separate from user accounts.

    Reuse the BTC/ETH rules for nine additional coins with independent
    hypothetical capital. Never starts a session, changes real budgets,
    asks a provider for new data, or signs/sends a transaction.
    """
    asset=asset.upper()
    if (asset not in REPLAY_ASSETS or mode not in REPLAY_MODES
        or horizon not in REPLAY_WINDOWS or not math.isfinite(initial_usd)
        or not 100 <= initial_usd <= 5000):
        raise HTTPException(422,"Invalid replay selection")
    if asset in EXTRA_REPLAY_ASSETS:
        samples=compact_replay_observations(extra_markets.history(asset,limit=3600))
    else:
        samples=market.history(asset,limit=3600)
    result=replay_strategy(asset,mode,horizon,samples,initial_usd=initial_usd)
    return JSONResponse(result,headers={"Cache-Control":"no-store",
                                         "X-Content-Type-Options":"nosniff"})


@app.get("/api/market/history/{asset}")
def price_history(asset: str, limit: int = 120):
    asset = asset.upper()
    if asset in ("BTC", "ETH"):
        points = market.history(asset, limit=limit)
    elif asset in EXTRA_ASSETS:
        points = extra_markets.history(asset, limit=limit)
    else:
        raise HTTPException(404, "Unsupported market asset")
    return {"asset": asset, "currency": "USDT", "points": points}


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
            await websocket.send_json(public_market_snapshot())
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


class NativeBtcQuoteRequest(BaseModel):
    """No user-selected destination, Bitcoin memo, wrapped token or provider."""
    model_config = ConfigDict(extra="forbid")
    side: Literal["BUY", "SELL"]
    stable: Literal["USDC", "USDT"]
    amount: str


@app.post("/api/live/btc/quote")
async def btc_native_crosschain_quote(
    request: NativeBtcQuoteRequest,
    authorization: str | None = Header(default=None),
):
    """Indicative native BTC ↔ Ethereum stable price, NEVER a deposit order."""
    did = verify_privy_bearer(authorization)["user_id"]
    price = await quote_native_btc(
        did,side=request.side,stable=request.stable,amount=request.amount)
    return JSONResponse(price,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


class EthLiveQuoteRequest(BaseModel):
    """Strict input: no client-selected wallet, recipient, chain or router."""
    model_config = ConfigDict(extra="forbid")
    side: Literal["BUY", "SELL"]
    stable: Literal["USDC", "USDT"]
    amount: str


class NativeManualSwapQuoteRequest(BaseModel):
    """Exact real provider quote only; wallet, venue and calldata are not inputs."""
    model_config = ConfigDict(extra="forbid")
    source: str
    target: str
    amount: str


@app.post("/api/wallet/swap/live-quote")
async def manual_wallet_swap_live_market_quote(
        request: NativeManualSwapQuoteRequest,
        authorization: str | None = Header(default=None)):
    """Live THORChain quote from verified public provider, no signing."""
    verify_privy_bearer(authorization)
    quote = await get_native_swap_quote(request.source, request.target,
                                        request.amount)
    return JSONResponse(quote, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/wallet/swap/route")
def manual_wallet_swap_route(source: str, target: str,
                             authorization: str | None = Header(default=None)):
    """One on-demand, authenticated static route check. No provider calls.

    Full source and destination chain IDs are server-defined; the request
    cannot choose a wallet, token contract, router, signer or recipient.
    """
    verify_privy_bearer(authorization)
    route = build_manual_swap_route(source, target)
    return JSONResponse(route, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/wallet/swap/wallet-readiness")
async def manual_wallet_swap_wallet_readiness(
        source: str, target: str,
        authorization: str | None = Header(default=None)):
    """One explicit user-requested inspection of source/destination wallet records.

    Privy identities and linked wallets come only from the server's verified
    user lookup. No client address/contract, signing, transfer or quote is used.
    """
    did = verify_privy_bearer(authorization)["user_id"]
    report = await get_manual_swap_wallet_readiness(did, source, target)
    return JSONResponse(report, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/readiness")
def all_live_networks_readiness(authorization: str | None = Header(default=None)):
    """Seven-chain LIVE preparation; never an operator activation endpoint."""
    verify_privy_bearer(authorization)
    return JSONResponse(get_global_live_readiness(), headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


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


@app.get("/api/live/eth/policy")
async def eth_privy_policy_status(authorization: str | None = Header(default=None)):
    """Read independently retrieved Privy policy rules, never allow enrollment."""
    did = verify_privy_bearer(authorization)["user_id"]
    review = await get_eth_policy_review(did)
    return JSONResponse(review, headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/eth/swap-plan")
async def eth_unsigned_swap_preview(
    request: EthLiveQuoteRequest,
    authorization: str | None = Header(default=None),
):
    """Provider-backed ETH swap plan: never returns calldata or executes."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await make_eth_swap_plan(
        did, side=request.side, stable=request.stable, amount=request.amount)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/eth/tx-receipt/{tx_hash}")
async def eth_live_chain_receipt(tx_hash: str,
                                 authorization: str | None = Header(default=None)):
    """Only explicit authenticated checks of a user's own Uniswap tx.

    On-chain success != verified swap output. No signer, send, polling,
    order creation or changes to existing BTC/ETH trade permissions.
    """
    did = verify_privy_bearer(authorization)["user_id"]
    result = await check_eth_swap_receipt(did, tx_hash)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/eth/auto-preflight")
async def eth_automatic_execution_readiness(
        authorization: str | None = Header(default=None)):
    """Unified Privy/policy/limits diagnostics. No grant or transaction path."""
    did = verify_privy_bearer(authorization)["user_id"]
    report = await get_eth_automatic_preflight(did)
    return JSONResponse(report, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/eth/enrollment-preflight")
async def eth_policy_enrollment_preflight(
    authorization: str | None = Header(default=None),
):
    """Wallet + LIVE limits readiness only; never creates a signer or grant.

    Authenticated DID is the only selection input. Return no draft body or
    authority until explicit user consent and verified token caps exist.
    """
    did = verify_privy_bearer(authorization)["user_id"]
    report = await get_eth_user_enrollment_preflight(did)
    return JSONResponse(report, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


class EthConsentSignatureRequest(BaseModel):
    """Only a wallet-produced EIP-191 proof, not a Privy delegation."""
    model_config = ConfigDict(extra="forbid")
    signature: str = Field(min_length=132, max_length=132, pattern=r"^0x[0-9a-fA-F]{130}$")


@app.post("/api/live/eth/consent/challenge")
async def eth_consent_challenge(
    authorization: str | None = Header(default=None),
):
    """Short-lived one-time user intent; does NOT enable trading."""
    identity = verify_privy_bearer(authorization)
    result = await prepare_eth_consent_challenge(
        identity["user_id"], identity["session_id"],
    )
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.post("/api/live/eth/consent/confirm")
async def eth_consent_confirm(
    body: EthConsentSignatureRequest,
    authorization: str | None = Header(default=None),
):
    """Store signed intent only; Privy additional signer still NOT granted."""
    identity = verify_privy_bearer(authorization)
    result = await confirm_eth_consent_intent(
        identity["user_id"], identity["session_id"], body.signature,
    )
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.post("/api/live/eth/consent/revoke")
def eth_consent_revoke(
    authorization: str | None = Header(default=None),
):
    """Atomic LOCAL intent revocation and MiniNex stop; not remote Privy removal."""
    identity = verify_privy_bearer(authorization)
    result = revoke_eth_consent_intent(identity["user_id"])
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/eth/policy-candidate")
async def eth_user_policy_candidate(
    authorization: str | None = Header(default=None),
):
    """Verified per-wallet ETH policy draft status; NO Privy API creation."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await get_eth_policy_candidate_preflight(did)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.post("/api/live/eth/policy/prepare")
async def eth_privy_policy_prepare(
    authorization: str | None = Header(default=None),
):
    """Persist inert wallet-specific candidate after matching signed intent."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await prepare_eth_policy_record(did)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/eth/policy/lifecycle")
async def eth_privy_policy_lifecycle(
    authorization: str | None = Header(default=None),
):
    """Read persisted user policy state. Never grants wallet authority."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await asyncio.to_thread(get_eth_policy_ledger_status, did)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/eth/policy/revocation-review")
async def eth_privy_policy_revocation_review(
    authorization: str | None = Header(default=None),
):
    """Verify remote signer absence; does NOT remove signer or policy."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await get_eth_policy_remote_revocation_review(did)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/eth/authorization")
async def eth_signer_authorization_status(authorization: str | None = Header(default=None)):
    """Independently inspect Privy signer + policy; never grant signer access."""
    did = verify_privy_bearer(authorization)["user_id"]
    status = await get_eth_signer_status(did)
    return JSONResponse(status, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/orders/{asset}")
def live_order_history(asset: str, limit: int = 30,
                       authorization: str | None = Header(default=None)):
    """Account-owned BTC/ETH LIVE order history; never creates an order."""
    did = verify_privy_bearer(authorization)["user_id"]
    data = get_live_order_history(did, asset=asset, limit=limit)
    return JSONResponse(data, headers={
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


@app.get("/api/home/paper-trades")
def personal_home_paper_trades(
    asset: str = "ALL",
    authorization: str | None = Header(default=None),
):
    """Caller-owned PAPER trades for selected coin or explicit ALL."""
    _require_paper_visible()
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(
        recent_home_trades(did,asset=asset),
        headers={"Cache-Control":"private, no-store",
                 "X-Content-Type-Options":"nosniff"},
    )


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

class ExtraPaperStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    budget_usd: StrictInt = Field(ge=100,le=5000)
    mode: Literal["LONG","SPOT_SHORT"] = "LONG"
    stable: Literal["USDC","USDT"] = "USDC"
    horizon: Literal["5m","15m","1h","24h"] = "15m"


@app.get("/api/extra-paper/comparison")
def extra_paper_comparison(authorization: str | None = Header(default=None)):
    """Private, read-only comparison; no coin settings or wallet permissions change."""
    _require_paper_visible()
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(
        compare_extra_paper(did,extra_markets),
        headers={"Cache-Control":"private, no-store",
                 "X-Content-Type-Options":"nosniff"})


@app.get("/api/extra-paper/{asset}")
def extra_paper_status(asset: str, authorization: str | None = Header(default=None)):
    """Account-owned virtual balance and history; no signing authority."""
    _require_paper_visible()
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(
        read_extra_paper(did,asset,extra_markets),
        headers={"Cache-Control":"private, no-store",
                 "X-Content-Type-Options":"nosniff"})


@app.post("/api/extra-paper/{asset}/start")
def extra_paper_start(asset: str, body: ExtraPaperStartRequest,
                      authorization: str | None = Header(default=None)):
    """Start only this account's selected coin using 100-5000 virtual USD."""
    _require_paper_visible()
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(
        start_extra_paper(did,asset,body.budget_usd,body.mode,
                          body.stable,body.horizon,extra_markets),
        headers={"Cache-Control":"private, no-store"})


@app.post("/api/extra-paper/{asset}/{action}")
def extra_paper_action(asset: str,action: str,
                       authorization: str | None = Header(default=None)):
    """Pause immediately or resume a named, opt-in PAPER coin session."""
    _require_paper_visible()
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(
        set_extra_paper_status(did,asset,action,extra_markets,time.time()),
        headers={"Cache-Control":"private, no-store"})


