"""MiniNex treasury integration is a safe ready-to-configure ledger, not a payout."""
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from backend import app as api
from backend.storage import SCHEMA, record_trusted_realized_close
from backend.treasury import (
    configured_treasury, create_unfunded_instruction, treasury_status,
    validate_treasury_address,
)

OWNER="0x1234567890aBCDEF1234567890abcdef12345678"


def test_treasury_absent_does_not_allow_payout(monkeypatch):
    monkeypatch.delenv("MININEX_TREASURY_WALLET",raising=False)
    assert configured_treasury() is None
    assert treasury_status()["collection_enabled"] is False
    with pytest.raises(ValueError,match="not configured"):
        create_unfunded_instruction(execution_id="close-1",fee_due_usd="5",token="USDC")


def test_treasury_ethereum_address_validation(monkeypatch):
    monkeypatch.setenv("MININEX_TREASURY_WALLET",OWNER)
    config=configured_treasury()
    assert config.address==OWNER
    assert config.network=="ethereum"
    assert treasury_status()["configured"] is True
    assert treasury_status()["collection_enabled"] is False
    assert "address" not in treasury_status()
    for bad in ("", "0x"+"0"*40, "bc1qxy", "0x"+"z"*40, "0x"+"1"*39):
        with pytest.raises(ValueError):
            validate_treasury_address(bad)


def test_treasury_instruction_never_substitutes_usd_for_token_amount(monkeypatch):
    monkeypatch.setenv("MININEX_TREASURY_WALLET",OWNER)
    instruction=create_unfunded_instruction(execution_id="trusted-close-7",
                                            fee_due_usd="4.250000",token="USDT")
    assert instruction["treasury_address"]==OWNER
    assert instruction["fee_due_usd"]==str(Decimal("4.250000000000"))
    assert instruction["token_amount"] is None
    assert instruction["can_execute"] is False
    assert instruction["status"]=="AWAITING_VERIFIED_LIVE_SETTLEMENT"
    for token in ("BTC","ETH","USD","USDC.e",""):
        with pytest.raises(ValueError):
            create_unfunded_instruction(execution_id="e1",fee_due_usd=1,token=token)
    for amount in (0,-10,"NaN",True):
        with pytest.raises(ValueError):
            create_unfunded_instruction(execution_id="e1",fee_due_usd=amount,token="USDC")


def test_obligations_are_immutable_pending_and_link_to_verified_fee_events():
    ddl="\n".join(SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS mininex_treasury_obligations" in ddl
    assert "FOREIGN KEY (privy_did,asset,execution_id)" in ddl
    assert "CHECK (settled_token_amount IS NULL AND settlement_tx_hash IS NULL)" in ddl
    assert "CHECK (status='AWAITING_VERIFIED_LIVE_SETTLEMENT')" in ddl
    import inspect
    source=inspect.getsource(record_trusted_realized_close)
    assert "assessment.fee_assessed_usd>0" in source
    assert "mininex_treasury_obligations" in source
    assert "ON CONFLICT (privy_did,asset,execution_id) DO NOTHING" in source


def test_treasury_status_endpoint_requires_privy_and_never_exposes_address(monkeypatch):
    monkeypatch.setenv("MININEX_TREASURY_WALLET",OWNER)
    client=TestClient(api.app)
    assert client.get("/api/treasury/status").status_code==401
    assert client.get("/api/treasury/status",headers={"Authorization":"Bearer invalid"}).status_code==401
    assert not any(route.path=="/api/treasury/send" for route in api.app.routes)
