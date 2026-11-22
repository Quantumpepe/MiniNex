"""Owner-selected ETH budget and strategy settings under the same SQL lock.

The combined ETH grant uses the EXISTING ETH settings and same Privy wallet,
never an extra coin-preferences row, administrative trading cap or signer.
"""
from __future__ import annotations

from decimal import Decimal,InvalidOperation

from fastapi import HTTPException


def locked_original_eth_settings(cur,*,did:str,stable:str,
                                 revision:int)->dict:
    if stable not in ("USDC","USDT") or type(revision) is not int or revision<1:
        raise HTTPException(409,"Original ETH user preference unavailable")
    cur.execute(
        """SELECT l.planned_capital_usd AS budget_usd,l.revision,
                  t.stable,t.mode,t.horizon
           FROM mininex_live_eth_limits l
           JOIN mininex_trade_preferences t
             ON t.privy_did=l.privy_did AND t.asset='ETH'
           WHERE l.privy_did=%s
           FOR UPDATE OF l,t""",(did,),
    )
    settings=cur.fetchone()
    if (type(settings) is not dict
            or type(settings.get("revision")) is not int
            or settings["revision"]!=revision
            or settings.get("stable")!=stable
            or settings.get("mode") not in ("LONG","SPOT_SHORT","AUTO")
            or settings.get("horizon") not in ("5m","15m","1h","24h")):
        raise HTTPException(409,"Original saved ETH capital/strategy changed")
    try:
        amount=Decimal(str(settings.get("budget_usd")))
    except (InvalidOperation,TypeError,ValueError):
        raise HTTPException(409,"Original ETH capital unavailable") from None
    if not amount.is_finite() or amount<=0:
        raise HTTPException(409,"Original ETH capital must be positive")
    return settings
