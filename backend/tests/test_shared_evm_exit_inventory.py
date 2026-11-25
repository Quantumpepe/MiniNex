from decimal import Decimal
import pytest
from fastapi import HTTPException
from backend.shared_evm_exit_inventory import assert_token_exit_quantity_locked

@pytest.mark.parametrize("asset",["LINK","QNT","UNI"])
def test_exit_quantity_cannot_sell_more_than_original_position(asset):
    class Cursor:
        def execute(self,sql,args):
            assert "chain_id=1" in sql
            assert args[1]==asset
        def fetchone(self):
            return {"position":Decimal("2")}
    cur=Cursor()
    assert_token_exit_quantity_locked(cur,"did:privy:test",asset,"0x"+"aa"*20,2*10**18)
    with pytest.raises(HTTPException):
        assert_token_exit_quantity_locked(cur,"did:privy:test",asset,"0x"+"aa"*20,3*10**18)
