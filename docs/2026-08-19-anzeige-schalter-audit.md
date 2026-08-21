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
Gesamtregression. Jede Runde endete mit Tests; die aktuelle Laufzeitprüfung
umfasst 309 JavaScript-Tests. Die frühere Matrix war trotzdem unvollständig:
Sie gab synthetische `cost_windows` direkt an den Renderer und umging damit
den realen DTO-Sanitizer `_safeConsumptionWindows()`. Dieser Sanitizer ließ
`baseline_used_percent` fallen. Deshalb konnten die Matrixtests AW als
vorhanden sehen, während das laufende Applet keinen AW mehr erhielt. Die
Regression prüft den echten Sanitizer-Pfad jetzt mit.

Zusätzlich wird `baseline_used_percent` fail-closed als echte endliche Zahl
validiert; `null`, fehlend und nichtnumerisch werden nicht mehr über
`Number(...)` versehentlich zu `0%`.

Die kompakte Tokenende-Darstellung verwendet jetzt Dezimalstunden mit einer
Nachkommastelle, z. B. `5,9h` oder `4,3h`. Delta-Prozentwerte werden ebenfalls
immer mit genau einer Nachkommastelle und deutschem Dezimaltrennzeichen
gerendert, z. B. `12,6%`.

## Architekturhinweis

Die Python-/CLI-Schicht bleibt die Quelle für Verbrauch, Coverage,
Baselinewert und Prognose. Das Applet validiert und rendert diese DTO-Werte;
es erfindet keine Tokenzahlen und keine Messpunkte.

## Masterjet-Notfalloverride

Der Masterjet darf im Notfallmodus für einen betroffenen Account eine
reversible Datei unter
`~/.local/state/codex-master-mcp/codex-usage-emergency-overrides.json`
veröffentlichen. Ein aktiver Eintrag schaltet Delta für Leiste und Hover ein
und setzt das beobachtete Fenster auf 5h, ersatzweise Woche oder 30 Tage.
Nach Ende des Notfallmodus wird der Eintrag entfernt; die normalen
Applet-Einstellungen werden nicht überschrieben.

## Nachtrag: Same-Pool-Refresh und vollständige TE-Matrix

Eine weitere Root Cause lag im asynchronen Verbrauchsabruf: Wenn
Tokenverbrauch und Tokenende denselben Pool, aber unterschiedliche Fenster
verwendeten — beispielsweise Verbrauch `5h` und Tokenende `Woche` — entfernte
der zweite Callback bisher den kompletten Pool, bevor er seine Antwort
anhängte. Damit gingen gültige Fenster verloren und die Oberfläche konnte
scheinbar vollständig leer werden. Der Callback ersetzt jetzt nur Fenster mit
identischem Pool und identischer Fensterdauer.

Die Regression prüft den echten Refreshpfad mit zwei getrennten Antworten und
verifiziert, dass 5h-Delta, Wochen-Tokenende und der eigene TE-AW gleichzeitig
erhalten bleiben. Zusätzlich werden alle vier TE-Formate (`compact`,
`compact-minutes`, `verbose`, `custom`), Leiste und Hover sowie Coverage und
TE-AW in allen Kombinationen geprüft. Bei benutzerdefinierten Formaten wird
Coverage automatisch ergänzt, wenn der Coverage-Schalter aktiv ist und das
Format keinen eigenen `{coverage}`-Platzhalter verwendet.

## Nachtrag: Defaultpfad der Tokenende-Tabelle

In der zweiten unabhängigen Fehlersuchrunde fiel ein Reload-/Migrationsfehler
auf: Eine unvollständige Tokenende-Zeile setzte `Tokenende Leiste` implizit
auf `an`, obwohl der kanonische Default der Tabelle `aus` lautet. Vollständige
Einstellungszeilen waren nicht betroffen; nach einer partiellen Migration oder
einem unvollständigen Speichervorgang konnte die Leistenanzeige aber unerwartet
erscheinen. Die Normalisierung verwendet nun für den fehlenden Wert den
zentralen Tabellen-Default. Der Regressionstest
`incomplete forecast rows retain the disabled token-end panel default`
belegt genau diesen Pfad.

## Nachtrag: Benutzerdefiniertes Tokenende-Markup

Die dritte Prüfrunde fand einen zweiten reinen Rendererfehler: Das eigene
Tokenende-Format wurde als Klartext korrekt erzeugt, im Markup für Leiste und
Hover aber durch die Standardbeschriftung `Zeit bis Tokenende` ersetzt. Damit
konnten Klartext- und sichtbare Oberfläche auseinanderlaufen. Der Renderer
verwendet jetzt für das benutzerdefinierte Format denselben Text in beiden
Ausgabekanälen; Stil- und AW-Anhänge bleiben dabei unverändert. Der Test
`custom token-end format is preserved in visible markup` deckt diesen Pfad ab.

## Nachtrag: AW bei fehlender Tokenende-Prognose

In der vierten Prüfrunde zeigte der Stale-/Fehlerpfad eine doppelte
AW-Darstellung im Markup: Bei fehlender Prognose war der AW bereits im
unformatierten Tokenende-Text enthalten und wurde anschließend noch einmal an
das Markup angehängt. Der Klartext war korrekt, die sichtbare Leiste/Hover
konnte den AW aber zweimal zeigen. Das Markup baut den Haupttext jetzt stets
ohne den später gemeinsamen AW-Anhang auf. Der Test
`missing token-end estimate does not duplicate its configured baseline in markup`
belegt die Fehler- und Stale-Konstellation.

## Nachtrag: Coverage im eigenen Creditverbrauch-Format

Die fünfte Prüfrunde ergab eine Inkonsistenz zwischen den vier Tabellen: Bei
Creditverbrauch verschwand ein aktivierter Coverage-Marker in einem eigenen
Format, wenn dieses keinen `{coverage}`-Platzhalter enthielt. Tokenverbrauch
und Tokenende ergänzen den Marker bereits in diesem Fall. Creditverbrauch
folgt nun derselben Regel; enthält das Format den Platzhalter, bleibt es bei
einer einzigen Ausgabe. Die Regression
`custom credit consumption retains the enabled coverage marker` sichert die
gemeinsame Semantik.

## Nachtrag: „Bei null ausblenden“ bei Credits

In der sechsten Prüfrunde wurde die Credits-Tabelle korrigiert: Die Einstellung
`Bei null ausblenden` prüfte neben dem Restbestand auch den Verbrauchsanteil.
Ein positives Guthaben konnte damit verschwinden, nur weil bislang 0% davon
verbraucht waren. Die Einstellung bezieht sich nun ausschließlich auf einen
tatsächlich nullen Restbestand. Die Regression
`credit hide-when-zero does not hide a positive balance with zero usage`
deckt diese Abgrenzung ab.

## Nachtrag: Fehlgeschlagener Verbrauchs-Refresh

Die siebte Prüfrunde fand einen unabhängigen Datenverlustpfad: Ein neuer
Verbrauchs-Refresh leerte alle bereits validierten Messfenster schon vor dem
Start seiner Anfragen. Schlug eine davon fehl, wurden Leiste und Hover leer,
obwohl ein letzter gültiger Messwert vorlag. Fenster bleiben nun bis zu einer
erfolgreichen, identisch zugeordneten Antwort erhalten und werden erst dann
ersetzt. Der Test `failed consumption refresh preserves the last validated
window` sichert das Fehlerverhalten ab.

## Nachtrag: Gleiches Fenster, unterschiedliche Berechnung

Die achte Prüfrunde schloss eine verbleibende Parallelitätslücke: Tokenverbrauch
und Tokenende können denselben Pool und dasselbe Limitfenster abfragen, aber
mit verschiedener EMA-Glättung oder verschiedenem AW. Die Antwort des zweiten
Auftrags ersetzte bisher die erste, weil nur Pool und Limitfenster verglichen
wurden. Messfenster tragen nun zusätzlich eine interne, nicht persistierte
Abfrageidentität aus Pool, Zeitraum, Glättung und AW. Renderer wählen nur das
zu ihrer eigenen Berechnung gehörende Ergebnis; unmarkierte Legacy-Fenster
bleiben bis zum ersten Live-Ergebnis kompatibel. Der Test
`same-window consumption and token-end queries retain their own smoothing results`
belegt die Trennung.

## Nachtrag: Begrenzte Query-Identitäten

Die neunte Prüfrunde prüfte die Speicherwirkung der neuen Query-Identitäten.
Bei wiederholtem Umschalten von Zeitraum, EMA oder AW hätten Ergebnisse früherer
Abfragen sonst im Runtime-Array verbleiben können. Vor jeder neuen
Verbrauchsrunde werden jetzt ausschließlich intern markierte Ergebnisse
entfernt, deren Identität nicht mehr zu einer aktuell benötigten Abfrage passt.
Unmarkierte Cache-/Legacy-Fenster bleiben bis zu einem Live-Ergebnis erhalten.
Der Test `consumption refresh prunes obsolete tagged query results but retains
legacy data` sichert Aufräumen und Rückwärtskompatibilität gemeinsam ab.

## Nachtrag: Breiter Integrationsdurchlauf

Die zehnte Runde verglich die Applet-Laufzeit erneut mit DTO-, Verbrauchs-,
Historien- und Snapshot-Tests der Python-/CLI-Schicht. Es wurde kein weiterer
Widerspruch gefunden: 119 Python-Tests und 317 Applet-Laufzeittests bestanden.
Die Query-Identität bleibt ausschließlich Laufzeitmetadatum; sie erweitert
weder das persistierte DTO noch die externe CLI-Schnittstelle.

## Nachtrag: Keine Abfrage für vollständig deaktivierten Creditverbrauch

Die elfte Runde prüfte, ob die Sichtbarkeitsziele auch tatsächlich die
Abfragekosten steuern. Ein vorhandenes, aber auf allen Flächen deaktiviertes
Creditverbrauch-Ziel löste bisher trotzdem eine Anfrage aus, weil nur auf die
Existenz der Zielzeile geprüft wurde. Die Entscheidung nutzt nun dieselben
Panel-/Hover-/Klickregeln wie der Renderer. Der Test
`fully disabled credit-consumption targets do not start a consumption request`
belegt, dass unsichtbarer Creditverbrauch keine Ressourcen mehr verbraucht.

## Nachtrag: Späte Antwort einer alten Refresh-Generation

Die dreizehnte Runde untersuchte einen Settingswechsel während einer noch
laufenden Verbrauchsanfrage. Die vorhandene Generationsprüfung verwirft die
späte Antwort korrekt und startet anschließend die Warteschlange der neuen
Generation; nur deren Ergebnis darf die Anzeige verändern. Der Test
`late consumption response from an older generation cannot replace newer
settings` dokumentiert und sichert diese Race-Condition.

## Nachtrag: Eigener AW ist tabellenlokal

Die vierzehnte Runde bestätigte die ursprüngliche Anzeigeursache: Ein
aktivierter eigener AW wurde zusätzlich durch das alte Formatierungsziel
`baseline` gefiltert. Dessen Panel-Standard ist aus, weshalb `Setze eigenen
AW` in der Leiste wirkungslos aussehen konnte. Das Baseline-Ziel ist keine
separate Nutzeroption mehr; der AW folgt ausschließlich dem Schalter seiner
eigenen Tabelle und dem dort gewählten Ziel Leiste/Hover. Der Test
`enabled own baseline remains visible when the legacy baseline target is
disabled` sichert die Regel ab.

## Runde 15: Altes globales AW-Ziel entfernt

Das historische Formatierungsziel `baseline` (Element 13) war weiterhin in
den *Formatierungsorten* sichtbar, obwohl es nach der Korrektur keinen
Einfluss mehr haben darf. Das war irreführend: Es wirkte wie eine zweite
Anzeigeoption für den tabellenlokalen Schalter `Setze eigenen AW`.

Element 13 wird nun beim Einlesen alter Einstellungen verworfen und nicht
mehr erzeugt. Die drei Anzeigeziele des jeweiligen Metrikwerts bleiben
unverändert zuständig. Der Regressionstest
`legacy global baseline style targets are discarded during migration`
sichert die Migration; alte gespeicherte Zeilen verursachen keinen
Einstellungsfehler. `legacy global baseline style target is removed from
persisted settings` deckt zusätzlich den Persistenzpfad ab: Beim nächsten
Synchronisieren wird die Altzeile auch dauerhaft aus den Einstellungen
entfernt und kann nach einem Reload nicht zurückkehren.

Der Schemaeintrag und die Panel-Vorauswahl verwendeten Element 13 ebenfalls
noch. Beide sind entfernt. Der Test `a legacy global baseline target cannot
keep an otherwise empty panel row alive` verhindert, dass ein aus einer alten
Konfiguration stammendes Ziel künftig eine inhaltlich leere Panelzeile
erzeugt.

## Runde 17: Unbekannte Anzeigeziele fail-closed

`_elementTargetEnabled()` und `_targetEnabled()` akzeptierten bisher einen
unbekannten Elementnamen implizit. Mit einem gesetzten Legacy-Sichtbarkeitswert
konnte er dadurch sichtbar werden. Beide Funktionen lehnen unbekannte Namen
jetzt vor jeder Fallback-Auswertung ab. Der Test
`unknown display targets fail closed even when a legacy visibility value is true`
sichert dies ab.

## Runde 18: Gemeinsame Werte- und Dauergrenzen

Die reinen Hilfsfunktionen für Dauer, Zahlen, Limitfenster und begrenzte
Ganzzahlen hatten keine direkte Regression. Der Test
`duration, numeric and limit-window helpers enforce their documented bounds`
prüft nun Nullwerte, Grenzen und unzulässige Werte. Damit bleiben unendliche,
negative, nichtganzzahlige oder übergroße Dauern aus Reset- und
Verbrauchspfaden ausgeschlossen.

## Runde 19: Delta-, Coverage- und Templateformatierung

Die Formatierungshelfer für Coverage, Verbrauchs- und Prognosevorlagen,
Dezimalwerte und Zeiteinheiten sind nun direkt mit ihren vollständigen
unterstützten Platzhaltern getestet. `consumption and forecast formatting
helpers preserve their supported placeholders` sichert insbesondere, dass
unbekannte Platzhalter sichtbar bleiben, anstatt still verändert zu werden.

## Runde 20: Tokenende-Warnformate

Alle Warnformatvarianten werden jetzt direkt getestet. `blink-red-yellow` ist
bewusst die statische, fette Ersatzdarstellung: Die Einstellung und ihr
Hilfetext benennen bereits, dass Cinnamon in diesem Kontext keine verlässliche
Text-Blinkanimation bietet. Der Test `forecast warning formats apply only
their documented Pango attributes` stellt sicher, dass keine nicht
unterstützten Attribute erzeugt werden.

## Runde 21: Vertrauensgrenzen für Text und Ganzzahlen

Die Anzeige darf beschmutzte Backendtexte bereinigen, Einstellungen hingegen
nicht still umformen. Der Test `text and strict-integer helpers distinguish
sanitizing display text from trusted settings` trennt diese Regeln ausdrücklich
und prüft zudem Begrenzung, Steuerzeichen sowie echte Ganzzahlwerte.

## Runde 22: Backend- und Routing-Provenienz

`backend provenance and routing identifiers are validated without
normalization` deckt die gemeinsamen Status-, Backend- und Routing-Helfer ab.
Lesedaten dürfen für die Anzeige bereinigt werden; als Provenienz oder
Routing-ID werden Leerzeichen, Steuerzeichen und unbekannte Werte dagegen
abgelehnt.

Dabei wurde ein Fehler gefunden und behoben: Fehler aus der vorgeschalteten
strikten Textprüfung einer Routing-ID wurden nach außen gereicht. Die Methode
übersetzt sie nun einheitlich in `invalid routing policy identifier`; Aufrufer
erhalten damit unabhängig von der Ursache denselben fachlichen Fehler.

## Runde 23: Lokale Accountpfade und URI-Grenzen

`local account path helpers accept local paths and localhost URIs only` prüft
die direkte Pfad-/URI-Normalisierung. Lokale Pfade und `localhost` bleiben
zulässig, fremde URI-Authorities und relative Pfade werden fail-closed
abgewiesen; auch leere optionale Werte behalten ihre definierte Bedeutung.

## Runde 25: Markup, Status und Capture-Provenienz

`_escapeMarkup()` verliert jetzt keine gültigen Null- oder Booleanwerte mehr;
leer sind nur noch `null` und `undefined`. Die direkten Tests prüfen zudem
Markup-Escaping, Statuslabel und Zeitstempel-Provenienz einschließlich
unzulässiger Zukunftswerte und veralteter Captures.

## Runde 26: Spark-Severity folgt dem Pool-Gate

Die Severity-Berechnung wertete zuvor Spark-Fenster unabhängig vom
`available`-/Usability-Status des Pools aus. Dadurch konnten gesperrte oder
veraltete Sparkdaten fälschlich eine Warnung auslösen. Sie verwendet nun
`_poolIsUsable()` wie die übrigen Poolpfade. Der Test `usage severity ignores
Spark windows from an unavailable model pool` prüft beide Zustände.

## Runde 24: Datum, Zeit, Restlaufzeit und Stilmodi

`date, time, duration and style helpers cover all supported display modes`
prüft sämtliche dokumentierten Darstellungsvarianten, einschließlich
Dezimalstunden, ungültiger Restlaufzeitdaten sowie der vier Stilmodi und ihrer
Schwellwertlogik.

## Runde 30: Credit-Custom-Format-Migration

Die Credit-Normalisierung konvertierte bisher `0` oder `false` in
`custom-format` durch einen Truthiness-Fallback still zu `""`. Das ist ein
Datenverlust und verschleiert eine fehlerhafte Einstellung. Die Werte werden
jetzt typstrikt geprüft; nur fehlende Werte bedeuten den leeren Default. Der
Test `credit custom formats reject non-text values instead of silently
becoming empty` sichert beide Credit-Tabellen ab.

## Runde 31: Legacy-Felder ohne Truthiness-Migration

Forecast- und Verbrauchs-Normalizer behandeln Legacywerte jetzt nur dann als
fehlend, wenn sie tatsächlich `undefined` sind. `0`, `false` und leere Werte
werden nicht mehr still durch gültige Defaults ersetzt. Der Test `legacy
consumption and forecast fields do not coerce falsey invalid values to
defaults` deckt die betroffenen Spalten ab.

## Runde 32: DTO-Sanitizer

`DTO sanitizers retain valid fields and fail closed on contradictory usage
metadata` prüft die direkte Sanitizer-Schicht für Limitfenster,
Verbrauchsfenster und Resetstatus. Gültige Werte bleiben erhalten; ungültige
Dauern, widersprüchliche Prozentdaten und übergroße Resetwerte werden nicht
als nutzbare Daten weitergereicht.

## Runde 33: Payload-Wertaggregation

`payload usage aggregation requires known windows and usable model pools`
prüft, dass Payloads nur mit bekannten Fenstern oder tatsächlich nutzbaren
Pools als wertbehaftet gelten. Unbekannte Fenster, nicht verfügbare Pools und
erschöpfte Werte bleiben aus der Aggregation ausgeschlossen.

## Runde 34: Cache-Fenster-Matching und Ablauf

`cache window matching and expiry use the declared kind and duration` prüft
Fensterlabels, Alias-Matching, widersprüchliche Dauern sowie Ablauf durch
Resetzeitpunkt, ungültige Zeitstempel und resetlose Cachewerte.

## Runde 27: Fensteridentität und Poolauswahl

`window identity helpers distinguish aliases, conflicts, duplicates and pool
selection` prüft die kanonischen 5h-/Wochen-/Monatsidentitäten, Aliasnamen,
widersprüchliche Dauerangaben, Duplikate, Durchschnitts- und
Alternativfenster sowie die Erschöpfungsgrenze eines Pools.

## Runde 28: Monatsquelle im Panel

Die monatliche Panelquelle (Slot 8) konnte den Nutzbarkeits-Gate des
Hauptpools umgehen: Der Wert wurde noch aus einem Monatsfenster gelesen,
obwohl der Pool als nicht verfügbar markiert war. `_panelValueForSource()`
wendet das Hauptpool-Gate nun auch auf Slot 8 an. Der Test `monthly panel
source cannot bypass an unusable main pool` sichert das ab.

## Runde 35: Cache-Merge und Reset-Provenienz

Die Merge-Grenzen sind jetzt direkt abgesichert. Nur die beiden bekannten
Inferenzquellen für ein inaktives 5h-Fenster werden als solche erkannt.
`values_captured_at` wird nur verwendet, wenn der Zeitstempel gültig und nicht
jünger als `captured_at` ist. Ein gültiger Cache darf bei einer frischen
Antwort den Messwert ergänzen, übernimmt dabei aber den frischen Reset; bei
abgelaufenem oder falsch klassifiziertem Cache bleibt ausschließlich die
frische Antwort erhalten. Ein fehlender Reset wird nur aus einem gültigen,
gleichartigen Cache ergänzt, ohne das Fresh-Objekt zu verändern.

Die direkten Regressionstests prüfen diese Entscheidungen einschließlich
Quellenschutz, Ablauf, Fenster-Mismatch und Objektimmutabilität. Die Runtime-
Suite läuft damit vollständig grün: 347 Tests, 347 bestanden.

## Runde 36: Resetlose dynamische Browserfenster

Der Funktionsabgleich hat eine echte Cache-Lücke gefunden: `_hasResetlessBrowserUsage()`
betrachtete nur die Legacy-Felder `five_hour` und `weekly`. Bei einem Browser-
Payload mit dynamischen `main`- oder Modellfenstern konnte deshalb
`_mergeMissingPoolResets()` trotzdem einen alten Resetzeitpunkt übernehmen.
Das ist bei Browserdaten unzulässig, weil der aktuelle Messwert dann mit einer
nicht belegten Resetprovenienz versehen würde.

Der Schutz prüft jetzt zusätzlich alle dynamischen Fenster in `main` und in
den Modellpools. Die Pool-Resetübernahme wird für jeden resetlosen Browser-
Payload übersprungen. Der Regressionstest `browser dynamic resetless usage does
not restore a cached reset` prüft den konkreten Fehlerfall. Die Runtime-Suite
läuft anschließend vollständig grün: 348 Tests, 348 bestanden.

## Runde 37: Identität, Provenienz und Prototyp-Sicherheit

Die direkten Tests für Backend-Identitäten decken jetzt vollständige,
partielle, inkompatible und nur kompatible Identitäten getrennt ab.
Fallback-Provenienz wird nur bei bekannten Backendrichtungen und bekannten
Fehlergründen akzeptiert. Fenster-Dauer, Resetablauf und Alias-/Dauerkonflikte
werden ebenfalls direkt geprüft.

Dabei wurde in `_modelPool()` ein echter Fail-Closed-Fehler behoben: Ein
gewöhnliches JavaScript-Objekt konnte über einen geerbten Schlüssel wie
`toString` einen Nicht-Pool liefern. Der Helper akzeptiert nun nur eigene
Objekte und keine Arrays.

Die Runde ergänzt vier Regressionstests. Verifikation: 352/352 Node-Tests
bestanden, `git diff --check` sauber; zusätzlich 1968 Python-Tests bestanden
und 1 Test übersprungen.

## Runde 38: Anzeige- und Formatierungshelfer

Die zuvor überwiegend indirekt getesteten Anzeigehelfer sind jetzt direkt
abgesichert: Accountseparatoren mit Fallback, alle Panelquellen, Account-Tags,
Separator-Sichtbarkeit, Query-/Pool-/Limitfenster-Auswahl, eigene
Ausgangswerte, Credit-Custom-Formate, Prozentdarstellung und Ausblendung.
Dabei wurde kein weiterer Pluginfehler gefunden. Zwei zunächst fehlschlagende
Fälle waren Testannahmen: Bei einer nicht passenden Query-ID bleibt nur der
ungetaggte Legacy-Kandidat zulässig; außerdem müssen VM-Objekte feldweise
geprüft werden statt per `deepStrictEqual`.

Verifikation: 355/355 Node-Tests bestanden, JSON-/Applet-Check bestanden und
`git diff --check` sauber.

## Runde 39: Backend-Zustand und Pool-Reset-Merge

Die direkten Tests decken jetzt auch Backend-Zuordnung, leere versus bereits
gecachte Nutzung, autoritativ leere Limits, authentifizierte Teilantworten,
dynamische Fenster und resetlose Browserdaten ab. Zusätzlich wird die
Resetübernahme innerhalb eines gültigen Modellpools einschließlich
Nicht-Mutation eines nicht verfügbaren Pools geprüft.

Zwei zunächst rote Tests waren fehlerhafte Testdaten: Ein direkt konfiguriertes
Backend kann nicht als `app-server` gelten; außerdem lag der behauptete 5h-
Resetzeitpunkt sechs statt fünf Stunden nach dem Cache-Capture. Die
Implementierung war in beiden Fällen korrekt; die Tests wurden auf die
definierten Regeln korrigiert.

Verifikation: 357/357 Node-Tests bestanden und `git diff --check` sauber.

## Runde 40: Einstellungs-Merger, Legacy-Teilzeilen und ungültige Duplikate

Die Einstellungs-Merger für Tokenverbrauch, Tokenende, Creditverbrauch,
Panel, Resets, Warnungen, Accountanzeige, Formatierungsstile und Ziele wurden
gegen ihre Normalizer und Defaults geprüft. Dabei wurden zwei reale Fehler in
der Legacy-Erkennung gefunden: Forecast- und Creditverbrauchszeilen wurden
nur berücksichtigt, wenn gerade die Felder Panel/Format beziehungsweise
Menge/Format vorhanden waren. Teilmigrationen mit ausschließlich Glättung,
Coverage, Ausgangswert oder Warnung fielen dadurch still auf den vollständigen
Default zurück. Die Erkennung akzeptiert nun nur bekannte Präfixfelder, aber
alle davon.

Zusätzlich konnte eine ungültige aktuelle Zeile durch eine ältere gültige
Legacy-Zeile ersetzt werden. Das war eine Verletzung der Quellenpräzedenz und
konnte veraltete Anzeigeeinstellungen zurückholen. Die aktuelle Zeile wird
jetzt separat als gesehen markiert; ist sie ungültig, bleibt der Merger beim
sicheren Default. Dieselbe Seen-Markierung verhindert außerdem, dass eine
spätere gültige Duplikatzeile eine ungültige erste Zeile überschreibt. Das gilt
für alle geprüften Account-Merger einschließlich Panel, Anzeige, Stile, Ziele,
Token-/Creditverbrauch, Tokenende, Resets und Warnungen.

Die Runde ergänzt fünf direkte Regressionstests für partielle Legacyfelder,
Quellenpräzedenz und ungültige Duplikate. Verifikation: 362/362 Node-Tests
bestanden, `make applet-check` erfolgreich und `git diff --check` sauber.

## Runde 41: Mapping- und Primitive-Helper

Die Funktionsinventur umfasst aktuell 328 Methoden. Für die zentrale Gruppe
der reinen Settings-/Identitätshelfer fehlten direkte Tests: acht
Settings-Maps, der zusammengesetzte Target-Key-Map, Zeilenvergleich,
Integer-Grenzen, strikte Integerwerte, gekürzte Texte, Account-Tags,
Statuslabels und Datumsparsing werden jetzt separat geprüft.

Die Tests bestätigen, dass die Maps auch bei Schlüsseln wie `__proto__` und
`toString` keine geerbten Eigenschaften verwenden. Der Target-Map-Test hält
dabei den tatsächlichen Vertrag fest: Targets werden absichtlich über
`Account:Element` adressiert. Für die Primitive werden Rundung, Fallback,
Grenzbegrenzung, Typablehnung, Textkürzung, unbekannte Statuswerte und
ungültige Datumswerte geprüft.

Ein erster Testlauf enthielt eine falsche Erwartung an den zusammengesetzten
Target-Schlüssel und wurde ohne Codeänderung korrigiert. Verifikation:
363/363 Node-Tests bestanden, `make applet-check` erfolgreich.

## Runde 42: Prozess-, Source- und Timer-Lifecycle

Die Lifecycle-Helfer wurden mit direkten Zustandsprüfungen ergänzt. Quellen
werden beim Setzen, Löschen und Entfernen sowohl aus der Property als auch aus
dem zentralen Source-Register entfernt. Idle-Callbacks entfernen ihre eigene
Referenz; bei zwei ausstehenden Guard-Releases darf nur der jüngste Token den
Guard lösen. Auch das Sammel-Cleanup der Idle-Quellen ist abgesichert.

Für Fehlerpfade wird jetzt direkt geprüft, dass `_runSafely()` den Fallback
liefert und entfernte Applets keinen Callback ausführen. Der Refresh-Circuit
öffnet nach drei Fehlern, aktualisiert das Panel einmal und wird durch einen
Erfolg vollständig zurückgesetzt. Der Timer-Test trennt die
60-Sekunden-Anzeige vom optionalen Usage-Polling und prüft den sicheren
Verhalten bei entferntem Applet.

Es wurde in dieser Runde kein weiterer Pluginfehler gefunden. Verifikation:
366/366 Node-Tests bestanden, `make applet-check` erfolgreich.

## Runde 43: Kombinierte Settings- und Legacy-Roundtrips

Der direkte Roundtrip-Test der kombinierten Token- und Creditzeilen hat einen
echten Quellenfehler gefunden. Eine alte kombinierte Tokenzeile enthält sowohl
die normalen Tokenfelder als auch die `forecast-*`-Felder. Beim Wiederaufbau
der Tokenendezeile bevorzugte der Normalizer bisher die normalen Tokenfelder;
dadurch konnte beispielsweise `forecast-show-panel: true` als
`show-panel: false` erscheinen. Der gleiche Präzedenzfehler war im
Creditverbrauchspfad möglich.

Die Legacy-Merger bauen für ihre jeweilige Präfixquelle jetzt eine isolierte
Normalisierungszeile auf. Dadurch werden nur die bekannten `forecast-*`-
beziehungsweise `consumption-*`-Felder übertragen; fehlende Felder fallen auf
die Defaults der richtigen Tabelle zurück und Felder der anderen Tabelle
können sie nicht mehr überschreiben.

Der Regressionstest prüft außerdem die Rückspeicherung: Forecast- und
Creditverbrauchsfelder werden aus Storagezeilen entfernt, während die jeweils
andere Tabellenfamilie erhalten bleibt. Verifikation: 367/367 Node-Tests
bestanden, `make applet-check` erfolgreich.

## Runde 44: Formatierungstabellen und Anzeigeziele isoliert

Die Formatierungsseite enthielt neben den bereits getrennten sechs Seiten-
Abschnitten noch zwei alte, kombinierte Layoutdefinitionen. Sie wurden nicht
mehr über `format-page` angezeigt, waren aber doppelte Container für dieselben
Schlüssel und konnten Cinnamon bei der Widget-/Layoutauflösung verwirren. Die
veralteten Container sind entfernt; jede Tabelle hängt jetzt genau einmal in
ihrem eigenen Abschnitt mit eigener Überschrift.

Die vier Stiltabellen und die Account-Anzeigetabelle erhalten 300 Pixel
Scrollbereich; die Anzeigeziel-Tabelle erhält 420 Pixel. Damit bleiben
Überschrift, Tabellenkopf, sichtbare Zeilen und Bearbeitungsleiste voneinander
getrennt, auch wenn mehrere Tabellen direkt nacheinander auf der Seite stehen.
Das ist nur eine UI-Geometrieänderung; gespeicherte Werte werden nicht
verändert.

Die Formatierungsziele sind gegen den Runtime-Vertrag geprüft: genau 13
Elemente (`0..12`) pro Account, keine historische globale AW-Zeile `13`, und
pro Tabelle bleibt der Bearbeiten-Button verfügbar. Das Tooltip erklärt jetzt
auch Doppelklick, Stift-Symbol und direkte Checkbox-Umschaltung. Der
Regressionstest prüft zusätzlich, dass kein Tabellen-Key doppelt in der
Formatierungsseite eingebunden wird.

## Runde 45: Codex-Usage-Serien und Routing-Einstellungen

Die Serien-/Account-Komponente war bisher im laufenden Audit nicht direkt
getestet. Dabei wurden zwei echte Fehler gefunden. Die Serienauswahl fügte
beim Bearbeiten des aktuellen Besitzers zusätzlich zu `C` noch einmal
`C (aktuell)` mit demselben Wert ein. Das erzeugte doppelte Auswahlwerte und
konnte den Eindruck einer falschen Elementanzahl erwecken. Der aktuelle Wert
wird jetzt nur noch als Legacy-/nicht mehr angebotene Serie ergänzt, wenn er
noch nicht in der verfügbaren Liste steht.

Außerdem behandelte `_active_owners()` jeden truthy Wert als aktiv. Ein
beschädigter gespeicherter String wie `"false"` konnte damit eine Serie
blockieren. Aktiv ist jetzt ausschließlich der echte Booleanwert `true`.

Die Masterjet-Serienabfrage wird mit Provider-, Aktiv-, Duplikat- und
ungültigen Präfixwerten geprüft; Antwortgröße und 30-Sekunden-Cache bleiben
begrenzt. Im Applet selbst gilt nur eine aktive Doppelbelegung als Konflikt.
Eine inaktive Reservierung darf gespeichert bleiben und später wieder
aktiviert werden; beim Aktivieren greift weiterhin die Konfliktprüfung.

Im Routingpfad wurden Validator, Limitwertumwandlung, globale/scoped
Limitkommandos sowie Allow/Deny/Inheritance-Kommandos direkt getestet. Eine
ungültige, extern veränderte Routingzeile wird jetzt im Änderungs-Callback
fail-closed verworfen: Der Callback loggt den Grund, startet keine Mutation
und lädt den autoritativen Routingstatus neu.

## Runde 46: Codex-Usage-Serienabfrage gegen hängende Masterjet-Prozesse

Der dynamische Serienwähler behandelt nun auch den Randfall, dass der
Masterjet-Kindprozess `stdout` bereits schließt, selbst aber weiterläuft. In
diesem Fall konnte `subprocess.TimeoutExpired` aus `_masterjet_series()` bis in
die Settings-Oberfläche gelangen. Das Widget fängt diesen Fehler jetzt wie
andere Transport-, JSON- und Timeoutfehler ab, beendet die Prozessgruppe und
liefert fail-closed eine leere Serienliste zurück. Das Abfrage-Timeout ist als
begrenzte Klassenkonstante geführt; der Test kann dadurch ohne fünf Sekunden
Wartezeit genau diesen Hängefall reproduzieren.

Der neue Regressionstest prüft den Prozessabbruch sowie die leere Antwort.
Die bestehende Serie-, Provider-, Cache- und Spaltenvertragsprüfung bleibt
erhalten. Diese Runde betrifft nur Codex Usage; keine Konfiguration wird bei
einer fehlgeschlagenen Masterjet-Abfrage automatisch verändert.

## Runde 47: Codex-Usage-Fast-Mode-Icon-Selector

Der Custom-Selector für das Fast-Mode-Warnsymbol wird jetzt unabhängig vom
Cinnamon-Dialog getestet. Der Test deckt Auswahl und Rücklesung eines Symbols,
Tooltiptext, Speichern bei einer echten Benutzeränderung, Fallback auf das
erste bekannte Symbol bei einem unbekannten gespeicherten Wert und das
Unterdrücken eines Schreibvorgangs während der Settings-Anwendung ab.

In diesem Funktionsblock wurde kein weiterer Fehler gefunden. Die Tests
bestätigen, dass die Icon-Auswahl nicht durch einen ungültigen gespeicherten
Wert leer wird und dass ein Settings-Roundtrip keine Rückkopplungsschleife
auslöst.

## Runde 48: Codex-Usage-Warnschwellen und Limitarten

Die bisher nur indirekt abgedeckten Warnschwellen-Helfer werden jetzt als
zusammenhängende Matrix geprüft. Der Test unterscheidet vorhandene 5h-,
Wochen- und 30-Tage-Fenster, fehlende Accounts, vorhandene/fehlende Spark-
Pools sowie unbekannte Spark-Daten. Zusätzlich werden gültige, fehlende und
außerhalb des Bereichs liegende Schwellenwerte geprüft.

Dabei wurde kein Produktfehler gefunden. Ein zunächst fehlerhafter Testwert
`Spark 5h` wurde als ungültige Fensterbezeichnung erkannt und auf die vom
Payload-Vertrag verlangte kanonische Bezeichnung `5h` korrigiert. Das bestätigt,
dass unbekannte Fensteridentitäten nicht versehentlich als gültige Spark-
Grenze gelten.

## Runde 49: Codex-Usage-Identitäts-, Cache- und UTF-8-Helfer

Der direkte Test der Accountzeilen hat einen echten Robustheitsfehler gefunden:
`_accountRowsEqual()` verglich `test-home` und `series-active` zuvor nur über
ihren Truthy-Wert. Dadurch konnten beschädigte Werte wie `1` oder der String
`"true"` als identisch zu einem echten Boolean gelten. Beide Felder werden
jetzt strikt verglichen; der Settingspfad akzeptiert weiterhin nur echte
Booleanwerte.

Zusätzlich werden die Stale-Zeitgrenzen, der NUL-getrennte Auxiliary-Request-
Schlüssel, die `.codex-test`-Erkennung bei ungültigen Eingaben und ein über
mehrere Chunks geteiltes UTF-8-Zeichen direkt geprüft. Der anfängliche Test
enthielt zwei falsche Erwartungswerte für die mathematisch definierte
`refreshInterval + 60`-Grenze; diese wurden korrigiert, ohne Produktionscode
zu lockern.

## Runde 50: Codex-Usage-Routing-Synchronisation

Der bisher nicht direkt geprüfte Rückweg von der autoritativen Routing-Policy
in die Settings-Tabelle ist jetzt abgedeckt. Regeln und Credit-Overrides
werden je Scope vereinigt, alphabetisch sortiert und mit den korrekten
Enable-/Allow- sowie Stunden-, Wochen- und Monatslimits geschrieben. Auch die
globalen Credit-Einstellungen und der Guard-Release werden geprüft.

Zusätzlich prüft der Test, dass `_clearRoutingState()` alte Policy- und
Entscheidungsdaten entfernt, die Anzeige genau einmal aktualisiert und bei
bereits leerem Zustand keine unnötige Aktualisierung auslöst. Es wurde in
diesem Block kein weiterer Produktionsfehler gefunden.

## Runde 51: Codex-Usage-Verbrauchs-/Tokenende-Rendering

Der zentrale Renderingpfad für Verbrauchsfenster wird jetzt direkt mit einer
Kombination aus Delta, Coverage, eigenem Ausgangswert, Tokenende, Custom-Format
und `Bei null ausblenden` geprüft. Zusätzlich wird der Delta-Schalter im
Rendering umgelegt und ein unzureichendes Coverage-Fenster kontrolliert.

Der Test bestätigt dabei die beabsichtigte Zuständigkeit: `_consumptionWindowPart`
rendert Delta, Coverage und Tokenende; `_consumptionParts` ergänzt den eigenen
AW als unabhängigen zweiten Wert. Damit wird der frühere Fehler, bei dem AW
oder Tokenende sich gegenseitig ausblendeten, auf Parent- und Child-Ebene
regressionsfest abgedeckt. Es wurde kein neuer Produktionsfehler gefunden.

## Runde 52: Codex-Usage-Prozess- und Reaktivierungs-Cleanup

Die Cleanup-Pfade werden jetzt direkt geprüft. Der Test bestätigt, dass
`_cancelProcess()` Generation, Primärrequest, Timer und Kindprozess bereinigt;
`_cancelAuxProcess()` löscht zusätzlich Device-Login-Livestatus, Account-
Aktivität, Auxiliary-Timer und Prozessreferenz; und
`_cancelReactivation()` entfernt Timeout, Prozess und Fehlerstatus. Der
anschließende Sammel-Cleanup bleibt idempotent.

Es wurde kein weiterer Produktionsfehler gefunden. Die Prüfung bleibt auf
Referenz-/Prozesszustände begrenzt und startet keine echte Login- oder
Reaktivierungsaktion.

## Runde 53: Codex-Usage-Fast-Mode-Status

Die Fast-Mode-Helfer werden jetzt direkt getestet: aktive Accounts werden aus
der Statusstruktur herausgefiltert, Flex-Abschlussereignisse erscheinen mit
Account und Grund, ungültige/fehlende Moduswerte werden ignoriert und der
Emergency-Override bleibt bei ungültiger Accountangabe wirkungslos.

Dabei wurde ein echter kleiner Vertragsfehler gefunden: `_fastModeIsActive()`
lieferte ohne Status `null` statt `false`. Die Funktion gibt jetzt immer einen
Booleanwert zurück; dadurch bleiben Panel-, Tooltip- und Warnlogik typstabil.

## Runde 54: Codex-Usage-Fehlerbenachrichtigung und Menü-Markup

Der persistente Fehlerbenachrichtigungszustand wird jetzt direkt über den
Fehlerpfad geprüft: Ein fehlgeschlagener Settings-Schreibvorgang bleibt als
Pending-Wert erhalten und wird beim Retry genau einmal erneut geschrieben.
Zusätzlich wird die begrenzte Ausgabe von `_addDisabled()` sowie die sichere
Weitergabe von Markup an Menüeinträge getestet.

Es wurde kein weiterer Produktionsfehler gefunden. Die Tests verwenden nur
Stub-Settings und ein Stub-Menü; es werden keine echten Benachrichtigungen
oder externen Prozesse ausgelöst.

## Runde 55: Codex-Usage-Account-Steuerungsmenü

Der Runtime-Teststub bildet jetzt die für diese Funktionen benötigten
Cinnamon-Menüverträge ab: Submenus, Switch-Zustände, `connect`/`activate`,
`addAction` und `setSensitive`. Dadurch werden die Account-Steuerungen direkt
ausgelöst statt nur auf ihre Erstellung geprüft.

Geprüft werden unabhängige Schalter für Statusleisten-Sichtbarkeit, Warnungen
und Fehler, Device-Login starten/abbrechen, Manage Account sowie Start
Terminal as User. Alle Callbacks laufen durch ihren Guard und erhalten den
richtigen Account bzw. Statuswert. Es wurde kein Produktionsfehler gefunden.

## Runde 56: Codex-Usage-Limitdetails und Reaktivierungsaktionen

Die bislang nur indirekt geprüften Menüpfade für Resetdetails, dynamische
Monats-/Spark-Limits, Routing-Entscheidungen, Reaktivierung und Health wurden
mit gezielten Runtime-Tests versehen. Dabei werden 5h-, Wochen- und 30-Tage-
Reset sowie Monats-, Spark- und Routingzeile getrennt geprüft; die
Reaktivierungsaktion wird sowohl im bereits laufenden Zustand als auch mit
Fehlermeldung und anschließendem Activate getestet. Der Health-Befehl und die
gemeinsamen Aktualisieren-/Analytics-/Einstellungen-Aktionen werden über ihre
Guard-Callbacks mit den erwarteten CLI-Argumenten ausgeführt.

Die erste Reproduktion scheiterte ausschließlich an unvollständigen Teststubs:
Das Fixture legte Resetfenster an der falschen Payload-Stelle ab, und der
Cinnamon-Stub fehlte `St.IconType.SYMBOLIC`. Beides wurde im Testharness
korrigiert; am Produktionscode war für diesen Block keine Änderung nötig.
Der fokussierte Testlauf endet mit 3/3 bestanden.

## Runde 57: Codex-Usage-Einstellungsfenster und Maximierungs-Cleanup

Der Pfad `_openSettings()` wird jetzt mit der exakten `xlet-settings`-
Argumentliste sowie dem anschließenden Maximierungsauftrag geprüft. Der
Timerpfad für `wmctrl` wird zwölfmal ausgeführt, danach beendet er sich und
setzt seine Source-Referenz zurück. Wird das Applet während der Wiederholungen
entfernt, wird kein weiterer Subprozess gestartet und der Timer ebenfalls
beendet.

Für diesen Block wurden nur die Cinnamon-Testverträge für
`Gio.Subprocess.new`, `Gio.SubprocessFlags`, `St.IconType` und den
`Mainloop`-Callback ergänzt. Es wurde kein Produktionsfehler gefunden; der
fokussierte Testlauf endet mit 2/2 bestanden.

## Runde 58: Codex-Usage-Menüfehler und systemd-Sonderzweige

Die Health-Aktion wird jetzt zusätzlich gegen beide Fehlergrenzen geprüft:
fehlender Basisbefehl und ein Backendfehler werden an
`_showCommandError()` weitergereicht; dabei wird kein Ergebnis- oder
Zusatzmenüeintrag zurückbehalten. Für die gemeinsamen Aktionen wird außerdem
kontrolliert, dass ein laufender Refresh den Aktualisieren-Eintrag deaktiviert
und dass die systemd-Reparatur nur bei geprüftem, inaktivem Dienst erscheint
und den richtigen Guard-Callback auslöst.

Auch in diesem Block wurde kein Produktionsfehler gefunden. Der fokussierte
Testlauf endet mit 2/2 bestanden.

## Runde 59: Codex-Usage-Serienpräfixe

Die dynamische Serienauswahl filterte Masterjet-Präfixe mit Bindestrich oder
Unterstrich durch `str.isalnum()` aus. Gültige Präfixe wie `q-inplace` oder
`a_b` erschienen dadurch nicht im Bearbeitungsdialog, obwohl der JavaScript-
Vertrag `[A-Z][A-Z0-9_-]{0,15}` sie erlaubt.

Der Filter nutzt jetzt denselben ASCII-Vertrag: ein führender Buchstabe,
danach höchstens 15 Buchstaben, Ziffern, `-` oder `_`. Führende Ziffern und
Unicode-Präfixe bleiben ausgeschlossen. Der Regressionstest deckt gültige
Bindestrich-/Unterstrich-Präfixe sowie beide ungültigen Klassen ab.

Der fokussierte Lauf `tests/test_dynamic_series_list.py tests/test_applet.py`
endet mit 31/31 bestanden. Ruff meldet daneben bestehende Zeilenlängen und
Testformatierung; diese sind kein Laufzeitbefund und bleiben für separate
Qualitätsrunde vorgemerkt.

## Runde 60: Fast-Mode-Icon-Reload

Beim Zurückladen des Settings setzte `FastModeIconSelector.on_setting_changed()`
den Combo-Wert. GTK kann dabei `changed` auslösen; `_on_changed()` schrieb den
bereits geladenen Wert sofort wieder in die Settings. Das erzeugte unnötige
Schreibvorgänge und konnte bei echten Settings-Signalen reentrant werden.

Der Reload-Pfad markiert die UI-Synchronisierung jetzt temporär als `_saving`.
Nur echte Benutzeränderungen schreiben weiterhin zurück. Ein Regressionstest
emuliert das GTK-Signal und weist den Reload-Schreibvorgang nach.

Der fokussierte Lauf `tests/test_fast_mode_icon_selector.py`
endet mit 4/4 bestanden; `tests/test_applet.py` endet mit 25/25 bestanden.

## Runde 61: Panel-Click-Testharness

Der vollständige JavaScript-Lauf meldete zunächst einen einzigen Fehler:
`panel click opens the menu and refreshes only when it was closed`. Der
Produktionspfad war korrekt; `_init()` setzt `refreshOnOpen` standardmäßig auf
`true`, während `makeApplet()` im Testharness diese Initialisierung nicht
abbildete. Dadurch übersprang der Stub-Click den erwarteten Refresh.

Das Testharness setzt das Produktionsdefault jetzt explizit. Kein
Produktionscode geändert. Der Lauf `node --test tests/applet_runtime.test.js`
endet danach mit **391/391 bestanden**.

## Runde 62: Serienlisten-Qualitätsprüfung

Die Produktionsdatei `dynamic_series_list.py` hatte zwei Ruff-Fehler wegen
überlanger Zeilen. Nur Bedingungen und `os.read()`-Aufruf wurden auf mehrere
Zeilen verteilt; Verhalten bleibt unverändert.

`ruff check files/codex-usage@H234598/dynamic_series_list.py` ist sauber.
Der fokussierte Serienlistentest endet mit 6/6 bestanden.

## Runde 63: Serienlisten-Testqualität

`tests/test_dynamic_series_list.py` hatte drei Ruff-Befunde: unnötige
Leerzeile, überlange Testsignatur und veränderliches Klassenattribut im
Testfixture. Die Korrektur ändert nur Teststruktur und Formatierung.

`ruff check tests/test_dynamic_series_list.py` ist sauber; der Testlauf endet
mit 6/6 bestanden.

## Runde 64: Gtk-Importreihenfolge im Fast-Mode-Selector

Beim automatischen Import-Sortieren wurde `gi.repository.Gtk` vor
`JsonSettingsWidgets` geladen. In dieser Umgebung initialisiert das Gtk4,
während Cinnamon anschließend Gtk3 verlangt; Testcollection brach mit
`ValueError: Namespace Gtk is already loaded with version 4.0` ab.

Der Selector fordert Gtk3 und GdkPixbuf2 explizit vor den Repository-Imports
an. Die absichtlich späte Importposition ist mit `E402` markiert. Ruff und
der fokussierte Selector-Test sind wieder sauber: 4/4 bestanden.

## Runde 65: OAuth-Browser-Marker

`oauth_browser._browser_configuration()` akzeptierte bisher ein Profil,
wenn einer von zwei Markern regulär und privat war, während der andere Marker
ein Symlink blieb. Damit konnten Marker-Symlinks in einem ansonsten gültigen
Profil unbemerkt bleiben. Beide Erzeugerpfade verbieten solche Symlinks bereits;
der Starter folgt jetzt demselben Vertrag und weist jeden symlinkten Marker
zurück.

Ein parametrischer Regressionstest prüft beide Markerpfade. Der vollständige
`tests/test_reactivate.py`-Lauf endet mit 42/42 bestanden; Ruff für
`src/codex_usage/oauth_browser.py` ist sauber.

## Runde 66: Browser-Kompatibilitätsausdruck

`reactivate._manage_browser_profile()` hatte einen RUF021-Befund im Ausdruck
für die Vivaldi-/Chromium-Kompatibilität. Die `and`-Teilbedingung ist jetzt
explizit parenthesisiert; Browserauswahl bleibt unverändert.

Ruff für `src/codex_usage/reactivate.py` ist sauber; der Reaktivierungstest
endet mit 42/42 bestanden.

## Runde 67: Groß-/Kleinschreibung aktiver Serien

`add_or_update_account()` validierte neue Serienpräfixe, übernahm sie aber in
der vom CLI gelieferten Schreibweise. Die Config-Ladefunktion normalisiert
hingegen auf Großbuchstaben. Dadurch konnten aktive Serien `A` und `a`
gleichzeitig gespeichert werden; der Konflikt erschien erst beim nächsten
Reload.

Neue Serien werden vor Account-/Konfliktvalidierung uppercase-normalisiert.
Der Regressionstest reproduziert den Fall und erwartet sofortigen Konflikt.
`tests/test_config.py`: 74/74 bestanden; Ruff für `src/codex_usage/config.py`
ist sauber.

## Runde 68: Test-Home-Rollback nach Auth-Verschiebung

Bei `account add --test-home` wurde `auth.json` vor dem State-Cleanup in das
neue Test-Profil verschoben. Scheiterte das Cleanup danach, wurde die Config
zwar zurückgeschrieben, die Quelle blieb aber leer; außerdem konnte das neue
Profil wegen des erzeugten `codex-home` nicht entfernt werden.

Der Ablauf initialisiert den Test-Home jetzt vor der Auth-Verschiebung und
merkt die dabei erzeugten Dateien und Verzeichnisse. Beide Rollback-Pfade
stellen die Auth-Datei zurück und entfernen nur unveränderte Artefakte mit
passender Datei-/Verzeichnisidentität. Der Regressionstest prüft Auth,
Profil und leere Config nach fehlgeschlagenem State-Cleanup.
`tests/test_config.py`: 75/75 bestanden; Ruff für `config.py` und den Test ist
sauber. Vollständige Python-Suite: 1983 bestanden, 1 übersprungen, 1 Warnung.

## Runde 69: Symlink-Prüfung nach fehlendem Pfadsegment

`assert_no_symlink_ancestors()` brach bisher beim ersten nicht vorhandenen
Pfadsegment ab. Ein Pfad wie `missing/../redirected/value` konnte dadurch
zuerst `missing/` anlegen und erst danach einen späteren Symlink als ungültig
erkennen. Die Ablehnung hinterließ dann einen Teilpfad.

Die Prüfung scannt jetzt alle Pfadsegmente weiter; `..` wird weiterhin
schrittweise verarbeitet, damit Symlink-Semantik nicht durch rein lexikalische
Normalisierung verloren geht. Zwei Regressionstests prüfen direkte Erkennung,
Fehlertext und fehlende Nebenartefakte. `tests/test_private_io.py`: 26/26;
vollständige Python-Suite: 1984 bestanden, 1 übersprungen, 1 Warnung; Ruff
sauber.

## Runde 70: Hardlink-Schutz bei State-Löschung

`remove_account_state()` verschob bisher vorhandene State-Dateien direkt in
sein Transaktionsverzeichnis. Bei einer nachträglich angelegten Hardlink-Kopie
wurde dadurch nur ein Verzeichnis-Eintrag entfernt; die Kopie blieb erhalten,
obwohl die State-Löschung erfolgreich erschien.

Vor dem Verschieben werden State-Ziele jetzt als reguläre Einzel-Link-Dateien
geprüft; Symlinks und andere Dateitypen werden ebenfalls früh abgelehnt. Der
Regressionstest stellt sicher, dass aktuelle Datei und Hardlink unverändert
bleiben. `tests/test_state.py`: 211/211 bestanden; Ruff für `state.py` und
Test ist sauber.

## Runde 71: Credits aus invalidierten Snapshots entfernen

`usage_from_dict()` sanitizierte bei `cache_invalidated` und
`login_required` bisher Kernlimits und Pools, ließ aber ein separat
gespeichertes `credits`-Fenster im Objekt. Damit konnte ein manipuliertes oder
veraltetes Payload trotz Fail-Closed-Pfad noch einen Creditwert tragen.

Credits werden jetzt in allen betreffenden Sanitization-Pfaden verworfen:
ungültiger Wertezeitpunkt, invalidierter Cache, Login erforderlich und fehlende
Kernnutzung. Drei parametrische Regressionen decken Login-, explizit
invalidierten und implizit ungültigen `ok`-Payload ab. `tests/test_state.py`:
214/214 bestanden; Ruff sauber.

## Runde 72: Direkter Credit-Parser-Test

`direct._credit_window()` hatte bislang nur indirekte Abdeckung über den
App-Server-Pfad. Direkte Tests prüfen verschachtelte absolute Guthaben sowie
Bool-, negative, nichtnumerische und widersprüchliche Werte. Produktionscode
unverändert; Fokus-Suite: 5/5 bestanden, Ruff sauber.

## Runde 73: App-Server-CODEX_HOME-Symlinkprüfung

`app_server._assert_no_symlink_ancestors()` brach ebenfalls am ersten
fehlenden Pfadsegment ab. Ein `missing/../symlink/...`-Pfad wurde dadurch nicht
vollständig geprüft. Die Schleife scannt jetzt alle Segmente weiter, analog
zur zentralen Privatpfadprüfung. Regressionstest deckt den bisher fehlenden
Pfadaufbau ab; `tests/test_app_server.py`: 77/77 bestanden.

Ruff für `src/codex_usage/app_server.py` ist sauber. Eine bereits vorhandene
E501-Zeile in `tests/test_app_server.py:115` bleibt unberührt.

## Runde 74: OAuth-Profil-Symlinkprüfung

`reactivate._assert_no_symlink_ancestors()` hatte denselben Abbruch am ersten
fehlenden Pfadsegment. Ein fehlendes Segment vor `..` konnte einen späteren
Symlink ungescannt lassen. Der Scanner prüft jetzt alle Segmente; der neue
Regressionstest deckt diesen Pfadaufbau ab. `tests/test_reactivate.py`:
43/43 bestanden; Ruff für `reactivate.py` sauber.

## Runde 75: Systemd-Unit-Symlinkprüfung

`service._assert_no_symlink_ancestors()` verwendete ebenfalls den vorzeitigen
Abbruch bei fehlenden Pfadsegmenten. Ein `missing/../symlink/...`-Pfad konnte
damit unvollständig geprüft werden. Der Scanner läuft jetzt über alle
Segmente; ein direkter Regressionstest deckt den Fall ab.
`tests/test_service.py`: 41/41 bestanden; Ruff für `service.py` sauber.

## Runde 76: Profiljob-Serienvalidierung

`profile_jobs._validate_create_arguments()` prüfte `series` nur innerhalb
eines truthy-Zweigs. Dadurch wurden `None`, `0`, `False` oder Listen als leere
Serien akzeptiert und bis ins Manifest weitergereicht. Die Validierung verlangt
jetzt zuerst einen String; leerer String bleibt erlaubt. Vier Regressionfälle
ergänzt; `tests/test_profile_jobs.py`: 43/43 bestanden.

## Runde 77: Account-ID-Typprüfung

`config._validate_account_id()` prüfte Listen vor dem Format-Ausdruck mit
Set-Mitgliedschaft. Eine unhashbare ID führte dadurch zu `TypeError` statt zu
kontrolliertem Eingabefehler. Explizite Stringprüfung ergänzt; vier Validator-
Regressionfälle, `tests/test_config.py`: 79/79 bestanden.

## Runde 78: Boolean-Prüfung für Auth-Löschung

`config.add_or_update_account()` validierte `clear_auth_json` nicht. Falsy
Nicht-Boolean-Werte konnten dadurch stillschweigend als `False` durchlaufen,
truthy Werte als Löschschalter wirken. Explizite Booleanprüfung ergänzt;
fünf Regressionfälle und die Konfigurationssuite: 84/84 bestanden.

## Runde 79: Installer-Pfadprüfung

`integration_installer._no_symlink_ancestors()` brach am ersten fehlenden
Segment ab und behandelte `..` nicht als Pfadauflösung. Ein
`missing/../symlink/...`-Pfad konnte damit einen Symlink unbemerkt lassen.
Der Scanner überspringt fehlende Segmente weiter und normalisiert
Aufwärtssegmente; Regressionstest ergänzt. `tests/test_integration_installer.py`:
103/103 bestanden; Ruff sauber.

## Runde 80: Cinnamon-Applet-Pfadprüfung

`install_cinnamon_applet.py` und `uninstall_cinnamon_applet.py` brachen ihre
Verzeichniskettenprüfung am ersten fehlenden Segment ab und behandelten `..`
nicht als Aufwärtssegment. Ein `missing/../symlink/...`-Pfad konnte so die
Symlinkprüfung umgehen. Beide Scanner normalisieren jetzt `..` und prüfen
weiter; `tests/test_applet.py`: 27/27 bestanden.

## Runde 81: Account-Lock-ID-Typprüfung

`account_lock.account_lock()` führte Set- und Regex-Prüfung direkt auf dem
übergebenen Wert aus. `None` oder unhashbare Werte erzeugten dadurch
`TypeError` statt `AccountLockError`. Explizite Stringprüfung ergänzt;
`tests/test_account_lock.py`: 14/14 bestanden; Ruff sauber.

## Runde 82: Snapshot-ID-Typprüfung

`state._validate_snapshot_account_id()` prüfte `account_id` direkt per Set und
Regex. Ungültige Typen leakten deshalb `TypeError` aus `load_current_usage()`
statt den Snapshot sauber als nicht vorhanden zu behandeln. Stringprüfung
ergänzt; `tests/test_state.py`: 217/217 bestanden; Ruff sauber.

## Runde 83: Health-Account-Typprüfung

`health.record_health_event()` rief Regex-Prüfung auf truthy Nicht-String-
`account`-Werten auf. Integerwerte leakten dadurch `TypeError`; ungültige
Accountwerte werden jetzt wie andere nicht vertrauenswürdige Labels verworfen.
Regression ergänzt; `tests/test_health.py`: 11/11 bestanden; Ruff sauber.

## Runde 84: Consumption-Unit-Typprüfung

`consumption.consumption_lookback_seconds()` prüfte `unit` per
Dictionary-Mitgliedschaft ohne Stringprüfung. Unhashbare Werte wie Listen
erzeugten dadurch `TypeError`; sie werden jetzt als ungültige Einheit mit
`ValueError` abgewiesen. `tests/test_consumption.py`: 19/19 bestanden.

## Runde 85: Profiljob-Event-Typprüfung

`profile_jobs._normalize_job_event()` prüfte `kind` direkt per Set.
Unhashbare Eventtypen leakten dadurch `TypeError`; sie werden jetzt wie andere
ungültige Eventdaten mit `ValueError` abgewiesen. `tests/test_profile_jobs.py`:
46/46 bestanden; Ruff ohne bestehende E501-Zeilen sauber.

## Runde 86: Integration-Cost-Window-Typprüfung

`integration_snapshot._canonical_cost_window()` prüfte `coverage` direkt per
Set. Unhashbare Werte leakten beim direkten Contract-Check `TypeError`; die
Prüfung verlangt jetzt zuerst Stringtyp. `tests/test_integration_snapshot.py`:
29/29 bestanden; Ruff sauber.

## Runde 87: Backend-Provenance-Typprüfung

`state.backend_provenance_matches_configured()` prüfte den konfigurierten
Backendwert direkt gegen ein `frozenset`. Unhashbare Fremdtypen leakten
`TypeError`; sie werden jetzt als nicht passende Provenance verworfen.
`tests/test_state.py`: 217/217 bestanden; Ruff sauber.

## Runde 88: OAuth-URL-Typprüfung

`oauth_browser._validate_login_url()` rief `len()` und `urlsplit()` ohne
Stringprüfung auf. Nicht-String-CLI-Werte leakten `TypeError` aus `main()`;
jetzt erfolgt kontrollierte Ablehnung mit Fehlercode 1. Regression ergänzt;
`tests/test_reactivate.py`: 46/46 bestanden; Ruff sauber.

## Runde 89: Account-Update-Argumenttypen

`config.add_or_update_account()` ließ falsy Fremdtypen bei `label` und
`profile_dir` als „nicht gesetzt“ durch, ignorierte falsches `path` und ließ
`auth_json_path=[]` als `TypeError` leaken. Optionale API-Argumente werden jetzt
vor jeder Seiteneffekt-Phase typgeprüft; leere Strings bleiben kompatibel.
`tests/test_config.py`: 88/88 bestanden; Ruff sauber.

## Runde 90: Terminal-Resolver-Fallback

`terminal._resolve_executable()` und `_resolve_terminal()` behandelten
explizite falsy Werte wie nicht gesetzte Optionen und starteten dadurch das
Standardprogramm. Explizit übergebene Werte werden jetzt immer validiert;
nur `None` aktiviert Fallback. `tests/test_terminal.py`: 11/11 bestanden;
Ruff sauber.

## Runde 91: Terminal-Auth-Dateischutz

`terminal._validate_auth_json()` akzeptierte bisher gruppenlesbare oder
hardlinkierte `auth.json`. Terminalstart konnte damit ungeschützte Tokens
verwenden. Prüfung verlangt jetzt Eigentümer, 0600-Rechte und Linkzähler 1;
`tests/test_terminal.py`: 13/13 bestanden; Ruff sauber.

## Runde 92: Device-Login-Auth-Validierung

`profile_login._validate_staged_auth()` akzeptierte jedes JSON-Objekt. Ein
staged `{}` konnte dadurch als erfolgreiches Profil publiziert werden, obwohl
Direct-Usage zwingend `tokens.access_token` benötigt. Die bestehende
Direct-Auth-Validierung wird jetzt wiederverwendet; fehlende oder ungültige
Tokens brechen den Login vor dem Kopieren ab. Regression ergänzt;
`tests/test_profile_login.py`: 30/30 bestanden; Ruff sauber.

## Runde 93: Device-Login-Live-Parser

Der Python-Live-Parser wertete das Ende jedes Output-Chunks als Tokenende.
Geteilte Device-Codes oder URLs konnten deshalb als unvollständiges Event
erscheinen, bevor der nächste Chunk eintraf. `final=False` hält jetzt
unbegrenzte Tokens am Chunkende zurück; die abschließende Gesamtausgabe wird
weiter vollständig ausgewertet. Regression ergänzt;
`tests/test_profile_login.py`: 31/31 bestanden; Ruff sauber.

## Runde 94: Device-Login-Befehlstyp

`device_auth_supported()` und `run_device_login()` übergaben ungeprüfte
`codex_bin`-Werte an den Prozessstarter. Listen, `None`, Booleans oder ein
leerer String konnten dadurch falsche Runner-Aufrufe oder rohe `TypeError`
auslösen. Gemeinsame Prüfung verlangt jetzt nichtleeren String ohne
Steuerzeichen vor jedem Seiteneffekt. Fünf Regressionfälle ergänzt;
`tests/test_profile_login.py`: 36/36 bestanden; Ruff sauber.

## Runde 95: Device-Login-Auth-TOCTOU

Die staged Auth wurde zuerst inklusive Backend-ID geprüft und danach separat
erneut gelesen. Ein Dateiwechsel dazwischen konnte damit fremde Auth als
geprüften Account publizieren. Kopierpfad validiert jetzt denselben gelesenen
Text nochmals inklusive erwarteter Backend-ID; Race-Regression ergänzt.
`tests/test_profile_login.py`: 37/37 bestanden; Ruff sauber.

## Runde 96: Device-Login-Streamgrenzen

Live-Ausgabe bekam bisher keinen Streamnamen; getrennte stdout-/stderr-Chunks
konnten dadurch zu einem falschen Device-Code verbunden werden. Neuer
streambewusster Sink hält je Stream eigenen Parserpuffer; bestehende
Ein-Argument-Sinks bleiben kompatibel. Mypy-Fehler im bounded reader ebenfalls
bereinigt. Regressionen ergänzt; `tests/test_profile_login.py`: 39/39
bestanden; Ruff und Mypy für `profile_login.py` sauber.

## Runde 97: Profiljob-Status-Typ

`profile_jobs._validate_manifest()` prüfte `status` direkt gegen ein
`frozenset`. Ein manipuliertes Manifest mit Liste oder Dictionary als Status
leakte `TypeError` aus Status-/List-Aufrufen. Explizite Stringprüfung ergänzt;
Regression hinzugefügt. `tests/test_profile_jobs.py`: 47/47 bestanden; Ruff
sauber.

## Runde 98: Profiljob-Profilpfad-Typ

`profile_jobs._validate_create_arguments()` übergab ungeprüfte
`profile_dir`-Werte an `Path()`. `None`, Listen oder Dictionaries leakten
`TypeError` statt kontrollierter Eingabeablehnung. `Path`-Konvertierung fängt
ungültige Typen jetzt als `ValueError` ab; drei Regressionen ergänzt.
`tests/test_profile_jobs.py`: 50/50 bestanden; Ruff sauber.

## Runde 99: Profiljob-Konfigurationspfad-Typ

`create_profile_job()` behandelte `""`, `[]`, `0` oder `False` bei
`config_path` wie fehlende Angabe und ließ Dictionaries über
`.expanduser()` mit `AttributeError` scheitern. Nur `None` oder echter
`Path`-Wert ist jetzt zulässig; sechs Regressionen verhindern Job-/Worker-
Seiteneffekte vor der Ablehnung. `tests/test_profile_jobs.py`: 56/56
bestanden; Ruff sauber.

## Runde 100: Profiljob-Auth-Eigentümer

`_verify_profile_job_completion()` prüfte kanonische `auth.json` bisher nur
auf Dateiart, Linkzähler und Rechte. Eine fremde Eigentümerdatei konnte damit
als erfolgreiche Job-Postcondition gelten, obwohl Direct-Usage sie ablehnt.
UID-Abgleich ergänzt; Regression mit simuliertem UID-Mismatch. `tests/test_profile_jobs.py`:
57/57 bestanden; Ruff sauber.

## Runde 101: Profiljob-Auth-Identität

Die Job-Completion prüfte trotz gespeichertem `expected_backend_account_id`
nicht die Identität der kanonischen Auth. Nach einem Datei-Race konnte damit
formal private, aber fremde Auth als Erfolg gelten. Postcondition liest und
vergleicht Backend-ID jetzt; Regression ergänzt. `tests/test_profile_jobs.py`:
58/58 bestanden; Ruff sauber.

## Runde 102: Profiljob-Workergruppen-Reap

Bei Trackingfehlern killte `_reap_untracked_worker()` nach Timeout nur den
Worker-Prozess. Da der Worker SIGTERM behandeln kann, blieben gestartete
Codex-Nachkommen bestehen. Reap sendet jetzt SIGKILL an die eigene
Prozessgruppe und fällt nur bei fehlender Gruppe auf Parent-Kill zurück.
Regression ergänzt; `tests/test_profile_jobs.py`: 59/59 bestanden; Ruff sauber.

## Runde 103: Config-Pfadtypen

`load_config()`, `save_config()`, `remove_account()` und `restore_account()`
behandelten falsy Fremdwerte als fehlenden Pfad und fielen auf den Default
zurück; truthy Fremdwerte leakten `AttributeError`. Gemeinsame
`_select_config_path()`-Prüfung verlangt jetzt `None` oder `Path` vor jedem
Fallback. Vier Regressionen ergänzt; `tests/test_config.py`: 92/92 bestanden;
Ruff sauber.

## Runde 104: Config-Restore-Account-Typ

`restore_account()` griff bei nicht leerer Config vor jeder Account-Prüfung auf
`account.id` zu. `None`, Listen oder Dictionaries leakten `AttributeError` und
konnten bereits Pfad-/Lock-Arbeit auslösen. Frühe `Account`-Prüfung ergänzt;
drei Regressionen. `tests/test_config.py`: 95/95 bestanden; Ruff sauber.

## Runde 105: Config-Restore-Index-Typ

`restore_account()` übergab ungeprüfte `index`-Werte an `min()`/`max()`.
Listen/Dictionaries leakten `TypeError`; `False` und Gleitkommazahlen wurden
als Position akzeptiert. Explizite strikte Integerprüfung ergänzt; vier
Regressionen. `tests/test_config.py`: 99/99 bestanden; Ruff sauber.

## Runde 106: Test-Home-Umgebung

`_prepare_test_codex_home()` startete `codex --help` mit kompletter
`os.environ`. Dadurch konnten `OPENAI_API_KEY` und andere Secrets in den
Subprozess gelangen. Help-Probe erhält jetzt nur minimale Locale-/Pfad-/XDG-
Variablen plus `CODEX_HOME`; Regression ergänzt. `tests/test_config.py`:
100/100 bestanden; Ruff sauber.

## Runde 107: Test-Home-Auth-Hardlinks

`_integrate_test_home_auth()` verschob reguläre Quellen ohne Link-/Rechte-
Prüfung. Hardlink-Quelle erzeugte dadurch Ziel mit `st_nlink > 1`, das Direct-
Usage später verwirft; Alias blieb bestehen. Quelle verlangt jetzt eigenen
Link, User-Eigentum und private Rechte; Regression ergänzt. `tests/test_config.py`:
101/101 bestanden; Ruff sauber.

## Runde 108: Test-Home-Config-Rechte

`_prepare_test_codex_home()` akzeptierte vorhandene `config.toml` mit bereits
gesetztem File-Store-Eintrag ohne Rechteprüfung. 0644-Datei blieb damit
bestehen. Bestehende Config verlangt jetzt Einzel-Link und private Rechte;
Regression ergänzt. `tests/test_config.py`: 102/102 bestanden; Ruff sauber.

## Runde 109: Service-Konfigurationspfadtypen

`service_install()` und `service_enable()` behandelten falsy Fremdwerte bei
`config_path` wie fehlende Angabe oder ließen truthy Fremdwerte später mit
`AttributeError` scheitern. Die öffentliche Eingabe wird jetzt vor
Lock-/Verzeichnis-Seiteneffekten auf `None` oder `Path` begrenzt; der Default
wird nur bei `None` gewählt. Zehn Regressionen prüfen alle ungültigen Typen
für beide Operationen; `tests/test_service.py`: 51/51 bestanden.

## Runde 110: Systemd-Reader-Typgrenzen

Der bounded-`systemctl`-Reader ließ `selectors.SelectorKey.fileobj` als
`int | IO[bytes]` bis zu `.fileno()` und den Stream-Puffer durch. Zur Laufzeit
war der Wert wegen der Registrierung immer ein Pipe-Objekt, aber Mypy meldete
zwei unsichere Union-Zugriffe. Explizite `IO[bytes]`-Verengung ergänzt;
`tests/test_service.py`: 51/51 bestanden, Mypy für `service.py` sauber.

## Runde 111: Service-Konfigurationsvalidierung

`service_install()` und `service_enable()` vertrauten bisher darauf, dass
direkt übergebene `AppConfig`-Objekte zuvor aus TOML validiert wurden. Direkt
konstruierte Konfiguration konnte `interval_seconds` als Bool, Float, String
oder zu kleinen Wert in `OnUnitActiveSec=` einschleusen; der String-Fall
ermöglichte Unit-Inhaltsverfälschung. Die bestehende vollständige
`_validate_config()`-Prüfung läuft jetzt vor Lock-/Verzeichnis-Seiteneffekten.
Zwölf Regressionen decken beide Operationen und alle Fehlertypen ab;
`tests/test_service.py`: 63/63 bestanden.

## Runde 112: Config-Mypy-Narrowing

`config.py` hatte 16 Mypy-Fehler in Account-Update und Restore: optionale
Auth-Pfade wurden trotz truthy-Checks als `str | None` weitergereicht, zwei
Rollback-Listen teilten denselben Namen mit inkompatiblen Elementtypen, und
der Restore-Index blieb beim Listen-Zugriff optional. Explizite
`None`-Prüfungen, getrennte Rollback-Variable und sichere Index-Verengung
behoben die Typfehler ohne Laufzeitpfadänderung. `tests/test_config.py`:
102/102 bestanden; Mypy und Ruff sauber.

## Runde 113: Profiljob-Mypy-Narrowing

`profile_jobs.py` reichte validierte Manifestwerte aus `dict[str, object]`
ohne Typverengung an Statuswechsel, `Account`, `Path` und Device-Login weiter.
Explizite `cast()`-Verengungen nutzen jetzt den bereits durch
`_validate_manifest()` garantierten Vertrag; Laufzeitvalidierung bleibt
unverändert. `tests/test_profile_jobs.py`: 59/59 bestanden; Mypy und Ruff
sauber.

## Runde 114: Usage-Reset-Typen

`usage_resets.parse_usage_resets()` inferierte `legacy_keys` zunächst als
zweielementiges Tuple und überschattete `candidates` mit einer inkompatiblen
Liste im Legacy-Zweig. Typannotation und eindeutiger Variablenname beheben die
zwei Mypy-Fehler ohne Parseränderung. `tests/test_usage_resets.py`: 5/5
bestanden; Mypy und Ruff sauber.

## Runde 115: Strikter JSON-Byte-Scan

`json_utils._reject_deep_nesting()` wurde beim Mypy-Lauf zunächst als
`str`-Scanner inferiert und danach mit `memoryview` sowie Integer-Markern
überschrieben. Explizite `str | memoryview[int]`- und Marker-Unionen bilden
beide unterstützten Eingabeformen korrekt ab. `tests/test_json_utils.py`: 3/3
bestanden; Mypy und Ruff sauber.

## Runde 116: Migrations-Record-Typ

`profile_migration._record()` wurde aus seinen String-/`None`-Werten als
`dict[str, str | None]` inferiert, obwohl die öffentliche Rückgabe
`dict[str, object]` ist. Explizite Ergebnisannotation behebt den
Invarianzfehler ohne Laufzeitänderung. `tests/test_profile_migration.py`:
24/24 bestanden; Mypy und Ruff sauber.

## Runde 117: Usage-Limit-Typen

`usage_limits.py` hatte vier Mypy-Fehler durch implizite Wechsel zwischen
`UsagePool` und `None`, Tuple und veränderlicher Liste sowie `bool` und
`bool | None`. Explizite lokale Typen halten diese Zustände korrekt fest;
Verarbeitungslogik bleibt unverändert. `tests/test_usage_limits.py`: 112/112
bestanden; Mypy und Ruff sauber.

## Runde 118: History-Pool-Typ

`history._iter_usage_samples()` inferierte `pools` zunächst als
`tuple[UsagePool]`; der leere Zweig wurde dadurch als inkompatibel gemeldet.
Explizite Tuple-Annotation erlaubt Main-Pool oder leere Poolmenge korrekt.
`tests/test_history.py`: 47/47 bestanden; Mypy und Ruff sauber.

## Runde 119: Routing-Credit-Limit-Typ

`routing._validate_policy()` verwendete `normalized` zuerst als
Identifier-String und danach als Credit-Limit-Dictionary. Die zweite
Zwischengröße heißt jetzt `normalized_limits`; Validierung und Policy-Inhalt
bleiben unverändert. `tests/test_routing.py`: 88/88 bestanden; Mypy und Ruff
für `routing.py` sauber. Eine bestehende unsortierte Importgruppe in
`tests/test_routing.py` blieb unberührt.

## Runde 120: App-Server-Fenster-Typen

`app_server._windows_from_response()` verengte `codex_snapshot` nur über ein
Bool-Flag und verwendete `secondary` für zwei inkompatible Formen. Direkte
Dict-Prüfung und `secondary_window`-Name beseitigen vier Mypy-Fehler ohne
Antwortauswertung zu ändern. `tests/test_app_server.py`: 77/77 bestanden;
Mypy und Ruff sauber.

## Runde 121: State-Modell- und Snapshot-Typen

`state.py` ließ malformed `models`-Einträge bis zu `pool.key` durchlaufen und
konnte `None` in Modell-/Fenster-Merges schreiben. Zusätzlich waren Fehlertext,
Snapshot-Quelle und Quellen-Tuple statisch zu eng inferiert. Ungültige Pools
werden jetzt verworfen, Merges bleiben bei nicht darstellbaren Werten
unverändert; lokale Typen sind explizit. `tests/test_state.py`: 218/218
bestanden; Mypy und Ruff sauber.

## Runde 122: Render-Zahlen-Narrowing

`render.py` prüfte numerische Werte zwar zur Laufzeit vollständig, gab
`_is_finite_number()` aber nur `bool` zurück. Mypy konnte danach optionale
Felder nicht als Zahlen erkennen und meldete 13 `float()`-Zugriffe. Der Helper
ist jetzt ein passender `TypeGuard[int | float]`; `tests/test_render.py`:
35/35 bestanden; Mypy und Ruff sauber.

## Runde 123: Integration-Installer-Fehlervertrag

`integration_installer._fail()` wirft auf jedem Pfad, war aber als rückkehrende
`None`-Funktion typisiert. Dadurch entstanden Folgefehler bei fehlenden
Returns, optionalen Identitäten und Zielpfaden. `NoReturn` korrigiert den
zentralen Vertrag; der verbleibende Selector-Stream wird explizit als
`IO[bytes]` verengt. `tests/test_integration_installer.py`: 103/103 bestanden;
Mypy und Ruff sauber.

## Runde 124: Extractor-Kandidaten-Typen

`extractor.py` konnte nach dem Optional-Filter Tuple-Elemente weiterhin als
`LimitWindow | None` behandeln; zusätzlich überschattete die Fallback-Liste
den vorherigen Set-Namen `values`. Typed Candidate-Liste und eindeutiger
Fallback-Name beseitigen 13 Mypy-Fehler ohne Extraktionsänderung.
`tests/test_extractor.py`: 175/175 bestanden; Mypy und Ruff sauber.

## Runde 125: Bridge-TLS-Optionalität

`bridge._tls_context()` validiert Zertifikat und Schlüssel bereits paarweise,
aber Mypy konnte die Korrelation beim Zugriff auf `tls_key` nicht ableiten.
Explizite defensive `None`-Guard verhindert zusätzlich unklare API-Nutzung.
`tests/test_bridge.py`: 177/177 bestanden; Mypy und Ruff sauber.

## Runde 126: Scheduler-Backend- und Signaltypen

`scheduler.py` hatte zehn Mypy-Fehler: optionale Backendwerte wurden ohne
Narrowing an Provenance-Prüfungen gereicht, typunsichere Keyword-Dictionaries
wurden an Direct-Fetch übergeben, Signal-Handler wurden als `object`
gespeichert, und `reset_at` blieb optional. Direkte Aufrufe, Guards,
Variablenverengung und `cast` beseitigen die Fehler; Clock-Subclass-Verhalten
bleibt erhalten. `tests/test_scheduler.py`: 160/160 bestanden; Mypy und Ruff
sauber.

## Runde 127: CLI-Fehlergruppen und Eingabetypen

`cli.py` hatte acht Mypy-Fehler: optionale Overview-Nutzungen wurden mehrfach
ohne lokale Verengung gelesen, Device-Login-Ergebnis wechselte zwischen
Dataclass und Dict, Endpoint-Port überschrieb seinen Integer-Parameter,
Rollback-Gruppen fingen `BaseException`, und CLI-Signalfehler blieben dadurch
statisch unsauber. Lokale Payload-Schleife, Ergebnisunion, `parsed_port` und
`BaseExceptionGroup` beheben die Verträge. 37 fokussierte CLI-Tests bestanden;
Mypy und Ruff sauber.

## Runde 128: Integration-Snapshot-Fehlervertrag

`integration_snapshot._invalid()` wirft immer, war aber als rückkehrende
`None`-Funktion typisiert. `NoReturn` verengt danach Mapping-/Listeingaben
korrekt; zwei sortierte Account-Schlüssel erhalten explizite String-Casts.
36 Mypy-Fehler verschwinden ohne Serialisierungslogik zu ändern.
`tests/test_integration_snapshot.py`: 29/29 bestanden; Mypy und Ruff sauber.

## Runde 129: Expliziter App-Server-Befehl

`app_server._resolve_codex()` behandelte explizite falsy Werte wie `""`,
`[]` oder `False` als fehlende Option und fiel dadurch still auf den
PATH-Befehl `codex` zurück. Ein Aufrufer konnte damit trotz explizit
ungültiger Konfiguration ein anderes Binary starten. Nur `None` aktiviert
Fallback; alle anderen Werte müssen nichtleere, unveränderte Strings sein.
Der Regressionstest deckt leeren, whitespace-only und nicht-string Werte ab.
`tests/test_app_server.py`: 81/81 bestanden; Mypy und Ruff für den
Produktionscode sauber. Die bestehende E501-Zeile im Testmodul blieb
unberührt.

## Runde 130: Expliziter Reaktivierungs-Befehl

`reactivate._resolve_executable()` behandelte explizite falsy Werte ebenfalls
als fehlende Option. Leerer `codex_command` oder `browser_helper` konnte so
unbemerkt auf den Fallback aus `PATH` wechseln. Der Resolver prüft jetzt
explizite Werte wie der Terminal- und App-Server-Resolver; nur `None` darf
Fallback auslösen. Vier Regressionen für leere, whitespace-only und
nicht-string Werte sind ergänzt. `tests/test_reactivate.py`: 50/50 bestanden;
Mypy und Ruff sauber (bestehende E501-Testzeile ausgenommen).

## Runde 131: Consumption-Formatierung

Der globale Ruff-Lauf meldete zwei E501-Zeilen in `consumption.py`: die
Baseline-Zeitbedingung und die `_ema_rate()`-Signatur. Beide sind nur auf
mehrere Zeilen verteilt; Berechnung und API bleiben unverändert.
`tests/test_consumption.py`: 19/19 bestanden; Mypy und Ruff für Modul und
Test sauber.

## Runde 132: Direct-Formatierung

Der globale Ruff-Lauf meldete eine E501-Zeile in der Credits-Feldschleife von
`direct.py`. Die Feldnamen sind jetzt nur auf mehrere Zeilen verteilt;
Extraktionsreihenfolge und Fallbacklogik bleiben unverändert.
`tests/test_direct.py`: 149/149 bestanden; Mypy und Ruff für das Modul sauber.

## Runde 133: Profile-Job-Formatierung

Der globale Ruff-Lauf meldete eine E501-Zeile beim Ergänzen von Legacy-Feldern
im Profile-Job-Manifest. Das Dictionary ist jetzt mehrzeilig formatiert;
Schema- und Migrationslogik bleiben unverändert. `tests/test_profile_jobs.py`:
59/59 bestanden; Mypy und Ruff für das Modul sauber.

## Runde 134: CLI-Parser-Formatierung

Der globale Ruff-Lauf meldete drei E501-Zeilen in den Consumption- und
Profile-Parsern. Choices und Argumentoptionen sind jetzt mehrzeilig formatiert;
CLI-Vertrag und Defaults bleiben unverändert. `tests/test_cli.py`: 104/104
bestanden; Mypy und Ruff für `cli.py` sauber.

## Runde 135: Cinnamon-Installer-Formatierung

Der globale Ruff-Lauf meldete zwei E501-Zeilen im Cinnamon-Installer: die
Liste erforderlicher Dateien und den Pfad zum Settings-Schema. Beide sind
mehrzeilig formatiert; Installations- und Migrationslogik bleiben unverändert.
`tests/test_applet.py`: 27/27 bestanden; Ruff für das Script sauber.

## Runde 136: Model-Dauertyp

`UsagePool.window_for_duration()` verglich den angeforderten Wert direkt mit
Fenster-Identitäten. Python behandelt dabei `True` und `1.0` wie die gültige
Ganzzahl `1`; ein benutzerdefiniertes `1s`-Fenster konnte so durch falschen
Aufruftyp ausgewählt werden. Der Helper akzeptiert jetzt nur positive
Ganzzahlen. `tests/test_models.py` deckt Bool, Float, String und ungültige
Ganzzahlen ab; Model-/Render-Tests: 41/41 bestanden; Mypy und Ruff sauber.

## Runde 137: Gesamtverifikation

Nach den Resolver-, Formatierungs- und Model-Änderungen lief die vollständige
Python-Suite mit **2113 bestanden, 1 übersprungen und 1 Warnung** in 86 s.
Der Lauf schreibt keine offenen Testfehler; die Warnung bleibt als bestehender
Test-/Umgebungsbefund separat zu beobachten.

## Runde 138: Cinnamon-JavaScript-Verifikation

Der vollständige Node-Testlauf `tests/applet_runtime.test.js` endet mit
**391/391 bestanden**, ohne Skip oder Fehler.

## Runde 139: App-Server-Testformatierung

Ruff meldete eine E501-Zeile in der Fake-App-Server-Credits-Fixture. Der
zusammengesetzte Test-String ist jetzt mehrzeilig; Testverhalten bleibt gleich.
`tests/test_app_server.py`: 81/81 bestanden; Ruff sauber.

## Runde 140: Routing-Testimport

Ruff meldete eine unsortierte Importgruppe in `test_routing.py`. Die beiden
Policy-Setter sind jetzt alphabetisch geordnet; Verhalten bleibt unverändert.
`tests/test_routing.py`: 88/88 bestanden; Ruff sauber.

## Runde 141: Applet-Testformatierung

Der vollständige Ruff-Testlauf meldete sieben E501-Zeilen in
`test_applet.py`. Settings-Assertions, erwartete Spalten und Dateinamen sind
jetzt mehrzeilig formatiert; Testsemantik bleibt unverändert.
`tests/test_applet.py`: 27/27 bestanden; Ruff für alle Tests jetzt sauber.

## Runde 142: Snapshot-Status-Typprüfung

`integration_snapshot._canonical_document()` prüfte `status` direkt per
Frozenset-Mitgliedschaft. Unhashbare Statuswerte aus einem Snapshot konnten
dadurch `TypeError` leaken; sie werden jetzt vor der Mitgliedschaft als String
geprüft und sauber als `IntegrationInvalidSource` abgewiesen.
`tests/test_integration_snapshot.py`: 31/31 bestanden; Mypy und Ruff sauber.

## Runde 143: Integration-Exitcode-Typ

`integration_entrypoint._error_result()` akzeptierte Float-/Bool-Werte, die
wegen Python-Schlüsselgleichheit als gültiger Integer-Exitcode `69` gelten
konnten. Fehlercodes werden jetzt mit strikt-integer Prüfung normalisiert.
`tests/test_integration_entrypoint.py`: 22/22 bestanden; Mypy und Ruff sauber.

## Runde 144: CLI-Provenienz-Typprüfung

`cli._has_valid_usage_provenance()` prüfte Backend-Felder direkt per
Set-Mitgliedschaft. Manipulierte `AccountUsage`-Objekte mit Liste oder Dict als
Backend-Wert konnten dadurch `TypeError` statt eines ungültigen Ergebnisses
auslösen. Beide Felder werden jetzt vor der Mitgliedschaft als Strings geprüft;
ungültige Provenienz liefert sauber `False`. `tests/test_cli.py`: 106/106
bestanden; Mypy und Ruff für Quelle und Test sauber.

## Runde 145: Fallback-Provenienz-Typprüfung

`state._has_backend_fallback_proof()` prüfte den Fallback-Grund direkt per
Frozenset-Mitgliedschaft. Untrusted `AccountUsage`-Objekte mit Liste oder Dict
als `fallback_reason` konnten dadurch `TypeError` auslösen. Der Wert wird jetzt
vor allen String-/Set-Operationen geprüft; ungültige Gründe liefern sauber
`False`. `tests/test_state.py`: 220/220 bestanden; Mypy und Ruff sauber.

## Runde 146: Scheduler-Fallback-Typprüfung

`scheduler._stabilize_authenticated_usage()` prüfte einen gespeicherten
Fallback-Grund direkt per Set-Mitgliedschaft. Ein manipuliertes vorheriges
`AccountUsage` mit Liste oder Dict als `fallback_reason` konnte beim
Stabilisieren `TypeError` auslösen. Der Grund wird jetzt als String validiert;
ungültige Werte verwerfen Stabilisierung sicher. `tests/test_scheduler.py`:
162/162 bestanden; Mypy und Ruff sauber.

## Runde 147: Scheduler-Backend-Typprüfung

`scheduler._stabilize_authenticated_usage()` prüfte zusätzlich
`previous.backend_used` vor einer Typprüfung per Frozenset-Mitgliedschaft. Ein
unhashbarer Backend-Wert konnte deshalb die Stabilisierung mit `TypeError`
abbrechen. Der Wert wird jetzt zuerst als String geprüft; ungültige Snapshots
werden ignoriert. `tests/test_scheduler.py`: 163/163 bestanden; Mypy und Ruff
sauber.

## Runde 148: State-Backend-Identitätstyp

`state.backend_identity_matches()` verwendete `backend_used`-Werte direkt als
Frozenset-Schlüssel. Unhashbare Werte aus manipulierten `AccountUsage`-Objekten
konnten dadurch `TypeError` auslösen. Beide Felder werden jetzt vor der
Identitätsprüfung als Strings validiert; ungültige Identitäten matchen nicht.
`tests/test_state.py`: 221/221 bestanden; Mypy und Ruff sauber.

## Runde 149: Leere-Limits-Backend-Typ

`state._authoritative_empty_limits()` verwendete `backend_used` in zwei
Frozenset-Prüfungen ohne vorherige Typprüfung. Unhashbare Backend-Werte konnten
bei Snapshot-Merges `TypeError` auslösen. Beide Zweige prüfen jetzt zuerst auf
String; ungültige Werte sind nicht autoritativ. `tests/test_state.py`: 223/223
bestanden; Mypy und Ruff sauber.

## Runde 150: Capture-Prioritäts-Typ

`state._backend_capture_priority()` prüfte `backend_used` direkt per
Frozenset-Mitgliedschaft. Unhashbare Werte konnten beim Vergleich gleichalter
Snapshots `TypeError` auslösen. Die Backend-Mitgliedschaft ist jetzt auf
Stringwerte begrenzt; ungültige Werte erhalten Priorität `-1`.
`tests/test_state.py`: 225/225 bestanden; Mypy und Ruff sauber.

## Runde 151: Scheduler-Snapshot-Persistenztyp

`scheduler._should_persist_snapshot()` prüfte `backend_used` bei partiellen
Snapshots direkt per Frozenset-Mitgliedschaft. Unhashbare Werte konnten den
Watchdog-Persistenzpfad mit `TypeError` abbrechen. Der Wert wird jetzt zuerst
als String geprüft; ungültige Snapshots werden nicht gespeichert.
`tests/test_scheduler.py`: 165/165 bestanden; Mypy und Ruff sauber.

## Runde 152: Policy-Entscheidungstyp

`cli._policy_decision_exit_code()` prüfte fremde `decision`-Werte direkt per
Set-Mitgliedschaft. Listen oder Dicts konnten den Policy-Statuspfad mit
`TypeError` abbrechen. Der Wert wird jetzt zuerst als String geprüft; unbekannte
oder unhashbare Entscheidungen liefern Exitcode `2`. `tests/test_cli.py`:
108/108 bestanden; Mypy und Ruff sauber.

## Runde 153: Scheduler-Status-Typ

`scheduler._should_persist_snapshot()` prüfte `status` partieller Snapshots
direkt per Set-Mitgliedschaft. Unhashbare Statuswerte konnten den Watchdog mit
`TypeError` abbrechen. Persistenzentscheid akzeptiert jetzt nur echte
`AccountStatus`-Werte; unhashbare oder fremde Statuswerte werden verworfen.
`tests/test_scheduler.py`: 167/167 bestanden; Mypy und Ruff sauber.

## Runde 154: Bridge-Status- und Backend-Typen

Die Bridge-Schutzpfade prüften bekannte authentifizierte `backend_used`- und
`status`-Werte direkt per Set-Mitgliedschaft. Unhashbare Werte konnten dadurch
`TypeError` auslösen; beide Pfade akzeptieren jetzt nur passende String-/Enum-
Werte und fallen sonst sicher auf `False` zurück. Die zugehörige State-Prüfung
weist nur Nicht-String-Nicht-None-Backends zurück und erhält legitime Browser-
Snapshots ohne Backend-Wert. `tests/test_bridge.py`: 179/179 und
`tests/test_state.py`: 225/225 bestanden; Mypy und Ruff sauber.

## Runde 155: Routing-Identitätstyp

`routing._backend_identity_is_valid()` verwendete `backend_used` direkt als
Set-Schlüssel. Unhashbare Werte konnten die Identitätsprüfung mit `TypeError`
abbrechen. Nicht-String-Nicht-None-Backends werden jetzt vorher abgewiesen;
unbekannte String-Backends behalten bisheriges Verhalten. `tests/test_routing.py`:
90/90 bestanden; Mypy und Ruff sauber.

## Runde 156: Routing-Policy-Schema-Version

`routing._validate_policy()` verglich `schema_version` nur per Gleichheit und
akzeptierte dadurch Bool- und Floatwerte als Integer `1`. Die Prüfung verlangt
jetzt exakt den eingebauten Typ `int`; fremde Typen werden abgewiesen.
`tests/test_routing.py`: 94/94 bestanden; Mypy und Ruff sauber.

## Runde 157: Spark-Health-Version

`spark_health._load_records()` verglich die persistierte Versionsnummer nur
per Gleichheit und akzeptierte dadurch Bool-/Floatwerte als Version `1`.
Gesundheitsdaten werden jetzt nur bei exakt eingebautem `int`-Versionstyp
geladen; andere Versionen bleiben unbekannt. `tests/test_spark_health.py`:
18/18 bestanden; Mypy und Ruff sauber.

## Runde 158: Profile-Job-Manifest-Version

`profile_jobs._validate_manifest()` verglich die Manifest-Version nur per
Gleichheit und akzeptierte Bool-/Floatwerte als Version `1`. Die Prüfung
verlangt jetzt exakt `int`; fremde Versionstypen werden als ungültiges Manifest
abgewiesen. `tests/test_profile_jobs.py`: 62/62 bestanden; Mypy und Ruff
sauber.

## Runde 159: Health-Version

`health._read_events()` verglich die persistierte Versionsnummer nur per
Gleichheit und akzeptierte Bool-/Floatwerte als Version `1`. Gesundheitsdaten
werden jetzt nur bei exakt eingebautem `int`-Versionstyp geladen; andere
Versionen ergeben eine leere Historie. `tests/test_health.py`: 14/14 bestanden;
Mypy und Ruff sauber.

## Runde 160: Gesamtverifikation

Nach den strikten Versions- und Payload-Prüfungen lief die vollständige
Python-Suite mit **2159 bestanden, 1 übersprungen und 1 externer
PyGObject-Warnung** in 88,38 s. Die Warnung betrifft die veraltete
`GLib.unix_signal_add_full`-API außerhalb des Repositories; keine Testfehler.

## Runde 161: Gesamt-Mypy und Ruff

Nach dem vollständigen Testlauf meldet `mypy src/codex_usage` weiterhin keine
Fehler in 35 Quelldateien. Der aggregierte Ruff-Lauf über Produktion, Scripts,
Launcher und Tests ist ebenfalls sauber.

## Runde 162: History-Prozent-Typ

`history.UsageSample` ließ die Float-Konvertierung eines extrem großen
Integerwerts ungefangen; `OverflowError` konnte die Sample-Validierung verlassen.
Die Konvertierung wird jetzt abgefangen und als normaler `ValueError` für
ungültige Prozentwerte gemeldet. `tests/test_history.py`: 48/48 bestanden; Mypy
und Ruff sauber.

## Runde 163: Credit-Float-Overflow

`direct._credit_window()` ließ bei skalarer und verschachtelter Credit-Balance
ein `OverflowError` aus `float()` entweichen, wenn Backend-Payload einen extrem
großen Integer enthielt. Beide Konvertierungspfade behandeln solche Werte jetzt
wie andere ungültige Balances. `tests/test_direct.py`: 151/151 bestanden; Mypy
und Ruff sauber.

## Runde 164: Routing-Credit-Float-Overflow

`routing._validate_credit_limits()` ließ einen extrem großen Integer in einer
Policy-Payload als `OverflowError` aus `float()` entweichen. Die Konvertierung
wandelt solche Werte jetzt in den erwarteten `ValueError` für ungültige Limits
um. `tests/test_routing.py`: 95/95 bestanden; Mypy und Ruff sauber.

## Runde 165: Snapshot-Float-Overflow

`integration_snapshot` ließ extrem große Integer bei der Projektion von
`remaining_percent`, kanonischen Prozentwerten und Kostenwerten ungefangen in
`float()` laufen. Die Exportvalidierung überspringt bzw. verwirft solche Werte
jetzt über ihren normalen Invalid-Source-Pfad. `tests/test_integration_snapshot.py`:
34/34 bestanden; Mypy und Ruff sauber.
