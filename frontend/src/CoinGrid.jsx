import React from 'react';
import {COINS,coinMarketSnapshot} from './coins.js';
import {formatMoney} from './data.js';

// Read-only coin selector. Trading inputs remain in a separate panel beneath
// this scroll viewport, so scrolling never closes or modifies a form.
export default function CoinGrid({t,lang='de',selected,onSelect,markets,syncedAt=0,kind='wallet'}) {
  return <div className={'mininex-coins-window mininex-coins-window--'+kind}
    role="group" aria-label={t.coinPickerTitle}
    tabIndex={0}>
    <div className="mininex-coins-grid">
      {COINS.map(coin=>{
        const q=coinMarketSnapshot(coin.symbol,markets,syncedAt);
        const live=!!q;
        const isSelected=selected===coin.symbol;
        return <button key={coin.symbol} type="button"
          className={'mininex-coin-button'+(isSelected?' is-selected':'')+
            (!coin.paperSettingsReady?' is-planned':'')}
          aria-pressed={isSelected}
          aria-label={coin.name+' '+coin.symbol+' · '+
            (coin.paperSettingsReady?(live?t.privyPriceLive:t.privyPriceUnavailable):t.coinPreparing)}
          onClick={()=>onSelect(coin.symbol)}>
          <span className={'mininex-coin-glyph mininex-coin-glyph--'+coin.symbol.toLowerCase()}
            aria-hidden="true">{coin.glyph}</span>
          <span className="mininex-coin-details">
            <strong>{coin.symbol}</strong>
            <small>{coin.paperSettingsReady
              ? (live?formatMoney(q.price_usdt,lang):t.privyPriceUnavailable)
              : t.coinPreparing}</small>
          </span>
        </button>;
      })}
    </div>
  </div>;
}
