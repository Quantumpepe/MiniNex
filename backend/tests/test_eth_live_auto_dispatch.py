"""End-to-end ETH auto signal dispatch is separate from PAPER fills."""
import asyncio
import time
from datetime import datetime
from fastapi import HTTPException
import pytest

from backend import eth_live_auto_dispatch as a

NOW=int(time.time())
TX={"from":"0x"+"a"*40,"to":"0x"+"b"*40,
    "chainId":1,"value":"0","data":"0x1234"}

def signal():
    return a.EthAutoSignal(strategy_run_id="engine1234",
             signal_event_id="signal9876",side="BUY",
             stable="USDC",amount="10")

def quote():
    return {"mode":"ETH_ROUTER02_POLICY_ABI_QUOTE_ONLY",
         "wallet_identity_verified":True,"unsigned_swap_validated":True,
         "side":"BUY","stable":"USDC","input_amount":"10",
         "execution_enabled":False,"quoted_at":NOW,
         "quote_ttl_seconds":15,
         "minimum_swap_output_amount":"0.002",
         "expected_swap_output_amount":"0.0025",
         "transaction":TX}
def prices():
    return {"usd-coin":{"usd":1,"last_updated_at":NOW},
            "tether":{"usd":1,"last_updated_at":NOW}}

def test_quote_produces_real_usd_capital_reservation():
    terms=a.exact_swap_quote_terms(quote(),signal(),prices(),now=NOW)
    assert terms["input_atomic"]=="10000000"
    assert terms["minimum_output_atomic"]=="2000000000000000"
    assert terms["notional_usd"]=="10.000000"
    assert len(terms["quote_digest"])==64

def test_paper_fill_cannot_submit_real_order():
    bogus=a.EthAutoSignal(strategy_run_id="engine1234",
             signal_event_id="signal9876",side="BUY",
             stable="USDC",amount="10",is_paper=True)
    with pytest.raises(ValueError):
        a.exact_swap_quote_terms(quote(),bogus,prices(),now=NOW)

def test_stale_quote_cannot_reserve_or_sign():
    q=quote();q["quoted_at"]=NOW-11
    with pytest.raises(ValueError):
        a.exact_swap_quote_terms(q,signal(),prices(),now=NOW)

def test_end_to_end_calls_actual_privy_sender_once(monkeypatch):
    calls=[]
    async def fake_quote(did,*,side,stable,amount,include_user_transaction):
        calls.append("quote")
        return quote()
    async def fake_prices():return prices()
    async def fake_signer(did):
        return {"signer_verified":True,"policy_reference_verified":True}
    def fake_policy(did):
        return {"status":"GRANT_VERIFIED","live_limits_revision_matches":True}
    def fake_reserve(did,signal,terms,signer,policy):
        calls.append("reserve")
        return {"claim":"RESERVED_NO_SEND","intent_key":"sig_abcdef1234567890"}
    async def fake_submit(**kw):
        calls.append("privy-send")
        return {"real_privy_broadcast":True,"tx_hash":"0x"+"b"*64}
    monkeypatch.setattr(a,"make_policy_compatible_eth_quote",fake_quote)
    monkeypatch.setattr(a,"fetch_reference_prices",fake_prices)
    monkeypatch.setattr(a,"get_eth_signer_status",fake_signer)
    monkeypatch.setattr(a,"get_eth_policy_ledger_status",fake_policy)
    monkeypatch.setattr(a,"_reserve_verified_live_signal",fake_reserve)
    monkeypatch.setattr(a,"submit_privy_eth_auto_order",fake_submit)
    monkeypatch.setattr(a.time,"time",lambda:NOW)
    result=asyncio.run(a.execute_eth_auto_signal("did:privy:verified",
                signal(),client=None))
    assert result["real_privy_broadcast"]
    assert calls==["quote","reserve","privy-send"]

def test_duplicate_signal_does_not_broadcast(monkeypatch):
    async def fake_quote(*args,**kwargs):return quote()
    async def fake_prices():return prices()
    async def fake_signer(*args):return {"signer_verified":True,
                                        "policy_reference_verified":True}
    monkeypatch.setattr(a,"make_policy_compatible_eth_quote",fake_quote)
    monkeypatch.setattr(a,"fetch_reference_prices",fake_prices)
    monkeypatch.setattr(a,"get_eth_signer_status",fake_signer)
    monkeypatch.setattr(a,"get_eth_policy_ledger_status",lambda did:
        {"status":"GRANT_VERIFIED","live_limits_revision_matches":True})
    monkeypatch.setattr(a,"_reserve_verified_live_signal",
        lambda *args:{"claim":"DUPLICATE_NO_NEW_ORDER",
                      "safe_to_send":False})
    monkeypatch.setattr(a.time,"time",lambda:NOW)
    result=asyncio.run(a.execute_eth_auto_signal(
        "did:privy:verified",signal(),client=None))
    assert result["claim"]=="DUPLICATE_NO_NEW_ORDER"
