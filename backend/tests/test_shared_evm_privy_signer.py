"""Real P-256 request signature uses the exact same EVM body on six chains."""
import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec

from backend.shared_evm_swap_receipts import _pair
from backend.shared_evm_execution import EvmPair,unsigned_exact_input_single
from backend.shared_evm_privy_transport import bound_privy_evm_swap_request
from backend.shared_evm_privy_signer import sign_privy_evm_swap_request
from backend.eth_privy_p256_signer import (
    _canonical_authorization_payload,sign_privy_eth_request
)

WALLET="0x"+"ab"*20
ID="wallet123456"
KEY="sig_abcdef01234567890123456789"


@pytest.fixture
def signer(monkeypatch):
    private=ec.generate_private_key(ec.SECP256R1())
    der=private.private_bytes(
        serialization.Encoding.DER,serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    public=private.public_key().public_bytes(
        serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setenv("MININEX_PRIVY_ETH_AUTH_P256_PKCS8",
                       base64.b64encode(der).decode())
    monkeypatch.setenv("MININEX_PRIVY_ETH_SIGNER_PUBLIC_KEY_SPKI",
                       base64.b64encode(public).decode())
    return private.public_key()


def prepare(asset,stable,side):
    pair=_pair(asset,stable,side)
    return unsigned_exact_input_single(
        pair=EvmPair(asset,pair["chain_id"],pair["network"],
                     pair["source_token"],pair["output_token"],pair["router"],500),
        wallet=WALLET,amount_in=1000000,min_out=900000)


@pytest.mark.parametrize("asset,stable",[
    ("ETH","USDC"),("LINK","USDC"),("UNI","USDT"),
    ("BNB","USDT"),("POL","USDC"),("AVAX","USDC"),
])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_common_evm_signer_cryptographically_binds_six_chains(
    signer,asset,stable,side
):
    tx=prepare(asset,stable,side)
    kwargs=dict(asset=asset,wallet_id=ID,wallet=WALLET,tx=tx,
                side=side,stable=stable,input_atomic="1000000",
                minimum_output_atomic="900000",idempotency_key=KEY)
    signature=base64.b64decode(
        sign_privy_evm_swap_request(**kwargs),validate=True)
    url,body=bound_privy_evm_swap_request(**{
        key:value for key,value in kwargs.items() if key!="idempotency_key"})
    canonical=_canonical_authorization_payload(url=url,body=body,key=KEY)
    signer.verify(signature,canonical,ec.ECDSA(hashes.SHA256()))
    assert json.loads(canonical)["body"]["caip2"]==(
        "eip155:"+str(_pair(asset,stable,side)["chain_id"]))
    if asset=="ETH":
        old_sig=base64.b64decode(sign_privy_eth_request(
            wallet_id=ID,wallet=WALLET,tx=tx,side=side,stable=stable,
            input_atomic="1000000",minimum_output_atomic="900000",
            idempotency_key=KEY))
        signer.verify(old_sig,canonical,ec.ECDSA(hashes.SHA256()))


@pytest.mark.parametrize("asset,stable,incorrect_chain",[
    ("BNB","USDT",1),("POL","USDC",56),
    ("AVAX","USDC",137),("LINK","USDC",43114),
])
def test_evm_signer_refuses_cross_chain_swap_even_with_valid_key(
    signer,asset,stable,incorrect_chain
):
    tx=prepare(asset,stable,"BUY")
    tx["chainId"]=incorrect_chain
    with pytest.raises(ValueError):
        sign_privy_evm_swap_request(
            asset=asset,wallet_id=ID,wallet=WALLET,tx=tx,
            side="BUY",stable=stable,input_atomic="1000000",
            minimum_output_atomic="900000",idempotency_key=KEY)


def test_signer_without_configured_key_still_refuses_to_sign(monkeypatch):
    monkeypatch.delenv("MININEX_PRIVY_ETH_AUTH_P256_PKCS8",raising=False)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        sign_privy_evm_swap_request(
            asset="BNB",wallet_id=ID,wallet=WALLET,tx=prepare("BNB","USDT","BUY"),
            side="BUY",stable="USDT",input_atomic="1000000",
            minimum_output_atomic="900000",idempotency_key=KEY)
    assert error.value.status_code==503
