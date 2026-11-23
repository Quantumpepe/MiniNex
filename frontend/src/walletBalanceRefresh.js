// Notify every wallet and portfolio view when an actual transfer is confirmed.
// This event contains public transaction metadata only; it never adjusts a
// displayed balance locally or grants access to another user's wallet.
export const WALLET_BALANCE_CONFIRMED_EVENT='mininex:wallet-transfer-confirmed';

export function notifyConfirmedWalletTransfer(userId,asset,transactionId){
  if(typeof window==='undefined'||typeof userId!=='string'||!userId.startsWith('did:privy:')
      ||typeof asset!=='string'||!asset||typeof transactionId!=='string'||!transactionId)return;
  window.dispatchEvent(new CustomEvent(WALLET_BALANCE_CONFIRMED_EVENT,{
    detail:{userId,asset,transactionId},
  }));
}

export function onConfirmedWalletTransfer(userId,refresh){
  if(typeof window==='undefined'||!userId||typeof refresh!=='function')return()=>{};
  const handler=event=>{
    if(event?.detail?.userId===userId&&typeof event?.detail?.asset==='string')refresh();
  };
  window.addEventListener(WALLET_BALANCE_CONFIRMED_EVENT,handler);
  return ()=>window.removeEventListener(WALLET_BALANCE_CONFIRMED_EVENT,handler);
}
