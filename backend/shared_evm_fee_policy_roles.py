"""Independent Privy signer identity for automatic 5%-net-profit fees.

Privy scopes an additional signer with its policy. Reusing the trading key
quorum ID for a *different* fee policy risks replacing the existing trading
grant. A distinct fee signer quorum ID, approved by the wallet owner, is
therefore required. The fee quorum can contain the separately registered
pinned backend P-256 key; the actual wallet and per-coin fee policy remain
distinct. No new permission is created merely by setting these variables.
"""
from __future__ import annotations

import os
import re

from fastapi import HTTPException

from .eth_user_consent import _require_policy_roles
from .eth_privy_remote_quorum import check_privy_backend_quorum

_ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")


def require_fee_policy_roles()->tuple[str,str]:
    owner,trading_signer=_require_policy_roles()
    fee_signer=os.getenv("MININEX_PRIVY_EVM_FEE_SIGNER_ID","").strip()
    if (not _ID.fullmatch(fee_signer)
            or fee_signer==trading_signer
            or fee_signer==owner):
        raise HTTPException(
            409,"A separately registered fee-only Privy signer quorum is required")
    return owner,fee_signer


async def check_privy_fee_backend_quorum()->dict:
    """Read-only remote GET of the separate, real fee signer P-256 key."""
    _,fee_signer=require_fee_policy_roles()
    return await check_privy_backend_quorum(expected_quorum_id=fee_signer)
