"""No live accounts or transactions: RPC transport and authenticated route tests."""
import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api, multicoin_evm_balances as m
from backend.tests.test_wallet_balances import signed, DID, ETH, record


@pytest.fixture
def rpc(monkeypatch):
    seen = []
    faults = {}
    real_client = httpx.AsyncClient
    chains = {row[2]: row[0] for row in m._RPC.values()}
    for row in m._RPC.values():
        monkeypatch.delenv(row[3], raising=False)
    async def user(did):
        assert did == DID
        return record()
    monkeypatch.setattr(m, 'fetch_privy_user', user)
    def respond(request):
        body = json.loads(request.content)
        seen.append((str(request.url), body))
        method = body['method']
        chain = chains[str(request.url).rstrip('/')]
        key = (chain, method, body['params'][0]['to'] if method == 'eth_call' else None)
        if key in faults:
            value = faults[key]
            if isinstance(value, Exception):
                raise value
            return httpx.Response(200, json=value)
        amount = 10**18 + 123
        result = hex(chain) if method == 'eth_chainId' else (
            '0x'+format(amount, '064x') if method == 'eth_call' else hex(amount))
        return httpx.Response(200, json={'jsonrpc':'2.0','id':body['id'],'result':result})
    monkeypatch.setattr(m.httpx, 'AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(respond), **kw))
    return seen, faults


def test_exact_networks_contracts_decimals_and_read_only_calls(rpc):
    seen, _ = rpc
    result = asyncio.run(m.get_multicoin_evm_balances(DID))
    assert {key: row[0] for key, row in m._RPC.items()} == {
        'BNB':56,'POL':137,'AVAX':43114,'ETH_TOKENS':1}
    assert {k:v.lower() for k,v in m._ETH_TOKENS.items()} == {
        'LINK':'0x514910771af9ca656af840dff83e8264ecf986ca',
        'UNI':'0x1f9840a85d5af5bf1d1762f925bddadc4201f984'}
    assert result['asset_order'] == ['BNB','POL','AVAX','LINK','UNI']
    assert result['real_trading_enabled'] is result['execution_available'] is False
    assert result['wallet_funds_used_for_paper'] is False
    for row in result['assets'].values():
        assert row['balance'] == '1.000000000000000123'
        assert row['decimals'] == 18
        assert row['status'] == 'verified'
    assert len(seen) == 9
    assert {body['method'] for _, body in seen} == {'eth_chainId','eth_getBalance','eth_call'}
    for _, body in seen:
        if body['method'] == 'eth_getBalance':
            assert body['params'] == [ETH, 'latest']
        if body['method'] == 'eth_call':
            assert body['params'][0]['data'] == '0x70a08231'+'0'*24+ETH[2:]


@pytest.mark.parametrize('bad',[
    {'jsonrpc':'2.0','id':1,'result':'0x1'},
    {'jsonrpc':'2.0','id':999,'result':'0x38'},
    {'jsonrpc':'2.0','id':1,'result':'0x38','error':{'code':-1}},
    httpx.ReadTimeout('offline'),
])
def test_bad_chain_isolated_from_other_chains(rpc, bad):
    seen, faults = rpc
    faults[(56,'eth_chainId',None)] = bad
    assets = asyncio.run(m.get_multicoin_evm_balances(DID))['assets']
    assert assets['BNB']['status'] == 'unavailable'
    assert assets['BNB']['balance'] is None
    assert all(assets[a]['status']=='verified' for a in ('POL','AVAX','LINK','UNI'))
    assert not any(url.rstrip('/')==m._RPC['BNB'][2] and b['method']=='eth_getBalance' for url,b in seen)


def test_bad_token_does_not_hide_other_token_and_zero_is_real(rpc):
    _, faults = rpc
    faults[(1,'eth_call',m._ETH_TOKENS['LINK'])] = {'jsonrpc':'2.0','id':3,'result':'0x0'}
    faults[(1,'eth_call',m._ETH_TOKENS['UNI'])] = {'jsonrpc':'2.0','id':4,'result':'0x'+'0'*64}
    assets = asyncio.run(m.get_multicoin_evm_balances(DID))['assets']
    assert assets['LINK']['balance'] is None
    assert assets['LINK']['status'] == 'unavailable'
    assert assets['UNI']['balance'] == '0'
    assert assets['UNI']['status'] == 'verified'


@pytest.mark.parametrize('user',[record(eth=None), {'id':'did:privy:other','linked_accounts':[]}])
def test_missing_or_foreign_wallet_never_queries_rpc(rpc,monkeypatch,user):
    async def fetch(did):return user
    monkeypatch.setattr(m,'fetch_privy_user',fetch)
    with pytest.raises(HTTPException) as error:
        asyncio.run(m.get_multicoin_evm_balances(DID))
    assert error.value.status_code == 503
    assert rpc[0] == []


def test_route_auth_identity_cache_and_method(signed,monkeypatch):
    seen=[]
    async def balances(did):
        seen.append(did)
        return {'mode':'REAL_MAINNET_BALANCES_READ_ONLY'}
    monkeypatch.setattr(api,'get_multicoin_evm_balances',balances)
    client=TestClient(api.app)
    path='/api/wallet/multicoin-balances'
    assert client.get(path).status_code==401
    assert client.get(path,headers={'Authorization':'Bearer fake'}).status_code==401
    reply=client.get(path+'?did=did:privy:other&address=0x'+'b'*40,headers=signed())
    assert reply.status_code==200
    assert reply.headers['cache-control']=='private, no-store'
    assert seen==[DID]
    assert client.post(path,headers=signed()).status_code==405


def test_large_holdings_keep_all_18_decimals():
    assert m._units(123456789012345678901234567890123456, m.WEI) == '123456789012345678.901234567890123456'
