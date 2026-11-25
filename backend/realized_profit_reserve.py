"""Automatic trading cash protection; not a fee calculation or wallet lock.

Confirmed stable proceeds are matched against actual FIFO entry cost, by
original account, coin, settlement token and coin wallet. Positive gross
spread is provisionally withheld in full until all historical gas/fees
can be attributed. Losses never consume an earlier protected surplus.
No simulation, unconfirmed fill, price-only gain or unrelated manual buy
can increase protected cash. No client may manufacture a reserve/release.
"""
from collections import defaultdict,deque
from decimal import Decimal
from fractions import Fraction
from math import ceil

from .ethereum_settlement_plan import COIN_NETWORKS,ETHEREUM_STABLES


def _exact(value):
    if isinstance(value,bool) or not isinstance(value,(Decimal,int,str)):
        raise ValueError("Exact confirmed fill amount required")
    d=Decimal(value)
    if not d.is_finite() or d<=0:
        raise ValueError("Invalid confirmed fill amount")
    return Fraction(d)


def protected_profit_atomic(rows,*,stable):
    """Return conservative gross hold in six-decimal stable atomics.

    Input is oldest first, from the original finalized journals. Every fully
    cost-matched automatic SELL protects its positive spread; no compounding.
    A manual SELL consumes known inventory but does not create an automatic
    profit reserve. Unmatched sells are not assigned imaginary cost basis.
    """
    if stable not in ETHEREUM_STABLES:
        raise ValueError("Unsupported settlement token")
    lots=defaultdict(deque)
    seen=set()
    protected=defaultdict(int)
    unmatched=0
    for row in rows:
        asset=row["asset"];side=row["side"];wallet=row["coin_wallet"]
        if (asset not in COIN_NETWORKS or row["stable"]!=stable
                or side not in ("BUY","SELL") or type(wallet) is not str or not wallet
                or type(row["automatic"]) is not bool
                or row["family"] not in ("ETHEREUM","CROSS_EVM","NATIVE")
                or row["family"]!=COIN_NETWORKS[asset][3]
                or not row["fill_key"]):
            raise ValueError("Original confirmed profit scope differs")
        key=(row["family"],asset,row["fill_key"])
        if key in seen:
            raise ValueError("Duplicate original profit fill")
        seen.add(key)
        source=_exact(row["source_amount"]);dest=_exact(row["destination_amount"])
        q=lots[(asset,wallet)]
        if side=="BUY":
            if row["automatic"]:
                q.append([dest,source])
            continue
        remaining=source;cost=Fraction(0)
        while remaining and q:
            lot=q[0];take=min(remaining,lot[0])
            used=lot[1]*take/lot[0]
            cost+=used;lot[0]-=take;lot[1]-=used;remaining-=take
            if not lot[0]:
                q.popleft()
        if remaining:
            if row["automatic"]:
                unmatched+=1
            continue
        if row["automatic"] and dest>cost:
            protected[asset]+=ceil((dest-cost)*10**6)
    return {"protected_gross_atomic":sum(protected.values()),
            "protected_gross_by_asset_atomic":dict(protected),
            "unmatched_automatic_sells":unmatched,
            "net_profit_verified":False}


# Read only canonical Ethereum settlement fills. Legacy local-chain stable
# balances cannot fund these reserves. The old ETH journal may coexist with
# the central worker; omit a legacy hash already present in the new ledger.
# Native quantities use each destination chain's actual atomic denomination.
PROFIT_FILLS_SQL="""SELECT asset,stable,side,source_amount,destination_amount,
        coin_wallet,family,fill_key,automatic,settled_at
    FROM (
      SELECT f.asset,f.stable,f.side,f.source_amount,f.destination_amount,
        lower(f.wallet_address) AS coin_wallet,'ETHEREUM' AS family,
        f.tx_hash AS fill_key,TRUE AS automatic,f.created_at AS settled_at
      FROM mininex_live_evm_verified_fills f
      WHERE f.privy_did=%(did)s AND f.stable=%(stable)s AND f.chain_id=1
        AND f.asset IN ('ETH','LINK','QNT','UNI','BTC','XRP')
      UNION ALL
      SELECT 'ETH',l.stable,l.side,l.source_amount,l.destination_amount,
        lower(b.wallet_address),'ETHEREUM',l.tx_hash,TRUE,o.finalized_at
      FROM mininex_live_order_intents o
      JOIN mininex_live_eth_verified_legs l
        ON l.privy_did=o.privy_did AND l.tx_hash=o.tx_hash
          AND l.kind='SWAP' AND l.side=o.side AND l.stable=o.stable
      JOIN mininex_live_eth_auto_broadcasts b
        ON b.privy_did=o.privy_did AND b.intent_key=o.intent_key AND b.tx_hash=o.tx_hash
      WHERE o.privy_did=%(did)s AND o.stable=%(stable)s AND o.asset='ETH'
        AND o.state='CONFIRMED' AND o.finalized_at IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM mininex_live_evm_verified_fills f
          WHERE f.privy_did=o.privy_did AND f.chain_id=1 AND f.tx_hash=o.tx_hash)
      UNION ALL
      SELECT q.asset,q.stable,q.side,
        q.input_atomic * CASE WHEN q.side='BUY' THEN 0.000001::numeric ELSE 0.000000000000000001::numeric END,
        f.received_atomic * CASE WHEN q.side='BUY' THEN 0.000000000000000001::numeric ELSE 0.000001::numeric END,
        lower(q.wallet_address),'CROSS_EVM',f.source_tx_hash,
        EXISTS(SELECT 1 FROM mininex_ethereum_settlement_submissions s
          WHERE s.quote_id=q.quote_id AND s.privy_did=q.privy_did
            AND s.state='SUBMITTED' AND s.source_tx_hash=f.source_tx_hash),
        f.verified_at
      FROM mininex_ethereum_settlement_finality f
      JOIN mininex_ethereum_settlement_quotes q
        ON q.quote_id=f.quote_id AND q.privy_did=f.privy_did
      WHERE q.privy_did=%(did)s AND q.stable=%(stable)s AND f.outcome='FINALIZED'
        AND ((q.side='BUY' AND q.source_chain_id=1) OR (q.side='SELL' AND q.destination_chain_id=1))
      UNION ALL
      SELECT n.asset,n.stable,n.side,n.input_amount,
        n.destination_received_atomic * CASE WHEN n.side='SELL' THEN 0.000001::numeric
          WHEN n.asset='BTC' THEN 0.00000001::numeric WHEN n.asset='SOL' THEN 0.000000001::numeric
          ELSE 0.000001::numeric END,
        CASE WHEN n.side='BUY' THEN n.destination_wallet ELSE n.source_wallet END,
        'NATIVE',n.intent_key,TRUE,n.destination_finalized_at
      FROM mininex_live_native_order_intents n
      WHERE n.privy_did=%(did)s AND n.stable=%(stable)s
        AND n.asset IN ('BTC','SOL','XRP','TRX') AND n.status='DESTINATION_FINALIZED'
        AND n.source_proof_sha256 IS NOT NULL AND n.destination_proof_sha256 IS NOT NULL
        AND n.destination_received_atomic>0
    ) fills"""
