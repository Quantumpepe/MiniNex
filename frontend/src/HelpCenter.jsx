import React from 'react';

const ITEMS = [
  ['helpWalletTitle','helpWalletText'],
  ['helpAddressTitle','helpAddressText'],
  ['helpBudgetTitle','helpBudgetText'],
  ['helpModesTitle','helpModesText'],
  ['helpSettlementTitle','helpSettlementText'],
  ['helpHorizonTitle','helpHorizonText'],
  ['helpTradingTitle','helpTradingText'],
  ['helpFeesTitle','helpFeesText'],
  ['helpMarketTitle','helpMarketText'],
  ['helpHistoryTitle','helpHistoryText'],
  ['helpSecurityTitle','helpSecurityText'],
];

export default function HelpCenter({t}) {
  return <section className="settings-help-panel" id="mininex-help" aria-label={t.helpTitle}>
    <h2>{t.helpTitle}</h2>
    {ITEMS.map(([heading,description])=><div className="settings-help-item" key={heading}>
      <h3>{t[heading]}</h3>
      <p>{t[description]}</p>
    </div>)}
  </section>;
}
