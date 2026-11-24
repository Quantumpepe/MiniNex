"""Owner-scoped, confirmed Ethereum ERC20 position for automatic Stop/Exit."""
from decimal import Decimal,localcontext
from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row
from .storage import _db_url,_ensure_schema,_safe_did

TOKEN_EXIT_ASSETS=frozenset(("LINK","QNT","UNI"))


def token_exit_position_locked(cur,did,asset,wallet):
    if asset not in TOKEN_EXIT_ASSETS:
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
        quantity=Decimal(amount_atomic).scaleb(-18)
        held=token_exit_position_locked(cur,did,asset,wallet)
        if not 0<quantity<=held:
            raise HTTPException(409,"Exit exceeds verified strategy position")
