"""Native owner permission adapter.

The original receiving wallet and saved preferences do not establish signing
authority over Ethereum funding or a native source. Until an owner-approved
native policy is persisted and independently read back, no automated send
may receive a positive audit from this adapter.
"""
from fastapi import HTTPException
from .storage import _safe_did

_ASSETS=frozenset(("BTC","SOL","XRP","TRX","DOGE"))

async def audit_native_owner_grant(did:str,*,asset:str,stable:str,amount:str,
        budget_revision:int,signal_event_id:str,side:str="BUY",
        source_wallet:str|None=None,destination_wallet:str|None=None,
        original_manifest_sha256:str|None=None,original_quote_sha256:str|None=None,
        intent_key:str|None=None)->dict:
    _safe_did(did)
    if asset not in _ASSETS or stable not in ("USDC","USDT") or side not in ("BUY","SELL"):
        raise HTTPException(422,"Unsupported native settlement pair")
    if type(budget_revision) is not int or budget_revision<=0:
        raise HTTPException(409,"Original owner budget revision missing")
    # No fabricated policy IDs, placeholder wallets, locally invented consent
    # hashes or positive quorum assertions. An actual remotely verified native
    # grant adapter must be connected before calling the existing sender.
    raise HTTPException(409,detail={"code":"NATIVE_PRIVY_GRANT_NOT_VERIFIED",
        "message":"Current owner-approved native settlement policy is not verified"})
