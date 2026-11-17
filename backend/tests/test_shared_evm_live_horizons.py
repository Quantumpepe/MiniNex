"""User-selected EVM trading horizons use genuine market observations."""
import pytest

from backend.eth_live_strategy_tick import verified_live_evm_momentum
from backend.extra_market_data import ExtraMarketData

NOW=2000000.0


class Trend:
    def __init__(self,window,period,step):
        self.window=window
        self.period=period
        self.step=step
    def snapshot(self,*,now):
        return {"LINK":{"market_data_valid":True,
                        "price_usdt":100+float(self.window)/20000}}
    def history(self,asset,*,limit):
        assert asset=="LINK"
        return [{"timestamp":float(i),
                 "price_usdt":100+(float(i)-(NOW-self.window))/20000}
                for i in range(int(NOW-self.window),int(NOW)+1,self.step)]


@pytest.mark.parametrize("horizon,window,fast,slow",[
    ("5m",300,60,120),
    ("15m",900,60,180),
    ("1h",3600,300,1200),
    ("24h",86400,3600,21600),
])
def test_owners_each_see_their_selected_signal_window(horizon,window,fast,slow):
    market=Trend(window,0,5 if window<=3600 else 900)
    result=verified_live_evm_momentum(
        market,asset="LINK",now=NOW,horizon=horizon)
    assert result["action"]=="BUY"
    assert result["horizon"]==horizon
    assert result["fast_lookback_seconds"]==fast
    assert result["slow_lookback_seconds"]==slow


@pytest.mark.parametrize("horizon",["5m","15m","1h","24h"])
def test_missing_actual_horizon_data_waits_instead_of_inventing_prices(horizon):
    market=Trend(190,0,5)
    result=verified_live_evm_momentum(
        market,asset="LINK",now=NOW,horizon=horizon)
    if horizon in ("1h","24h"):
        assert result["action"]=="WAIT"
        assert result["reason"]=="LIVE_OBSERVATION_INCOMPLETE"
    else:
        assert result["action"] in ("BUY","WAIT")


@pytest.mark.parametrize("bad",[None,"instant","unlimited","24hours",15])
def test_unrecognized_horizon_cannot_bypass_real_market_evidence(bad):
    with pytest.raises(ValueError):
        verified_live_evm_momentum(
            Trend(900,0,5),asset="LINK",now=NOW,horizon=bad)


def test_24h_real_15minute_candles_survive_large_ws_tick_history():
    market=ExtraMarketData(clock=lambda:NOW)
    long=[{"timestamp":NOW-86400+i*900,
           "price_usdt":100+i*0.05} for i in range(97)]
    market._long_historical["LINK"]=long
    market._historical["LINK"]=[
        {"timestamp":NOW-3999+i,"price_usdt":104+i*0.001}
        for i in range(4000)]
    market._ticks["LINK"].extend(
        {"timestamp":NOW-1499+i,"price_usdt":109+i*0.0001}
        for i in range(1500))
    actual=market.history("LINK",limit=3600)
    assert len(actual)<=3600
    assert actual[0]["timestamp"]==NOW-86400
    assert actual[-1]["timestamp"]==NOW
    assert len([p for p in actual if p["timestamp"]<=NOW-21600])>15
