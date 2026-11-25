"""Actual destination credits are required for cross-chain reconciliation."""
import pytest

from backend.ethereum_settlement_status import _net_token_credit, _status_terms, _TRANSFER
from backend.live_eth_quotes import STABLES

WALLET = "0x" + "12" * 20
OTHER = "0x" + "34" * 20
HASH = "0x" + "ab" * 32
DEST = "0x" + "cd" * 32
WPOL = "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270"

def event(token, sender, receiver, value):
    return {"address": token, "topics": [_TRANSFER,
        "0x" + sender[2:].lower().rjust(64, "0"),
        "0x" + receiver[2:].lower().rjust(64, "0")],
        "data": "0x" + hex(value)[2:].rjust(64, "0")}

def test_actual_ethereum_usdc_credit():
    logs = [event(STABLES["USDC"], OTHER, WALLET, 1_000_000),
            event(STABLES["USDC"], WALLET, OTHER, 100_000),
            event(STABLES["USDT"], OTHER, WALLET, 2_000_000)]
    assert _net_token_credit(logs, STABLES["USDC"], WALLET) == 900_000
    assert _net_token_credit(logs, STABLES["USDC"], OTHER) == -900_000

def test_provider_done_requires_exact_source_and_destination():
    payload = {"status": "DONE", "substatus": "COMPLETED",
        "fromAddress": WALLET, "toAddress": WALLET,
        "sending": {"chainId": 137, "txHash": HASH, "amount": "25000000000000000000",
                    "token": {"chainId": 137, "address": WPOL, "decimals": 18}},
        "receiving": {"chainId": 1, "txHash": DEST, "amount": "1000000",
                      "token": {"chainId": 1, "address": STABLES["USDC"], "decimals": 6}}}
    kwargs = dict(wallet=WALLET, tx_hash=HASH, source_chain=137, dest_chain=1,
        source_tokens=[WPOL], dest_token=STABLES["USDC"], source_decimals=18,
        dest_decimals=6, units="25000000000000000000")
    result = _status_terms(payload, **kwargs)
    assert result["state"] == "PROVIDER_COMPLETED"
    assert result["hash"] == DEST
    with pytest.raises(ValueError):
        _status_terms({**payload, "toAddress": OTHER}, **kwargs)
    wrong = {**payload, "receiving": {**payload["receiving"], "chainId": 137}}
    with pytest.raises(ValueError):
        _status_terms(wrong, **kwargs)
    assert _status_terms({**payload, "status": "PENDING"}, **kwargs) is None
    assert _status_terms({**payload, "substatus": "REFUNDED"}, **kwargs)["state"] == "REFUND_OR_PARTIAL"



def test_native_payout_counts_real_successful_transfers_only():
    from backend.ethereum_settlement_status import _net_native_credit
    def call(sender, recipient, amount, **extra):
        return {"type": "CALL", "from": sender, "to": recipient,
                "value": hex(amount), **extra}
    trace = call(OTHER, OTHER, 0, calls=[
        call(OTHER, WALLET, 250),
        call(WALLET, OTHER, 10),
        call(OTHER, OTHER, 0, error="execution reverted",
             calls=[call(OTHER, WALLET, 1000)]),
        {"type": "DELEGATECALL", "from": OTHER, "to": WALLET,
         "value": hex(999), "calls": [call(OTHER, WALLET, 5)]},
    ])
    assert _net_native_credit(trace, WALLET) == 245
    with pytest.raises(ValueError):
        _net_native_credit(call(OTHER, WALLET, 1, value="bad"), WALLET)
