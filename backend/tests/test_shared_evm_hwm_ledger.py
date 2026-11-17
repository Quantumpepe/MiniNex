"""Five separate owner/coin 5% net-profit high-water marks, no paper fees."""
from decimal import Decimal
import pytest
from fastapi import HTTPException

from backend.shared_evm_hwm_ledger import _assess_locked
from backend.storage import SCHEMA

DID="did:privy:alice"
KEY="sig_abcdef01234567890123456789"
TX="0x"+"ab"*32


class Cur:
    def __init__(self,rows):
        self.rows=iter(rows)
        self.sql=[]
    def execute(self,query,args):
        self.sql.append((query,args))
    def fetchone(self):
        return next(self.rows)


def rows(asset,net_cumulative="0",hwm="0",stable=None):
    stable=stable or ("USDT" if asset=="BNB" else "USDC")
    chains={"BNB":56,"POL":137,"AVAX":43114,"LINK":1,"UNI":1}
    result=[
        {"status":"FINALIZED_SUCCESS","side":"SELL","tx_hash":TX,
         "chain_id":chains[asset],"stable":stable},
        {"chain_id":chains[asset],"tx_hash":TX,"side":"SELL",
         "stable":stable,"source_amount":Decimal("1"),
         "destination_amount":Decimal("200"),
         "network_fee_native":Decimal("0.001")},
        {"cumulative_realized_net_usd":Decimal(net_cumulative),
         "high_water_mark_usd":Decimal(hwm)},
        None,
    ]
    if asset in ("BNB","POL","AVAX"):
        result.insert(2,{
            "swap_tx_hash":TX,
            "conversion_tx_hash":"0x"+"cd"*32,
            "chain_id":chains[asset],"side":"SELL","stable":stable,
        })
    return result


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_five_independent_verified_coin_hwm_ledgers(asset):
    cur=Cur(rows(asset))
    r=_assess_locked(
        cur,did=DID,asset=asset,execution_id=KEY,
        source_tx_hash=TX,realized_net_usd="200.00")
    assert Decimal(r["fee_assessed_usd"])==Decimal("10.000000")
    assert Decimal(r["high_water_mark_usd"])==Decimal("200")
    assert r["fee_token_transferred"] is False
    assert len(cur.sql)==(9 if asset in ("BNB","POL","AVAX") else 8)
    assert "FOR UPDATE" in cur.sql[0][0]
    assert "FOR UPDATE" in cur.sql[1][0]
    assert "FOR UPDATE" in cur.sql[4 if asset in ("BNB","POL","AVAX") else 3][0]
    assert "mininex_evm_fee_obligations" in cur.sql[-1][0]


@pytest.mark.parametrize("previous,hwm,delta,fee",[
    ("100","100","-35","0"),
    ("65","100","20","0"),
    ("85","100","30","0.75"),
    ("100","100","500","25"),
])
def test_hwm_only_assesses_new_net_profit_after_recovery(previous,hwm,delta,fee):
    cur=Cur(rows("LINK",previous,hwm))
    r=_assess_locked(cur,did=DID,asset="LINK",execution_id=KEY,
                     source_tx_hash=TX,realized_net_usd=delta)
    assert Decimal(r["fee_assessed_usd"])==Decimal(fee)
    assert ("mininex_evm_fee_obligations" in cur.sql[-1][0])==(Decimal(fee)>0)


@pytest.mark.parametrize("change",[
    lambda r:r.update(status="SUBMITTED"),
    lambda r:r.update(side="BUY"),
    lambda r:r.update(tx_hash="0x"+"cd"*32),
    lambda r:r.update(chain_id=1),
])
def test_unfinished_wrong_side_or_network_order_cannot_assess_fee(change):
    ordered=rows("POL")
    change(ordered[0])
    cur=Cur(ordered)
    with pytest.raises(HTTPException) as err:
        _assess_locked(cur,did=DID,asset="POL",execution_id=KEY,
                       source_tx_hash=TX,realized_net_usd="100")
    assert err.value.status_code==409
    assert len(cur.sql)==1


@pytest.mark.parametrize("original_delta,original_hash",[
    (Decimal("99"),TX),
    (Decimal("100"),"0x"+"cd"*32),
])
def test_duplicate_execution_with_changed_economic_facts_fails(
    original_delta,original_hash
):
    prev={"realized_net_delta_usd":original_delta,"source_tx_hash":original_hash,
          "fee_assessed_usd":Decimal("5"),
          "high_water_mark_usd":Decimal("100"),
          "cumulative_realized_net_usd":Decimal("100")}
    allrows=rows("AVAX","100","100")
    allrows[-1]=prev
    cur=Cur(allrows)
    with pytest.raises(HTTPException) as err:
        _assess_locked(cur,did=DID,asset="AVAX",execution_id=KEY,
                       source_tx_hash=TX,realized_net_usd="100")
    assert err.value.status_code==409
    assert len(cur.sql)==6


def test_identical_execution_is_idempotent_and_does_not_add_second_fee():
    old={"realized_net_delta_usd":Decimal("100"),"source_tx_hash":TX,
         "fee_assessed_usd":Decimal("5"),
         "high_water_mark_usd":Decimal("100"),
         "cumulative_realized_net_usd":Decimal("100")}
    items=rows("UNI","100","100")
    items[-1]=old
    cur=Cur(items)
    result=_assess_locked(cur,did=DID,asset="UNI",execution_id=KEY,
                          source_tx_hash=TX,realized_net_usd="100")
    assert result["duplicate"] is True
    assert result["fee_assessed_usd"]=="5"
    assert len(cur.sql)==5
    assert not any("UPDATE mininex_evm_asset_hwm" in sql for sql,args in cur.sql)


@pytest.mark.parametrize("mutation",[
    lambda f:f.update(chain_id=56),
    lambda f:f.update(tx_hash="0x"+"cd"*32),
    lambda f:f.update(side="BUY"),
    lambda f:f.update(stable="USDT"),
    lambda f:f.update(source_amount=Decimal("0")),
    lambda f:f.update(destination_amount=Decimal("-1")),
    lambda f:f.update(network_fee_native=None),
])
def test_hwm_refuses_nonfinal_or_wrong_ledger_fill(mutation):
    evidence=rows("POL")
    mutation(evidence[1])
    cur=Cur(evidence)
    with pytest.raises(HTTPException) as err:
        _assess_locked(cur,did=DID,asset="POL",execution_id=KEY,
                       source_tx_hash=TX,realized_net_usd="100")
    assert err.value.status_code==409
    assert len(cur.sql)==2


def test_evm_hwm_does_not_modify_original_btc_eth_ledger():
    schema="\n".join(SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS mininex_evm_asset_hwm" in schema
    assert "CREATE TABLE IF NOT EXISTS mininex_evm_fee_events" in schema
    assert "CREATE TABLE IF NOT EXISTS mininex_evm_fee_obligations" in schema
    assert "CREATE TABLE IF NOT EXISTS mininex_asset_hwm" in schema
    from backend.app import app
    assert all("evm-fee-write" not in r.path for r in app.routes)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
@pytest.mark.parametrize("corrupt",[
    lambda c:c.update(swap_tx_hash="0x"+"ee"*32),
    lambda c:c.update(conversion_tx_hash="0x"+"ab"*32),
    lambda c:c.update(conversion_tx_hash=None),
    lambda c:c.update(chain_id=1),
    lambda c:c.update(side="BUY"),
    lambda c:c.update(stable="USDT"),
])
def test_hwm_never_assesses_native_fee_without_full_matching_sell_cycle(asset,corrupt):
    entries=rows(asset,stable="USDC")
    corrupt(entries[2])
    cur=Cur(entries)
    with pytest.raises(HTTPException) as failure:
        _assess_locked(cur,did=DID,asset=asset,
                       execution_id=KEY,source_tx_hash=TX,
                       realized_net_usd="100")
    assert failure.value.status_code==409
    assert len(cur.sql)==3
    assert not any("INSERT INTO mininex_evm_fee_events" in sql
                   for sql,args in cur.sql)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_missing_native_cycle_never_assesses_fee(asset):
    entries=rows(asset)
    entries[2]=None
    cur=Cur(entries)
    with pytest.raises(HTTPException):
        _assess_locked(cur,did=DID,asset=asset,execution_id=KEY,
                       source_tx_hash=TX,realized_net_usd="100")
    assert len(cur.sql)==3
