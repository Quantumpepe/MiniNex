# Multi-Coin-Wallet und tatsächlicher Live-Stand

Stand: 14. November 2026. Quellcodeprüfung, keine Behauptung einer erfolgreichen Echtgeld-Ausführung oder einer geprüften Produktionskonfiguration.

## Gelieferte Wallet-Integration

Die authentifizierte Route `/api/wallet/multicoin-balances` liest die bei Privy serverseitig ermittelte EVM-Adresse. Weder Adresse noch Netzwerk oder Tokenvertrag werden vom Browser übernommen. Die Wallet zeigt BNB, POL, AVAX, LINK und UNI zusätzlich zu BTC, ETH, USDC und USDT. Die beiden Abfragen laufen unabhängig. Fehler erscheinen als „nicht verfügbar“, echte Nullen als `0`. Aktualisieren lädt beide Bereiche neu; Benutzerwechsel verwirft alte Antworten.

| Asset | Netzwerk | ChainID | Dezimalstellen | Herkunft |
|---|---|---:|---:|---|
| BNB | BNB Smart Chain | 56 | 18 | natives Guthaben |
| POL | Polygon PoS | 137 | 18 | natives Guthaben, kein Ethereum-MATIC |
| AVAX | Avalanche C-Chain | 43114 | 18 | kein X-/P-Chain-Guthaben |
| LINK | Ethereum | 1 | 18 | `0x514910771AF9CA656AF840DFF83E8264ECF986CA` |
| UNI | Ethereum | 1 | 18 | `0x1f9840a85d5af5bf1d1762f925bddadc4201f984` |

Quellen: [BNB](https://docs.bnbchain.org/bnb-smart-chain/developers/wallet-configuration/), [Polygon](https://support.polygon.technology/support/solutions/articles/82000889434-how-to-add-polygon-mainnet-to-your-wallet-), [Avalanche](https://build.avax.network/docs/primary-network/exchange-integration), [LINK](https://docs.chain.link/resources/link-token-contracts), [UNI-Adresse](https://developers.uniswap.org/docs/ecosystem/governance/uni), [UNI-Dezimalstellen im Vertrag](https://github.com/Uniswap/governance/blob/master/contracts/Uni.sol).

Die Umrechnung bewahrt auch große Beträge mit 18 Nachkommastellen ohne Rundung durch den standardmäßigen Decimal-Kontext. Es gibt keine Fiat-Summierung dieser Coin-Mengen und keine Übergabe an PAPER-Budgets, Strategist oder Historien. Ein realer Portfolio-Gesamtwert braucht zusätzlich verlässliche Preise und die übrigen nativen Bestände; er wird hier nicht vorgetäuscht.

## Was für elf aktive Coins fehlt

„Aktiv“ in Auswahl/Simulation ist nicht gleich ausführbarer Live-Handel. `live_asset_registry.require_live_engine` lehnt Ausführung fest ab; `live_orders.prepare_live_order_reservation` und `live_eth_reservations.prepare_live_eth_reservation` sind ebenfalls gesperrte Einstiegspunkte. Die Readiness-Anzeige enthält feste `False`-Werte und ist kein Test der laufenden Produktionsumgebung. Teilweise sind Kommentare veraltet: zusätzliche PAPER-Sitzungen existieren bereits; „nur BTC/ETH-PAPER“ beschreibt den gesamten heutigen Stand nicht zuverlässig.

| Coin | Im Code vorhanden | Noch für echte Ausführung nötig |
|---|---|---|
| BTC | Eigentumsgeprüfte Bestände, externe Quote-Vorbereitung | UTXO-Auswahl, Gebühren/Wechselgeld, automatisierter Signer, überprüfter Cross-Chain-Anbieter samt Ziel-Stablecoin, Bestätigungen und Rückerstattung |
| ETH | Bestände, Uniswap-Quotes, Transaktionspläne, zahlreiche Prüfbausteine | Einen durchgängigen Router-Pfad mit passender Privy-Berechtigung verbinden; Freigaben/WETH, Reservierung, Signieren/Senden und endgültige Verbuchung integrieren |
| BNB | Neue reale BSC-Bestände, Route-/Quote-Kandidaten | BSC-Swapadapter, richtige Stablecoin-Verträge/Dezimale, Router, Gas, Freigaben, Signer und Abrechnung |
| POL | Neue reale Polygon-Bestände | Polygon-Swapadapter und passende Stablecoin-/Router-Verträge; native USDC und USDC.e nicht verwechseln |
| AVAX | Neue reale C-Chain-Bestände | C-Chain-Swapadapter, Stablecoins, Gas, Freigaben und Abrechnung |
| LINK | Neue Ethereum-Tokenbestände | ERC-20-Paar tatsächlich ausführbar integrieren, Allowance und ETH für Gas, Ein-/Ausgangsmengen verbuchen |
| UNI | Neue Ethereum-Tokenbestände | Wie LINK; ein eingetragener Anbieter-Ticker garantiert keine verfügbare Liquidität |
| SOL | Privy-Wallet-Erstellung/Adressintegration | Servergeprüfte native/SPL-Bestände, ausführbare DEX-Route, Mint-Prüfung, Blockhash, Signer und Finalität |
| XRP | Prüfung verknüpfter XRPL-Adressen; Erstellung per Freigabevariable eingeschränkt | Tatsächliche Privy-/Signer-Unterstützung, serverseitige Erstellungssperre, Bestände, Reserve/Trustlines/Issuer, DEX und Ledger-Abrechnung |
| TRX | Native Privy-Wallet-Anbindung | Bestände, TRC-20-USDT-Vertrag, Energy/Bandwidth, Swapadapter, Signer und Receipts |
| DOGE | Native Adressprüfung/Anzeige und Quote-Kandidaten | Verifizierte Wallet-/Signer-Anbindung, Bestände, UTXO-Ausführung, externe Stablecoin-Abwicklung samt Rückerstattung |
| ADA | In Arbeit | Bleibt außerhalb der elf aktiven Coins |

Gemeinsam fehlt die Verbindung vom bestehenden Coin-Signal zu einem dauerhaften, pro Benutzer und Coin getrennten Live-Auftrag: Budget reservieren, einmalig senden, nach Neustart wiederfinden, tatsächliche Ergebnisse inklusive Gebühren verbuchen und im jeweiligen Coin-Verlauf anzeigen. Auch Pause mit kontrolliertem Stablecoin-Verkauf ist kein bloßer Schalter. Ein Stop-Signal führt derzeit keinen solchen Verkauf aus. Vor Freigabe muss dieser vollständige Ablauf mit einem dafür vorgesehenen Testverfahren nachgewiesen werden. In dieser Änderung wurden keine Wallet-Berechtigungen vergeben und keine Transaktionen gesendet.

## ETH-Prüfungen verständlich erklärt

**Unterscheidung:** Eine Prüfung kann heute eine Vorschau oder Vorbereitung ablehnen. Das ist noch keine tatsächlich laufende Handelsblockade, weil der Live-Executor fehlt. Die festen Ausführungssperren sind dagegen unmittelbar wirksam. „Notwendig“ unten meint den Zweck für einen funktionierenden Executor, nicht jede derzeitige starre Zahl oder jede doppelte Vorabprüfung.

| Prüfung / Quelldatei | Wirkung | Blockade und Bewertung |
|---|---|---|
| Login und Wallet-Eigentum (`auth`, `privy_wallet_verification`, `wallet_balances`) | Ordnet Daten und Wallet dem angemeldeten Nutzer zu. | Ablehnung bei ungültiger Identität; notwendig. Ein Login allein erteilt keine Handelsvollmacht. |
| Netzwerk, Tokenvertrag und Dezimale (`live_eth_quotes`, `wallet_balances`) | Verhindert falsche Chain, falschen Token oder einen millionenfach falschen Betrag. | Ungültige Abfragen/Quotes werden abgelehnt; notwendig. |
| Privy-Delegation (`privy_live_signer`) | Prüft, ob die Wallet einem automatischen Signer zugeordnet wurde. | Readiness bleibt negativ, wenn sie fehlt; für unbeaufsichtigtes Handeln notwendig. |
| Signer und eingeschränkte Policy-ID (`privy_live_signer`) | Prüft den konkreten Signer, Wallet-Besitzer und seine beschränkte Berechtigung. | Readiness-Prüfung; notwendig, statt einem beliebigen Signer zu vertrauen. |
| Policy-Inhalt und gespeicherter Fingerabdruck (`privy_eth_policy`) | Erkennt geänderte Regeln, Empfänger, Router oder Betragsgrenzen. | Lehnt unpassende Policy ab; notwendiger Zweck. Mehrere überlappende Berichte können zusammengeführt werden. |
| Policy-Laufzeit und Einzelfreigaben (`eth_privy_user_policy_blueprint`) | Begrenzt Rechte zeitlich und auf konkrete Aufrufe; maximal 30 Tage im Entwurf. | Verhindert unpassende Vorbereitung. Laufzeit und konkrete Höchstbeträge sind Produktentscheidungen, keine Blockchain-Pflicht. |
| Zustimmung und Versionsbindung (`eth_user_consent`, `eth_user_enrollment_preflight`) | Bindet Freigabe an Benutzer, Wallet und aktuellen Budgetstand. | Alte Zustimmung wird bei Änderungen ungültig; Zustimmung notwendig. Jede kleine Änderung mit vollständiger Neueinrichtung zu verbinden ist vereinfachbar. |
| Policy-Erstellung, Rücklesen und unklare Ergebnisse (`eth_privy_policy_ledger`, `eth_privy_policy_provisioner`, `eth_privy_policy_transport`) | Verhindert doppelte oder unbestätigte Berechtigungen nach einem Timeout. | Der ältere Transport-Einstieg ist deaktiviert; der neuere benutzergebundene Publish-Pfad kann nach signierter Zustimmung eine zunächst unverbundene Policy erstellen. Das ist noch keine Wallet-Vollmacht. Nach unbekanntem Ergebnis abgleichen ist notwendig. |
| Nutzerfreigabe des Signers (`eth_privy_user_grant`) | Prüft die tatsächlich bei Privy eingerichteten Rechte. | `_PRIVY_SIGNER_GRANTS_AUDITED_IN_CODE=False` verhindert das reguläre Freigabeangebot. Entwicklungsblockade; muss durch vollständige Integration ersetzt werden. |
| Stop und Widerruf (`live_eth_controls`, `eth_privy_signer_revocation`) | Setzt lokale Pause, verwirft Zustimmung und prüft entfernte Signer. | Echte lokale Vorbereitungssperre. Notwendig als Nutzerfunktion. Stop entfernt nicht automatisch den Privy-Signer und macht gesendete Transaktionen nicht rückgängig. |
| Coin-Kapital (`live_eth_controls`, `live_eth_reservations`) | Verhindert, dass reserviertes und neues Kapital zusammen das ETH-Budget überschreiten. | Prüffunktionen lehnen Überschreitungen ab; für getrennte Budgets notwendig. |
| Maximaler Einzelauftrag (`live_eth_controls`) | Begrenzt die Größe eines Auftrags zusätzlich zum Kapitalbudget. | Zusätzliche Handelsgrenze; optional/konfigurierbar. Kann DCA unnötig begrenzen. |
| Tagesumsatz (`live_eth_controls`, `eth_cost_budget_review`) | Zählt gehandeltes Volumen, nicht bloß Verlust. | Zusätzliche Handelsgrenze; optional/konfigurierbar. Wiederholte Käufe/Verkäufe können trotz genug Kapital gesperrt werden. |
| Doppelte Signale, Nonce und offene Aufträge (`live_orders`, `live_eth_reservations`) | Verhindert doppelte Käufe und doppelt vergebenes Kapital. | Notwendig. Öffentliche Reservierung ist jedoch fest deaktiviert; dauerhafte Signalbindung ist noch nicht im Executor durchgesetzt. |
| Kursangebot, Mindestempfang und Frist (`live_eth_quotes`, `live_eth_swap_plan`) | Prüft Paar, Menge, Empfänger, Aktualität und Ablauf. Quote begrenzt Slippage derzeit auf 0,5 %. | Lehnt ungültige Pläne ab. Mindestempfang/Frist notwendig; genau 0,5 % ist anpassbare Ausführungspolitik, keine Gewinn-/Verlustgrenze der Strategie. |
| Router und Aufrufdaten (`live_eth_swap_plan`, `eth_router_semantics`, `eth_swaprouter02_preflight`) | Prüft, was der Smart Contract tatsächlich tun soll. | Notwendig. Der alte automatische Preflight kennt Universal Router `execute`, die statische Policy `exactInputSingle`: diese Pfade sind nicht austauschbar. Zusätzlicher Router02-Prüfcode beweist noch keinen integrierten Executor. |
| Tokenfreigabe sowie ETH/WETH (`eth_native_weth9_review`, `live_eth_swap_plan`) | Klärt, ob Token ausgegeben werden dürfen und ob am Ende natives ETH oder WETH vorliegt. | Technisch notwendig, wenn die Route es verlangt. Fehlende Freigabe führt nur zu einem unvollständigen Plan, nicht automatisch zur Freigabe. |
| Guthaben und Gas (`eth_cost_budget_review`) | Prüft ausreichende Eingangscoins und ETH für Gebühren. | Notwendig. Starre Zusatzwerte sind optional: 0,005 ETH Reserve, höchstens 0,004 ETH maximale Gaslast, 300 Gwei und 2 Mio. Gas können kleine Trades blockieren; besser bedarfsgerecht kalkulieren. |
| Frische Preise, Stablecoin-Kurs und Preiswirkung (`eth_cost_preview`, `eth_cost_budget_review`) | Verwendet Daten höchstens 30 Sekunden alt; Stablecoin 0,98–1,02 USD; Mindestempfang höchstens 2 % unter Eingabewert. | Vorschau kann scheitern. Plausibilitätsprüfung sinnvoll; diese konkreten Schwellen sind optional/konfigurierbar, keine technische Notwendigkeit. |
| Blockchain-Beleg und endgültige Abrechnung (`live_eth_receipts`, `eth_live_ledger_audit`) | Prüft Absender, Router, Status, Block und Gebühren; unterscheidet ausstehend/erfolgreich/fehlgeschlagen. | Für korrekte Verbuchung notwendig. Vorhandener Belegprüfer bestätigt noch keine vollständigen Swap-Ausgangsmengen und schreibt keinen Live-Trade. |
| Alte/offene Aufträge und Budgetrevision (`eth_prelaunch_safety`, `eth_cost_preview`) | Erkennt ungeklärte Aufträge, widersprüchliche Budgets und über 30 Minuten offene Vorgänge. | Diagnose beziehungsweise Vorschau-Blocker. Ungeklärtes Geld nicht erneut ausgeben ist notwendig; pauschale Sperre aller weiteren Aktionen und feste 30 Minuten sind differenzierbar. |
| Sammelberichte (`eth_auto_preflight`, `eth_prelaunch_safety`) | Fassen die Punkte zusammen. Einige Ergebnisse sind absichtlich konstant negativ. | Keine zusätzliche Blockchain-Funktion; optionale Diagnoseoberfläche. Nicht als Ersatz für Adapterentwicklung behandeln. |
| Globaler Live-Schalter (`live_asset_registry`) | Verweigert jede Nutzung des vorgesehenen Live-Einstiegs. | **Absolute Entwicklungsblockade**, unabhängig davon, ob alle Berichte grün wären. Einfach `True` setzen reicht wegen fehlender Adapter nicht. |

Die nächste funktionale Arbeit sollte diese fehlenden Ausführungspfade und die vier übrigen Bestandsabfragen schließen. Zusätzliche ETH-Diagnoseberichte bringen dafür allein keinen Fortschritt. Starre Umsatz-/Auftrags-/Gasgrenzen sollten ausdrücklich als einstellbare Produktregeln behandelt werden. Eigentum, korrekte Transaktion, Budgettrennung, Doppelauftragsschutz und tatsächliche Abrechnung bleiben erforderlich.
