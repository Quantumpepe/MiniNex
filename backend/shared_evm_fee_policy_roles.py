"""Independent, per-asset Privy fee signer quorums for EVM LIVE.

A Privy additional signer currently supports one override policy. To avoid
silently replacing the existing trading grant OR another coin's fee policy,
each of the five different per-asset fee policies needs its own signer ID.
The owner registers each fee key quorum once; all may use the same pinned
server-held P-256 public member, but their quorum IDs must be distinct.
These configuration checks create no grant or additional user trade limit.
"""
from __future__ import annotations

import os
import re

from fastapi import HTTPException

from .eth_user_consent import _require_policy_roles
from .eth_privy_remote_quorum import check_privy_backend_quorum

_ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_ASSETS=("BNB","POL","AVAX","LINK","UNI","ARB")


def _fee_env_name(asset:str)->str:
    if asset not in _ASSETS:
        raise ValueError("Unsupported independently approved fee asset")
    return f"MININEX_PRIVY_EVM_{asset}_FEE_SIGNER_ID"


def require_fee_policy_roles(asset:str)->tuple[str,str]:
    owner,trading_signer=_require_policy_roles()
    fee_signer=os.getenv(_fee_env_name(asset),"").strip()
    configured=[os.getenv(_fee_env_name(coin),"").strip()
                for coin in _ASSETS]
    configured.extend(os.getenv(
        f"MININEX_PRIVY_EVM_{coin}_TRADE_SIGNER_ID","").strip()
        for coin in _ASSETS)
    configured=[name for name in configured if name]
    if (not _ID.fullmatch(fee_signer)
            or fee_signer==trading_signer or fee_signer==owner
            or len(configured)!=len(set(configured))):
        raise HTTPException(
            409,"Separately registered per-coin fee-only Privy quorum required")
    return owner,fee_signer


async def check_privy_fee_backend_quorum(asset:str)->dict:
    """Independently GET this coin's original fee signer, not ETH trader."""
    _,fee_signer=require_fee_policy_roles(asset)
    return await check_privy_backend_quorum(expected_quorum_id=fee_signer)
