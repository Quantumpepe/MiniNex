import {ROUTER02,STABLES,WETH9,ownerAccounts,abiAllowance,decodeUint,validHash} from './ethLiveSwapRules.js';

export const AUTO_APPROVAL_TOKENS={USDC:STABLES.USDC,USDT:STABLES.USDT,WETH:WETH9};
const ADDRESS=/^0x[0-9a-fA-F]{40}$/;
const hexPad=(s)=>s.slice(2).toLowerCase().padStart(64,'0');
export function validateEthAutoOwnerApproval(plan,{wallet,token}){
  if(!ADDRESS.test(wallet)||!(token in AUTO_APPROVAL_TOKENS)||
     !plan||plan.mode!=='ETH_OWNER_LIMITED_ROUTER02_APPROVAL'||
     plan.asset!=='ETH'||plan.network!=='ethereum-mainnet'||
     plan.chain_id!==1||plan.token!==token||
     plan.wallet_address?.toLowerCase()!==wallet.toLowerCase()||
     plan.spender?.toLowerCase()!==ROUTER02.toLowerCase()||
     plan.token_contract?.toLowerCase()!==AUTO_APPROVAL_TOKENS[token]||
     plan.requires_explicit_owner_privy_confirmation!==true||
     plan.transaction_broadcast!==false||plan.backend_can_sign!==false||
     !Number.isSafeInteger(plan.expires_at_unix)||
     plan.expires_at_unix<=Date.now()/1000||
     plan.expires_at_unix>Date.now()/1000+125)
    throw Error('Owner-only approval plan not verified');
  const tx=plan.transaction;
  const units=BigInt(plan.amount_atomic||'0');
  const data='0x095ea7b3'+hexPad(ROUTER02)+units.toString(16).padStart(64,'0');
  if(units<=0n||units>=10n**45n||
     tx?.from?.toLowerCase()!==wallet.toLowerCase()||
     tx.to?.toLowerCase()!==AUTO_APPROVAL_TOKENS[token]||
     tx.chainId!==1||tx.value!=='0x0'||tx.data?.toLowerCase()!==data)
    throw Error('Unsafe token approval calldata');
  return {token:tx.to,wallet,spender:ROUTER02,
          data:tx.data,amount:units,expires:plan.expires_at_unix*1000,
          approvalCapUsd:plan.approval_cap_usd};
}
export function zeroAllowanceCalldata(){
  return '0x095ea7b3'+hexPad(ROUTER02)+'0'.repeat(64);
}
export {ownerAccounts,abiAllowance,decodeUint,validHash};
