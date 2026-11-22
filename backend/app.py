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
from .extra_coin_preferences import get_coin_preferences, save_coin_preference
from .unified_strategy_profile import get_strategy_profile, save_strategy_profile
from .tax_reports import list_verified_activity, make_csv, make_pdf, validate_year
from .blockchain_preview import fetch_preview, check_address
from .blockchain_history import fetch_history, validate_request
from .verified_sync import synchronize_one_page
from .wallet_balances import get_wallet_balances
from .multicoin_evm_balances import get_multicoin_evm_balances
from .native_coin_balances import get_native_balances
from .native_trade_receipts import get_owner_native_sender_receipt
from .native_privy_owner_signer import get_original_native_privy_signer_status
from .native_source_readiness import read_original_native_source_readiness
from .native_thor_buy_approval_plan import read_original_thor_buy_allowance
from .native_live_destination_finality import get_private_native_ethereum_destination
from .native_live_buy_destination import get_private_original_native_buy_destination
from .native_live_sell_refund import get_private_original_native_sell_refund
from .native_live_order_status import get_owner_native_live_order_status
from .native_live_auto_sessions import (
    get_owner_native_auto,pause_owner_native_auto,
)
from .native_live_eth_buy_source import get_original_eth_buy_source
from .native_thor_transaction_status import get_private_original_protocol_status
from .user_paper import read_user_paper, start_user_paper, set_user_paper_status, step_user_paper, add_paper_mark_to_market
from .personal_paper_worker import PersonalPaperWorker
from .eth_auto_live_sessions import EthAutoLiveWorker, start_eth_auto, pause_eth_auto, get_eth_auto_session
from .shared_evm_passive_settlement import PassiveEvmSettlementWorker
from .native_passive_settlement import PassiveNativeSettlementWorker
from .native_live_worker import NativeLiveWorker
from .native_grant_auditor import audit_native_owner_grant
from .shared_evm_live_worker import SharedEvmLiveWorker
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
from .eth_router02_live_quote import make_policy_compatible_eth_quote
from .eth_erc20_trade_quote import quote_owner_erc20_trade
from .native_evm_v3_quote import quote_native_evm_trade
from .shared_evm_swap_receipts import check_user_evm_swap
from .shared_evm_fill_history import get_owner_evm_fill_history
from .shared_evm_order_status import get_owner_live_evm_order_status
from .shared_evm_permission_status import read_owner_evm_permission_status
from .shared_evm_wallet_policy_status import read_owner_central_wallet_policy_status
from .shared_evm_wallet_policy_prepare import prepare_owner_central_evm_wallet_policy
from .shared_evm_wallet_policy_publish import publish_initial_owner_wallet_policy
from .shared_evm_wallet_policy_revoke import confirm_original_wallet_policy_revocation
from .shared_evm_wallet_policy_update import (
    apply_original_wallet_coin_extension,reconcile_original_wallet_extension,
)
from .shared_evm_wallet_policy_extend import (
    prepare_existing_wallet_coin_extension, confirm_existing_wallet_coin_extension,
)
from .shared_evm_wallet_policy_grant import (
    get_original_one_signer_grant_offer,confirm_original_one_signer_grant,
)
from .shared_evm_live_readiness import get_owner_evm_auto_readiness
from .shared_evm_remote_grant_audit import get_owner_evm_remote_grant_audit
from .shared_evm_policy_candidate_stage import stage_owner_evm_policy_from_budget
from .shared_evm_privy_policy_publish import publish_user_evm_privy_policy
from .shared_evm_privy_grant_confirm import confirm_owner_evm_privy_grant
from .shared_evm_trade_grant_offer import get_owner_trade_grant_offer
from .shared_evm_auto_sessions import (
    start_owner_evm_auto, pause_owner_evm_auto, get_owner_evm_auto,
)
from .shared_evm_fifo import read_evm_fifo_gross_preview
from .shared_evm_fee_policy_publish import publish_owner_signed_fee_policy
from .shared_evm_fee_auto_grant_confirm import confirm_owner_privy_fee_auto_grant
from .shared_evm_fee_grant_offer import get_owner_auto_fee_grant_offer
from .shared_evm_fee_auto_status import (
    owner_fee_auto_permission_status,revoke_owner_fee_auto_permission,
)
from .shared_evm_fee_auto_consent import (
    begin_owner_fee_auto_consent,confirm_owner_fee_auto_consent,
)
from .shared_evm_fee_payment import (
    prepare_owner_verified_evm_fee,confirm_owner_verified_evm_fee,
    get_owner_verified_evm_fee,
)
from .shared_evm_allowance import check_evm_source_allowance
from .shared_evm_swap_preparation import prepare_owner_evm_swap
from .eth_router02_allowance import inspect_router02_approval
from .eth_live_wallet_equity import read_owner_eth_live_wallet_value
from .eth_owner_auto_approval import make_eth_owner_approval_plan
from .eth_router02_receipts import check_user_router02_receipt
from .eth_weth9_receipts import check_user_weth9_receipt
from .shared_native_evm_receipts import check_user_native_evm_conversion
from .shared_evm_trade_cycle_proof import verify_evm_trade_cycle
from .eth_native_cycle_comparison import compare_eth_native_cycle
from .real_eth_trade_legs import record_real_eth_leg, get_real_eth_legs, get_confirmed_eth_swaps
from .eth_fifo_gross_preview import read_eth_fifo_gross_preview
from .eth_cost_preview import get_eth_readonly_cost_preview
from .live_eth_receipts import check_eth_swap_receipt
from .live_btc_quotes import quote_native_btc
from .live_btc_receipts import check_native_btc_transaction
from .live_eth_controls import get_live_eth_limits, save_live_eth_limits, stop_live_eth
from .live_btc_controls import get_live_btc_limits, save_live_btc_limits, stop_live_btc
from .privy_live_signer import get_eth_signer_status
from .privy_eth_policy import get_eth_policy_review
from .eth_auto_preflight import get_eth_automatic_preflight
from .eth_user_enrollment_preflight import get_eth_user_enrollment_preflight
from .eth_privy_policy_plan import get_eth_policy_candidate_preflight
from .eth_privy_policy_ledger import (
    prepare_eth_policy_record, get_eth_policy_ledger_status,
    get_eth_policy_remote_revocation_review,
)
from .eth_privy_signer_revocation import (
    get_privy_wallet_signer_absence, confirm_privy_wallet_signer_revocation,
)
from .eth_privy_policy_provisioner import publish_user_eth_privy_policy
from .eth_privy_user_grant import get_eth_user_grant_review, confirm_eth_user_grant
from .eth_live_ledger_audit import get_eth_live_ledger_audit
from .eth_prelaunch_safety import get_eth_prelaunch_safety_report
from .eth_privy_policy_incident import get_eth_privy_policy_incident_review
from .shared_evm_owner_consent import (
    prepare_evm_consent_challenge, confirm_evm_consent_intent,
    revoke_evm_consent,
)
from .eth_user_consent import (
    prepare_eth_consent_challenge, confirm_eth_consent_intent,
    revoke_eth_consent_intent,
)
from .live_orders import get_live_order_history
from .live_asset_registry import get_global_live_readiness
from .manual_swap_routes import build_manual_swap_route
from .manual_swap_live_quotes import get_native_swap_quote
from .live_trade_router import get_unified_trade_plan
from .manual_swap_wallet_readiness import get_manual_swap_wallet_readiness

market = MarketDataService()
extra_markets = ExtraMarketData()
paper = PaperAutoEngine(market)
personal_paper = PersonalPaperWorker(market,extra_markets)
eth_auto_live = EthAutoLiveWorker(market)
evm_passive_settlement = PassiveEvmSettlementWorker()
native_passive_settlement = PassiveNativeSettlementWorker()
evm_auto_live = SharedEvmLiveWorker(extra_markets)
# Native worker starts with the independent grant auditor
# that verifies wallet ownership and budget before any send.
native_auto_live = NativeLiveWorker(market, extra_markets, grant_auditor=audit_native_owner_grant)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await market.start()
    await extra_markets.start()
    if _paper_visible():
        await paper.start()
    if _paper_visible():
        await personal_paper.start()
    await eth_auto_live.start()
    await evm_auto_live.start()
    await native_auto_live.start()
    await evm_passive_settlement.start()
    await native_passive_settlement.start()
    try:
        yield
    finally:
        await native_passive_settlement.stop()
        await evm_passive_settlement.stop()
        await native_auto_live.stop()
        await evm_auto_live.stop()
        await eth_auto_live.stop()
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


@app.get("/api/wallet/native-balances")
async def authenticated_native_balances(
    authorization: str | None = Header(default=None),
):
    """Verified real SOL/XRP/TRX wallet holdings from their own networks."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(await get_native_balances(did),headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/native/auto-session/{asset}")
def read_owner_native_auto_session(
    asset: Literal["BTC","SOL","XRP","TRX","DOGE"],
    authorization: str | None = Header(default=None),
):
    """Per-owner native AUTO status; login never starts a transaction."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_owner_native_auto(did,asset),headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/native/auto-session/{asset}/pause")
def pause_owner_native_auto_session(
    asset: Literal["BTC","SOL","XRP","TRX","DOGE"],
    authorization: str | None = Header(default=None),
):
    """Stop new orders only; do not retry or cancel transactions on-chain."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(pause_owner_native_auto(did,asset),headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/native/order-status/{asset}")
async def read_owner_native_live_original_orders(
    asset: Literal["BTC","SOL","XRP","TRX","DOGE"],
    limit: int = 20,
    authorization: str | None = Header(default=None),
):
    """Current original owner journal status; never a paper trade or signer."""
    did=verify_privy_bearer(authorization)["user_id"]
    report=get_owner_native_live_order_status(did,asset=asset,limit=limit)
    return JSONResponse(report,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/native/authorization/{asset}")
async def read_original_native_privy_owner_authorization(
    asset: Literal["BTC","SOL","XRP"],
    authorization: str | None = Header(default=None),
):
    """Owner-only native Privy signer readback, never trade permission."""
    did=verify_privy_bearer(authorization)["user_id"]
    status=await get_original_native_privy_signer_status(did,asset=asset)
    return JSONResponse(status,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/native/source-receipt/{asset}/{transaction_hash}")
async def read_owner_finalized_native_source_receipt(
    asset: Literal["SOL","XRP"], transaction_hash: str,
    authorization: str | None = Header(default=None),
):
    """Authenticated source-chain observation, never claimed as swap payout."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await get_owner_native_sender_receipt(
        did,asset=asset,transaction_hash=transaction_hash)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/native/settlement/{asset}/{intent_key}")
async def read_owner_original_native_settlement(
    asset: Literal["BTC","SOL","XRP"],intent_key: str,
    authorization: str | None = Header(default=None),
):
    """Independent source+protocol+destination receipt; never a trading grant."""
    did=verify_privy_bearer(authorization)["user_id"]
    proof=await get_private_native_ethereum_destination(
        did,asset=asset,intent_key=intent_key)
    return JSONResponse(proof,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/native/buy-source/{asset}/{intent_key}")
async def read_owner_original_native_eth_buy_source(
    asset: Literal["BTC","SOL","XRP","TRX","DOGE"],intent_key: str,
    authorization: str | None = Header(default=None),
):
    """Read-only proof of original Ethereum THORChain stablecoin funding."""
    did=verify_privy_bearer(authorization)["user_id"]
    report=await get_original_eth_buy_source(
        did,asset=asset,intent_key=intent_key)
    return JSONResponse(report,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })



@app.get("/api/live/native/protocol-status/{asset}/{intent_key}")
async def read_owner_native_protocol_progress(
    asset: Literal["BTC","SOL","XRP","TRX","DOGE"],intent_key: str,
    authorization: str | None = Header(default=None),
):
    """Read THORChain source/outbound status; never claim native payout."""
    did=verify_privy_bearer(authorization)["user_id"]
    report=await get_private_original_protocol_status(
        did,asset=asset,intent_key=intent_key)
    return JSONResponse(report,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/native/buy-settlement/{asset}/{intent_key}")
async def read_owner_original_native_buy_settlement(
    asset: Literal["BTC","SOL","XRP","TRX","DOGE"],intent_key: str,
    authorization: str | None = Header(default=None),
):
    """Owner-only destination-chain BUY readback; never a signing endpoint."""
    did=verify_privy_bearer(authorization)["user_id"]
    proof=await get_private_original_native_buy_destination(
        did,asset=asset,intent_key=intent_key)
    return JSONResponse(proof,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/native/sell-refund/{asset}/{intent_key}")
async def read_owner_original_native_sell_refund(
    asset: Literal["BTC","SOL","XRP"],intent_key: str,
    authorization: str | None = Header(default=None),
):
    """Original chain refund readback; never a swap-fill or fee record."""
    did=verify_privy_bearer(authorization)["user_id"]
    proof=await get_private_original_native_sell_refund(
        did,asset=asset,intent_key=intent_key)
    return JSONResponse(proof,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/wallet/multicoin-balances")
async def authenticated_multicoin_balances(
    authorization: str | None = Header(default=None),
):
    """Actual BNB/POL/AVAX/LINK/UNI/ARB holdings on each correct blockchain."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await get_multicoin_evm_balances(did)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


class UnifiedLiveTradeRequest(BaseModel):
    """One exact input-asset quantity, no client-selected router or address."""
    model_config = ConfigDict(extra="forbid")
    side: Literal["BUY", "SELL"]
    stable: Literal["USDC", "USDT"]
    amount: str


@app.post("/api/live/trade-plan/{asset}")
async def unified_real_market_trade_plan(
    asset: str, request: UnifiedLiveTradeRequest,
    authorization: str | None = Header(default=None),
):
    """Owner-bound read-only quote route for all twelve coins; never sends funds."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await get_unified_trade_plan(
        did,asset=asset,side=request.side,stable=request.stable,
        amount=request.amount,
    )
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/native/source-readiness/{asset}")
async def inspect_owner_native_source_readiness(
    asset: Literal["BTC", "SOL", "XRP", "TRX", "DOGE"],
    request: UnifiedLiveTradeRequest,
    authorization: str | None = Header(default=None),
):
    """Authenticated read-only original source preflight, never an order."""
    did = verify_privy_bearer(authorization)["user_id"]
    status = await read_original_native_source_readiness(
        did, asset=asset, side=request.side,
        stable=request.stable, amount=request.amount,
    )
    return JSONResponse(status, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.post("/api/live/native/buy-approval/{asset}")
async def inspect_owner_native_buy_router_approval(
    asset: Literal["BTC", "SOL", "XRP","TRX","DOGE"],
    request: UnifiedLiveTradeRequest,
    authorization: str | None = Header(default=None),
):
    """Read exact first-buy USDC/USDT allowance; never signs or sends."""
    did=verify_privy_bearer(authorization)["user_id"]
    if request.side!="BUY":
        raise HTTPException(422,"Only original native BUY uses an ETH router approval")
    report=await read_original_thor_buy_allowance(
        did,asset=asset,stable=request.stable,amount=request.amount)
    return JSONResponse(report,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
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
    max_order_usd: str | None = None
    max_daily_turnover_usd: str | None = None
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


@app.post("/api/live/eth/router02-allowance")
async def eth_verified_token_allowance(
    request: EthLiveQuoteRequest,
    authorization: str | None = Header(default=None),
):
    """Owner-only onchain WETH/stable router allowance, not token approval."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await inspect_router02_approval(
        did,side=request.side,stable=request.stable,amount=request.amount)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/eth/owner-approval-plan")
async def eth_owner_live_auto_allowance(
    token: Literal["USDC","USDT","WETH"],
    authorization: str | None = Header(default=None),
):
    """Provide only bounded, owner-wallet-signable ERC20 approve calldata."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await make_eth_owner_approval_plan(did,token=token)
    return JSONResponse(
        result,headers={"Cache-Control":"private, no-store",
                        "X-Content-Type-Options":"nosniff"})


@app.post("/api/live/evm/owner-swap-preparation/{asset}")
async def owner_evm_swap_preparation(
    asset: Literal["ETH","BNB","POL","AVAX","LINK","UNI","ARB","ARB"],
    request: EthLiveQuoteRequest,
    authorization: str | None = Header(default=None),
):
    """Join current owner wallet quote, allowance and exact swap ABI.

    No server-side signing, wallet transfer, or trading state mutation.
    """
    did=verify_privy_bearer(authorization)["user_id"]
    result=await prepare_owner_evm_swap(
        did,asset=asset,side=request.side,stable=request.stable,
        amount=request.amount)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/evm/fifo-preview/{asset}")
def owner_evm_fifo_preview(
    asset: Literal["ETH","BNB","POL","AVAX","LINK","UNI","ARB","ARB"],
    authorization: str | None = Header(default=None),
):
    """Read-only verified FIFO gross cost basis; never net USD or a fee charge."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(read_evm_fifo_gross_preview(did,asset),
        headers={"Cache-Control":"private, no-store",
                 "X-Content-Type-Options":"nosniff"})


class EvmFeePaymentConfirmRequest(BaseModel):
    tx_hash: str = Field(min_length=66,max_length=66)


class EvmAutoFeeOwnerIntentRequest(BaseModel):
    """Only owner's fee-per-transfer choice and policy expiry are supplied."""
    model_config=ConfigDict(extra="forbid")
    max_fee_atomic: str = Field(min_length=1,max_length=45,
                                pattern=r"^[1-9][0-9]*$")
    policy_expires_unix: StrictInt = Field(gt=0)


@app.post("/api/live/evm/fee-auto-consent/{asset}/challenge")
async def owner_evm_auto_fee_consent_challenge(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    request: EvmAutoFeeOwnerIntentRequest,
    authorization: str | None = Header(default=None),
):
    """Explicit separate fee scope signature; never publish or attach signer."""
    identity=verify_privy_bearer(authorization)
    result=await begin_owner_fee_auto_consent(
        did=identity["user_id"],session_id=identity["session_id"],
        asset=asset,max_fee_atomic=request.max_fee_atomic,
        expires_at=request.policy_expires_unix)
    return JSONResponse(result,headers={"Cache-Control":"private, no-store"})


class EvmAutoFeeConfirmIntentRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    signature: str = Field(
        min_length=132,max_length=132,
        pattern=r"^0x[0-9a-fA-F]{130}$")


@app.post("/api/live/evm/fee-auto-consent/{asset}/confirm")
async def owner_evm_auto_fee_consent_confirm(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    request: EvmAutoFeeConfirmIntentRequest,
    authorization: str | None = Header(default=None),
):
    """User-signed EIP191 proof; still no Privy policy or wallet grant."""
    identity=verify_privy_bearer(authorization)
    result=await confirm_owner_fee_auto_consent(
        did=identity["user_id"],session_id=identity["session_id"],
        asset=asset,signature=request.signature)
    return JSONResponse(result,headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/evm/fee-auto-consent/{asset}/publish-policy")
async def owner_evm_fee_auto_policy_publish(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Owner-initiated once-only Privy fee policy; signer not attached."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await publish_owner_signed_fee_policy(did=did,asset=asset)
    return JSONResponse(result,headers={"Cache-Control":"private, no-store"})


@app.get("/api/live/evm/fee-auto-consent/{asset}/grant-review")
async def owner_evm_fee_auto_grant_review(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Owner-only exact Privy fee signer offer; no wallet delegation."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await get_owner_auto_fee_grant_offer(did=did,asset=asset)
    return JSONResponse(result,headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/evm/fee-auto-consent/{asset}/grant-confirm")
async def owner_evm_fee_auto_grant_confirm(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Confirm exact remote owner addSigners fee grant, never create it."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await confirm_owner_privy_fee_auto_grant(did=did,asset=asset)
    return JSONResponse(result,headers={"Cache-Control":"private, no-store"})


@app.get("/api/live/evm/fee-auto-consent/{asset}")
def owner_evm_auto_fee_permission_status(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Only this authenticated owner can read their own separate fee grant."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(
        owner_fee_auto_permission_status(did=did,asset=asset),
        headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/evm/fee-auto-consent/{asset}/revoke")
def owner_evm_auto_fee_permission_revoke(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Owner immediately revokes MiniNex local fee permission; Privy remote separate."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(
        revoke_owner_fee_auto_permission(did=did,asset=asset),
        headers={"Cache-Control":"private, no-store"})


@app.get("/api/live/evm/fee-payment/{asset}/{execution_id}")
def owner_real_evm_fee_payment_status(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    execution_id: str,
    authorization: str | None = Header(default=None),
):
    """Owner-only: display real HWM fee obligation and paid blockchain state."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_owner_verified_evm_fee(
        did=did,asset=asset,execution_id=execution_id),
        headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/evm/fee-payment/{asset}/{execution_id}/prepare")
async def owner_prepare_signed_evm_fee_payment(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    execution_id: str,
    authorization: str | None = Header(default=None),
):
    """Prepare exact unsigned stablecoin transfer; user must sign in Privy."""
    did=verify_privy_bearer(authorization)["user_id"]
    draft=await prepare_owner_verified_evm_fee(
        did=did,asset=asset,execution_id=execution_id)
    return JSONResponse(draft,headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/evm/fee-payment/{asset}/{execution_id}/confirm")
async def owner_confirm_signed_evm_fee_payment(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    execution_id: str,
    request: EvmFeePaymentConfirmRequest,
    authorization: str | None = Header(default=None),
):
    """Credit only canonical final ERC-20 transfer, never a user-supplied claim."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await confirm_owner_verified_evm_fee(
        did=did,asset=asset,execution_id=execution_id,
        tx_hash=request.tx_hash)
    return JSONResponse(result,headers={"Cache-Control":"private, no-store"})


@app.get("/api/live/evm/wallet-policy/grant-offer")
async def owner_one_signer_central_evm_grant_offer(
    authorization: str | None = Header(default=None),
):
    """Independently verify original one-signer grant before Privy UI."""
    did=verify_privy_bearer(authorization)["user_id"]
    report=await get_original_one_signer_grant_offer(did)
    return JSONResponse(report,headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/evm/wallet-policy/confirm-grant")
async def owner_confirm_one_signer_central_evm_grant(
    authorization: str | None = Header(default=None),
):
    """Independently re-read actual Privy addSigners result, then persist."""
    did=verify_privy_bearer(authorization)["user_id"]
    report=await confirm_original_one_signer_grant(did)
    return JSONResponse(report,headers={"Cache-Control":"private, no-store"})


class CentralWalletScopeSignatureRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    nonce: str = Field(min_length=64,max_length=64,pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(
        min_length=132,max_length=132,pattern=r"^0x[0-9a-fA-F]{130}$")


class CentralEvmScopeApplyRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    nonce: str = Field(min_length=64,max_length=64,pattern=r"^[0-9a-f]{64}$")


@app.post("/api/live/evm/wallet-policy/confirm-revocation")
async def owner_confirm_central_evm_wallet_signer_removed(
    authorization: str | None = Header(default=None),
):
    """Confirm Privy user-controlled signer removal, stop all future EVM orders."""
    did=verify_privy_bearer(authorization)["user_id"]
    report=await confirm_original_wallet_policy_revocation(did)
    return JSONResponse(report,headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/evm/wallet-policy/extend/reconcile")
async def owner_reconcile_original_wallet_policy_update(
    request: CentralEvmScopeApplyRequest,
    authorization: str | None = Header(default=None),
):
    """GET Privy only; finish previously signed update without new PATCH."""
    did=verify_privy_bearer(authorization)["user_id"]
    report=await reconcile_original_wallet_extension(
        did,nonce=request.nonce,
    )
    return JSONResponse(report,headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/evm/wallet-policy/extend/apply")
async def owner_apply_one_signed_wallet_scope_change(
    request: CentralEvmScopeApplyRequest,
    authorization: str | None = Header(default=None),
):
    """Only an EIP-191-confirmed original wallet policy may be PATCHed."""
    did=verify_privy_bearer(authorization)["user_id"]
    report=await apply_original_wallet_coin_extension(
        did,nonce=request.nonce,
    )
    return JSONResponse(report,headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/evm/wallet-policy/extend/{asset}/prepare")
async def owner_prepare_shared_evm_scope_extension(
    asset: Literal["ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    did=verify_privy_bearer(authorization)["user_id"]
    result=await prepare_existing_wallet_coin_extension(did,asset)
    return JSONResponse(result,headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/evm/wallet-policy/extend/confirm")
async def owner_confirm_shared_evm_scope_extension(
    request: CentralWalletScopeSignatureRequest,
    authorization: str | None = Header(default=None),
):
    did=verify_privy_bearer(authorization)["user_id"]
    result=confirm_existing_wallet_coin_extension(
        did,nonce=request.nonce,signature=request.signature,
    )
    return JSONResponse(result,headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/evm/wallet-policy/publish")
async def owner_publish_first_central_evm_wallet_policy(
    authorization: str | None = Header(default=None),
):
    """Publish a restrictive wallet-wide draft once, without granting signer.

    Only the original signed-in wallet owner may request first publication.
    This endpoint does not enable a trading session and cannot send funds.
    """
    did=verify_privy_bearer(authorization)["user_id"]
    status=await publish_initial_owner_wallet_policy(did)
    return JSONResponse(status,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/evm/wallet-policy/prepare/{asset}")
async def owner_prepare_first_shared_evm_wallet_policy(
    asset: Literal["ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Explicit original owner's initial central signer POLICY DRAFT only.

    Reads saved owner-selected coin capital server-side and never sends,
    attaches, publishes or changes a Privy signer. Existing legacy signers
    are not silently migrated.
    """
    did=verify_privy_bearer(authorization)["user_id"]
    result=await prepare_owner_central_evm_wallet_policy(did,asset)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/evm/wallet-policy/status")
async def original_owner_one_signer_wallet_policy_status(
    authorization: str | None = Header(default=None),
):
    """Authenticated READ-ONLY original one-signer policy verification.

    Cannot create/update/attach a Privy signer or activate trading.
    Current legacy single-coin LIVE routes are unaffected.
    """
    did=verify_privy_bearer(authorization)["user_id"]
    status=await read_owner_central_wallet_policy_status(did)
    return JSONResponse(status,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/evm/wallet-policy/permission-status/QNT")
async def owner_one_signer_qnt_permission_status(
    authorization: str | None = Header(default=None),
):
    """QNT has no legacy coin signer row. Report only real central proof."""
    did=verify_privy_bearer(authorization)["user_id"]
    status=await read_owner_central_wallet_policy_status(did)
    approved=(status.get("status")=="REMOTE_VERIFIED_READONLY"
        and "QNT" in status.get("scope_coins",[])
        and status.get("one_scoped_signer_verified") is True
        and status.get("remote_policy_exactly_verified") is True
        and status.get("backend_quorum_current") is True
        and status.get("current_user_budgets_verified") is True
        and status.get("legacy_reconciliation_required") is False)
    response={
        "mode":"OWNER_EVM_PRIVY_PERMISSION_STATUS_ONLY",
        "asset":"QNT","chain_id":1,"network":"ethereum-mainnet",
        "status":("GRANT_VERIFIED" if approved
                  else "REVOKED" if status.get("status")=="REVOKED"
                  else "NOT_PREPARED"),
        "authorization_method":"PRIVY_CENTRAL_ONE_SIGNER",
        "policy_published":approved,
        "signer_grant_recorded":approved,
        "auto_execution_enabled":False,
        "can_submit":False,
    }
    return JSONResponse(response,headers={"Cache-Control":"private, no-store"})


@app.get("/api/live/evm/auto-session/{asset}")
def owner_evm_auto_session(
    asset: Literal["ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Read-only per-coin user LIVE session, never implied by login."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_owner_evm_auto(did,asset),headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/evm/wallet-policy/auto-session/{asset}/start")
async def owner_start_one_signer_evm_auto_session(
    asset: Literal["ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Explicit owner Start Trade after original one-signer remote audit."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await start_owner_evm_auto(did,asset,central=True)
    return JSONResponse(result,headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/evm/auto-session/{asset}/start")
async def owner_start_evm_auto_session(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Only this user's explicit action after fresh remote Privy approval."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(await start_owner_evm_auto(did,asset),headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/evm/auto-session/{asset}/pause")
def owner_pause_evm_auto_session(
    asset: Literal["ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Stops future per-coin sends without retrying an in-flight tx."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(pause_owner_evm_auto(did,asset),headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/evm/policy/{asset}/grant-review")
async def owner_evm_trading_grant_review(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Read-only owner-signed exact Privy trading policy offer."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await get_owner_trade_grant_offer(did=did,asset=asset)
    return JSONResponse(result,headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/evm/policy/{asset}/confirm-grant")
async def owner_confirm_evm_privy_signer_grant(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Read back the owner's *existing* Privy signer approval; never grant it."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await confirm_owner_evm_privy_grant(did,asset)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/evm/policy/{asset}/publish")
async def owner_publish_evm_privy_policy(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Owner explicitly publishes signed policy; no Privy wallet delegation."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await publish_user_evm_privy_policy(did,asset)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/evm/policy/{asset}/prepare-grant")
async def owner_prepare_evm_policy_single_wallet_grant(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Build a restricted owner policy without an extra EIP-191 signature.

    This request does not delegate Privy or authorize ANY trade. The
    wallet owner subsequently approves the exact policy via addSigners.
    """
    did=verify_privy_bearer(authorization)["user_id"]
    result=await stage_owner_evm_policy_from_budget(
        did,asset,single_privy_grant=True)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/evm/policy/{asset}/prepare")
async def owner_prepare_evm_policy(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Owner-requested local draft; no remote Privy policy or wallet grant."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await stage_owner_evm_policy_from_budget(did,asset)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/evm/remote-grant-audit/{asset}")
async def owner_evm_remote_grant_audit(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Re-read Privy policy and scope; never creates a wallet grant."""
    identity=verify_privy_bearer(authorization)
    result=await get_owner_evm_remote_grant_audit(
        identity["user_id"],asset)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/evm/auto-readiness/{asset}")
async def owner_evm_auto_readiness(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Independently review owner Privy wallet, grant journal and quorum."""
    did=verify_privy_bearer(authorization)["user_id"]
    report=await get_owner_evm_auto_readiness(did,asset)
    return JSONResponse(report,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/evm/permission-status/{asset}")
def owner_evm_permission_status(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Read-only Privy permission ledger for this authenticated owner and coin."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(
        read_owner_evm_permission_status(did,asset=asset),
        headers={"Cache-Control":"private, no-store",
                 "X-Content-Type-Options":"nosniff"})


@app.get("/api/live/evm/order-status/{asset}")
def owner_evm_order_status(
    asset: Literal["ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"],
    limit: int = 20,
    authorization: str | None = Header(default=None),
):
    """Only the authenticated Privy owner's durable per-coin order states."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(
        get_owner_live_evm_order_status(did,asset=asset,limit=limit),
        headers={"Cache-Control":"private, no-store",
                 "X-Content-Type-Options":"nosniff"})


@app.get("/api/live/evm/verified-fills/{asset}")
def owner_finalized_evm_fills(
    asset: Literal["ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"],
    limit: int = 30,
    authorization: str | None = Header(default=None),
):
    """Real-only confirmed trades, independently isolated per Privy wallet and coin."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_owner_evm_fill_history(did,asset=asset,limit=limit),
        headers={"Cache-Control":"private, no-store",
                 "X-Content-Type-Options":"nosniff"})


@app.get("/api/live/evm/source-allowance/{asset}")
async def read_owner_evm_source_allowance(
    asset: Literal["ETH","BNB","POL","AVAX","LINK","UNI","ARB","ARB"],
    side: Literal["BUY","SELL"],
    stable: Literal["USDC","USDT"],
    amount: str,
    authorization: str | None = Header(default=None),
):
    """One on-chain source/allowance inspection for all EVM trade assets."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await check_evm_source_allowance(
        did,asset=asset,side=side,stable=stable,amount=amount)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/evm/finalized-swap/{asset}/{tx_hash}")
async def finalized_owner_evm_swap(
    asset: Literal["ETH","BNB","POL","AVAX","LINK","UNI","ARB","ARB"],
    tx_hash: str,
    side: Literal["BUY","SELL"],
    stable: Literal["USDC","USDT"],
    authorization: str | None = Header(default=None),
):
    """Read verified owner wallet swap transfers; no order or fee credit."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await check_user_evm_swap(
        did,tx_hash,asset=asset,side=side,stable=stable)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/evm/native-user-transaction/{asset}")
async def owner_native_evm_router02_user_transaction(
    asset: Literal["BNB","POL","AVAX"],
    request: EthLiveQuoteRequest,
    authorization: str | None = Header(default=None),
):
    """Owner-signed only after approved ERC20 allowance and native wrap/unwrap.

    Does not send, execute AUTO, or claim complete native coin settlement.
    """
    did=verify_privy_bearer(authorization)["user_id"]
    result=await quote_native_evm_trade(
        did,asset=asset,side=request.side,stable=request.stable,
        amount=request.amount,include_user_transaction=True,
    )
    if (result.get("wallet_identity_verified") is not True
            or result.get("onchain_quote_verified") is not True
            or result.get("transaction",{}).get("chainId")!=result.get("chain_id")
            or result.get("execution_enabled") is not False):
        raise HTTPException(502,"Native EVM quote identity mismatch")
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/evm/erc20-user-transaction/{asset}")
async def owner_link_uni_router02_user_transaction(
    asset: Literal["LINK","UNI","ARB"],
    request: EthLiveQuoteRequest,
    authorization: str | None = Header(default=None),
):
    """Wallet-owner-signable, chain-bound ERC-20 calldata, no server broadcast.

    Caller signs separately using its Privy wallet and checks approvals plus
    finality; these data are never a claimed LIVE AUTO fill or HWM event.
    """
    did=verify_privy_bearer(authorization)["user_id"]
    # ARB is an ERC-20 on Arbitrum One, NOT an Ethereum token:
    # route through chain-specific verified Router02/Quoter and reject any
    # cross-chain quote. LINK/UNI keep the original Ethereum quote.
    quote_fn=quote_native_evm_trade if asset=="ARB" else quote_owner_erc20_trade
    result=await quote_fn(
        did,asset=asset,side=request.side,stable=request.stable,
        amount=request.amount,include_user_transaction=True,
    )
    expected_chain=42161 if asset=="ARB" else 1
    if (result.get("wallet_identity_verified") is not True
            or result.get("onchain_quote_verified") is not True
            or result.get("chain_id")!=expected_chain
            or result.get("transaction",{}).get("chainId")!=expected_chain
            or result.get("execution_enabled") is not False):
        raise HTTPException(502,"Owner ERC-20 quote identity mismatch")
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/eth/router02-user-transaction")
async def eth_router02_user_wallet_transaction(
    request: EthLiveQuoteRequest,
    authorization: str | None = Header(default=None),
):
    """Actual owner-wallet-signable Router02 trade calldata; never signs for user.

    This is an ERC20/WETH leg only. The app must handle token allowance,
    native WETH9 conversion and finalized receipts as separate real steps.
    """
    did = verify_privy_bearer(authorization)["user_id"]
    result = await make_policy_compatible_eth_quote(
        did, side=request.side, stable=request.stable,
        amount=request.amount, include_user_transaction=True,
    )
    if (result.get("wallet_identity_verified") is not True
            or result.get("unsigned_swap_validated") is not True
            or result.get("transaction", {}).get("chainId") != 1):
        raise HTTPException(502, "Executable ETH transaction was not verified")
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.post("/api/live/eth/router02-quote")
async def eth_swaprouter02_quote_preview(
    request: EthLiveQuoteRequest,
    authorization: str | None = Header(default=None),
):
    """Real onchain V3 quote + prepared matching ABI, not a signed order.

    Stable/WETH quote is separate from the final native ETH wrap/unwrap.
    """
    did=verify_privy_bearer(authorization)["user_id"]
    result=await make_policy_compatible_eth_quote(
        did,side=request.side,stable=request.stable,amount=request.amount)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/eth/cost-preview")
async def eth_readonly_cost_preview(
    request: EthLiveQuoteRequest,
    authorization: str | None = Header(default=None),
):
    """User-scoped indicative ETH costs; never an executable order."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await get_eth_readonly_cost_preview(
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


@app.get("/api/live/evm/completed-cycle/{asset}/{swap_tx_hash}")
async def owner_verified_evm_trade_cycle(
    asset: Literal["ETH","BNB","POL","AVAX","LINK","UNI","ARB","ARB"],
    swap_tx_hash: str,
    side: Literal["BUY","SELL"],
    stable: Literal["USDC","USDT"],
    native_tx_hash: str | None = None,
    authorization: str | None = Header(default=None),
):
    """Independent owner-verified read-only swap/native receipt pairing."""
    did=verify_privy_bearer(authorization)["user_id"]
    native=asset in ("ETH","BNB","POL","AVAX")
    if (native and not native_tx_hash) or (not native and native_tx_hash):
        raise HTTPException(422,"Expected exactly the native conversion for this asset")
    swap=await check_user_evm_swap(
        did,swap_tx_hash,asset=asset,side=side,stable=stable)
    conversion=(await check_user_native_evm_conversion(
        did,native_tx_hash,asset=asset,
        operation="WITHDRAW" if side=="BUY" else "DEPOSIT")
        if native else None)
    try:
        evidence=verify_evm_trade_cycle(
            asset=asset,side=side,stable=stable,
            wallet=swap.get("wallet_address"),
            swap_proof=swap,native_proof=conversion)
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Finalized EVM trade cycle not independently verified") from None
    return JSONResponse(evidence,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/evm/native-conversion-receipt/{asset}/{tx_hash}")
async def owner_native_evm_conversion_receipt(
    asset: Literal["ETH","BNB","POL","AVAX"],
    tx_hash: str,
    operation: Literal["DEPOSIT","WITHDRAW"],
    authorization: str | None = Header(default=None),
):
    """Confirm independently finalized owner-only native conversion; no send."""
    did=verify_privy_bearer(authorization)["user_id"]
    report=await check_user_native_evm_conversion(
        did,tx_hash,asset=asset,operation=operation)
    return JSONResponse(report,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/eth/weth9-receipt/{tx_hash}")
async def eth_weth9_real_conversion_receipt(
    tx_hash: str,
    operation: Literal["DEPOSIT","WITHDRAW"],
    authorization: str | None = Header(default=None),
):
    """Check owned native ETH wrap/unwrap separately from Router02 swap."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await check_user_weth9_receipt(
        did,tx_hash,operation=operation)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/eth/router02-receipt/{tx_hash}")
async def eth_router02_real_swap_leg_receipt(
    tx_hash: str,
    side: Literal["BUY","SELL"],
    stable: Literal["USDC","USDT"],
    authorization: str | None = Header(default=None),
):
    """Real finalized SwapRouter02 ERC20 fills; never claims native ETH exit."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await check_user_router02_receipt(
        did,tx_hash,side=side,stable=stable)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.get("/api/live/eth/native-cycle-compare")
async def eth_compare_verified_native_cycle(
    swap_tx_hash: str,
    conversion_tx_hash: str,
    side: Literal["BUY", "SELL"],
    stable: Literal["USDC", "USDT"],
    authorization: str | None = Header(default=None),
):
    """Owner-authenticated dual-receipt diagnostic, NOT an order settlement.

    Each independent reader resolves the wallet from the SAME Privy DID and
    verifies its finalized on-chain receipt. Never credits a portfolio.
    """
    did = verify_privy_bearer(authorization)["user_id"]
    if swap_tx_hash.lower() == conversion_tx_hash.lower():
        raise HTTPException(422, "Swap and conversion hashes must differ")
    swap = await check_user_router02_receipt(
        did, swap_tx_hash, side=side, stable=stable)
    conversion = await check_user_weth9_receipt(
        did, conversion_tx_hash,
        operation="WITHDRAW" if side == "BUY" else "DEPOSIT")
    try:
        result = compare_eth_native_cycle(
            swap=swap, conversion=conversion, side=side, stable=stable)
    except ValueError:
        raise HTTPException(
            409, "ETH cycle receipts do not prove matching finalized legs"
        ) from None
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


class EthVerifiedLegRequest(BaseModel):
    tx_hash: str
    side: Literal["BUY","SELL"]
    stable: Literal["USDC","USDT"]
    kind: Literal["SWAP","WRAP","UNWRAP"]


@app.post("/api/live/eth/record-finalized-leg")
async def record_finalized_eth_trade_leg(
    request: EthVerifiedLegRequest,
    authorization: str | None = Header(default=None),
):
    """Persist one actually finalized owner-wallet transaction, no paper fills."""
    did=verify_privy_bearer(authorization)["user_id"]
    record=await record_real_eth_leg(
        did,tx_hash=request.tx_hash,side=request.side,
        stable=request.stable,kind=request.kind)
    return JSONResponse(record,headers={"Cache-Control":"private, no-store"})


class EthAutoSessionStart(BaseModel):
    stable: Literal["USDC","USDT"]


@app.get("/api/live/eth/auto/session")
def eth_auto_session_status(
    authorization: str | None = Header(default=None),
):
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_eth_auto_session(did),
                        headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/eth/auto/start")
async def eth_auto_start(
    request: EthAutoSessionStart,
    authorization: str | None = Header(default=None),
):
    did=verify_privy_bearer(authorization)["user_id"]
    result=await start_eth_auto(did,request.stable)
    return JSONResponse(result,headers={"Cache-Control":"private, no-store"})


@app.post("/api/live/eth/auto/pause")
def eth_auto_pause(
    authorization: str | None = Header(default=None),
):
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(pause_eth_auto(did),
                        headers={"Cache-Control":"private, no-store"})


@app.get("/api/live/eth/real-wallet-value")
async def eth_wallet_real_network_equity(
    authorization: str | None = Header(default=None),
):
    """Real ETH/WETH/stable balance totals, with no fake PnL or PAPER value."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(
        await read_owner_eth_live_wallet_value(did),
        headers={"Cache-Control":"private, no-store"})


@app.get("/api/live/eth/gross-spread-preview")
def eth_fifo_realized_gross_spread_preview(
    authorization: str | None = Header(default=None),
):
    """Read-only FIFO gross spread; no net USD PnL or performance fee."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(read_eth_fifo_gross_preview(did),
                        headers={"Cache-Control":"private, no-store"})


@app.get("/api/live/eth/confirmed-swaps")
def eth_confirmed_buy_sell_history(
    authorization: str | None = Header(default=None),
):
    """Actual confirmed ETH BUY/SELL fills, kept separate from native stages."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_confirmed_eth_swaps(did),
                        headers={"Cache-Control":"private, no-store"})


@app.get("/api/live/eth/recorded-legs")
def eth_recorded_onchain_trades(
    authorization: str | None = Header(default=None),
):
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_real_eth_legs(did),
                        headers={"Cache-Control":"private, no-store"})


@app.get("/api/live/btc/tx-receipt/{tx_hash}")
async def btc_live_chain_receipt(
    tx_hash: str, authorization: str | None = Header(default=None),
):
    """Owner-scoped BTC UTXO confirmation status, NOT a crosschain swap fill."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=await check_native_btc_transaction(did,tx_hash)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
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


@app.post("/api/live/evm/consent/{asset}/challenge")
async def owner_evm_consent_challenge(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """One user-initiated wallet-signable coin intent. Does NOT delegate."""
    identity=verify_privy_bearer(authorization)
    result=await prepare_evm_consent_challenge(
        identity["user_id"],identity["session_id"],asset)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/evm/consent/{asset}/confirm")
async def owner_evm_consent_confirm(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    body: EthConsentSignatureRequest,
    authorization: str | None = Header(default=None),
):
    """Verify exact EIP-191 owner signature; not a wallet signer grant."""
    identity=verify_privy_bearer(authorization)
    result=await confirm_evm_consent_intent(
        identity["user_id"],identity["session_id"],asset,body.signature)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/evm/consent/{asset}/revoke")
def owner_evm_consent_revoke(
    asset: Literal["BNB","POL","AVAX","LINK","UNI","ARB"],
    authorization: str | None = Header(default=None),
):
    """Locally stop future coin orders; Privy remote signer revoke remains separate."""
    identity=verify_privy_bearer(authorization)
    result=revoke_evm_consent(identity["user_id"],asset)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff",
    })


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


@app.post("/api/live/eth/policy/prepare-grant")
async def eth_privy_policy_prepare_single_grant(
    authorization: str | None = Header(default=None),
):
    """Stage restricted ETH owner policy without a second EIP191 signature.

    No wallet authority is delegated: Privy's owner addSigners approval
    is still mandatory before any source transaction can be signed.
    """
    did=verify_privy_bearer(authorization)["user_id"]
    result=await prepare_eth_policy_record(
        did,single_privy_grant=True)
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
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


@app.get("/api/live/eth/ledger-audit")
async def eth_live_reserved_capital_audit(
    authorization: str | None = Header(default=None),
):
    """User-owned, read-only rolling 24h ETH LIVE ledger reconciliation."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await asyncio.to_thread(get_eth_live_ledger_audit, did)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/eth/prelaunch-safety")
async def eth_live_prelaunch_readiness(
    authorization: str | None = Header(default=None),
):
    """Never authorizes live trades; reports detailed fail-closed blockers."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await get_eth_prelaunch_safety_report(did)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/eth/policy/incident-review")
async def eth_privy_policy_incident_review(
    authorization: str | None = Header(default=None),
):
    """Owner-bound read-only Privy recovery; no policy POST/retry or delegation."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await get_eth_privy_policy_incident_review(did)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.post("/api/live/eth/policy/create")
async def eth_create_scoped_privy_policy(
    authorization: str | None = Header(default=None),
):
    """One user-initiated, signed-intent-gated, owner-bound Privy policy."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await publish_user_eth_privy_policy(did)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/eth/policy/grant-review")
async def eth_scoped_privy_grant_review(
    authorization: str | None = Header(default=None),
):
    """Only independently verified owner-bound one-policy signer offer."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await get_eth_user_grant_review(did)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.post("/api/live/eth/policy/confirm-grant")
async def eth_confirm_wallet_owner_grant(
    authorization: str | None = Header(default=None),
):
    """Record Privy-owner approved signer only after server GET verification."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await confirm_eth_user_grant(did)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/live/eth/policy/wallet-signer-absence")
async def eth_wallet_signer_absence(
    authorization: str | None = Header(default=None),
):
    """Independent Privy GET verification of the exact user's signer list."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await get_privy_wallet_signer_absence(did)
    return JSONResponse(result, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.post("/api/live/eth/policy/confirm-revocation")
async def eth_wallet_signer_revocation_confirmation(
    authorization: str | None = Header(default=None),
):
    """Confirm local stop + Privy-verified absence, never remote DELETE."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = await confirm_privy_wallet_signer_revocation(did)
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


@app.get("/api/live/btc/limits")
def btc_live_limits_status(authorization: str | None = Header(default=None)):
    """Owner-only BTC live cap preferences; no native Bitcoin signer."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_live_btc_limits(did),headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.put("/api/live/btc/limits")
def btc_live_limits_update(
    body: LiveEthLimitsRequest,
    authorization: str | None = Header(default=None),
):
    """Save BTC-only limits without touching ETH and keep BTC LIVE paused."""
    did=verify_privy_bearer(authorization)["user_id"]
    result=save_live_btc_limits(
        did,capital=body.planned_capital_usd,
        order=body.max_order_usd,daily=body.max_daily_turnover_usd,
        expected_revision=body.expected_revision,
    )
    return JSONResponse(result,headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
    })


@app.post("/api/live/btc/stop")
def btc_live_stop(authorization: str | None = Header(default=None)):
    """Owner BTC stop never resumes trading or alters Privy policies."""
    did=verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(stop_live_btc(did),headers={
        "Cache-Control":"private, no-store",
        "X-Content-Type-Options":"nosniff",
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
        order=body.max_order_usd or body.planned_capital_usd,
        daily=body.max_daily_turnover_usd or body.planned_capital_usd,
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


class IndependentCoinPreferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The user chooses the capital; only positive JSON-safe integers are valid.
    budget_usd: StrictInt = Field(gt=0, le=9007199254740991)
    mode: Literal["LONG", "SPOT_SHORT", "AUTO"]
    stable: Literal["USDC", "USDT"]
    horizon: Literal["5m", "15m", "1h", "24h"]
    expected_revision: StrictInt = Field(ge=0, lt=2_000_000_000)


@app.get("/api/coin-preferences")
def coin_preferences_status(
    authorization: str | None = Header(default=None),
):
    """Load ten independent saved preferences for this one Privy account."""
    did = verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_coin_preferences(did), headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    })


@app.put("/api/coin-preferences/{asset}")
def coin_preferences_update(
    asset: str, body: IndependentCoinPreferenceRequest,
    authorization: str | None = Header(default=None),
):
    """Save this coin only; no active session, wallet or order mutation."""
    did = verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(
        save_coin_preference(
            did, asset, budget_usd=body.budget_usd, mode=body.mode,
            stable=body.stable, horizon=body.horizon,
            expected_revision=body.expected_revision,
        ),
        headers={"Cache-Control": "private, no-store",
                 "X-Content-Type-Options": "nosniff"},
    )


class SharedStrategyProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["LONG", "SPOT_SHORT", "AUTO"]
    horizon: Literal["5m", "15m", "1h", "24h"]
    expected_revision: StrictInt = Field(ge=0, lt=2_000_000_000)


@app.get("/api/strategy/profile")
def shared_strategy_profile(authorization: str | None = Header(default=None)):
    """Load the user's ONE mode/horizon template for all eleven trading coins."""
    did = verify_privy_bearer(authorization)["user_id"]
    return JSONResponse(get_strategy_profile(did), headers={
        "Cache-Control": "private, no-store",
    })


@app.put("/api/strategy/profile")
def update_shared_strategy_profile(
    body: SharedStrategyProfileRequest,
    authorization: str | None = Header(default=None),
):
    """Save once for all coins; per-coin budgets and active sessions persist."""
    did = verify_privy_bearer(authorization)["user_id"]
    result = save_strategy_profile(
        did, mode=body.mode, horizon=body.horizon,
        expected_revision=body.expected_revision,
    )
    return JSONResponse(result, headers={"Cache-Control": "private, no-store"})


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
    if (body.mode not in ("LONG", "SPOT_SHORT", "AUTO")
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
    """PAPER is independent from LIVE; only an explicit PAPER cutover hides it.

    A separately authorized ETH LIVE worker may run alongside a still-visible
    simulation. PAPER session data never grants wallet signing authority.
    """
    return os.getenv("MININEX_PAPER_ENABLED","true").strip().lower() != "false"


def _require_paper_visible() -> None:
    if not _paper_visible():
        raise HTTPException(404,"Paper simulation unavailable in live mode")


@app.get("/api/user-paper/availability")
def user_paper_availability():
    return JSONResponse({
        "paper_available":_paper_visible(),
        # Informational global flag only; not a user wallet permission.
        "live_trading_enabled":os.getenv("MININEX_LIVE_TRADING_ENABLED","false").lower()=="true",
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
    mode: Literal["LONG","SPOT_SHORT","AUTO"] = "LONG"
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




# Native coin send endpoints (XRP, TRX)
@app.post("/api/wallet/send/xrp")
async def send_xrp(
    body: dict,
    authorization: str | None = Header(default=None),
):
    """Send XRP from user's wallet. Privy signs, backend submits to XRPL."""
    import os, httpx
    user = verify_privy_bearer(authorization)
    from_addr = body.get("from")
    to_addr = body.get("to")
    amount = body.get("amount")
    if not from_addr or not to_addr or not amount:
        raise HTTPException(400, "Missing from, to, or amount")
    # XRP uses drops (1 XRP = 1,000,000 drops)
    amount_drops = int(float(amount) * 1_000_000)
    app_id = os.environ.get("MININEX_PRIVY_APP_ID", "")
    app_secret = os.environ.get("MININEX_PRIVY_APP_SECRET", "")
    if not app_id or not app_secret:
        raise HTTPException(500, "Privy credentials not configured")
    async with httpx.AsyncClient() as client:
        # Get wallet ID
        resp = await client.get(
            f"https://api.privy.io/v1/users/{user['user_id']}",
            auth=(app_id, app_secret),
            headers={"privy-app-id": app_id}
        )
        if resp.status_code != 200:
            raise HTTPException(500, "Failed to fetch user wallets")
        user_data = resp.json()
        xrp_wallet = None
        for acc in user_data.get("linked_accounts", []):
            if acc.get("type") == "wallet" and acc.get("chain_type") == "xrpl":
                xrp_wallet = acc
                break
        if not xrp_wallet:
            raise HTTPException(400, "No XRP wallet found")
        wallet_id = xrp_wallet.get("id")
        # Sign transaction via Privy
        sign_resp = await client.post(
            f"https://api.privy.io/v1/wallets/{wallet_id}/rpc",
            auth=(app_id, app_secret),
            headers={"privy-app-id": app_id, "Content-Type": "application/json"},
            json={
                "method": "xrpl_signTransaction",
                "params": {
                    "transaction": {
                        "TransactionType": "Payment",
                        "Account": from_addr,
                        "Destination": to_addr,
                        "Amount": str(amount_drops)
                    }
                }
            }
        )
        if sign_resp.status_code != 200:
            raise HTTPException(500, f"Sign failed: {sign_resp.text}")
        signed = sign_resp.json()
        # Submit to XRPL (using public endpoint)
        tx_blob = signed.get("data", {}).get("signed_transaction", "")
        if not tx_blob:
            raise HTTPException(500, "No signed transaction returned")
        submit_resp = await client.post(
            "https://xrplcluster.com/",
            json={
                "method": "submit",
                "params": [{"tx_blob": tx_blob}]
            }
        )
        if submit_resp.status_code != 200:
            raise HTTPException(500, "XRPL submission failed")
        result = submit_resp.json()
        return JSONResponse({
            "status": "submitted",
            "txid": result.get("result", {}).get("tx_json", {}).get("hash", "unknown"),
            "from": from_addr,
            "to": to_addr,
            "amount": amount,
        }, headers={"Cache-Control": "no-store"})


@app.post("/api/wallet/send/trx")
async def send_trx(
    body: dict,
    authorization: str | None = Header(default=None),
):
    """Send TRX from user's wallet via Privy tron_sendTransaction."""
    import os, httpx
    user = verify_privy_bearer(authorization)
    from_addr = body.get("from")
    to_addr = body.get("to")
    amount = body.get("amount")
    if not from_addr or not to_addr or not amount:
        raise HTTPException(400, "Missing from, to, or amount")
    # Convert TRX to sun (1 TRX = 1,000,000 sun)
    amount_sun = int(float(amount) * 1_000_000)
    # Get wallet ID from Privy user
    app_id = os.environ.get("MININEX_PRIVY_APP_ID", "")
    app_secret = os.environ.get("MININEX_PRIVY_APP_SECRET", "")
    if not app_id or not app_secret:
        raise HTTPException(500, "Privy credentials not configured")
    # Find TRX wallet for user
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.privy.io/v1/users/{user['user_id']}",
            auth=(app_id, app_secret),
            headers={"privy-app-id": app_id}
        )
        if resp.status_code != 200:
            raise HTTPException(500, "Failed to fetch user wallets")
        user_data = resp.json()
        trx_wallet = None
        for acc in user_data.get("linked_accounts", []):
            if acc.get("type") == "wallet" and acc.get("chain_type") == "tron":
                trx_wallet = acc
                break
        if not trx_wallet:
            raise HTTPException(400, "No TRX wallet found")
        wallet_id = trx_wallet.get("id")
        # Send via Privy
        send_resp = await client.post(
            f"https://api.privy.io/v1/wallets/{wallet_id}/rpc",
            auth=(app_id, app_secret),
            headers={"privy-app-id": app_id, "Content-Type": "application/json"},
            json={
                "method": "tron_sendTransaction",
                "params": {
                    "raw_data": {
                        "contract": [{
                            "type": "TransferContract",
                            "owner_address": from_addr,
                            "to_address": to_addr,
                            "amount": amount_sun
                        }]
                    }
                }
            }
        )
        if send_resp.status_code != 200:
            raise HTTPException(500, f"Send failed: {send_resp.text}")
        result = send_resp.json()
        return JSONResponse({
            "status": "submitted",
            "txid": result.get("data", {}).get("txid", "unknown"),
            "from": from_addr,
            "to": to_addr,
            "amount": amount,
        }, headers={"Cache-Control": "no-store"})
