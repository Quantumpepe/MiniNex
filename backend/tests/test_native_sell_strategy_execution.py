import asyncio
from datetime import datetime,timezone,timedelta
import httpx
import pytest
from backend import native_sell_strategy_execution as m
from backend.native_grant_auditor import audit_native_owner_grant


@pytest.mark.parametrize("asset",["BTC","SOL"])
def test_real_preflight_cannot_create_missing_owner_permission(monkeypatch,asset):
    quote={"source_asset":asset,"destination_asset":"USDC","amount":"1",
           "source_wallet":"native_owner","destination_wallet":"0x"+"aa"*20,
           "quote_expires_at":datetime.now(timezone.utc)+timedelta(minutes=2)}
    manifest={"source_wallet":quote["source_wallet"],"destination_wallet":quote["destination_wallet"],
              "psbt_sha256":"a"*64,"message_sha256":"a"*64}
    async def prepared(*args,**kw):
        assert kw["_private_quote"]==quote and kw["_internal_bundle"]
        return {"_private_quote":quote,"unsigned_psbt":manifest,"original_compiled_message":manifest}
    def forbid(*a,**kw):
        pytest.fail("Missing grant may not persist or dispatch a SELL")
    monkeypatch.setattr(m,"prepare_original_btc_unsigned_psbt",prepared)
    monkeypatch.setattr(m,"prepare_original_sol_unsigned_owner_transfer",prepared)
    monkeypatch.setattr(m,"persist_internal_native_order",forbid)
    monkeypatch.setattr(m,"sign_and_dispatch_original_btc_sell_once",forbid)
    monkeypatch.setattr(m,"sign_and_dispatch_original_sol_sell_once",forbid)
    async def run():
        async with httpx.AsyncClient() as client:
            return await m.coordinate_original_native_sell_once("did:privy:original_owner",
                asset=asset,stable="USDC",amount="1",quote=quote,
                strategy_run_id="owner_12345",signal_event_id="market_12345",budget_revision=5,
                grant_auditor=audit_native_owner_grant,client=client)
    result=asyncio.run(run())
    assert result["reason"]=="NATIVE_PRIVY_GRANT_NOT_VERIFIED"
    assert result["real_trade_sent"] is False
