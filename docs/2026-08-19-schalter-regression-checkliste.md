# Codex Usage: Schalter-Regression-Checkliste

## Metriktabellen

Die vier Metriktabellen verwenden voneinander getrennte Zustände:

- Tokenverbrauch: `show-panel`, `show-tooltip`, `hide-when-zero`, `show-coverage-marker`, `baseline-enabled`
- Tokenende: `show-panel`, `show-tooltip`, `hide-when-zero`, `show-coverage-marker`, `baseline-enabled`
- Credits: `show-panel`, `show-tooltip`, `hide-when-zero`, `show-coverage-marker`, `baseline-enabled`
- Creditverbrauch: `show-panel`, `show-tooltip`, `hide-when-zero`, `show-coverage-marker`, `baseline-enabled`

Die Regressionstests prüfen alle `2^5 = 32` Kombinationen je Tabelle, also 128 Kombinationen insgesamt. Jede Kombination wird normalisiert und auf unveränderte Einzelzustände geprüft. Zusätzlich wird jeder der fünf Schalter einzeln mit einem ungültigen Nicht-Boolean-Wert geprüft.

## Unabhängigkeit

- Coverage der Tokenverbrauchstabelle überschreibt nicht Coverage der Tokenendetabelle.
- Eigener Ausgangswert blendet weder Delta noch Tokenende aus.
- Tokenende besitzt ein eigenes Limitfenster, Format, Glättung, Nullverhalten und Warnformat.
- Creditverbrauch besitzt eigene Sichtbarkeit und eigene Coverage-/AW-Zustände.
- Creditverbrauch-Hover wird nicht aus dem Credit-Hover-Schalter abgeleitet.

## Weitere Schaltergruppen

- Formatierungsstile: Fett/Kursiv oberhalb und unterhalb der Schwelle für Prozent, Datum, Uhrzeit und Restzeit.
- Formatierungsziele: Leiste, Hover und Klick-Menü.
- Panel: Stumm und vier Wertslots.
- Reset-Anzeige: Leiste, Hover, Nullunterdrückung und unbekannte Werte.
- Accountanzeige: Hover- und Klick-Abstandshalter.
- Warnungen: Warnungen und Fehler.
- Routing: Eigene Regel und Credits erlauben.
- Accounts: Serie aktiv und Test-Home.

## Ausgeführte Nachweise

- `node --check files/codex-usage@H234598/applet.js`
- `node --test tests/applet_runtime.test.js` zweimal: jeweils 305/305 bestanden
- `pytest -q tests/test_applet.py tests/test_consumption.py tests/test_config.py`
- `pytest -q`: 1968 bestanden, 1 übersprungen

Die neuen Metrik-Kombinationstests stehen in `tests/applet_runtime.test.js`. Die Normalisierung von Tokenende und Creditverbrauch weist nun ungültige Typen und Werte fail-closed zurück, statt Schalter stillschweigend zu coercen. Die Schema-Prüfung findet keinen Boolean-Schalter ohne Referenz im Applet-Code.
