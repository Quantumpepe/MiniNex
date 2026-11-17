"""Authorized EVM RPC outcomes must atomically update reserved coin capital."""
import pytest
from fastapi import HTTPException

from backend.shared_evm_order_journal import _record_authorized_result_locked

DID="did:privy:owner"
KEY="sig_abcdef01234567890123456789"
DIGEST="a"*64
TX="0x"+"ab"*32


class Cursor:
    def __init__(self,rows):self.rows=iter(rows);self.sql=[]
    def execute(self,sql,args):self.sql.append((sql,args))
    def fetchone(self):return next(self.rows)


@pytest.mark.parametrize("tx_hash,state",[
    (TX,"SUBMITTED"),
    (None,"RECONCILIATION_REQUIRED"),
])
def test_one_signed_send_and_user_budget_transition_same_sql_transaction(
    tx_hash,state
):
    cur=Cursor([
        {"request_digest":DIGEST,"status":"SENDING"},
        {"state":state},
    ])
    result=_record_authorized_result_locked(
        cur,did=DID,asset="AVAX",key=KEY,
        digest=DIGEST,tx_hash=tx_hash)
    assert result==state
    assert len(cur.sql)==3
    assert "mininex_live_evm_auto_broadcasts" in cur.sql[1][0]
    assert "mininex_live_evm_budget_reservations" in cur.sql[2][0]
    assert "AND state='SENDING'" in cur.sql[2][0]
    assert cur.sql[-1][1][0]==state


@pytest.mark.parametrize("budget",[
    None,{"state":"RESERVED"},{"state":"SUBMITTED"},
])
def test_inconsistent_budget_transition_aborts_authorized_outcome(budget):
    cur=Cursor([
        {"request_digest":DIGEST,"status":"SENDING"},budget,
    ])
    with pytest.raises(HTTPException) as err:
        _record_authorized_result_locked(
            cur,did=DID,asset="POL",key=KEY,
            digest=DIGEST,tx_hash=TX)
    assert err.value.status_code==409


def test_existing_uncertain_send_does_not_repeat_privy_rpc():
    cur=Cursor([{"request_digest":DIGEST,"status":"RECONCILIATION_REQUIRED"}])
    with pytest.raises(HTTPException):
        _record_authorized_result_locked(
            cur,did=DID,asset="LINK",key=KEY,
            digest=DIGEST,tx_hash=None)
    assert len(cur.sql)==1
