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


def test_tron_fee_reserve_uses_signed_size_live_rates_and_separate_quotas():
    from fastapi import HTTPException
    from backend.native_trx_owner_preflight import _trx_fee_snapshot
    owner=_tron_address_from_public_key(keys.PrivateKey(bytes([1])*32).public_key.to_bytes())
    vault=_tron_address_from_public_key(keys.PrivateKey(bytes([2])*32).public_key.to_bytes())
    unsigned={"raw_data_hex": "ab"*180}
    parameters={"chainParameter":[{"key":"getTransactionFee","value":1000},
                                  {"key":"getMemoFee","value":1000000}]}
    args=dict(source=owner,vault=vault,sun=1000000,parameters=parameters,
              sender={"address":tron_address_hex(owner),"balance":3000000},
              recipient={"address":tron_address_hex(vault)})
    resources={"NetLimit":200,"freeNetLimit":200}
    result=_trx_fee_snapshot(unsigned,resources=resources,**args)
    assert result["bandwidth_required"]==314
    assert result["estimated_source_fee_sun"]=="1314000"
    assert result["source_fee_reserve_sun"]=="1314000"
    assert result["actual_source_fee_sun"] is None
    assert result["fee_reserve_verified"] is True
    result=_trx_fee_snapshot(unsigned,resources={"freeNetLimit":600},**args)
    assert result["estimated_source_fee_sun"]=="1000000"
    assert result["source_fee_reserve_sun"]=="1314000"
    with pytest.raises(HTTPException):
        _trx_fee_snapshot(unsigned,resources=resources,**{**args,"sender":{
            "address":tron_address_hex(owner),"balance":2313999}})
    with pytest.raises(ValueError):
        _trx_fee_snapshot(unsigned,resources=resources,**{**args,"recipient":{}})
    with pytest.raises(ValueError):
        _trx_fee_snapshot(unsigned,resources=resources,**{**args,"parameters":{"chainParameter":[]}})
