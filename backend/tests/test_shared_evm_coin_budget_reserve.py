"""User-configured independent budgets; no fixed MiniNex size limit."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import inspect

import pytest
from fastapi import HTTPException
from backend.shared_evm_coin_budget_reserve import _reserve_locked
from backend.storage import SCHEMA

DID="did:privy:owner"
KEY="sig_abcdef01234567890123456789"
SHA="a"*64


class Cur:
    def __init__(self,rows):
        self.rows=iter(rows);self.sql=[]
    def execute(self,query,args):self.sql.append((query,args))
    def fetchone(self):return next(self.rows)


def rows(asset,stable,budget=5000,reserved=0):
    return [
        {"budget_usd":budget,"stable":stable,"revision":5},
        {"status":"GRANT_VERIFIED","stable":stable,"revoked_at":None,
         "wallet_id":"wallet_12345678",
         "wallet_address":"0x"+"ab"*20,
         "consent_sha256":SHA,
         "expires_at":datetime.now(timezone.utc)+timedelta(days=1)},
        {"status":"SIGNED_INTENT",
         "wallet_id":"wallet_12345678","wallet_address":"0x"+"ab"*20,
         "chain_id":{"BNB":56,"POL":137,"AVAX":43114,"LINK":1,"UNI":1}[asset],
         "stable":stable,"budget_revision":5,"budget_usd":budget,
         "message_sha256":SHA,"confirmed_at":datetime.now(timezone.utc)},
        {"reserved_usd":Decimal(reserved)},
        {"state":"RESERVED"},
    ]


def run(cur,asset,stable,amount="100",side="BUY"):
    return _reserve_locked(cur,did=DID,asset=asset,key=KEY,side=side,
        stable=stable,amount_usd=amount,quote_sha256=SHA,
        expected_revision=5)


@pytest.mark.parametrize("asset,stable",[
    ("BNB","USDT"),("POL","USDC"),("AVAX","USDC"),
    ("LINK","USDC"),("UNI","USDT"),
])
def test_five_assets_reserve_only_owner_configured_budget(asset,stable):
    cur=Cur(rows(asset,stable,budget=1000000000))
    r=run(cur,asset,stable,amount="150000")
    assert r["state"]=="RESERVED"
    assert r["real_trade_sent"] is False
    assert r["privy_send_authorized"] is False
    assert len(cur.sql)==5
    assert "FOR UPDATE" in cur.sql[0][0]
    assert "WHERE privy_did=%s AND asset=%s" in cur.sql[2][0]
    assert "ON CONFLICT" in cur.sql[4][0]


def test_exact_remaining_user_budget_can_be_used_100_percent():
    cur=Cur(rows("AVAX","USDC",budget=1000,reserved=750))
    assert run(cur,"AVAX","USDC","250")["reserved_usd"]=="250"
    cur=Cur(rows("AVAX","USDC",budget=1000,reserved=750))
    with pytest.raises(HTTPException):
        run(cur,"AVAX","USDC","250.000001")


@pytest.mark.parametrize("mutation",[
    lambda r:r[0].update(revision=4),
    lambda r:r[0].update(stable="USDT"),
    lambda r:r[1].update(status="DRAFT"),
    lambda r:r[1].update(revoked_at=datetime.now(timezone.utc)),
    lambda r:r[1].update(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)),
    lambda r:r[1].update(stable="USDT"),
])
def test_no_budget_reservation_without_current_consent(mutation):
    data=rows("POL","USDC")
    mutation(data)
    cur=Cur(data)
    with pytest.raises(HTTPException) as error:
        run(cur,"POL","USDC")
    assert error.value.status_code==409
    assert not any("INSERT INTO mininex_live_evm_budget_reservations" in sql
                   for sql,args in cur.sql)


@pytest.mark.parametrize("alter",[
    lambda rows:rows[2].update(status="PENDING"),
    lambda rows:rows[2].update(wallet_address="0x"+"cd"*20),
    lambda rows:rows[2].update(budget_revision=4),
    lambda rows:rows[2].update(message_sha256="b"*64),
    lambda rows:rows[2].update(budget_usd=100),
    lambda rows:rows[2].update(confirmed_at=None),
])
def test_reserve_never_uses_unsigned_stale_or_other_wallet_consent(alter):
    data=rows("POL","USDC")
    alter(data)
    cur=Cur(data)
    with pytest.raises(HTTPException) as err:
        run(cur,"POL","USDC")
    assert err.value.status_code==409
    assert len(cur.sql)==3
    assert not any("INSERT INTO mininex_live_evm_budget_reservations" in q
                   for q,args in cur.sql)


def test_duplicate_signal_key_cannot_replay_even_after_confirmed():
    data=rows("LINK","USDC")
    data[-1]=None
    cur=Cur(data)
    with pytest.raises(HTTPException) as error:
        run(cur,"LINK","USDC")
    assert error.value.status_code==409


def test_budget_reservations_do_not_mix_other_assets_or_paper():
    src=inspect.getsource(_reserve_locked)
    assert "WHERE privy_did=%s AND asset=%s" in src
    assert "RECONCILIATION_REQUIRED" in src
    assert "PAPER" not in src
    assert "max_order_usd" not in src
    assert "max_daily_turnover_usd" not in src
    assert "mininex_live_evm_budget_reservations" in "\n".join(SCHEMA)
    from backend.app import app
    assert all("evm-budget-reserve" not in route.path for route in app.routes)


@pytest.mark.parametrize("asset,stable",[
    ("BNB","USDT"),("POL","USDC"),("AVAX","USDC"),
    ("LINK","USDC"),("UNI","USDC"),
])
def test_appreciated_spot_sell_never_hits_arbitrary_usd_budget_cap(asset,stable):
    cur=Cur(rows(asset,stable,budget=1000,reserved=950))
    result=run(cur,asset,stable,amount="1400.50",side="SELL")
    assert result["state"]=="RESERVED"
    assert result["reserved_usd"]=="1400.50"
    assert "AND side='BUY'" in cur.sql[3][0]
    assert cur.sql[4][1][4]=="SELL"


def test_buy_still_cannot_overspend_owner_selected_coin_budget():
    cur=Cur(rows("LINK","USDC",budget=1000,reserved=900))
    with pytest.raises(HTTPException):
        run(cur,"LINK","USDC",amount="100.000001",side="BUY")
    assert "AND side='BUY'" in cur.sql[3][0]
    assert len(cur.sql)==4
