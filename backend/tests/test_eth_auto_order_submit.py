"""Real ETH AUTO sender: durable claim always precedes wallet RPC."""
import asyncio
import pytest
from fastapi import HTTPException
from backend import eth_auto_order_submit as submit

DID="did:privy:approved_wallet"
KEY="sig_abcdef01234567890123456789"
WALLET="0x"+"a"*40
HASH="0x"+"b"*64


def test_claim_requires_active_user_consent_and_policy():
    class Cursor:
        def __init__(self,rows): self.rows=iter(rows);self.sql=[]
        def execute(self,q,args):self.sql.append(q)
        def fetchone(self):return next(self.rows)
    rows=[
      {"trading_paused":False,"revision":3},
      {"status":"GRANT_VERIFIED","policy_id":"policy123456",
       "wallet_address":WALLET,"limits_revision":3,
       "execution_signer_id":"signer123456",
       "candidate_sha256":"c"*64,"remote_policy_sha256":"c"*64,
       "expires_at_unix":10**11},
      {"status":"SIGNED_INTENT","revoked_at":None,"limits_revision":3},
      {"pending":0},
      {"pending":0},
      {"state":"RESERVED","side":"BUY","stable":"USDC",
       "chain_network":"ethereum-mainnet"},
    ]
    cur=Cursor(rows)
    assert submit._claim_locked(cur,did=DID,key=KEY,wallet=WALLET,
        wallet_id="wallet123456",signer_id="signer123456",
        policy_id="policy123456",digest="d"*64)=="BUY|USDC"
    assert any("INSERT INTO mininex_live_eth_auto_broadcasts" in q for q in cur.sql)
    assert any("state='SENDING'" in q for q in cur.sql)

@pytest.mark.parametrize("row_index,alter",[
  (0,{"trading_paused":True}),
  (1,{"status":"REVOKE_REQUESTED"}),
  (1,{"policy_id":"different"}),
  (2,{"status":"REVOKED"}),
  (3,{"pending":1}),
  (4,{"pending":1}),
  (5,{"state":"SENDING"}),
])
def test_claim_does_not_send_on_invalid_state(row_index,alter):
    class Cursor:
        def __init__(self,rows):self.rows=iter(rows);self.sql=[]
        def execute(self,q,args):self.sql.append(q)
        def fetchone(self):return next(self.rows)
    rows=[
      {"trading_paused":False,"revision":3},
      {"status":"GRANT_VERIFIED","policy_id":"policy123456",
       "wallet_address":WALLET,"limits_revision":3,
       "execution_signer_id":"signer123456",
       "candidate_sha256":"c"*64,"remote_policy_sha256":"c"*64,
       "expires_at_unix":10**11},
      {"status":"SIGNED_INTENT","revoked_at":None,"limits_revision":3},
      {"pending":0},
      {"pending":0},
      {"state":"RESERVED","side":"BUY","stable":"USDC",
       "chain_network":"ethereum-mainnet"},
    ]
    rows[row_index].update(alter)
    cur=Cursor(rows)
    with pytest.raises(HTTPException):
        submit._claim_locked(cur,did=DID,key=KEY,wallet=WALLET,
            wallet_id="wallet123456",signer_id="signer123456",
            policy_id="policy123456",digest="d"*64)
    assert not any("INSERT INTO mininex_live_eth_auto_broadcasts" in q
                   for q in cur.sql)
