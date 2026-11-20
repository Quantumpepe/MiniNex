"""Original BTC auto source selection avoids manual UTXO choice."""
import pytest

from backend import native_btc_auto_utxo as m
from backend.tests.test_native_btc_thor_outputs import (
    SOURCE, SOURCE_SCRIPT, quote, AMOUNT, NOW,
)

A="aa"*32
B="bb"*32


def _candidates():
    return [
        {"txid":A,"vout":0,"value":500000,"status":{"confirmed":True}},
        {"txid":B,"vout":1,"value":3500000,"status":{"confirmed":True}},
        {"txid":"cc"*32,"vout":0,"value":4000000,
         "status":{"confirmed":False}},
    ]


def test_confirmed_original_owner_utxo_auto_selection_minimizes_inputs():
    candidates=m.parse_original_btc_utxo_candidates(SOURCE,_candidates())
    assert len(candidates)==2
    assert candidates[0]["txid"]==B
    assert candidates[0]["value_sats"]==3500000
    assert candidates[0]["script_pubkey_hex"]==SOURCE_SCRIPT
    result=m.select_original_btc_utxos(
        quote=quote(),amount=AMOUNT,now=NOW,candidates=candidates,
        fee_rate_sats_per_vbyte=5,
    )
    assert result["input_count"]==1
    assert result["input_total_sats"]=="3500000"
    assert result["input_utxos"][0]["txid"]==B
    assert result["owner_change_sats"]!="0"
    for key in ("candidate_outpoints_independently_verified",
                "owner_privy_policy_verified","wallet_signed",
                "source_transaction_sent","execution_enabled","can_submit"):
        assert result[key] is False


def test_auto_selection_can_combine_two_utxos_when_one_does_not_cover_fee():
    raw=_candidates()[:2]
    raw[0]["value"]=2000000
    raw[1]["value"]=1100000
    result=m.select_original_btc_utxos(
        quote=quote(),amount=AMOUNT,now=NOW,
        candidates=m.parse_original_btc_utxo_candidates(SOURCE,raw),
        fee_rate_sats_per_vbyte=5,
    )
    assert result["input_count"]==2
    assert result["input_total_sats"]=="3100000"


@pytest.mark.parametrize("mutation", [
    lambda rows: rows.append(dict(rows[0])),
    lambda rows: rows[0].update(txid="bad"),
    lambda rows: rows[0].update(vout=-1),
    lambda rows: rows[0].update(value=500000.5),
    lambda rows: rows[0].update(value=-1),
    lambda rows: rows[0].update(status={"confirmed":"true"}),
])
def test_auto_selection_discovery_rejects_duplicate_or_forged_entries(mutation):
    raw=_candidates()
    mutation(raw)
    with pytest.raises(ValueError):
        m.parse_original_btc_utxo_candidates(SOURCE,raw)


def test_auto_selection_does_not_spend_unconfirmed_funding():
    raw=[{"txid":A,"vout":0,"value":8000000,
          "status":{"confirmed":False}}]
    candidates=m.parse_original_btc_utxo_candidates(SOURCE,raw)
    assert candidates==[]
    with pytest.raises(ValueError):
        m.select_original_btc_utxos(
            quote=quote(),amount=AMOUNT,now=NOW,candidates=candidates,
            fee_rate_sats_per_vbyte=5,
        )


def test_auto_selection_without_enough_funds_fails_without_spend():
    candidates=m.parse_original_btc_utxo_candidates(
        SOURCE,[{"txid":A,"vout":1,"value":100000,
                 "status":{"confirmed":True}}])
    with pytest.raises(ValueError):
        m.select_original_btc_utxos(
            quote=quote(),amount=AMOUNT,now=NOW,
            candidates=candidates,fee_rate_sats_per_vbyte=5,
        )


def test_original_auto_selection_cannot_use_different_wallet_or_vault():
    original=m.parse_original_btc_utxo_candidates(SOURCE,_candidates())
    for mutate in (
        lambda q: q.update(source_wallet="bc1q"+"q"*38),
        lambda q: q.update(inbound_vault="bc1q"+"p"*38),
        lambda q: q.update(amount="0.001"),
    ):
        q=quote()
        mutate(q)
        with pytest.raises(ValueError):
            m.select_original_btc_utxos(
                quote=q,amount=AMOUNT,now=NOW,
                candidates=original,fee_rate_sats_per_vbyte=5,
            )


def test_no_btc_signing_or_spend_in_discovery_selection():
    import inspect
    source=inspect.getsource(m.select_original_btc_utxos)
    for dangerous in ("sendrawtransaction","PrivyClient","psycopg",
                      "private_key", "seed_phrase", "broadcast("):
        assert dangerous not in source
