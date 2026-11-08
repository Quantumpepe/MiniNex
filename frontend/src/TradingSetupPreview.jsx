import React, {useState} from 'react';

// Safe local preview only: these selections never become a signer authorization,
// never alter the paper engine, and are deliberately not persisted.
const INITIAL = Object.freeze({
  BTC: {mode:'LONG', stable:'USDC', horizon:'15m'},
  ETH: {mode:'LONG', stable:'USDC', horizon:'15m'},
});
const MODES = ['LONG','SPOT_SHORT'];
const STABLES = ['USDC','USDT'];
const HORIZONS = ['5m','15m','1h','24h'];

export default function TradingSetupPreview({t}) {
  const [settings,setSettings]=useState(INITIAL);
  const change=(asset,field,value)=>{
    const accepted = field==='mode' ? MODES : field==='stable' ? STABLES :
      field==='horizon' ? HORIZONS : [];
    if(!['BTC','ETH'].includes(asset) || !accepted.includes(value))return;
    setSettings(old=>({...old,[asset]:{...old[asset],[field]:value}}));
  };
  return <section className="privy-hwm-box" aria-label={t.privySetupTitle}>
    <div className="privy-hwm-title">
      <strong>{t.privySetupTitle}</strong>
    </div>
    {['BTC','ETH'].map(asset=><div key={asset} className="privy-setup-asset"
      style={{display:'flex',flexDirection:'column',gap:10,marginBottom:18}}>
      <strong>{asset}</strong>
      <label style={{display:'flex',justifyContent:'space-between',alignItems:'center',gap:10}}>
        <span>{t.privySetupMode}</span>
        <select value={settings[asset].mode}
          onChange={event=>change(asset,'mode',event.target.value)}>
          <option value="LONG">{t.privySetupLong}</option>
          <option value="SPOT_SHORT">{t.privySetupSpotShort}</option>
        </select>
      </label>
      <label style={{display:'flex',justifyContent:'space-between',alignItems:'center',gap:10}}>
        <span>{t.privySetupStable}</span>
        <select value={settings[asset].stable}
          onChange={event=>change(asset,'stable',event.target.value)}>
          {STABLES.map(value=><option key={value} value={value}>{value}</option>)}
        </select>
      </label>
      <label style={{display:'flex',justifyContent:'space-between',alignItems:'center',gap:10}}>
        <span>{t.privySetupHorizon}</span>
        <select value={settings[asset].horizon}
          onChange={event=>change(asset,'horizon',event.target.value)}>
          <option value="5m">5m</option><option value="15m">15m</option><option value="1h">1h</option>
          <option value="24h">24h</option>
        </select>
      </label>
    </div>)}
  </section>;
}
