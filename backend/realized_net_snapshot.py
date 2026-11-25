"""One owner-scoped valuation snapshot for funding and trade history."""
from fractions import Fraction
from .realized_profit_reserve import PROFIT_FILLS_SQL
from .realized_net_accounting import exact

NET_FILLS_SQL="""
    SELECT f.*,COALESCE(v.evidence,e.evidence) AS evidence
    FROM ("""+PROFIT_FILLS_SQL.replace("=%(stable)s"," IN ('USDC','USDT')")+""") f
    LEFT JOIN mininex_staged_net_valuations v ON f.family='CROSS_EVM'
        AND v.quote_id::text=f.fill_key AND v.privy_did=%(did)s AND v.asset=f.asset AND v.stable=f.stable
    LEFT JOIN mininex_ethereum_net_valuations e ON f.family IN ('ETHEREUM','WRAPPED_SPOT')
        AND e.tx_hash=f.fill_key AND e.privy_did=%(did)s AND e.asset=f.asset AND e.stable=f.stable
"""

def read_ratio(value,*,positive=False):
    if (type(value) is not list or len(value)!=2 or
            any(type(x) is not str or not x.isascii() or not x.isdecimal() or len(x)>256 for x in value)):
        raise ValueError("Invalid historical valuation ratio")
    if int(value[1])<=0:
        raise ValueError("Invalid historical valuation denominator")
    result=Fraction(int(value[0]),int(value[1]))
    if positive and not result:
        raise ValueError("Missing historical settlement rate")
    return result

def hydrate_net_rows(rows):
    result=[]
    for original in rows:
        row=dict(original)
        value=row.pop("evidence",None)
        if value is not None:
            amount=exact(row["source_amount"] if row["side"]=="BUY" else row["destination_amount"])*10**6
            if (value["version"]!=1 or value["side"]!=row["side"] or value["stable"]!=row["stable"]
                    or amount!=int(value["stable_atomic"])):
                raise ValueError("Stored historical valuation no longer matches original fill")
            row["stable_usd"]=read_ratio(value["stable_usd"],positive=True)
            row["network_cost_usd"]=read_ratio(value["network_cost_usd"])
        result.append(row)
    return result
