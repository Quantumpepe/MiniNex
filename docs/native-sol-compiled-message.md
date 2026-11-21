# MiniNex native SOL message compilation

Internal-only Solana legacy-message constructor for Paket 3, BTC/SOL/XRP.

The original wallet-bound THORChain quote and current Solana balance, finalized slot, blockhash, fee, destination and owner-refund memo are revalidated. The result is exact unsigned legacy-message bytes, signer account order, program indices and a SHA-256 fingerprint. Independent tests decode the bytes and verify transfer amount and memo.

No signing, Privy grant, network transmission, durable order claim or fee posting occurs. A real sender must re-check the owner's remote Privy policy, verify the current blockhash, atomically claim the order before signing, broadcast only once and reconcile the canonical original chain receipt. A successful compiler test does not prove a LIVE trade.

No system trading limits or Render toggles are modified.
