"""Read-only profit allocation suggestion, not spending authorization."""
from .ethereum_settlement_plan import COIN_NETWORKS,ETHEREUM_STABLES


def _atomic(n):
    if type(n) is not int or n<0:
        raise ValueError("Exact nonnegative settlement atomics required")
    return n


def propose_profit_allocation(*,asset,stable,gross_reserved_atomic,
        net_profit_after_fees_atomic=None,requested_reinvest_atomic=0,
        free_budget_atomic=0):
    if asset not in COIN_NETWORKS or stable not in ETHEREUM_STABLES:
        raise ValueError("Unknown coin or settlement token")
    gross=_atomic(gross_reserved_atomic)
    requested=_atomic(requested_reinvest_atomic)
    budget=_atomic(free_budget_atomic)
    if net_profit_after_fees_atomic is None:
        return {"asset":asset,"stable":stable,"protected_gross_atomic":str(gross),
                "confirmed_net_profit_atomic":None,"suggested_reinvest_atomic":"0",
                "suggested_saved_net_atomic":None,"cost_reconciliation_pending":True,
                "can_release":False}
    net=_atomic(net_profit_after_fees_atomic)
    eligible=min(net,gross)
    reinvest=min(eligible,requested,budget)
    return {"asset":asset,"stable":stable,"protected_gross_atomic":str(gross),
            "confirmed_net_profit_atomic":str(net),
            "suggested_reinvest_atomic":str(reinvest),
            "suggested_saved_net_atomic":str(eligible-reinvest),
            "cost_reconciliation_pending":False,"can_release":False}
