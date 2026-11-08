"""Static MiniNex UI is allowed to fetch public, PAPER-only backend values."""
from fastapi.testclient import TestClient

from backend.app import app


def test_ui_origin_can_access_demo_api():
    response = TestClient(app).get(
        '/api/paper/status',
        headers={'Origin': 'https://mininex-ui.onrender.com'},
    )
    assert response.status_code == 200
    assert response.headers['access-control-allow-origin'] == 'https://mininex-ui.onrender.com'
    payload = response.json()
    assert payload['mode'] == 'PAPER_AUTO_DEMO'
    assert payload['real_trading_enabled'] is False
    assert payload['wallet_connected'] is False


def test_unknown_origin_not_allowed_to_read_cross_origin():
    response = TestClient(app).get('/health', headers={'Origin': 'https://unknown.example'})
    assert response.status_code == 200
    assert 'access-control-allow-origin' not in response.headers


def test_preflight_requires_known_origin():
    client = TestClient(app)
    ok = client.options('/api/paper/status', headers={
        'Origin': 'https://mininex-ui.onrender.com',
        'Access-Control-Request-Method': 'GET',
    })
    assert ok.status_code == 200
    bad = client.options('/api/paper/status', headers={
        'Origin': 'https://unknown.example',
        'Access-Control-Request-Method': 'GET',
    })
    assert bad.status_code == 400

def test_capacitor_mobile_origins_are_allowed():
    client = TestClient(app)
    for origin in ('https://localhost', 'capacitor://localhost'):
        response = client.get('/api/paper/status', headers={'Origin': origin})
        assert response.status_code == 200
        assert response.headers['access-control-allow-origin'] == origin
        assert response.json()['real_trading_enabled'] is False

def test_paper_mode_stays_disabled_after_mobile_update():
    client = TestClient(app)
    root = client.get('/').json()
    assert root['version'] == '0.5.0'
    assert root['real_orders'] is False
    assert root['trading'] == 'disabled'
