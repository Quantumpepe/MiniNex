import React, {useEffect, useRef, useState} from 'react';
import {useCreateWallet as useCreateSolanaWallet} from '@privy-io/react-auth/solana';
import {useCreateWallet as useCreateExtendedWallet} from '@privy-io/react-auth/extended-chains';
import {findPrivyNativeWallet, validNativePreviewAddress} from './nativeCoinAddresses.js';

// New wallets are created only for a verified, persisted Privy user.
// The app never stores or handles secret keys, signs transactions, or enables deposits.
// One attempt per chain/user until an explicit retry; account changes clear visibility.
function useAdditionalNativeWallet(user, enabled, chainType, createWallet) {
  const [created, setCreated] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const [phase, setPhase] = useState({userId: null, name: 'checking'});
  const attemptedFor = useRef(null);
  const currentUserId = useRef(user?.id);
  currentUserId.current = user?.id;

  const linked = findPrivyNativeWallet(user, chainType);
  const local = created?.userId === user?.id &&
    created.chainType === chainType &&
    validNativePreviewAddress(chainType, created.address)
    ? created.address : null;
  const address = linked?.address || local;

  useEffect(() => {
    const userId = user?.id;
    if (!enabled || !userId) return;
    if (address) {
      setPhase({userId, name:'created'});
      return;
    }
    const attemptKey = userId + ':' + chainType + ':' + attempt;
    if (attemptedFor.current === attemptKey) return;
    attemptedFor.current = attemptKey;
    setPhase({userId, name:'creating'});
    (async () => {
      try {
        const result = await createWallet();
        if (currentUserId.current !== userId) return;
        const wallet = result?.wallet || result;
        // A wallet for another chain must NEVER appear as a deposit address.
        if ((wallet?.chainType && wallet.chainType !== chainType) ||
            !validNativePreviewAddress(chainType, wallet?.address)) {
          setPhase({userId, name:'failed'});
          return;
        }
        setCreated({userId, chainType, address:wallet.address});
        setPhase({userId, name:'created'});
      } catch {
        if (currentUserId.current === userId) {
          setPhase({userId, name:'failed'});
        }
      }
    })();
  }, [enabled, user?.id, address, chainType, attempt, createWallet]);

  return {
    address: enabled ? address : null,
    state: !enabled ? 'checking' :
      address ? 'created' : phase.userId === user?.id ? phase.name : 'checking',
    retry: () => setAttempt(n => n + 1),
  };
}

export function useAdditionalCoinWallets(user, enabled) {
  const {createWallet: createSolanaWallet} = useCreateSolanaWallet();
  const {createWallet: createExtendedWallet} = useCreateExtendedWallet();
  const solanaCreator = React.useCallback(() => createSolanaWallet(), [createSolanaWallet]);
  const tronCreator = React.useCallback(
    () => createExtendedWallet({chainType:'tron'}), [createExtendedWallet]);
  // SOL and TRX are independent user-owned native wallets, not EVM aliases.
  const solana = useAdditionalNativeWallet(user, enabled, 'solana', solanaCreator);
  const tron = useAdditionalNativeWallet(user, enabled, 'tron', tronCreator);
  return {solana, tron};
}
