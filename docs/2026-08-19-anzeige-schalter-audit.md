# Codex Usage: Audit der Anzeige-Schalter

## Status

Umgesetzt am 2026-08-19. Die vier Mess-/Anzeigetabellen behandeln ihre
Schalter jetzt unabhängig voneinander:

1. Tokenverbrauch
2. Tokenende
3. Credits
4. Creditverbrauch

Die Prüfung umfasst jeweils `Coverage`, `Setze eigenen AW`, `Eigener AW in
M.`, Anzeigeziel und die Kombination mit den jeweils anderen Werten.

## Rückwirkend festgehaltene Fehler

- Ein aktivierter eigener AW wurde früher als globaler Frühabbruch behandelt
  und konnte dadurch Delta oder Tokenende ausblenden.
- Die Tokenende-Prognose wurde aus der Verbrauchszeile gerendert. Dadurch
  konnten deren Coverage-/AW-Schalter die Tokenende-Anzeige indirekt
  beeinflussen.
- Forecast-Einstellungen für Coverage und Baseline wurden beim Zusammenführen
  der Tabellen nicht vollständig weitergereicht.
- Bei gleichem Provider und Limitfenster wurden Delta und Tokenende teilweise
  mit nur einer Anfrage berechnet, obwohl unterschiedliche AW- oder
  Glättungseinstellungen eine getrennte Berechnung erfordern.
- Coverage war bei vollständiger Abdeckung unsichtbar. Ein aktivierter
  Coverage-Schalter zeigt nun auch `(vollständig)`; deaktiviert bleibt der
  Marker vollständig verborgen.
- Credits und Creditverbrauch hatten keine vollständige, gleichartige
  Coverage-/AW-Konfiguration.

## Verbindliche Anzeige-Regeln

- `Setze eigenen AW` steuert ausschließlich die eigene AW-Ausgabe und die
  Berechnung der jeweiligen Tabelle.
- Delta, Tokenende, Credits und Creditverbrauch dürfen sich gegenseitig nicht
  ausblenden.
- `Coverage` steuert ausschließlich den Coverage-Marker der eigenen Tabelle.
- `Setze eigenen AW` zeigt einen AW-Wert nur, wenn das DTO einen belastbaren
  `baseline_used_percent` liefert; andernfalls wird kein erfundener Wert
  angezeigt.
- Tokenende besitzt seine eigene Coverage-, AW-, Limit- und
  Anzeigeeinstellung. Die Verbrauchseinstellungen dürfen sie nicht ersetzen.
- Für Tokenverbrauch und Tokenende werden getrennte Backend-Abfragen erzeugt,
  sobald Fenster, Glättung oder AW voneinander abweichen.
- `Bei null ausblenden` bleibt auf die jeweils eigene Tabelle begrenzt.

## Testmatrix

Für alle vier Tabellen wurden die Kombinationen

| Coverage | eigener AW |
|---|---|
| aus | aus |
| aus | an |
| an | aus |
| an | an |

ausgeführt. Zusätzlich wurden die Anzeigeziele für Leiste, Hover und Klick
als getrennte Zustände berücksichtigt. Erwartet wird immer:

- Tokenverbrauch bleibt sichtbar, wenn sein Ziel aktiv ist.
- Tokenende bleibt sichtbar, wenn sein Ziel aktiv ist.
- AW erscheint genau dann, wenn der AW-Schalter aktiv und ein AW-Wert im DTO
  vorhanden ist.
- Coverage erscheint genau dann, wenn der Coverage-Schalter aktiv ist.
- Credits und Creditverbrauch verhalten sich analog.

Die Regressionstests liegen in `tests/applet_runtime.test.js`.

## Nachtrag: Account-Reihenfolge und Fast-Mode-Auswahl

- Die Serienübersicht zeigt die bereits belegte A-Serie als `A`; der Zusatz
  `(belegt)` wird nicht mehr in die Tabelle geschrieben. Im Bearbeitungsdialog
  bleibt A für fremde Accounts nicht auswählbar.
- Die Tabelle `Leiste` wird anhand der Reihenfolge aus
  `Abrufwege und Accounts` synchronisiert.
- `fast-mode-icon` verwendet einen eigenen Selector mit SVG-Vorschau und
  Icon-spezifischem Tooltip. Das ausgewählte Warnschild kann direkt im
  Dropdown angesehen werden, ohne den Fast-Modus tatsächlich aktivieren zu
  müssen.

## Nachtrag: EMA, Kürzel und zehn Audit-Runden

- Der Standard der Tokenverbrauchstabelle ist jetzt `EMA – 10 Minuten`; die
  vorhandenen Tokenverbrauchszeilen wurden auf diesen Standard migriert.
  Tokenende, Credits und Creditverbrauch behalten ihre eigenen Defaults.
- Die Account-Kürzel werden für die Leiste zusätzlich aus der zentralen
  Accounttabelle aufgelöst, falls der Backend-Überblick kein Kürzel liefert.
- Die Baseline-Anzeige benötigt kein separates Anzeigeziel mehr: `Setze
  eigenen AW` ist innerhalb der jeweiligen Tabelle maßgeblich.
- Forecast-Zeilen werden bei jeder direkten Änderung der Tokenende-Tabelle
  akzeptiert; sie fallen nicht mehr auf Legacy-Verbrauchseinstellungen zurück.

Es wurden zehn fokussierte Audit-Runden durchgeführt: Schema/Defaults,
Normalisierung, Tokenverbrauch, Tokenende, Credits, Oberflächen, Kürzel und
Reihenfolge, Persistenz/Reload, Prozess-/Heap-Pfade sowie eine abschließende
Gesamtregression. Jede Runde endete mit Tests; insgesamt bestanden zuletzt
302 JavaScript- und 80 Python-Tests.

## Architekturhinweis

Die Python-/CLI-Schicht bleibt die Quelle für Verbrauch, Coverage,
Baselinewert und Prognose. Das Applet validiert und rendert diese DTO-Werte;
es erfindet keine Tokenzahlen und keine Messpunkte.
