"""One independently registered Privy trade signer for each EVM coin.

The five coin-specific original policies have different destinations/rules.
Privy's single policy per additional signer must not replace a previously
granted ETH or other coin rule. Every asset therefore needs a separate
policy-bound signer/quorum, although all can share the same server-held
pinned P-256 member. No wallet access follows from environment variables.
"""
from __future__ import annotations

import os
import re
from fastapi import HTTPException

from .eth_user_consent import _require_policy_roles
from .eth_privy_remote_quorum import check_privy_backend_quorum

_ASSETS=("BNB","POL","AVAX","LINK","UNI")
_ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")


def _variable(asset:str)->str:
    if asset not in _ASSETS:
        raise ValueError("Unsupported additional EVM trader coin")
    return f"MININEX_PRIVY_EVM_{asset}_TRADE_SIGNER_ID"


def require_coin_trade_policy_roles(asset:str)->tuple[str,str]:
    owner,eth_signer=_require_policy_roles()
    signer=os.getenv(_variable(asset),"").strip()
    trades=[os.getenv(_variable(coin),"").strip() for coin in _ASSETS]
    fees=[os.getenv(
        f"MININEX_PRIVY_EVM_{coin}_FEE_SIGNER_ID","").strip()
        for coin in _ASSETS]
    active=[v for v in [eth_signer,*trades,*fees] if v]
    if (not _ID.fullmatch(signer)
            or owner==signer or signer==eth_signer
            or len(active)!=len(set(active))):
        raise HTTPException(409,"Separate per-coin Privy trading signer quorum required")
    return owner,signer


async def check_coin_trade_backend_quorum(asset:str)->dict:
    _,signer=require_coin_trade_policy_roles(asset)
    return await check_privy_backend_quorum(expected_quorum_id=signer)
