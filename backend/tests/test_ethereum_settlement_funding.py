import pytest
from backend.ethereum_settlement_funding import summarize_pending_funding

def test_all_engine_reservations_use_one_stable_wallet_not_one_coin():
    rows=[
        dict(asset="POL",stable="USDC",unit="ATOMIC",amount="10000000"),
        dict(asset="LINK",stable="USDC",unit="USD",amount="20"),
        dict(asset="BTC",stable="USDC",unit="STABLE",amount="15"),
    ]
    report=summarize_pending_funding(rows,asset="POL",stable="USDC",stable_usd="1")
    assert report["pending_wallet_stable_atomic"]=="45000000"
    assert report["pending_coin_usd"]=="10.000000"
    assert report["atomic_claimed"] is False

def test_usd_reservation_rounds_up_and_rejects_wrong_stable():
    rows=[dict(asset="ETH",stable="USDT",unit="USD",amount="1")]
    assert summarize_pending_funding(rows,asset="ETH",stable="USDT",
        stable_usd="0.99")["pending_wallet_stable_atomic"]=="1010102"
    with pytest.raises(ValueError):
        summarize_pending_funding(rows,asset="ETH",stable="USDC",stable_usd="1")
