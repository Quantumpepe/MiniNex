"""Privy exact request P-256 authentication, no secrets persisted."""
import base64
import json
import pytest
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec

from backend.eth_privy_p256_signer import (
    _canonical_authorization_payload,sign_privy_eth_request,
)
from backend.eth_router02_live_quote import build_unsigned_router02_swap
from backend.eth_privy_auto_transport import bound_privy_rpc_request

WALLET="0x"+"a"*40
KEY="sig_abcdef01234567890123456789"
ID="wallet1234567"
def tx():
    return build_unsigned_router02_swap(wallet=WALLET,
         side="BUY",stable="USDC",amount_in=1_000_000,
         min_out=2_000_000_000_000_000,fee=500)
def args():
    return dict(wallet_id=ID,wallet=WALLET,tx=tx(),side="BUY",
      stable="USDC",input_atomic="1000000",
      minimum_output_atomic="2000000000000000",idempotency_key=KEY)

def test_signs_the_exact_privy_rpc_payload(monkeypatch):
    private=ec.generate_private_key(ec.SECP256R1())
    der=private.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    monkeypatch.setenv("MININEX_PRIVY_ETH_AUTH_P256_PKCS8",
                      base64.b64encode(der).decode())
    sig=base64.b64decode(sign_privy_eth_request(**args()),validate=True)
    body_url,body=bound_privy_rpc_request(wallet_id=ID,wallet=WALLET,
        tx=tx(),side="BUY",stable="USDC",
        input_atomic="1000000",minimum_output_atomic="2000000000000000")
    payload=_canonical_authorization_payload(url=body_url,body=body,key=KEY)
    assert json.loads(payload)["headers"]["privy-idempotency-key"]==KEY
    private.public_key().verify(sig,payload,ec.ECDSA(hashes.SHA256()))

def test_signing_request_is_stable_under_dict_order_changes():
    u="https://api.privy.io/v1/wallets/wallet1234567/rpc"
    body={"method":"eth_sendTransaction","caip2":"eip155:1",
          "params":{"transaction":{"to":WALLET,"value":"0"}}}
    reversed_body={"params":{"transaction":{"value":"0","to":WALLET}},
                   "caip2":"eip155:1","method":"eth_sendTransaction"}
    assert _canonical_authorization_payload(url=u,body=body,key=KEY)==(
        _canonical_authorization_payload(url=u,body=reversed_body,key=KEY))

def test_no_key_never_generates_a_new_signer(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.delenv("MININEX_PRIVY_ETH_AUTH_P256_PKCS8",raising=False)
    with pytest.raises(HTTPException) as e:
        sign_privy_eth_request(**args())
    assert e.value.status_code==503

def test_wrong_curve_rejected(monkeypatch):
    from fastapi import HTTPException
    private=ec.generate_private_key(ec.SECP384R1())
    der=private.private_bytes(serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    monkeypatch.setenv("MININEX_PRIVY_ETH_AUTH_P256_PKCS8",
                      base64.b64encode(der).decode())
    with pytest.raises(HTTPException) as e:
        sign_privy_eth_request(**args())
    assert e.value.status_code==503
