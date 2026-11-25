// Original MiniNex wallet: ONE scoped Privy signer and combined EVM policy.
// This code only changes an original owner's wallet authorization after their
// direct Start Trade click. It NEVER starts or claims a LIVE trade on its own.
const ADDR=/^0x[0-9a-fA-F]{40}$/;
const ID=/^[A-Za-z0-9_-]{6,160}$/;
const SHA=/^[a-f0-9]{64}$/;
const COINS=Object.freeze(['ETH','BNB','POL','AVAX','LINK','QNT','UNI','ARB','BTC','XRP','SOL','TRX']);
const MODE='MININEX_ORIGINAL_EVM_WALLET_POLICY_READONLY_STATUS';
const DIRECT_POLICY='ETHEREUM_ACROSS_DIRECT_V1';
const DIRECT_COINS=['BNB','POL','ARB'];
const WRAPPED_POLICY='ETHEREUM_WRAPPED_SPOT_V1';
const WRAPPED_COINS=['BTC','XRP','SOL','TRX'];
const needsWrapped=asset=>WRAPPED_COINS.includes(asset);
const hasWrapped=(report,asset)=>report.settlement_policy_by_asset?.[asset]===WRAPPED_POLICY;
const needsDirect=asset=>DIRECT_COINS.includes(asset);
const hasDirect=(report,asset)=>report.settlement_policy_by_asset?.[asset]===DIRECT_POLICY;

const eqAddress=(a,b)=>ADDR.test(a||'')&&
  typeof b==='string'&&a.toLowerCase()===b.toLowerCase();
const isId=x=>typeof x==='string'&&ID.test(x);

export function validCentralWalletStatus(report){
  return !!report&&report.mode===MODE&&
    [
      'NOT_CONFIGURED','DRAFT','SUBMITTING','UNCERTAIN',
      'REMOTE_VERIFIED_UNATTACHED','GRANT_VERIFIED',
      'REMOTE_VERIFIED_READONLY','UPDATE_PENDING','UPDATING',
      'UPDATE_UNCERTAIN','REVOKED','REQUIRES_RECONCILIATION',
    ].includes(report.status)&&
    Array.isArray(report.scope_coins)&&
    report.scope_coins.length<=COINS.length&&
    report.scope_coins.every(coin=>COINS.includes(coin))&&
    new Set(report.scope_coins).size===report.scope_coins.length&&
    report.can_submit===false&&report.auto_execution_enabled===false&&
    report.privy_wallet_grant_changed===false&&
    report.new_policy_created===false;
}

export function centralWalletHasCoin(report,asset){
  return validCentralWalletStatus(report)&&
    report.status==='REMOTE_VERIFIED_READONLY'&&
    report.scope_coins.includes(asset)&&
    (!needsDirect(asset)||hasDirect(report,asset))&&
    (!needsWrapped(asset)||hasWrapped(report,asset))&&
    report.one_scoped_signer_verified===true&&
    report.remote_policy_exactly_verified===true&&
    report.current_user_budgets_verified===true&&
    report.backend_quorum_current===true&&
    report.legacy_reconciliation_required===false;
}

const requireStatus=status=>{
  if(!validCentralWalletStatus(status)){
    throw Error('Combined wallet authorization cannot be verified');
  }
  return status;
};
const requireCurrent=(status,asset)=>{
  if(!centralWalletHasCoin(status,asset)){
    throw Error('Original wallet signer or combined Privy policy changed');
  }
  return status;
};

function validateFirstDraft(draft,{asset,address}){
  if(!draft||draft.mode!=='ORIGINAL_EVM_ONE_SIGNER_POLICY_DRAFT_ONLY'||
    draft.status!=='DRAFT'||draft.asset!==asset||
    !eqAddress(address,draft.wallet_address)||
    !isId(draft.shared_signer_id)||
    !SHA.test(draft.policy_sha256||'')||
    draft.policy_created_in_privy!==false||
    draft.wallet_additional_signer_granted!==false||
    draft.live_trading_enabled!==false||draft.can_submit!==false){
      throw Error('Original first-coin policy draft differs from your wallet');
  }
}
function validatePublished(published,asset){
  if(!published||published.status!=='REMOTE_VERIFIED_UNATTACHED'||
    published.asset!==asset||
    !isId(published.policy_id)||
    !isId(published.execution_signer_id)||
    !SHA.test(published.policy_sha256||'')||
    published.wallet_additional_signer_attached!==false||
    published.owner_must_approve_add_signers_once!==true||
    published.can_submit!==false){
    throw Error('Privy original policy publication is uncertain');
  }
}
function validateOffer(offer,{address,asset,stable}){
  if(!offer||offer.mode!=='ORIGINAL_EVM_ONE_SIGNER_GRANT_OFFER'||
    !eqAddress(address,offer.wallet_address)||offer.asset!==asset||
    offer.stable!==stable||!isId(offer.execution_signer_id)||
    !isId(offer.policy_id)||!SHA.test(offer.policy_sha256||'')||
    !((offer.owner_add_signer_required===true&&offer.original_wallet_approved===false)||
      (offer.owner_add_signer_required===false&&offer.original_wallet_approved===true))||
    offer.live_trading_enabled!==false||offer.can_submit!==false||
    (needsWrapped(asset)?(offer.settlement_policy!==WRAPPED_POLICY||offer.fee_scope!==null):
      needsDirect(asset)?(offer.settlement_policy!==DIRECT_POLICY||offer.fee_scope!==null):
      (offer.fee_scope?.performance_fee_percent!==5||
       offer.fee_scope?.fee_basis!=='REALIZED_NET_GAIN_ABOVE_HWM'||
       !ADDR.test(offer.fee_scope?.treasury_wallet||'')))){
      throw Error('Privy original wallet signing offer changed');
  }
}
function validateChallenge(stage,{asset,address}){
  const time=Math.floor(Date.now()/1000);
  const challenge=stage?.challenge;
  if(!stage||stage.mode!=='OWNER_EVM_WALLET_POLICY_INTENT_ONLY'||
     stage.status!=='PENDING'||stage.asset!==asset||
     !SHA.test(stage.nonce||'')||
     !SHA.test(stage.previous_policy_sha256||'')||
     !SHA.test(stage.target_policy_sha256||'')||
     !Array.isArray(stage.scope_coins)||
     !stage.scope_coins.includes(asset)||
     stage.additional_privy_signer_required!==false||
     stage.one_user_message_signature_required!==true||
     stage.policy_updated_in_privy!==false||
     typeof challenge!=='string'||challenge.length>3500||
     !challenge.includes('New or changed coins: '+asset)||
     (needsWrapped(asset)&&!challenge.includes('BTC/XRP trading uses wrapped ERC-20 inventory on Ethereum; native wallets are separate.')&&!challenge.includes('BTC/XRP/SOL trading uses wrapped ERC-20 inventory on Ethereum; native wallets are separate.')&&!challenge.includes('BTC/XRP/SOL/TRX trading uses wrapped ERC-20 inventory on Ethereum; native wallets are separate.'))||
     !challenge.includes('Wallet: '+address.toLowerCase())||
     !challenge.includes('Requested policy SHA256: '+stage.target_policy_sha256)||
     !challenge.includes('Previous policy SHA256: '+stage.previous_policy_sha256)||
     !Number.isSafeInteger(stage.expires_at_unix)||
     stage.expires_at_unix<=time||
     stage.expires_at_unix>time+300){
     throw Error('Coin authorization challenge is stale or not wallet-bound');
  }
}

/**
 * User-visible action only. Initial grant: ONE Privy addSigners approval.
 * New coin under existing grant: ONE original-wallet signMessage prompt.
 * Never call addSigners twice for the same user's original EVM wallet.
 *
 * The returned status is an authorization, not a LIVE session; the
 * separate server-side LIVE worker must independently authorize orders.
 */
export async function authorizeCentralEvmWalletCoin({
  asset,stable,address,request,addSigners,signMessage,
}){
  if(!COINS.includes(asset)||!['USDC','USDT'].includes(stable)||
     !ADDR.test(address||'')||typeof request!=='function'){
    throw Error('Original wallet, coin and settlement must be selected');
  }
  const root='/api/live/evm/wallet-policy';
  let report=requireStatus(await request(root+'/status'));
  if(report.status==='NOT_CONFIGURED'){
    if(typeof addSigners!=='function'){
      throw Error('Original Privy wallet grant is not available');
    }
    const draft=await request(root+'/prepare/'+asset,'POST');
    validateFirstDraft(draft,{asset,address});
    report={...report,status:'DRAFT',scope_coins:[asset]};
  }
  if(report.status==='DRAFT'){
    if(report.scope_coins.length!==1||
       report.scope_coins[0]!==asset){
      throw Error('Another original wallet coin policy is already pending');
    }
    validatePublished(await request(root+'/publish','POST'),asset);
    report={...report,status:'REMOTE_VERIFIED_UNATTACHED'};
  }
  if(report.status==='REMOTE_VERIFIED_UNATTACHED'){
    if(typeof addSigners!=='function'){
      throw Error('Original Privy wallet authorization is unavailable');
    }
    const offer=await request(root+'/grant-offer');
    validateOffer(offer,{asset,address,stable});
    if(offer.owner_add_signer_required){
      await addSigners({
        address,
        signers:[{
          signerId:offer.execution_signer_id,
          policyIds:[offer.policy_id],
        }],
      });
    }
    const verified=await request(root+'/confirm-grant','POST');
    if(verified?.state!=='GRANT_VERIFIED'||
       verified.owner_signer_grant_verified!==true||
       verified.additional_coin_grants_created!==0||
       verified.live_auto_session_started!==false||
       verified.can_submit!==false){
      throw Error('Original Privy signer grant not independently confirmed');
    }
    report=requireStatus(await request(root+'/status'));
    return requireCurrent(report,asset);
  }
  if(report.status==='REMOTE_VERIFIED_READONLY'){
    if(centralWalletHasCoin(report,asset))return report;
    const migrating=report.scope_coins.includes(asset)&&needsDirect(asset)&&!hasDirect(report,asset);
    if(report.scope_coins.includes(asset)&&!migrating){
      throw Error('Wallet grant requires independent re-verification');
    }
    if(typeof signMessage!=='function'){
      throw Error('Wallet scope extension requires a signature');
    }
    const originalCoins=[...report.scope_coins];
    const expectedLength=originalCoins.length+(migrating?0:1);
    const stage=await request(root+'/extend/'+asset+'/prepare','POST');
    validateChallenge(stage,{asset,address});
    if(stage.scope_coins.length!==expectedLength||
       !originalCoins.every(coin=>stage.scope_coins.includes(coin))){
      throw Error('Previously approved EVM coin was removed from wallet scope');
    }
    // This is the ONE wallet confirmation for another EVM coin.
    const signed=await signMessage(
      {message:stage.challenge},{address},
    );
    if(!/^0x[0-9a-fA-F]{130}$/.test(signed?.signature||'')){
      throw Error('Original wallet did not sign the selected coin scope');
    }
    const signedIntent=await request(root+'/extend/confirm','POST',{
      nonce:stage.nonce,signature:signed.signature,
    });
    if(signedIntent?.status!=='SIGNED_INTENT'||
       signedIntent.wallet_signature_verified!==true||
       signedIntent.policy_updated_in_privy!==false){
      throw Error('Original wallet signed intent was not verified');
    }
    const result=await request(root+'/extend/apply','POST',{
      nonce:stage.nonce,
    });
    if(result?.status!=='GRANT_VERIFIED'||
       result.one_original_signer!==true||
       result.new_coin!==asset||
       result.additional_signer_created!==false||
       result.remote_policy_verified!==true||
       result.can_submit!==false||
       !Array.isArray(result.scope_coins)||
       result.scope_coins.length!==expectedLength||
       !originalCoins.every(coin=>result.scope_coins.includes(coin))){
      throw Error('Original Privy policy PATCH requires remote reconciliation');
    }
    report=requireStatus(await request(root+'/status'));
    requireCurrent(report,asset);
    if(report.scope_coins.length!==expectedLength||
       !originalCoins.every(coin=>report.scope_coins.includes(coin))){
      throw Error('Previously approved wallet signer or coin policy changed');
    }
    return report;
  }
  // An uncertain publish/patch, a revoked signer, or an existing draft for
  // another coin must NEVER be automatically retried or replaced.
  throw Error('Existing Privy wallet authorization needs reconciliation');
}
