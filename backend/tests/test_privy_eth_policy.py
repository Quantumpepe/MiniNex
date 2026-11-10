"""Security regression tests for read-only Privy ETH policy inspection."""
import asyncio
import copy
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import privy_eth_policy as p

DID="did:privy:policy-user"
WALLET="0x"+"a"*40
ROUTER="0x"+"b"*40
SIGNER="quorum_mininex_test_ETH"
POLICY_ID="policy_eth_user_exact_test"
NOW=1890000000
ABI=[{"name":"exactInputSingle","type":"function","inputs":[]}]


def c(source,field,op,value,**kwargs):
    return {"field_source":source,"field":field,"operator":op,
            "value":value,**kwargs}


def policy(rules=None):
    if rules is None:
        rules=[{
            "method":"eth_sendTransaction","action":"ALLOW",
            "conditions":[
                c("ethereum_transaction","chain_id","eq","1"),
                c("ethereum_transaction","to","eq",ROUTER),
                c("ethereum_transaction","value","lte",str(10**17)),
                c("ethereum_calldata","function_name","eq","exactInputSingle",abi=ABI),
                c("ethereum_calldata","exactInputSingle.params.recipient","eq",WALLET,abi=ABI),
                c("ethereum_calldata","exactInputSingle.params.amountIn","lte",str(10**8),abi=ABI),
                c("system","current_unix_timestamp","lt",str(NOW+86400)),
            ]
        }]
    return {
        "id":POLICY_ID,"version":"1.0","chain_type":"ethereum",
        "rules":rules,
    }


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("MININEX_PRIVY_ETH_SIGNER_ID",SIGNER)
    monkeypatch.setenv("MININEX_PRIVY_ETH_POLICY_ID",POLICY_ID)
    monkeypatch.setenv("MININEX_ETH_SWAP_ROUTER",ROUTER)
    monkeypatch.setenv("MININEX_PRIVY_ETH_POLICY_SHA256",p._canonical_rules_hash(policy()))
    monkeypatch.setenv("MININEX_PRIVY_APP_SECRET","test-privy-secret")


@pytest.fixture
def headers(monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    public=key.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"policy-session","iat":now,"exp":now+600,
    },key,algorithm="ES256")
    return {"Authorization":"Bearer "+token}


def test_policy_digest_changes_on_rule_or_network_and_ignores_key_order():
    original=policy()
    reordered={"rules":original["rules"],"chain_type":"ethereum",
               "version":"1.0","id":POLICY_ID}
    assert p._canonical_rules_hash(original)==p._canonical_rules_hash(reordered)
    modified=copy.deepcopy(original)
    modified["rules"][0]["conditions"][2]["value"]=str(10**18)
    assert p._canonical_rules_hash(original)!=p._canonical_rules_hash(modified)
    with pytest.raises(ValueError):
        p._canonical_rules_hash({"rules":"ALLOW_ALL"})


def test_conservative_pinned_policy_passes_static_shape_only():
    checked=p.inspect_policy_structure(
        policy(),policy_id=POLICY_ID,wallet_address=WALLET,
        router_address=ROUTER,now=NOW)
    assert checked["structure_checked"] is True
    assert checked["recipient_wallet_bound"] is True
    assert checked["time_bounded"] is True
    assert checked["amount_bounded"] is True


@pytest.mark.parametrize("mutation",[
    lambda x:x.update(chain_type="bitcoin"),
    lambda x:x.update(id="other_policy"),
    lambda x:x.update(version="0.8"),
    lambda x:x["rules"][0].update(method="*"),
    lambda x:x["rules"][0].update(method="eth_signTypedData_v4"),
    lambda x:x["rules"][0]["conditions"].pop(0),
    lambda x:x["rules"][0]["conditions"].pop(1),
    lambda x:x["rules"][0]["conditions"].pop(2),
    lambda x:x["rules"][0]["conditions"].pop(3),
    lambda x:x["rules"][0]["conditions"].pop(4),
    lambda x:x["rules"][0]["conditions"].pop(5),
    lambda x:x["rules"][0]["conditions"].pop(6),
    lambda x:x["rules"][0]["conditions"][0].update(value="56"),
    lambda x:x["rules"][0]["conditions"][1].update(value="0x"+"d"*40),
    lambda x:x["rules"][0]["conditions"][2].update(value=str(10**20)),
    lambda x:x["rules"][0]["conditions"][3].update(value="multicall"),
    lambda x:x["rules"][0]["conditions"][4].update(value="0x"+"f"*40),
    lambda x:x["rules"][0]["conditions"][5].update(value="2000000000000000000"),
    lambda x:x["rules"][0]["conditions"][6].update(value=str(NOW+90*86400)),
])
def test_wrong_broad_or_stale_policy_never_passes(mutation):
    changed=copy.deepcopy(policy())
    mutation(changed)
    result=p.inspect_policy_structure(
        changed,policy_id=POLICY_ID,wallet_address=WALLET,
        router_address=ROUTER,now=NOW)
    assert result["structure_checked"] is False


def test_broad_extra_allow_rule_bypasses_or_semantics_and_is_denied():
    open_rule={"method":"eth_sendTransaction","action":"ALLOW",
               "conditions":[c("ethereum_transaction","chain_id","eq","1")]}
    for extra in (open_rule,{"method":"*","action":"ALLOW","conditions":[]},
                  {"method":"exportPrivateKey","action":"ALLOW","conditions":[]}):
        result=p.inspect_policy_structure(
            policy(rules=policy()["rules"]+[extra]),
            policy_id=POLICY_ID,wallet_address=WALLET,
            router_address=ROUTER,now=NOW)
        assert result["structure_checked"] is False


def test_wrong_recipient_cannot_reuse_other_users_policy():
    result=p.inspect_policy_structure(
        policy(),policy_id=POLICY_ID,wallet_address="0x"+"c"*40,
        router_address=ROUTER,now=NOW)
    assert result["recipient_wallet_bound"] is False
    assert result["structure_checked"] is False


def test_unconfigured_policy_keeps_grants_and_execution_disabled(monkeypatch):
    monkeypatch.delenv("MININEX_PRIVY_ETH_SIGNER_ID",raising=False)
    monkeypatch.delenv("MININEX_PRIVY_ETH_POLICY_ID",raising=False)
    monkeypatch.delenv("MININEX_ETH_SWAP_ROUTER",raising=False)
    monkeypatch.delenv("MININEX_PRIVY_ETH_POLICY_SHA256",raising=False)
    result=asyncio.run(p.get_eth_policy_review(DID))
    assert result["grant_enrollment_enabled"] is False
    assert result["policy_structure_checked"] is False
    assert result["execution_enabled"] is False
    assert result["policy_contents_independently_audited"] is False


def test_pinned_privy_rule_body_is_checked_not_just_policy_id(
        configured,monkeypatch):
    async def user(did):
        assert did==DID
        return {"id":did,"linked_accounts":[{"type":"wallet",
            "chain_type":"ethereum","wallet_client_type":"privy","address":WALLET}]}
    async def get_policy(policy_id):
        assert policy_id==POLICY_ID
        return policy()
    monkeypatch.setattr(p,"fetch_privy_user",user)
    monkeypatch.setattr(p,"fetch_privy_policy",get_policy)
    monkeypatch.setattr(p.time,"time",lambda:NOW)
    result=asyncio.run(p.get_eth_policy_review(DID))
    assert result["policy_digest_matches"] is True
    assert result["policy_structure_checked"] is True
    assert result["recipient_wallet_bound"] is True
    assert result["grant_enrollment_enabled"] is False
    assert result["execution_enabled"] is False
    assert result["policy_contents_independently_audited"] is False

    changed=copy.deepcopy(policy())
    changed["rules"][0]["conditions"][1]["value"]="0x"+"d"*40
    async def modified(_):return changed
    monkeypatch.setattr(p,"fetch_privy_policy",modified)
    result=asyncio.run(p.get_eth_policy_review(DID))
    assert result["policy_digest_matches"] is False
    assert result["policy_structure_checked"] is False


def test_privy_policy_endpoint_fetches_only_expected_fixed_id(
        configured,monkeypatch):
    original=httpx.AsyncClient
    def mocked(*args,**kwargs):
        def respond(request):
            assert str(request.url)==p._POLICY_URL+POLICY_ID
            assert request.headers["privy-app-id"]==p.APP_ID
            assert kwargs.get("follow_redirects") is False
            return httpx.Response(200,json=policy())
        return original(*args,transport=httpx.MockTransport(respond),**kwargs)
    monkeypatch.setattr(p.httpx,"AsyncClient",mocked)
    result=asyncio.run(p.fetch_privy_policy(POLICY_ID))
    assert result["id"]==POLICY_ID
    with pytest.raises(HTTPException):
        asyncio.run(p.fetch_privy_policy("other-user-policy"))


def test_policy_endpoint_requires_privy_identity_and_is_readonly(
        headers,monkeypatch):
    async def fake(did):
        assert did==DID
        return {
            "mode":"PRIVY_POLICY_STATIC_REVIEW","asset":"ETH","chain_id":1,
            "policy_structure_checked":False,"grant_enrollment_enabled":False,
            "execution_enabled":False,
        }
    monkeypatch.setattr(api,"get_eth_policy_review",fake)
    client=TestClient(api.app)
    assert client.get("/api/live/eth/policy").status_code==401
    assert client.get("/api/live/eth/policy",headers={
        "Authorization":"Bearer wrong"}).status_code==401
    resp=client.get("/api/live/eth/policy",headers=headers)
    assert resp.status_code==200
    assert resp.json()["execution_enabled"] is False
    assert resp.headers["cache-control"]=="private, no-store"
    assert client.post("/api/live/eth/policy",headers=headers).status_code==405
    assert not any(x.path=="/api/live/eth/grant" for x in api.app.routes)
