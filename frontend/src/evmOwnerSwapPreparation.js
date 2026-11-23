// Exact user-owned EVM Router02 preparation, validated independently from UI.
// All transaction bytes are provided by the authenticated MiniNex quote route.
// NEVER use the legacy V2 frontend swap builder or amountOutMin=0.
const ADDRESS=/^0x[0-9a-fA-F]{40}$/;
const CALL=/^0x04e45aaf[0-9a-fA-F]{448}$/i;
const APPROVE=/^0x095ea7b3[0-9a-fA-F]{128}$/i;
export const OWNER_SWAP_CHAINS=Object.freeze({
  BNB:56,POL:137,AVAX:43114,LINK:1,QNT:1,UNI:1,ARB:42161,
});
export const NATIVE_OWNER_SWAP_ASSETS=new Set(['BNB','POL','AVAX']);
const addr=n=>{
  if(n >= 1n<<160n)throw Error('Noncanonical token/address word');
  return '0x'+n.toString(16).padStart(40,'0');
};
const words=(data,length)=>{
  if(typeof data!=='string'||data.length!==10+64*length)
    throw Error('Unexpected transaction ABI');
  return Array.from({length},(_,i)=>BigInt('0x'+data.slice(10+i*64,10+(i+1)*64)));
};
const eq=(a,b)=>typeof a==='string'&&typeof b==='string'&&a.toLowerCase()===b.toLowerCase();
const positive=x=>typeof x==='string'&&/^(0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(x)&&Number(x)>0;

export function validateOwnerSwapPreparation(data,{asset,side,stable,amount,wallet},now=Date.now()){
  const chainId=OWNER_SWAP_CHAINS[asset];
  if(!chainId||!['BUY','SELL'].includes(side)||!['USDC','USDT'].includes(stable)
    ||!positive(amount)||!ADDRESS.test(wallet)
    ||!data||data.mode!=='SHARED_EVM_OWNER_SWAP_PREPARATION'
    ||data.asset!==asset||data.side!==side||data.stable!==stable
    ||data.input_amount!==amount||data.chain_id!==chainId
    ||data.wallet_identity_verified!==true
    ||data.exact_swap_abi_and_amount_verified!==true
    ||!eq(data.wallet_address,wallet)
    ||!ADDRESS.test(data.router)||!ADDRESS.test(data.source_token_address)
    ||!ADDRESS.test(data.output_token_address)
    ||eq(data.source_token_address,data.output_token_address)
    ||data.execution_enabled!==false||data.can_submit!==false
    ||data.requires_separate_user_signature!==true)
    throw Error('Owner swap request does not match the wallet or chain');
  const tx=data.unsigned_transaction;
  let swap=null;
  if(tx!==null&&tx!==undefined){
    if(typeof tx!=='object'||!CALL.test(tx.data)
      ||tx.chainId!==chainId||!eq(tx.from,wallet)
      ||!eq(tx.to,data.router)||!['0','0x0'].includes(tx.value)
      ||data.owner_wallet_signing_possible!==true
      ||data.source_funds_sufficient!==true
      ||data.router_allowance_sufficient!==true
      ||data.native_gas_available!==true
      ||!Number.isSafeInteger(data.transaction_expires_at)
      ||data.transaction_expires_at*1000<=now
      ||data.transaction_expires_at*1000>now+50000)
      throw Error('Swap quote expired or is not authorized for signing');
    const [input,output,fee,recipient,quantity,minimum,limit]=words(tx.data,7);
    if(!eq(addr(input),data.source_token_address)
       ||!eq(addr(output),data.output_token_address)
       ||!eq(addr(recipient),wallet)
       ||![500n,3000n].includes(fee)
       ||quantity<=0n||quantity>=10n**45n||minimum<=0n||minimum>=10n**45n
       ||limit!==0n)
      throw Error('Untrusted swap calldata or zero minimum output');
    swap={from:wallet,to:data.router,data:tx.data,value:'0x0',chainId,
      quantity,minimum,expiresAt:data.transaction_expires_at*1000};
  }
  let approve=null;
  if(data.owner_approval_transaction!=null){
    const tx=data.owner_approval_transaction;
    if(typeof tx!=='object'||!APPROVE.test(tx.data)
      ||tx.chainId!==chainId||!eq(tx.from,wallet)
      ||!eq(tx.to,data.source_token_address)
      ||!['0x0','0'].includes(tx.value)
      ||data.source_funds_sufficient!==true||data.native_gas_available!==true)
      throw Error('Owner approval is not bound to the wallet');
    const [spender,atomic]=words(tx.data,2);
    if(!eq(addr(spender),data.router)
      ||atomic<=0n||atomic>=10n**45n)
      throw Error('Invalid or unlimited token approval');
    approve={from:wallet,to:data.source_token_address,data:tx.data,
      value:'0x0',chainId,amount:atomic};
  }
  // A quote is not a user authorization. Only a subsequent wallet click can
  // sign the exact returned amount, route, chain, and deadline.
  if(data.owner_wallet_signing_possible===true&&!swap)
    throw Error('Spendable quote has no signed transaction plan');
  return {asset,side,stable,amount,chainId,wallet,swap,approve,
    gasAvailable:data.native_gas_available===true,
    sourceAvailable:data.source_funds_sufficient===true,
    source:data.source_token_address,
    output:data.output_token_address,
    router:data.router,
    wrapPossible:NATIVE_OWNER_SWAP_ASSETS.has(asset)&&side==='SELL'
      &&data.source_funds_sufficient!==true};
}

export function exactNativeOwnerUnits(value){
  if(typeof value!=='string'||!/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?$/.test(value))
    throw Error('Invalid native coin amount');
  const [major,fraction='']=value.split('.');
  const atomic=BigInt(major)*10n**18n+BigInt(fraction.padEnd(18,'0')||'0');
  if(atomic<=0n||atomic>=10n**45n)throw Error('Invalid native coin amount');
  return atomic;
}

const RECEIPT_HASH=/^0x[0-9a-fA-F]{64}$/;

// On-chain inclusion is not confirmation that Privy broadcast the exact
// approved action. Validate original bytes for swaps, allowance approvals,
// native deposits and withdrawals BEFORE claiming the corresponding step.
export function verifyOwnerEvmMinedTx(onchain,expected,hash,chainId){
  if(!RECEIPT_HASH.test(hash)||!onchain||!expected||
    typeof onchain.input!=='string'||
    !/^0x(?:[0-9a-fA-F]{2})*$/.test(onchain.input)||
    typeof expected.data!=='string'||
    !eq(onchain.hash,hash)||
    !eq(onchain.from,expected.from)||
    !eq(onchain.to,expected.to)||
    !eq(onchain.input,expected.data))
    throw Error('Mined EVM transaction differs from owner-approved operation');
  try {
    if(BigInt(onchain.chainId)!==BigInt(chainId)||
       BigInt(onchain.value)!==BigInt(expected.value))
      throw Error('Mined EVM chain or native value mismatch');
  }catch{
    throw Error('Mined EVM chain or native value mismatch');
  }
  return true;
}


// Android OTA/reload can destroy React state while a user-signed transaction
// remains on-chain. Persist only public, exact owner-bound transaction metadata.
// Loading this record NEVER signs, sends, approves or retries a transaction.
const PENDING_HASH=/^0x[0-9a-fA-F]{64}$/;
const PENDING_DATA=/^0x(?:[0-9a-fA-F]{2})*$/;
const PENDING_KINDS=new Set(['swap','approval','wrap','unwrap']);
const NATIVE_ALLOWED_STABLE=Object.freeze({BNB:'USDT',POL:'USDC',AVAX:'USDC'});
const pendingStorageKey=({userId,asset,wallet}={})=>
  typeof userId==='string'&&userId.startsWith('did:privy:')&&
  OWNER_SWAP_CHAINS[asset]&&ADDRESS.test(wallet||'')
    ?'mininex:evm:pending:v1:'+userId+':'+asset+':'+wallet.toLowerCase()
    :null;

function normalizedPendingOwnerSwap(value,context){
  if(!value||typeof value!=='object'||!context)return null;
  const key=pendingStorageKey(context);
  if(!key||value.userId!==context.userId||value.asset!==context.asset||
    value.side!=='BUY'&&value.side!=='SELL'||
    !['USDC','USDT'].includes(value.stable)||
    (NATIVE_ALLOWED_STABLE[value.asset]&&
       NATIVE_ALLOWED_STABLE[value.asset]!==value.stable)||
    !PENDING_KINDS.has(value.kind)||!PENDING_HASH.test(value.hash||'')||
    !value.plan||value.plan.chainId!==OWNER_SWAP_CHAINS[value.asset]||
    !ADDRESS.test(value.plan.output||'')||
    !value.tx||!ADDRESS.test(value.tx.from||'')||
    !ADDRESS.test(value.tx.to||'')||
    value.tx.from.toLowerCase()!==context.wallet.toLowerCase()||
    typeof value.tx.data!=='string'||value.tx.data.length>520||
    !PENDING_DATA.test(value.tx.data)||
    typeof value.tx.value!=='string'||!/^0x[0-9a-fA-F]+$/.test(value.tx.value)||
    ((!NATIVE_OWNER_SWAP_ASSETS.has(value.asset))&&
       ['wrap','unwrap'].includes(value.kind))||
    (value.kind==='unwrap'&&value.side!=='BUY')||
    (value.kind==='wrap'&&value.side!=='SELL'))return null;
  return {
    userId:value.userId,asset:value.asset,
    hash:value.hash.toLowerCase(),kind:value.kind,
    side:value.side,stable:value.stable,
    tx:{from:value.tx.from.toLowerCase(),to:value.tx.to.toLowerCase(),
      data:value.tx.data,value:value.tx.value},
    plan:{chainId:value.plan.chainId,output:value.plan.output.toLowerCase()},
  };
}

export function savePendingOwnerSwap(storage,pending){
  const context={userId:pending?.userId,asset:pending?.asset,
    wallet:pending?.tx?.from};
  const key=pendingStorageKey(context);
  const normalized=normalizedPendingOwnerSwap(pending,context);
  if(!key||!normalized||!storage)return false;
  try{storage.setItem(key,JSON.stringify(normalized));return true;}
  catch{return false;}
}

export function readPendingOwnerSwap(storage,context){
  const key=pendingStorageKey(context);
  if(!key||!storage)return null;
  try{
    const raw=storage.getItem(key);
    return raw&&raw.length<2000
      ?normalizedPendingOwnerSwap(JSON.parse(raw),context):null;
  }catch{return null;}
}

export function clearPendingOwnerSwap(storage,context,hash){
  const key=pendingStorageKey(context);
  if(!key||!PENDING_HASH.test(hash||'')||!storage)return false;
  try{
    const record=readPendingOwnerSwap(storage,context);
    if(!record||record.hash!==hash.toLowerCase())return false;
    storage.removeItem(key);
    return true;
  }catch{return false;}
}
