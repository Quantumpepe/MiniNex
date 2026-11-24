import hashlib
import time
from copy import deepcopy
import pytest
from eth_keys import keys
from backend.native_trx_user_signed_send import _tron_address_from_public_key
from backend.native_trx_manual_send import tron_address_hex
from backend.native_trx_owner_preflight import _bytes,_number,verify_tron_swap_wire

def test_tron_sell_wire_matches_memo_and_owner_transfer():
    owner=_tron_address_from_public_key(keys.PrivateKey(bytes([1])*32).public_key.to_bytes())
    vault=_tron_address_from_public_key(keys.PrivateKey(bytes([2])*32).public_key.to_bytes())
    memo="=:ETH.USDT:0x"+"a"*40+"/"+owner
    timestamp=int(time.time()*1000)
    expiry=timestamp+60000
    src=tron_address_hex(owner)
    dst=tron_address_hex(vault)
    amount=1000000
    transfer=_bytes(1,bytes.fromhex(src))+_bytes(2,bytes.fromhex(dst))+_number(3,amount)
    contract=_number(1,1)+_bytes(2,_bytes(1,b"type.googleapis.com/protocol.TransferContract")+_bytes(2,transfer))
    wire=_bytes(1,b"ab")+_bytes(4,b"abcdefgh")+_number(8,expiry)+_bytes(10,memo.encode())+_bytes(11,contract)+_number(14,timestamp)
    tx={"txID":hashlib.sha256(wire).hexdigest(),"raw_data_hex":wire.hex(),"raw_data":{
        "contract":[{"type":"TransferContract","parameter":{"type_url":"type.googleapis.com/protocol.TransferContract",
        "value":{"owner_address":src,"to_address":dst,"amount":amount}}}],
        "ref_block_bytes":b"ab".hex(),"ref_block_hash":b"abcdefgh".hex(),"expiration":expiry,
        "timestamp":timestamp,"data":memo.encode().hex()}}
    args=dict(source=owner,destination=vault,sun=amount,memo=memo)
    assert verify_tron_swap_wire(tx,**args)[0]==tx["txID"].upper()
    changed=deepcopy(tx)
    changed_wire=wire.replace(memo.encode(),b"X"*len(memo))
    changed["raw_data_hex"]=changed_wire.hex()
    changed["txID"]=hashlib.sha256(changed_wire).hexdigest()
    with pytest.raises(ValueError):
        verify_tron_swap_wire(changed,**args)
