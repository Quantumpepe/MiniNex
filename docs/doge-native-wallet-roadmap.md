# MiniNex native DOGE wallet — fail-closed preparation

Status: **In Arbeit**. DOGE is displayed as one of the 12 coins; live wallet provisioning, deposits, manual swaps and automatic trades remain disabled.

## Native network (not wrapped DOGE)
- Dogecoin mainnet is its own UTXO chain, not Bitcoin/EVM/Solana.
- Only a DOGE-mainnet **P2PKH** address (Base58Check version `0x1e`, checksum SHA256d) is eligible for read-only display. Reject BTC addresses, Dogecoin testnet, invalid checksums and unsupported script address formats.
- This check never establishes *custody* alone: a valid address must also be tied to the authenticated user's verified native wallet record.
- Privy client support for direct DOGE wallet creation is **not verified**. Do not call `createWallet({chainType:'dogecoin'})` without official SDK support, and never derive DOGE addresses from an EVM or Bitcoin public address.
- For production, select and test native DOGE custody/signing infrastructure with per-user keys, safe backups/recovery, server-controlled permissions, UTXO selection, network fees, spend/nonce/duplicate protections and confirmation-based reconciliation.
- Tatum's documented DOGE balance/UTXO endpoints are an **indexer**, not by themselves the custody/key-signing layer. Do not create or send private mnemonics to arbitrary external API services.
- DOGE↔stablecoin swaps would require a separately verified native cross-chain settlement provider. Wallet address validation does not imply a safe route.

## No changes to trade behavior
BTC and ETH remain the only planned initial LIVE engines; no cross-asset capital rotation, no DOGE automatic orders and no user-facing deposit address unless all gates are verified.
