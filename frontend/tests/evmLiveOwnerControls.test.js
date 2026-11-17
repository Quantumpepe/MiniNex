import test from 'node:test';
import assert from 'node:assert/strict';
import {validEvmTradeChallenge,validEvmTradeSignerOffer,
  validEvmTradePermission,validEvmAutoSession,LIVE_EVM_ASSETS,
} from '../src/evmLiveOwnerRules.js';

const WALLET='0x'+'ab'.repeat(20);
const ASSETS={BNB:56,POL:137,AVAX:43114,LINK:1,UNI:1};

for(const [asset,chain] of Object.entries(ASSETS)){
  const req={asset,address:WALLET};
  const challenge={
    mode:'EVM_OWNER_SIGNED_INTENT_ONLY',
    asset,chain_id:chain,state:'PENDING',
    wallet_address:WALLET,
    challenge:'MiniNex '+asset+' owner trading. Owner wallet: '+WALLET,
    budget_revision:1,
    user_wallet_signature_required:true,
    privy_signer_granted:false,live_trading_enabled:false,
    expires_at_unix:Math.floor(Date.now()/1000)+100,
  };
  test(asset+' owner-signed trade budget intent verified',()=>{
    assert.equal(validEvmTradeChallenge(challenge,req),true);
  });
  test(asset+' wrong network/wallet cannot authorize LIVE',()=>{
    assert.equal(validEvmTradeChallenge({...challenge,chain_id:1e9},req),false);
    assert.equal(validEvmTradeChallenge({
      ...challenge,wallet_address:'0x'+'cc'.repeat(20)},req),false);
  });
  const offer={
    mode:'OWNER_EVM_TRADING_SIGNER_OFFER_ONLY',
    asset,chain_id:chain,stable:asset==='BNB'?'USDT':'USDC',
    wallet_address:WALLET,
    budget_revision:1,policy_id:'policy_12345678',
    execution_signer_id:'signer_12345678',
    owner_add_signer_required:true,
    privy_signer_attached:false,
    live_session_started:false,trade_sent:false,
  };
  const ctx={asset,address:WALLET,
    policyId:'policy_12345678',stable:offer.stable};
  test(asset+' exact owner Privy trading signer only',()=>{
    assert.equal(validEvmTradeSignerOffer(offer,ctx),true);
    assert.equal(validEvmTradeSignerOffer({...offer,trade_sent:true},ctx),false);
    assert.equal(validEvmTradeSignerOffer({...offer,policy_id:'bad'},ctx),false);
  });
  const permission={
    mode:'OWNER_EVM_PRIVY_PERMISSION_STATUS_ONLY',
    asset,chain_id:chain,status:'GRANT_VERIFIED',
    can_submit:false,auto_execution_enabled:false,
  };
  test(asset+' live state never inferred from a read-only ledger',()=>{
    assert.equal(validEvmTradePermission(permission,asset),true);
    assert.equal(validEvmTradePermission({
      ...permission,can_submit:true},asset),false);
  });
  test(asset+' original coin session statuses only',()=>{
    for(const status of ['NOT_STARTED','ACTIVE','PAUSED']){
      assert.equal(validEvmAutoSession({
        mode:'OWNER_EVM_LIVE_SESSION_CONTROL',asset,
        chain_id:status==='NOT_STARTED'?null:chain,
        status,new_transactions_sent:0,
      },asset),true);
    }
    assert.equal(validEvmAutoSession({
      mode:'OWNER_EVM_LIVE_SESSION_CONTROL',asset,
      chain_id:chain,status:'FILLED',new_transactions_sent:0,
    },asset),false);
  });
}
test('exact LIVE EVM asset scope excludes ADA and native BTC',()=>{
  assert.deepEqual(LIVE_EVM_ASSETS,Object.keys(ASSETS));
});
