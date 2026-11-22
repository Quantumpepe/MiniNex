import test from 'node:test';
import assert from 'node:assert/strict';
import {validEvmTradeChallenge,validEvmTradeSignerOffer,
  validEvmTradePermission,validEvmAutoSession,validEvmOrderHistory,
  validEvmFinalizedFills,LIVE_EVM_ASSETS,
} from '../src/evmLiveOwnerRules.js';

const WALLET='0x'+'ab'.repeat(20);
const ASSETS={BNB:56,POL:137,AVAX:43114,LINK:1,QNT:1,UNI:1,ARB:42161};

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
    combined_single_grant:true,
    fee_scope:{
      treasury_wallet:'0x'+'cd'.repeat(20),
      fee_max_atomic:'1000000',
      performance_fee_percent:5,
      fee_basis:'REALIZED_NET_GAIN_ABOVE_HWM',
      router_allowance_scoped:true,
    },
    privy_signer_attached:false,
    live_session_started:false,trade_sent:false,
  };
  const ctx={asset,address:WALLET,
    policyId:'policy_12345678',stable:offer.stable};
  test(asset+' first owner grant requires explicit HWM fee and treasury scope',()=>{
    const combined={...offer,combined_single_grant:true,
      fee_scope:{
        treasury_wallet:'0x'+'cd'.repeat(20),
        fee_max_atomic:'1000000',
        performance_fee_percent:5,
        fee_basis:'REALIZED_NET_GAIN_ABOVE_HWM',
        router_allowance_scoped:true,
      }};
    assert.equal(validEvmTradeSignerOffer(combined,ctx),true);
    assert.equal(validEvmTradeSignerOffer({
      ...combined,fee_scope:{...combined.fee_scope,
        performance_fee_percent:6}},ctx),false);
    assert.equal(validEvmTradeSignerOffer({
      ...combined,fee_scope:{...combined.fee_scope,
        treasury_wallet:'not-an-address'}},ctx),false);
    assert.equal(validEvmTradeSignerOffer({
      ...combined,fee_scope:{...combined.fee_scope,
        router_allowance_scoped:false}},ctx),false);
  });
  test(asset+' exact owner Privy trading signer only',()=>{
    assert.equal(validEvmTradeSignerOffer(offer,ctx),true);
    assert.equal(validEvmTradeSignerOffer({...offer,combined_single_grant:false},ctx),false);
    assert.equal(validEvmTradeSignerOffer({...offer,fee_scope:null},ctx),false);
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


test('coin-specific real EVM order history rejects other coins and PAPER',()=>{
  const tx='0x'+'a'.repeat(64);
  const order={
    asset:'LINK',chain_id:1,intent_key:'sig_real_123456789',
    side:'BUY',stable:'USDC',status:'FINALIZED_SUCCESS',tx_hash:tx,
    swap_erc20_leg_finalized:true,native_trade_cycle_settled:false,
    fee_transferred:false,created_at:'2026-11-21T07:30:00+00:00',
  };
  const report={
    mode:'OWNER_DURABLE_EVM_ORDER_STATUS_ONLY',asset:'LINK',
    includes_simulation:false,wallet_signing_enabled_by_this_api:false,
    automatic_trade_execution_enabled_by_this_api:false,
    history_truncated:false,orders:[order],
  };
  assert.equal(validEvmOrderHistory(report,'LINK'),true);
  assert.equal(validEvmOrderHistory(report,'UNI'),false);
  assert.equal(validEvmOrderHistory({...report,includes_simulation:true},'LINK'),false);
  assert.equal(validEvmOrderHistory({...report,orders:[{...order,asset:'UNI'}]},'LINK'),false);
  assert.equal(validEvmOrderHistory({...report,orders:[{...order,tx_hash:null}]},'LINK'),false);
  assert.equal(validEvmOrderHistory({...report,orders:[order,order]},'LINK'),false);
  assert.equal(validEvmOrderHistory({...report,orders:[]},'LINK'),true);
});

test('native settlement and real on-chain swap remain distinct',()=>{
  const order={
    asset:'BNB',chain_id:56,intent_key:'sig_real_bnb_123456',
    side:'SELL',stable:'USDT',status:'FINALIZED_SUCCESS',
    tx_hash:'0x'+'1'.repeat(64),swap_erc20_leg_finalized:true,
    native_trade_cycle_settled:false,fee_transferred:false,
    created_at:'2026-11-21T07:30:00+00:00',
  };
  const report={
    mode:'OWNER_DURABLE_EVM_ORDER_STATUS_ONLY',asset:'BNB',
    includes_simulation:false,wallet_signing_enabled_by_this_api:false,
    automatic_trade_execution_enabled_by_this_api:false,
    history_truncated:false,orders:[order],
  };
  assert.equal(validEvmOrderHistory(report,'BNB'),true);
  assert.equal(validEvmOrderHistory({
    ...report,orders:[{...order,status:'SUBMITTED',
      swap_erc20_leg_finalized:false,native_trade_cycle_settled:true}]
  },'BNB'),false);
});

test('finalized on-chain quantities match the selected coin and exclude fabricated PnL',()=>{
  const fill={
    asset:'LINK',chain_id:1,intent_key:'sig_link_123456789',
    tx_hash:'0x'+'a'.repeat(64),side:'BUY',stable:'USDC',
    spent_asset:'USDC',received_asset:'LINK',
    spent_amount:'100.000001',received_amount:'1.234567890000000001',
    created_at:'2026-11-21T07:30:00+00:00',
    onchain_finalized:true,native_coin_cycle_complete:false,
    realized_net_usd:null,performance_fee_paid:false,
  };
  const report={
    mode:'FINALIZED_EVM_REAL_FILLS_ONLY',asset:'LINK',
    includes_simulation:false,wallet_signing_authorized:false,
    full_native_order_settlement_verified:false,
    realized_net_pnl_calculated:false,performance_fee_settled:false,
    history_truncated:false,fills:[fill],
  };
  assert.equal(validEvmFinalizedFills(report,'LINK'),true);
  assert.equal(validEvmFinalizedFills(report,'UNI'),false);
  assert.equal(validEvmFinalizedFills({...report,includes_simulation:true},'LINK'),false);
  assert.equal(validEvmFinalizedFills({...report,fills:[{...fill,realized_net_usd:'1'}]},'LINK'),false);
  assert.equal(validEvmFinalizedFills({...report,fills:[{...fill,asset:'UNI'}]},'LINK'),false);
  assert.equal(validEvmFinalizedFills({...report,fills:[{...fill,spent_amount:'NaN'}]},'LINK'),false);
  assert.equal(validEvmFinalizedFills({...report,fills:[{...fill,received_amount:'0'}]},'LINK'),false);
});
