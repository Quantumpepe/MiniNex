"""Owner-scoped, confirmed Ethereum ERC20 position for automatic Stop/Exit."""
from decimal import Decimal,localcontext
from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row
from .storage import _db_url,_ensure_schema,_safe_did

TOKEN_EXIT_ASSETS=frozenset(("LINK","QNT","UNI","BTC","XRP"))
ETHEREUM_EXIT_ASSETS=TOKEN_EXIT_ASSETS | {"ETH"}


def token_exit_position_locked(cur,did,asset,wallet):
    if asset not in ETHEREUM_EXIT_ASSETS:
        raise ValueError("Unsupported token exit")
    cur.execute("""SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN destination_amount
                  ELSE -source_amount END),0) AS position
        FROM mininex_live_evm_verified_fills
        WHERE privy_did=%s AND asset=%s AND wallet_address=%s AND chain_id=1""",
        (did,asset,wallet.lower()))
    row=cur.fetchone()
    if not row or row["position"] is None:
        raise HTTPException(409,"Original token position unavailable")
    held=Decimal(row["position"])
    if not held.is_finite() or held<0:
        raise HTTPException(409,"Original token inventory requires reconciliation")
    return held


def read_token_exit_position(did,asset,wallet):
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return token_exit_position_locked(cur,_safe_did(did),asset,wallet)


def assert_token_exit_quantity_locked(cur,did,asset,wallet,amount_atomic):
    if type(amount_atomic) not in (str,int) or not str(amount_atomic).isascii() or not str(amount_atomic).isdecimal():
        raise HTTPException(409,"Exact original exit amount required")
    with localcontext() as ctx:
        ctx.prec=100
        quantity=Decimal(amount_atomic).scaleb(-8 if asset=="BTC" else -18)
        held=token_exit_position_locked(cur,did,asset,wallet)
        if not 0<quantity<=held:
            raise HTTPException(409,"Exit exceeds verified strategy position")


def assert_central_eth_exit_ready_locked(cur,did):
    """Do not overlap legacy Ethereum automation or unaccounted legacy positions."""
    cur.execute("""SELECT 1 FROM mininex_live_eth_auto_sessions
        WHERE privy_did=%s AND status='ACTIVE' LIMIT 1""",(did,))
    if cur.fetchone() is not None:
        raise HTTPException(409,"Pause legacy Ethereum session before central exit")
    cur.execute("""SELECT 1 FROM mininex_live_order_intents
        WHERE privy_did=%s AND asset='ETH' AND state IN
        ('RESERVED','SENDING','SUBMITTED','RECONCILIATION_REQUIRED','REORG_PENDING') LIMIT 1""",(did,))
    if cur.fetchone() is not None:
        raise HTTPException(409,"Legacy Ethereum settlement unresolved")
    cur.execute("""SELECT 1 FROM mininex_live_eth_auto_native_steps
        WHERE privy_did=%s AND status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED') LIMIT 1""",(did,))
    if cur.fetchone() is not None:
        raise HTTPException(409,"Legacy Ethereum conversion unresolved")
    cur.execute("""SELECT COALESCE(SUM(CASE WHEN l.side='BUY' THEN l.destination_amount
                       ELSE -l.source_amount END),0) AS position
        FROM mininex_live_eth_verified_legs l
        JOIN mininex_live_eth_auto_broadcasts b ON b.privy_did=l.privy_did AND b.tx_hash=l.tx_hash
        WHERE l.privy_did=%s AND l.kind='SWAP'
          AND NOT EXISTS(SELECT 1 FROM mininex_live_evm_verified_fills f
            WHERE f.privy_did=l.privy_did AND f.chain_id=1 AND f.tx_hash=l.tx_hash)""",(did,))
    row=cur.fetchone()
    if not row or Decimal(row["position"])!=0:
        raise HTTPException(409,"Legacy Ethereum position requires reconciliation")


def assert_eth_exit_deposit_locked(cur,did,wallet,amount):
    assert_central_eth_exit_ready_locked(cur,did)
    assert_token_exit_quantity_locked(cur,did,"ETH",wallet,amount)


def assert_eth_exit_swap_locked(cur,did,wallet,key,amount):
    assert_eth_exit_deposit_locked(cur,did,wallet,amount)
    cur.execute("""SELECT amount_wei FROM mininex_live_evm_native_sell_intents
        WHERE privy_did=%s AND asset='ETH' AND intent_key=%s
          AND wallet_address=%s AND status='FINALIZED_SUCCESS'""",(did,key,wallet))
    row=cur.fetchone()
    if not row or Decimal(row["amount_wei"])!=Decimal(amount):
        raise HTTPException(409,"Exit requires original finalized ETH deposit")
