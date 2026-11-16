"""One chain-bound P-256 Privy request signer for all six MiniNex EVM coins.

The existing ETH AUTO signer calls the same implementation. This signs only
exact server-validated calldata, but does not create permission, consent,
quorum registration, or a durable SENDING order claim. The worker must
independently verify all of those before using this signature on mainnet.
"""
from __future__ import annotations

from .shared_evm_privy_transport import bound_privy_evm_swap_request


def sign_privy_evm_swap_request(
    *, asset: str, wallet_id: str, wallet: str, tx: dict,
    side: str, stable: str, input_atomic: str,
    minimum_output_atomic: str, idempotency_key: str,
) -> str:
    """Sign EXACT request body/CAIP2 with the already-registered P-256 key."""
    url,body=bound_privy_evm_swap_request(
        asset=asset,wallet_id=wallet_id,wallet=wallet,tx=tx,
        side=side,stable=stable,input_atomic=input_atomic,
        minimum_output_atomic=minimum_output_atomic,
    )
    # Lazy import avoids a circular dependency: ETH's public historical API
    # forwards here and the actual shared signer stays in its original
    # audited private-key handling implementation.
    from .eth_privy_p256_signer import sign_privy_rpc_payload
    return sign_privy_rpc_payload(
        url=url,body=body,idempotency_key=idempotency_key)
