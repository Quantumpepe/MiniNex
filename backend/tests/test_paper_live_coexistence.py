"""PAPER mode cannot fabricate a real owner-scoped trading grant."""
from backend import app as backend_app
from backend import eth_auto_live_sessions


def test_paper_stays_available_when_eth_live_global_flag_is_on(monkeypatch):
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED", "true")
    monkeypatch.delenv("MININEX_PAPER_ENABLED", raising=False)
    assert backend_app._paper_visible() is True
    response = backend_app.user_paper_availability()
    import json
    body = json.loads(response.body)
    assert body["paper_available"] is True
    assert body["live_trading_enabled"] is True


def test_paper_cutover_does_not_grant_real_wallet_execution(monkeypatch):
    monkeypatch.setenv("MININEX_PAPER_ENABLED","false")
    monkeypatch.setenv("DATABASE_URL","postgresql://unused.example/mininex")
    assert backend_app._paper_visible() is False
    assert eth_auto_live_sessions._worker_approved() is True
    # Service scheduling never implies that any user signed Start Trade.
    import inspect
    code=inspect.getsource(eth_auto_live_sessions.active_sessions)
    assert "s.status='ACTIVE'" in code
    assert "p.status='GRANT_VERIFIED'" in code


def test_paper_does_not_enable_live_engine(monkeypatch):
    monkeypatch.setenv("MININEX_PAPER_ENABLED", "true")
    monkeypatch.delenv("MININEX_LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.setenv("MININEX_ETH_AUTO_WORKER_ENABLED", "explicit-live")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused.example/mininex")
    assert backend_app._paper_visible() is True
    assert eth_auto_live_sessions._worker_approved() is True
