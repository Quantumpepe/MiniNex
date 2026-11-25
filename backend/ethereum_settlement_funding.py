"""Owner-scoped funding snapshot across existing Ethereum BUY journals.

Read-only migration groundwork, not an atomic send claim. Legacy journals
with USD-only notional are conservatively translated to stable units using
the current verified rate. Already-debited but unresolved source orders can
reduce available funds until reconciliation; never count their destination
proceeds as spendable cash. No balance is fabricated.
"""
from decimal import Decimal, ROUND_CEILING, localcontext
import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException

from .storage import _db_url,_ensure_schema,_safe_did
from .ethereum_settlement_plan import ETHEREUM_STABLES,COIN_NETWORKS
from .realized_profit_reserve import PROFIT_FILLS_SQL,protected_profit_atomic


def summarize_pending_funding(rows,*,asset,stable,stable_usd):
    if asset not in COIN_NETWORKS or stable not in ETHEREUM_STABLES:
        raise ValueError("Invalid funding scope")
    with localcontext() as ctx:
        ctx.prec=100
        rate=Decimal(str(stable_usd))
        if not rate.is_finite() or rate<=0:
            raise ValueError("Invalid stable reference")
        wallet_atomic=0
        coin_usd=Decimal(0)
        finalized=[row for row in rows if row["unit"]=="FINALIZED_PROFIT_INPUT"]
        pending=[row for row in rows if row["unit"]!="FINALIZED_PROFIT_INPUT"]
        reserve=protected_profit_atomic(finalized,stable=stable)
        for row in pending:
            if row["stable"]!=stable or row["asset"] not in (*COIN_NETWORKS,"DOGE"):
                raise ValueError("Funding journal scope differs")
            value=Decimal(row["amount"])
            if not value.is_finite() or value<=0:
                raise ValueError("Invalid reserved funding")
            if row["unit"]=="USD":
                usd=value
                atomic=int((value/rate*10**6).to_integral_value(rounding=ROUND_CEILING))
            elif row["unit"]=="STABLE":
                units=value*10**6
                if units!=units.to_integral_value():
                    raise ValueError("Nonexact Ethereum stable units")
                atomic=int(units)
                usd=value*rate
            elif row["unit"]=="ATOMIC":
                if value!=value.to_integral_value():
                    raise ValueError("Nonexact journal atomics")
                atomic=int(value)
                usd=value.scaleb(-6)*rate
            else:
                raise ValueError("Unknown funding journal unit")
            wallet_atomic+=atomic
            if row["asset"]==asset:
                coin_usd+=usd
        # Existing sizing callers subtract the combined unavailable cash;
        # the protected surplus is NOT an in-flight order or coin exposure.
        return {"pending_wallet_stable_atomic":str(wallet_atomic+reserve["protected_gross_atomic"]),
                "pending_order_stable_atomic":str(wallet_atomic),
                "protected_profit_gross_atomic":str(reserve["protected_gross_atomic"]),
                "protected_profit_by_asset_atomic":{a:str(v) for a,v in reserve["protected_gross_by_asset_atomic"].items()},
                "profit_reserve_net_verified":False,
                "unmatched_profit_sells":reserve["unmatched_automatic_sells"],
                "pending_coin_usd":format(coin_usd,"f"),
                "pending_entries":len(pending),"atomic_claimed":False}


# Each original journal is queried once. The old BTC/ETH and EVM systems can
# overlap; conservative overcount is possible during migration. Do not
# interpret this snapshot as an exact consolidated accounting ledger.
_PENDING_SQL="""SELECT asset,stable,reserved_notional_usd AS amount,'USD' AS unit
    FROM mininex_live_order_intents
    WHERE privy_did=%(did)s AND stable=%(stable)s AND side='BUY'
      AND state IN ('RESERVED','SENDING','SUBMITTED','RECONCILIATION_REQUIRED','REORG_PENDING')
    UNION ALL
    SELECT asset,stable,amount_usd,'USD'
    FROM mininex_live_evm_budget_reservations
    WHERE privy_did=%(did)s AND stable=%(stable)s AND side='BUY'
      AND asset IN ('ETH','LINK','QNT','UNI','BTC','XRP','SOL','TRX')
      AND state IN ('RESERVED','SENDING','SUBMITTED','RECONCILIATION_REQUIRED')
    UNION ALL
    SELECT asset,stable,input_amount,'STABLE'
    FROM mininex_live_native_order_intents
    WHERE privy_did=%(did)s AND stable=%(stable)s AND side='BUY'
      AND chain='ethereum-mainnet'
      AND (status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED','SOURCE_FINALIZED')
           OR (status='PREPARED' AND quote_expires_at>now()))
      AND (asset, intent_key) IS DISTINCT FROM
          (%(excluded_native_asset)s::text,%(excluded_native_key)s::text)
    UNION ALL
    SELECT q.asset,q.stable,q.input_atomic,'ATOMIC'
    FROM mininex_ethereum_settlement_quotes q
    WHERE q.privy_did=%(did)s AND q.stable=%(stable)s AND q.side='BUY'
      AND q.source_chain_id=1 AND NOT EXISTS(
      SELECT 1 FROM mininex_ethereum_settlement_finality f WHERE f.quote_id=q.quote_id
    ) AND (
        EXISTS(SELECT 1 FROM mininex_ethereum_settlement_manual_attempts m
            WHERE m.quote_id=q.quote_id AND m.privy_did=q.privy_did
            AND m.final_outcome IS NULL
            AND m.state IN ('AWAITING_SIGNATURE','SUBMITTED','UNCERTAIN'))
        OR EXISTS(SELECT 1 FROM mininex_ethereum_settlement_submissions s
            WHERE s.quote_id=q.quote_id AND s.privy_did=q.privy_did
            AND s.state IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED'))
      )"""


# One database statement/snapshot for pending funding and finalized profit:
# no gap in which a newly settled SELL is treated as free automatic cash.
_PROTECTED_FUNDING_SQL="""WITH pending AS ("""+_PENDING_SQL+"""),
    finalized AS ("""+PROFIT_FILLS_SQL+""")
    SELECT asset,stable,amount,unit,NULL::text AS side,
      NULL::numeric AS source_amount,NULL::numeric AS destination_amount,
      NULL::text AS coin_wallet,NULL::text AS family,NULL::text AS fill_key,
      NULL::boolean AS automatic,NULL::timestamptz AS settled_at FROM pending
    UNION ALL
    SELECT asset,stable,0::numeric,'FINALIZED_PROFIT_INPUT',side,
      source_amount,destination_amount,coin_wallet,family,fill_key,automatic,settled_at
    FROM finalized ORDER BY settled_at NULLS FIRST,fill_key NULLS FIRST"""


def read_pending_ethereum_funding(did,*,asset,stable,stable_usd):
    did=_safe_did(did)
    if asset not in COIN_NETWORKS or stable not in ETHEREUM_STABLES:
        raise ValueError("Invalid funding scope")
    db=_db_url()
    try:
        _ensure_schema(db)
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(_PROTECTED_FUNDING_SQL,{"did":did,"stable":stable,
                    "excluded_native_asset":None,"excluded_native_key":None})
                rows=cur.fetchall()
        return summarize_pending_funding(rows,asset=asset,stable=stable,stable_usd=stable_usd)
    except psycopg.Error:
        raise HTTPException(503,"Ethereum funding journal unavailable") from None


def assert_ethereum_funds_locked(cur,*,did,asset,stable,input_atomic,balance_atomic,stable_usd,
                                 excluded_native_key=None,protect_realized=True):
    """Recheck shared funding under the same owner lock as new bridge claims.

    Legacy workers still need migration to the identical atomic claim path
    before cross-engine automatic trading is enabled.
    """
    if type(balance_atomic) is not int or type(input_atomic) is not int or input_atomic<=0 or balance_atomic<0:
        raise ValueError("Exact observed Ethereum token balance required")
    cur.execute(_PROTECTED_FUNDING_SQL if protect_realized else _PENDING_SQL,{"did":did,"stable":stable,
        "excluded_native_asset":asset if excluded_native_key is not None else None,
        "excluded_native_key":excluded_native_key})
    pending=summarize_pending_funding(cur.fetchall(),asset=asset,stable=stable,stable_usd=stable_usd)
    if input_atomic+int(pending["pending_wallet_stable_atomic"])>balance_atomic:
        raise HTTPException(409,"Ethereum cash reserved for another order or protected profit")
    return pending
