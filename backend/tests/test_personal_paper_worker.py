"""Automatic per-account strategy scheduler stays paper-only and fail-closed."""
import asyncio
from types import SimpleNamespace

import pytest

from backend import personal_paper_worker as worker
from backend.auto_engine import PaperAutoEngine, PaperPortfolio
from backend.user_paper import _restore, _state_of, market_feed_for_paper_start


def test_worker_default_off_without_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL",raising=False)
    assert worker.auto_worker_allowed() is False


def test_live_cutover_disables_simulation_worker(monkeypatch):
    monkeypatch.setenv("DATABASE_URL","postgresql://fake")
    monkeypatch.setenv("MININEX_PAPER_AUTO_ENABLED","true")
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","true")
    assert worker.auto_worker_allowed() is False
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","false")
    assert worker.auto_worker_allowed() is True
    monkeypatch.setenv("MININEX_PAPER_AUTO_ENABLED","false")
    assert worker.auto_worker_allowed() is False


def test_worker_evaluates_only_active_rows_and_keeps_separate_identities(monkeypatch):
    monkeypatch.setenv("DATABASE_URL","postgresql://fake")
    monkeypatch.setenv("MININEX_PAPER_AUTO_ENABLED","true")
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","false")
    monkeypatch.setattr(worker,"active_session_keys",
        lambda:[("did:privy:alice","BTC"),("did:privy:bob","ETH")])
    calls=[]
    def tick(did,asset,market,now):
        calls.append((did,asset,market))
    monkeypatch.setattr(worker,"step_user_paper",tick)
    market=object()
    assert worker.process_active_sessions(market)==2
    assert calls==[
        ("did:privy:alice","BTC",market),
        ("did:privy:bob","ETH",market),
    ]


def test_worker_failure_isolated_to_single_user(monkeypatch):
    monkeypatch.setenv("DATABASE_URL","postgresql://fake")
    monkeypatch.setenv("MININEX_PAPER_AUTO_ENABLED","true")
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","false")
    monkeypatch.setattr(worker,"active_session_keys",
        lambda:[("did:privy:bad","BTC"),("did:privy:good","ETH")])
    good=[]
    def tick(did,asset,market,now):
        if did.endswith("bad"):
            raise ValueError("simulated failure")
        good.append(asset)
    monkeypatch.setattr(worker,"step_user_paper",tick)
    assert worker.process_active_sessions(object())==1
    assert good==["ETH"]


def test_virtual_spot_downtrend_requires_verified_reference_feed():
    class Fake:
        def __init__(self,valid):self.valid=valid
        def snapshot(self):
            return {"markets":{"BTC":{"market_data_valid":self.valid,"price_usdt":50000}}}
    assert market_feed_for_paper_start("BTC",Fake(True))["price_usdt"]==50000
    with pytest.raises(Exception):
        market_feed_for_paper_start("BTC",Fake(False))


def test_paper_last_evaluation_persists_but_not_real_funds():
    p=PaperPortfolio(asset="ETH",settlement="USDC",stable_cash=400.0,
                     initial_equity=400.0,last_evaluated_at=1700000000)
    recovered=_restore("ETH","USDC",_state_of(p),400)
    assert recovered.last_evaluated_at==1700000000
    assert recovered.coin_quantity==0.0
