"""Native user Start Trade is one owner action, never login auto-grant."""
import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend import native_live_auto_sessions as m

DID="did:privy:session"
WALLET="0x"+"a"*40


def current(asset="BTC"):
    return {
        "mode":"NATIVE_PRIVY_REMOTE_GRANT_INDEPENDENT_AUDIT",
        "did":DID,"asset":asset,
        "stable":"USDT" if asset=="TRX" else "USDC",
        "source_wallet":WALLET,"budget_revision":3,
        "owner_wallet_current":True,
        "exact_remote_policy_verified":True,
        "backend_quorum_current":True,
        "owner_signed_budget_current":True,
        "user_consent_current":True,
    }


class Cursor:
    def __init__(self,rows):
        self.rows=iter(rows)
        self.queries=[]
    def execute(self,query,params=None):
        self.queries.append((query,params))
    def fetchone(self):
        return next(self.rows,None)


@pytest.mark.parametrize("asset",["BTC","SOL","XRP","TRX","DOGE"])
def test_explicit_owner_native_session_binds_current_grant_and_coin_preferences(asset):
    settings=({"revision":3,"stable":"USDT" if asset=="TRX" else "USDC",
               "percent":100,"mode":"LONG","horizon":"15m"} if asset=="BTC"
              else {"revision":3,"stable":"USDT" if asset=="TRX" else "USDC",
                    "budget_usd":2000,"mode":"AUTO","horizon":"15m"})
    cur=Cursor([{"privy_did":DID},settings,{"status":"ACTIVE"}])
    got=m._start_locked(cur,did=DID,asset=asset,grant=current(asset))
    assert got["status"]=="ACTIVE"
    assert got["original_blockchain_sends"]==0
    assert any("INSERT INTO mininex_live_native_auto_sessions" in q
               for q,args in cur.queries)
    assert any("FOR UPDATE" in q for q,args in cur.queries)


@pytest.mark.parametrize("broken",[
    {"mode":"AUTHENTICATED_LOGIN_IS_CONSENT"},
    {"exact_remote_policy_verified":False},
    {"owner_signed_budget_current":False},
    {"user_consent_current":False},
    {"did":"did:privy:another_owner"},
    {"budget_revision":0},
    {"source_wallet":"bad_address"},
])
def test_no_fake_owner_grant_can_start_native_trading(broken):
    obj={**current(),**broken}
    if broken.get("source_wallet")=="bad_address":
        # Guard must prevent empty/non-Ethereum wallet references.
        with pytest.raises(HTTPException):
            m._original_grant_for_start(obj,did=DID,asset="BTC")
    else:
        with pytest.raises(HTTPException):
            m._original_grant_for_start(obj,did=DID,asset="BTC")


def test_native_start_without_real_server_auditor_never_reaches_database(monkeypatch):
    async def run():
        return await m.start_owner_native_auto(
            DID,"BTC",grant_auditor=None)
    with pytest.raises(HTTPException) as err:
        asyncio.run(run())
    assert err.value.status_code==409


@pytest.mark.parametrize("asset",["BTC","SOL"])
def test_owner_changed_budget_or_stable_before_start_does_not_create_session(asset):
    wrong=({"revision":4,"stable":"USDC","percent":100,
            "mode":"LONG","horizon":"15m"} if asset=="BTC"
           else {"revision":3,"stable":"USDT","budget_usd":100,
                 "mode":"AUTO","horizon":"15m"})
    cur=Cursor([{"privy_did":DID},wrong])
    with pytest.raises(HTTPException):
        m._start_locked(cur,did=DID,asset=asset,grant=current(asset))
    assert not any("INSERT INTO mininex_live_native_auto_sessions" in q
                   for q,args in cur.queries)
