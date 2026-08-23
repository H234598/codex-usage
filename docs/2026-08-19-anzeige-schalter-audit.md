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

## Runde 166: RECORD-Größen-Overflow

`integration_installer._parse_record()` und
`integration_attestation._record_rows()` ließen 5000-stellige CSV-Dateigrößen
als `ValueError` aus `int()` entweichen. Beide RECORD-Parser behandeln solche
Größen jetzt als ungültige Eingabe und brechen kontrolliert ab.
`tests/test_integration_installer.py`: 105/105 bestanden; Mypy und Ruff sauber.

## Runde 167: Gesamtverifikation

Die vollständige Python-Suite lief nach den Snapshot-, Routing-, Credit- und
RECORD-Validierungen mit **2168 bestanden, 1 übersprungen und 1 externer
PyGObject-Warnung** in 86,82 s. Die Warnung betrifft weiterhin die veraltete
`GLib.unix_signal_add_full`-API außerhalb des Repositories; keine Testfehler.

## Runde 168: Scheduler-Backend-Typ

`scheduler.fetch_all()` prüfte `backend_used` direkt gegen ein Set. Ein
fehlerhaftes Fetch-Ergebnis mit Liste oder anderem unhashbarem Wert konnte so
`TypeError` auslösen. Der Pfad verlangt jetzt vor Set-Mitgliedschaft einen
String und gibt fehlerhafte Ergebnisse unverändert zurück.
`tests/test_scheduler.py`: 168/168 bestanden; Mypy und Ruff sauber.

## Runde 169: CLI-Policy-Backend-Typ

`cli._usage_for_policy()` setzte geladene `backend_used`-Werte direkt in
Set-Mitgliedschaft ein. Ein malformed Loader-Ergebnis mit unhashbarem Wert
führte zu `TypeError`; beide Policy-Prüfungen verlangen jetzt zuerst einen
String. `tests/test_cli.py`: 109/109 bestanden; Mypy und Ruff sauber.

## Runde 170: Gesamt-Lint

Nach Scheduler- und CLI-Provenienzschutz meldet `mypy src/codex_usage` keine
Fehler in 35 Quelldateien. Der aggregierte Ruff-Lauf über Produktion, Scripts,
Launcher und Tests ist ebenfalls sauber.

## Runde 171: Snapshot-Pool-Key

`integration_snapshot._source_limits()` setzte `pool.key` vor dessen
Typvalidierung in ein Set. Ein unhashbarer Key konnte die Projektion mit
`TypeError` abbrechen. Die Funktion verwirft solche Pool-Objekte jetzt über
den normalen Invalid-Source-Pfad. `tests/test_integration_snapshot.py`:
35/35 bestanden; Mypy und Ruff sauber.

## Runde 172: Restore-Backend-Typ

`state._allow_missing_window_restore()` prüfte bei partiellen Werten
`backend_used` ohne String-Guard gegen ein Set. Unhashbare Werte führten zu
`TypeError`; unbekannte/malformed Backends folgen jetzt kontrolliert der
bisherigen Restore-Semantik. `tests/test_state.py`: 227/227 bestanden; Mypy
und Ruff sauber.

## Runde 173: Verbrauchsprognose-Overflow

`consumption.calculate_consumption()` rief `math.ceil()` auch für eine
unendliche Forecast-Division auf. Ein kleinster positiver Float als Rate führte
dadurch zu `OverflowError`; nicht darstellbare Prognosen bleiben jetzt leer.
`tests/test_consumption.py`: 20/20 bestanden; Mypy und Ruff sauber.

## Runde 174: Gesamtverifikation

Die vollständige Python-Suite lief nach dem Verbrauchsprognose-Fix mit
**2174 bestanden, 1 übersprungen und 1 externer PyGObject-Warnung** in 86,36 s.
Die Warnung betrifft weiterhin die veraltete `GLib.unix_signal_add_full`-API
außerhalb des Repositories; keine Testfehler.

## Runde 175: Gesamt-Mypy und Ruff

Nach dem vollständigen Testlauf meldet `mypy src/codex_usage` weiterhin keine
Fehler in 35 Quelldateien. Der aggregierte Ruff-Lauf über Produktion, Scripts,
Launcher und Tests ist ebenfalls sauber.

## Runde 176: Serializer-Schutz für Modell-Pool-Keys

`AccountUsage.as_dict()` setzte Modell-Pool-Keys ungeprüft als Dictionary-
Schlüssel ein. Ein malformed `UsagePool` mit Liste oder Dict als `key` konnte
dadurch JSON- und Render-Ausgabe mit `TypeError` abbrechen. Der Serializer
berücksichtigt jetzt nur echte `UsagePool`-Objekte mit String-Key; ungültige
Modell-Pools werden verworfen. `tests/test_models.py` und `tests/test_render.py`:
42/42 bestanden; Mypy und Ruff für betroffene Dateien sauber.

## Runde 177: Serializer-Status-Typ

`AccountUsage.as_dict()` verwendete einen fremden Statuswert direkt in
Set-Mitgliedschaft und anschließend über `.value`. Listen, Dicts oder rohe
Strings konnten deshalb die Ausgabe mit `TypeError` oder `AttributeError`
abbrechen. Ungültige Statuswerte werden jetzt für die Serializer-Ausgabe als
terminaler `error`-Status behandelt und Werte sicher verborgen.
`tests/test_models.py` und `tests/test_render.py`: 45/45 bestanden; Mypy und
Ruff für betroffene Dateien sauber.

## Runde 178: Serializer-Container-Typen

`AccountUsage.as_dict()` iterierte `source_urls` und rief `as_dict()` auf
`usage_resets` ohne Laufzeit-Typprüfung auf. `None` oder andere malformed
Container konnten JSON-/Render-Ausgabe mit `TypeError` oder `AttributeError`
abbrechen. Ungültige optionale Container werden jetzt als leere bzw. unbekannte
Werte serialisiert. `tests/test_models.py` und `tests/test_render.py`: 46/46
bestanden; Mypy und Ruff für betroffene Dateien sauber.

## Runde 179: Render-Schutz für malformed Main-Pools

`render._extra_main_value()` vertraute bei gültiger Provenienz blind auf den
`main`-Pool. Ein malformed Pool-Typ oder ein Pool mit nicht-tuplem
`windows`-Container konnte Tabellen-Rendering mit `AttributeError` oder
`TypeError` abbrechen. Der Pfad prüft den Pool und Container jetzt vor Zugriff
und blendet ungültige Zusatzlimits aus. `tests/test_render.py` sowie die
relevanten CLI-Render-Tests: 45/45 bestanden; Mypy und Ruff für betroffene
Dateien sauber.

## Runde 180: Render-Schutz für Status-Text

`render._status_value()` übergab `error` und `blocked_reason` ohne
Typprüfung an `_shorten()`. Malformed Listen oder Dicts konnten Tabellen-
Rendering mit `TypeError` abbrechen. Nur String-Texte werden jetzt formatiert;
andere Werte bleiben ohne Zusatztext. `tests/test_render.py`: 39/39
bestanden; Mypy und Ruff für betroffene Dateien sauber.

## Runde 181: Render-Schutz für Usage-Fenster

`render._usage_value()` und `_reset_value()` griffen bei `five_hour` oder
`weekly` ohne Laufzeit-Typprüfung auf Fensterattribute zu. Malformed Listen
oder Dicts konnten Tabellen-Rendering mit `AttributeError` abbrechen. Beide
Hilfen liefern für Nicht-`LimitWindow` jetzt sicher `-`. `tests/test_render.py`:
41/41 bestanden; Mypy und Ruff für betroffene Dateien sauber.

## Runde 182: JSON-sicherer Usage-Serializer

`AccountUsage.as_dict()` und die verschachtelten Fenster-/Pool-Serializer
übernahmen malformed Datums-, Zahlen- und Textfelder ungeprüft. `render_json()`
konnte dadurch mit `AttributeError` oder `TypeError` abbrechen. Zeitstempel,
numerische Werte, Texte, Pool-Container und Status-Metadaten werden jetzt
typisiert normalisiert; ungültige Werte werden verborgen oder als `null`
ausgegeben. `tests/test_models.py`, `tests/test_render.py`, `tests/test_cli.py`
und `tests/test_state.py`: 389/389 bestanden; Mypy und Ruff für betroffene
Dateien sauber.

## Runde 183: CLI-Überblick nutzt sichere Serialisierung

`cli._overview_usage_json()` las `captured_at`, `status`, `error` und `stale`
nach dem Serializer-Schutz nochmals direkt aus dem Usage-Objekt. Ein malformed
Snapshot konnte den JSON-Überblick dadurch weiterhin mit `AttributeError`
abbrechen. Der Überblick übernimmt diese Felder jetzt ausschließlich aus der
normalisierten Serializer-Ausgabe. `tests/test_cli.py`: 110/110 bestanden;
Mypy und Ruff für betroffene Dateien sauber.

## Runde 184: History-Sampler überspringt malformed Usage-Container

`history._iter_usage_samples()` iterierte `models` und Pool-`windows` ohne
Laufzeitprüfung und griff auf malformed `main`, `credits` oder Zeitstempel
direkt zu. Beschädigte In-Memory-Usage konnte History-Aufzeichnung mit
`AttributeError` oder `TypeError` abbrechen. Nicht-iterierbare Container und
ungültige Pools/Fenster werden jetzt übersprungen; lazy Modell-Iteratoren und
Sample-Bound bleiben erhalten. `tests/test_history.py`: 50/50 bestanden; Mypy
und Ruff für betroffene Dateien sauber.

## Runde 185: Routing-Decision JSON-Schutz

`routing.evaluate_routing()` übernahm `account_id` und
`backend_account_id` ungeprüft in das Decision-Dict. Malformed In-Memory-
Identitäten konnten den CLI-JSON-Output mit `TypeError` abbrechen. Beide
Felder werden jetzt als String oder `null` ausgegeben. `tests/test_routing.py`:
96/96 bestanden; Mypy und Ruff für betroffene Dateien sauber.

## Runde 186: Legacy-Fenster-Konstruktor

`AccountUsage.__post_init__()` griff bei truthy malformed `five_hour`- oder
`weekly`-Werten direkt auf `duration_seconds` zu. Ein beschädigtes Legacy-
Fenster konnte die Usage-Konstruktion mit `AttributeError` abbrechen. Die
automatische Main-Pool-Synthese berücksichtigt jetzt nur echte
`LimitWindow`-Objekte und übernimmt gültige Geschwisterfenster weiter.
`tests/test_models.py`, `tests/test_render.py` und `tests/test_state.py`:
282/282 bestanden; Mypy und Ruff für betroffene Dateien sauber.

## Runde 187: State-Expiry für malformed Pool-Fenster

`state._expire_pool_windows()` übernahm nicht-tupleme Fenstercontainer und
Nicht-`LimitWindow`-Elemente in den nächsten State. Beschädigte Pools blieben
damit verfügbar oder konnten bei Attributzugriffen die Ablaufprüfung brechen.
Ungültige Fenster werden jetzt verworfen; der Pool wird leer und nicht
verfügbar markiert. `tests/test_state.py`: 228/228 bestanden; Mypy und Ruff
für betroffene Dateien sauber.

## Runde 188: State-Expiry für malformed Capture-Zeitstempel

Die Ablaufprüfung griff bei malformed `captured_at` oder Legacy-Fenstern vor
der Validierung auf `tzinfo`/`reset_at` zu. Beschädigte Usage konnte dadurch
`AttributeError` auslösen. Ungültige Fenster gelten jetzt als abgelaufen;
ungültige Capture-Zeitstempel verwenden fail-closed Minimalzeit. `tests/test_state.py`:
231/231 bestanden; Mypy und Ruff für betroffene Dateien sauber.

## Runde 189: Gesamtverifikation nach Hardening-Runden

Die vollständige Python-Suite lief nach den Model-, Render-, CLI-, History-,
Routing- und State-Härtungen mit **2196 bestanden, 1 übersprungen und 1
externer PyGObject-Warnung** in 87,67 s. Die Warnung betrifft weiterhin die
veraltete `GLib.unix_signal_add_full`-API außerhalb des Repositories; keine
Testfehler.

## Runde 190: Gesamt-Mypy und Ruff

`mypy src/codex_usage` meldet keine Fehler in 35 Quelldateien. Der aggregierte
Ruff-Lauf über Produktion, Scripts, Launcher und Tests ist ebenfalls sauber.

## Runde 191: Scheduler-Reset- und Stabilisierungsguards

`scheduler._watch_core_resets_current()` und `_stabilize_main_pool()` griffen
bei malformed Main-Pools/Fenstercontainern direkt auf Iteration und Attribute
zu. Health-Prüfung oder Auth-Stabilisierung konnte dadurch `TypeError` bzw.
`AttributeError` auslösen. Beide Pfade prüfen jetzt `UsagePool`, tuple-Container
und `LimitWindow`; malformed Daten führen fail-closed zu `False` bzw. werden
unverändert weitergereicht. `tests/test_scheduler.py`: 171/171 bestanden;
Mypy für Source und Ruff für betroffene Dateien sauber.

## Runde 192: Watchdog-Fenster-Normalisierung

`scheduler._watchdog_windows()` gab malformed Main-Window-Container direkt
weiter. Ein Nicht-Tuple konnte in `_block_state()` unkontrolliert iteriert
werden; Watchdog-Entscheidungen waren dadurch nicht zuverlässig fail-closed.
Malformed Main-Pools liefern jetzt leere Fenster, sodass Pool-Flags den
bekannten Unknown-Blockpfad auslösen; gültige Legacy-Fenster bleiben erhalten.
`tests/test_scheduler.py`: 173/173 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 193: History-Zeitstempel aus SQLite

`history._from_millis()` übernahm SQLite-Werte ohne Typ-/Bereichsprüfung.
Beschädigte INTEGER-Werte führten zu rohem `OverflowError`/`TypeError`; Bool
und Float wurden akzeptiert. Die Konvertierung verlangt jetzt striktes
Integer und meldet ungültige Werte kontrolliert als `ValueError`.
`tests/test_history.py`: 56/56 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 194: State-Expiry für malformed Main-Pool-Typen

`state._expire_pool_windows()` prüfte bisher nur `pool is None`, bevor
`pool.windows` gelesen wurde. Ein beschädigter `usage.main`-Wert ohne
`UsagePool`-Struktur konnte die Reset-Ablaufprüfung mit `AttributeError`
abbrechen. Nicht-`UsagePool`-Werte werden jetzt entfernt und als Änderung
markiert; der übergeordnete State fällt dadurch kontrolliert auf `PARTIAL`
und `stale`. `tests/test_state.py`: 232/232 bestanden; Mypy für Source und
Ruff für betroffene Dateien sauber.

## Runde 195: State-Merge für malformed Core-Pools und Legacy-Fenster

`merge_current_with_last_success()` vertraute bei malformed `main`-Pools und
`five_hour`/`weekly`-Feldern auf `.has_valid_usage` oder `.windows`. Beschädigte
Snapshots konnten Cache-Merge mit `AttributeError` abbrechen, besonders im
Browser-Pfad mit resetlosen Fenstern. Die gemeinsamen Validierungs- und
Merge-Helfer prüfen jetzt `UsagePool`/`LimitWindow` vor jedem Zugriff und
ignorieren ungültige Werte fail-closed. `tests/test_state.py`: 235/235
bestanden; Mypy für Source und Ruff für betroffene Dateien sauber.

## Runde 196: Scheduler-Usability für malformed Core-Werte

`_has_usable_core_usage()` griff bei malformed `main`-Pools oder Legacy-
Fenstern direkt auf `.has_valid_usage` bzw. `.has_usage_value` zu. Der
Watchdog konnte dadurch vor seiner kontrollierten Partial-/Block-Entscheidung
mit `AttributeError` abbrechen. Die Prüfung akzeptiert jetzt nur echte
`UsagePool`-/`LimitWindow`-Objekte; ungültige Werte führen fail-closed zu
`False`. `tests/test_scheduler.py`: 178/178 bestanden; Mypy für Source und
Ruff für betroffene Dateien sauber.

## Runde 197: Gesamtverifikation nach State-/Scheduler-Härtung

Die vollständige Python-Suite lief nach den State- und Scheduler-Guards mit
**2216 bestanden, 1 übersprungen und 1 externer PyGObject-Warnung** in 91,19 s.
Die Warnung betrifft weiterhin die veraltete `GLib.unix_signal_add_full`-API
außerhalb des Repositories; keine Testfehler.

## Runde 198: History-Status validiert SQLite-Zeitstempel

`HistoryStore.status()` wandelte `MIN`/`MAX(captured_at_ms)` per `int()` um und
akzeptierte dadurch Float-Truncation oder warf rohe Konvertierungsfehler bei
malformed SQLite-Werten. Status-Aggregate werden jetzt wie einzelne Samples
strict typ- und bereichsgeprüft. `tests/test_history.py` und
`tests/test_history_cli.py`: 61/61 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 199: Integration-Snapshot validiert Export-Zeitstempel

`integration_snapshot._utc_text()` griff bei malformed `generated_at` oder
Usage-`captured_at` direkt auf `tzinfo`/`utcoffset` zu. Direkte
Schema-1-Projektion konnte dadurch mit rohem `AttributeError` abbrechen.
Zeitstempel werden jetzt typ- und fehlergeprüft; ungültige Werte führen
kontrolliert zu `IntegrationInvalidSource`. `tests/test_integration_snapshot.py`:
47/47 bestanden; Mypy für Source und Ruff für betroffene Dateien sauber.

## Runde 200: Gesamt-Mypy und Ruff nach Parser-Härtung

`mypy src/codex_usage` meldet keine Fehler in 35 Quelldateien. Der aggregierte
Ruff-Lauf über Produktion, Scripts, Launcher und Tests ist ebenfalls sauber.

## Runde 201: Extractor-Eingangsgrenzen

`extractor.extract_windows()` nahm malformed `body_text`, nicht-iterierbare
JSON-Candidates, fehlerhafte Textquellen oder ungültige Capture-Zeitpunkte
ungeprüft an. Browser-/Parser-Aufrufe konnten dadurch mit `AttributeError` oder
`TypeError` abbrechen. Ungültige Eingänge werden jetzt als leere Quelle bzw.
aktueller Zeitpunkt behandelt; gültige Quellen bleiben unverändert.
`tests/test_extractor.py`: 195/195 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 202: Health-Event-Clock fail-closed

`health.record_health_event()` übernahm truthy Nicht-datetime-Werte als
`now` und rief darauf `.astimezone()` auf. Fehlertelemetrie konnte dadurch
selbst mit `AttributeError` abbrechen. Der Event-Clock akzeptiert jetzt nur
echte `datetime`-Werte und fällt sonst auf aktuelle UTC-Zeit zurück.
`tests/test_health.py`: 20/20 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 203: Consumption-Eingang validiert Sample-Iterator

`consumption.calculate_consumption()` übergab nicht-iterierbare `samples`
direkt an `itertools.islice`; API-Aufrufer erhielten rohen `TypeError` statt
kontrollierter Eingabefehler. Der Sample-Iterator wird jetzt abgefangen und
als `ValueError("samples are invalid")` gemeldet. `tests/test_consumption.py`:
24/24 bestanden; Mypy für Source und Ruff für betroffene Dateien sauber.

## Runde 204: Identity-Candidate-Iterator validiert

`identity._usable_candidates()` übergab nicht-iterierbare Candidate-Container
direkt an `islice`. Identitäts- und Plan-Typ-Helfer konnten dadurch mit rohem
`TypeError` abbrechen. Ungültige Container liefern jetzt eine leere Candidate-
Menge. `tests/test_identity.py`: 27/27 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 205: App-Server-Response-Helper validieren Payload-Typ

`app_server._windows_from_response()`, `_unsupported_window_durations()` und
`_missing_usage_limits_error()` griffen bei non-dict Payloads direkt auf `.get()`
zu. Beschädigte RPC-Antworten konnten dadurch mit rohem `AttributeError`
abbrechen. Fenster-Parsing meldet jetzt kontrolliert
`AppServerProtocolError`; Diagnose liefert fail-closed keine unsupported
Fenster. `tests/test_app_server.py`: 87/87 bestanden; Mypy für Source und Ruff
für betroffene Dateien sauber.

## Runde 206: Browser-Diagnose validiert Response- und Usage-Typen

`browser._detect_page_state()` und `_status_for_result()` vertrauten bei
Diagnose-Strings, Response-Containern und Usage-Fenstern auf korrekte
Laufzeittypen. Malformed Diagnose-Daten konnten mit `AttributeError` oder
`TypeError` abbrechen. Strings werden jetzt fail-closed normalisiert,
Response-Listen filtern nur Dicts und Usage-Checks akzeptieren nur
`LimitWindow`. `tests/test_browser_diagnose.py` und
`tests/test_browser_profile.py`: 103/103 bestanden; Mypy für Source und Ruff
für betroffene Dateien sauber.

## Runde 207: Bridge-Ingest validiert Payload-Objekt

`bridge.usage_from_ingest_payload()` nahm Nicht-Objekte an und griff danach
mit `.get()` oder Container-Operationen darauf zu. Manuell oder beschädigt
eingehende Bridge-Daten konnten dadurch mit rohem `TypeError` bzw.
`AttributeError` abbrechen. Der Parser weist Nicht-Dict-Payloads jetzt
kontrolliert mit `ValueError("ingest payload must be an object")` zurück.
`tests/test_bridge.py`: 185/185 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 208: Auth-Payload-Helfer validieren Objektgrenze

`direct.auth_identity_from_payload()`, `auth_email_from_payload()`,
`auth_plan_type_from_payload()` und `auth_metadata_from_payload()` griffen bei
Nicht-Objekten direkt auf `.get()` zu. Beschädigte Parser-Aufrufe konnten damit
mit rohem `AttributeError` abbrechen. Nicht-Dict-Payloads liefern jetzt die
jeweiligen sicheren Leerwerte. `tests/test_direct.py`: 157/157 bestanden;
Mypy für Source und Ruff für betroffene Dateien sauber.

## Runde 209: Gesamtverifikation nach Bridge-/Auth-Härtung

Die Vollsuite bestätigt den Stand: `2280 passed, 1 skipped, 1 warning` in
88.53s. Warnung bleibt externe PyGObject-Deprecation außerhalb des
Repositories. `mypy src/codex_usage` ist in 35 Quelldateien fehlerfrei;
aggregierter Ruff-Lauf über Produktion, Scripts, Launcher und Tests ist
ebenfalls sauber.

## Runde 210: Usage-Limits validieren Zeit und Pool-Iterator

`usage_limits.parse_wham_usage_pools()` konnte bei relativem Reset und
malformed `captured_at` mit rohem `AttributeError` abbrechen;
`merge_model_catalog()` tat dasselbe bei nicht-iterierbaren Pool-Containern.
Beide Parserfamilien akzeptieren jetzt nur echte Zeitstempel, und der
Katalog-Helper fail-closed bei ungültigen Pool-Iterables.
`tests/test_usage_limits.py`: 122/122 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 211: State-Deserializer validiert Payload-Objekt

`state.usage_from_dict()` griff bei Nicht-Objekten direkt auf `.get()` zu.
Direkte Deserializer-Aufrufer bekamen dadurch rohen `AttributeError`; der
Dateiladepfad konnte den Fehler nur außerhalb des Helpers abfangen. Der
Deserializer weist solche Payloads jetzt kontrolliert als
`ValueError("state payload must be an object")` zurück.
`tests/test_state.py`: 241/241 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 212: Gesamtverifikation nach Usage-Limits-/State-Härtung

Die Vollsuite bestätigt den aktuellen Stand: `2302 passed, 1 skipped, 1
warning` in 87.70s. Die einzige Warnung bleibt externe PyGObject-
Deprecation außerhalb des Repositories. `mypy src/codex_usage` meldet keine
Fehler in 35 Quelldateien; der aggregierte Ruff-Lauf über Produktion, Scripts,
Launcher und Tests ist sauber.

## Runde 213: Gemeinsamer Malformed-Payload-Grenztest

Ein kleiner assert-basierter Grenztest speiste sechs Nicht-Objekt-Varianten
(`None`, Liste, String, Zahl, Boolean, beliebiges Objekt) in Bridge-Ingest,
Auth-Metadaten, State-Deserializer sowie Usage-Limits-Parser ein. Alle sechs
Varianten wurden kontrolliert zurückgewiesen bzw. als sichere Leerwerte
behandelt; kein roher `TypeError` oder `AttributeError` blieb an diesen
öffentlichen Eingangsgrenzen.

## Runde 214: Routing-Entry-Point validiert Usage-Typ

`routing.evaluate_routing()` griff bei Fremdtypen für `usage` vor jeder
Validierung auf Account-Felder zu. Direkte Fehlaufrufe konnten dadurch mit
rohem `AttributeError` abbrechen. Der Entscheidungs-Entry-Point weist
Nicht-`AccountUsage` jetzt kontrolliert als `ValueError("usage is invalid")`
zurück. `tests/test_routing.py`: 102/102 bestanden; Mypy für Source und Ruff
für betroffene Dateien sauber.

## Runde 215: Render-Entry-Points validieren Usage-Container

`render_table()` und `render_json()` übernahmen beliebige Iterable und
reichten Nicht-`AccountUsage`-Elemente bis zum Attributzugriff durch. Strings,
nicht-iterierbare Werte oder Fremdobjekte konnten dadurch mit rohem
`TypeError`/`AttributeError` abbrechen. Der gemeinsame Bounded-Helper weist
ungültige Container und Einträge jetzt kontrolliert als
`ValueError("usage records are invalid")` zurück. `tests/test_render.py`:
45/45 bestanden; Mypy für Source und Ruff für betroffene Dateien sauber.

## Runde 216: Account-Render-Entry-Point validiert Container

`render_account_values()` hatte dieselbe ungeprüfte Iterator-/Elementgrenze;
ungültige Accounts konnten mit rohem `TypeError` oder `AttributeError` in die
Tabellenaufbereitung gelangen. Account-Container werden jetzt bounded und
typgeprüft; malformed Usage-Mapping-Werte fallen in Übersichtsspalten
fail-closed auf `-`. `tests/test_render.py`: 50/50 bestanden; Mypy für Source
und Ruff für betroffene Dateien sauber.

## Runde 217: State-Write-/Expiry-Entry-Points validieren Usage-Typ

`save_usage_snapshot()`, `save_current_usage()` und
`expire_reset_windows()` griffen bei Fremdtypen vor Typvalidierung auf
Account-/Pool-Felder zu. Direkte Fehlaufrufe konnten mit rohem
`AttributeError` abbrechen. Alle drei Entry-Points weisen ungültige Werte
jetzt kontrolliert als `ValueError("usage is invalid")` zurück.
`tests/test_state.py`: 247/247 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 218: Scheduler-Entry-Point validiert Config-/Account-Container

`scheduler.fetch_all()` und sein Account-Bounded-Helper übernahmen fremde
Config-/Iterator-Typen vor Typprüfung. Ungültige Aufrufe konnten dadurch mit
rohem `TypeError` oder späterem Attributzugriff abbrechen. Config und Accounts
werden jetzt vor Fetch kontrolliert geprüft; ungültige Container melden
`ValueError`. `tests/test_scheduler.py`: 186/186 bestanden; Mypy für Source
und Ruff für betroffene Dateien sauber.

## Runde 219: Account-Übersicht validiert Config-/Usage-Mapping

`render_account_values()` und `render_account_overview()` griffen bei
Nicht-Mappings bzw. Fremd-Configs vor Validierung auf `.get()` oder
`.accounts` zu. Solche Diagnose-/Übersichtsaufrufe konnten mit rohem
`TypeError`/`AttributeError` abbrechen. Beide Entry-Points weisen ungültige
Container jetzt kontrolliert zurück; einzelne malformed Usage-Werte bleiben
fail-closed. `tests/test_render.py`: 61/61 bestanden; Mypy für Source und Ruff
für betroffene Dateien sauber.

## Runde 220: Abschlussverifikation aktueller HEAD

Der vollständige Testlauf nach Scheduler-, Render- und State-Entry-Point-
Härtung ist grün: `2342 passed, 1 skipped, 1 warning` in 93.77s. Die einzige
Warnung bleibt externe PyGObject-Deprecation außerhalb des Repositories.
`mypy src/codex_usage` meldet keine Fehler in 35 Quelldateien; aggregierter
Ruff über Produktion, Scripts, Launcher und Tests ist sauber.

## Runde 221: Routing-Policy-Resolver validieren Policy-Objekt

`effective_credit_limits()` und `effective_paid_overage()` griffen bei
Fremdtypen oder malformed Policy-Strukturen direkt auf `.get()` bzw.
Schlüsselzugriff zu. Direkte Resolver-Aufrufe konnten dadurch mit rohem
`AttributeError`/`TypeError` abbrechen. Beide Resolver normalisieren jetzt
über `_validate_policy()` und weisen ungültige Policies kontrolliert zurück.
`tests/test_routing.py`: 108/108 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 222: Abschlussverifikation nach Policy-Härtung

Der vollständige Testlauf des aktuellen HEAD ist grün: `2348 passed, 1
skipped, 1 warning` in 86.78s. Die externe PyGObject-Deprecation bleibt die
einzige Warnung außerhalb des Repositories. `mypy src/codex_usage` ist in 35
Quelldateien fehlerfrei; aggregierter Ruff über Produktion, Scripts, Launcher
und Tests ist sauber.

## Runde 223: Direct-/App-Server-Fetch validiert Account-Typ

`fetch_account_usage_direct()` und `fetch_account_usage_app_server()` griffen
bei Fremdtypen vor dem Fehlerpfad auf Account-Felder zu. Ungültige Fetch-
Aufrufe konnten dadurch mit rohem `AttributeError` abbrechen. Beide
öffentlichen Fetch-Entry-Points prüfen jetzt `Account` vor I/O und melden
`ValueError("account is invalid")`. `tests/test_direct.py` und
`tests/test_app_server.py`: 256/256 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 224: Abschlussverifikation nach Fetch-Härtung

Der vollständige Testlauf des aktuellen HEAD bestätigt `2360 passed, 1
skipped, 1 warning` in 88.31s. Die einzelne Warnung bleibt externe
PyGObject-Deprecation außerhalb des Repositories. `mypy src/codex_usage`
meldet keine Fehler in 35 Quelldateien; der aggregierte Ruff-Lauf über
Produktion, Scripts, Launcher und Tests ist sauber.

## Runde 225: Reactivation-Entry-Points validieren Account-Typ

`reactivate_account()` und `open_account_in_reactivation_browser()` griffen
bei Fremdtypen vor Validierung auf `account.id` bzw. Browserfelder zu.
Fehlaufrufe konnten dadurch mit rohem `AttributeError` abbrechen. Beide
öffentlichen Entry-Points weisen ungültige Accounts jetzt kontrolliert als
`ReactivationError("account is invalid")` zurück. `tests/test_reactivate.py`:
56/56 bestanden; Mypy für Source und Ruff für betroffene Dateien sauber.

## Runde 226: Config-Account-Resolver validieren Config-Typ

`config.get_account()` und `config.resolve_account()` griffen bei Fremdtypen
direkt auf `config.accounts` zu. Fehlaufrufe konnten dadurch mit rohem
`AttributeError` abbrechen. Beide Resolver prüfen jetzt `AppConfig` vor der
Iteration und melden ungültige Eingaben als `ValueError`.
`tests/test_config.py`: 108/108 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 227: Routing-Policy-APIs validieren Pfad-Typ

`load_policy()`, `set_policy_rule()` und `set_credit_limits()` übernahmen
Fremdtypen für `path` bis zum Dateisystemzugriff. Dadurch konnten ungültige
Aufrufe mit rohem `AttributeError` abbrechen. Alle drei APIs weisen solche
Pfade jetzt kontrolliert als `ValueError("policy path is invalid")` zurück.
`tests/test_routing.py`: 113/113 bestanden; Mypy für Source und Ruff für
betroffene Dateien sauber.

## Runde 228: Browser-Entry-Points validieren Account-/Config-Typ

`browser.login_account()`, `fetch_account_usage()`, `probe_account()` und
`diagnose_account()` griffen bei Fremdtypen vor Browser-/I/O-Aufrufen auf
Account- oder Config-Felder zu. Fehlaufrufe konnten dadurch mit rohem
`AttributeError` abbrechen. Alle vier Entry-Points prüfen jetzt beide
Objekttypen kontrolliert. `tests/test_browser_profile.py` und
`tests/test_browser_diagnose.py`: 151/151 bestanden; Mypy für Source und Ruff
für betroffene Dateien sauber.

## Runde 229: Bridge-Ingest validiert Account-Typ

`bridge.usage_from_ingest_payload()` validierte bisher nur den Payload. Ein
Fremdobjekt für `account` führte vor der eigentlichen Verarbeitung mit rohem
`AttributeError` zum Abbruch. Der Ingest-Parser weist ungültige Accounts jetzt
kontrolliert als `ValueError("account is invalid")` zurück.
`tests/test_bridge.py`: 191/191 bestanden; Mypy für Source und Ruff für die
betroffenen Dateien sauber.

## Runde 230: Bridge-Latest-Loader validiert Config-/Pfad-Typ

`bridge.load_latest_usages()` griff bei Fremdtypen für Config oder Snapshot-
Verzeichnis vor dem Laden direkt auf `.accounts` bzw. `.parent` zu. Direkte
Fehlaufrufe konnten dadurch mit rohem `AttributeError` abbrechen. Der Loader
weist beide Eingaben jetzt kontrolliert als `ValueError` zurück.
`tests/test_bridge.py`: 200/200 bestanden; Mypy für Source und Ruff für die
betroffenen Dateien sauber.

## Runde 231: Bridge-Generatoren validieren Account-/Endpoint-/Intervall-Typen

`render_bridge_snippet()` und `write_bridge_extension()` konnten bei
mitgeliefertem Token die Account-ID-Prüfung umgehen; fremde Endpoints oder
Intervalle führten außerdem zu unsicheren Ausgaben oder rohen Typfehlern.
`save_bridge_debug_payload()` griff bei Fremdtypen für Account, Payload oder
Snapshot-Verzeichnis ebenfalls ungeprüft zu. Gemeinsame Guards weisen solche
Eingaben jetzt kontrolliert zurück. `tests/test_bridge.py`: 236/236 bestanden;
Mypy für Source und Ruff für die betroffenen Dateien sauber.

## Runde 232: Abschlussverifikation nach Bridge-Härtung

Die Vollsuite bestätigt den aktuellen Stand: `2476 bestanden, 1 übersprungen,
1 Warnung` in 81,74 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. `mypy src/codex_usage` ist in 35 Quelldateien
fehlerfrei; der aggregierte Ruff-Lauf über Produktion, Scripts, Launcher und
Tests ist ebenfalls sauber.

## Runde 233: Profile-Migration validiert Roots und Plan-Felder

`profile_migration.plan_auth_migration()` konnte bei `search_roots=None` mit
rohem `TypeError` abbrechen. `apply_auth_migration()` übernahm manuell
konstruierten malformed Pläne bis zu Attribut- und Pfadzugriffen. Root- und
Plan-Validierung weisen solche Eingaben jetzt kontrolliert zurück.
`tests/test_profile_migration.py`: 30/30 bestanden; Mypy für Source und Ruff
für die betroffenen Dateien sauber.

## Runde 234: Integration-Entry-Point validiert argv-Container

`integration_entrypoint.execute()` rief `tuple(argv)` außerhalb des
Fehlerpfads auf. `None` oder Fremdobjekte konnten dadurch statt des
definierten Invalid-Arguments-Ergebnisses mit rohem `TypeError` abbrechen.
Nicht-sequenzielle argv-Werte liefern jetzt kontrolliert Exit-Code 64.
`tests/test_integration_entrypoint.py`: 25/25 bestanden; Mypy für Source und
Ruff für die betroffenen Dateien sauber.

## Runde 235: Profile-Job-Worker validiert argv-Container

`profile_jobs.worker_main()` griff bei `None`, Fremdcontainern oder falschen
Elementen vor dem Fehlerpfad auf `len()` bzw. Indexzugriff zu. Der Worker weist
ungültige argv-Form jetzt kontrolliert mit Exit-Code 2 zurück.
`tests/test_profile_jobs.py`: 69/69 bestanden; Mypy für Source und Ruff für die
betroffenen Dateien sauber.

## Runde 236: Health-APIs validieren Pfad-Typ

`health.record_health_event()`, `load_health()` und `clear_health()` übernahmen
Fremdtypen für den optionalen Pfad bis zu `.parent`/`.exists()` und konnten mit
rohem `AttributeError` abbrechen. Ein gemeinsamer Pfad-Resolver weist solche
Eingaben jetzt kontrolliert als `ValueError("health path is invalid")` zurück.
`tests/test_health.py`: 25/25 bestanden; Mypy für Source und Ruff für die
betroffenen Dateien sauber.

## Runde 237: Spark-Health-APIs validieren Pfad-Typ

`spark_health_status()` und `set_spark_health()` griffen bei Fremdtypen für den
optionalen Pfad roh auf `.exists()` bzw. `.parent` zu. Ein gemeinsamer Resolver
weist ungültige Pfade jetzt kontrolliert als `ValueError` zurück.
`tests/test_spark_health.py`: 23/23 bestanden; Mypy für Source und Ruff für die
betroffenen Dateien sauber.

## Runde 238: Abschlussverifikation nach Health-/Entrypoint-Härtung

Die Vollsuite bestätigt den aktuellen lokalen HEAD: `2502 bestanden, 1
übersprungen, 1 Warnung` in 81,27 s. Die Warnung bleibt externe PyGObject-
Deprecation außerhalb des Repositories. `mypy src/codex_usage` ist in 35
Quelldateien fehlerfrei; der aggregierte Ruff-Lauf über Produktion, Scripts,
Launcher und Tests ist ebenfalls sauber.

## Runde 239: Direct-Auth-Datei-/Account-Helper validieren Eingaben

`direct.auth_identity_from_file()`, `auth_email_from_file()`,
`auth_plan_type_from_file()`, `read_auth_json_file()` und
`validate_auth_json_file()` griffen bei Fremdpfaden roh auf Path-Methoden zu.
`auth_identity_for_account()` und `auth_plan_type_for_account()` taten dasselbe
bei Fremd-Accounts. Gemeinsame Guards liefern jetzt kontrolliert
`DirectAuthError`.
`tests/test_direct.py`: 173/173 bestanden; Mypy für Source und Ruff für die
betroffenen Dateien sauber.

## Runde 240: Private-IO-APIs validieren Path-Typ

`private_io.assert_no_symlink_ancestors()`, `ensure_private_directory()`,
`read_private_text()`, `write_private_text()` und `private_path_lock()`
übernahmen Fremdtypen bis zu rohen Path-/Attributfehlern. Ein gemeinsamer
Resolver weist ungültige Pfade jetzt kontrolliert als `ValueError` zurück,
bevor Sicherheits-I/O beginnt. `tests/test_private_io.py`: 32/32 bestanden;
Mypy für Source und Ruff für die betroffenen Dateien sauber.

## Runde 241: OAuth-Browser validiert argv-Container

`oauth_browser.main()` wandelte ein nicht-iterierbares `argv` vor dem
Fehlerpfad direkt mit `list()` um. Fremdobjekte konnten dadurch roh mit
`TypeError` abbrechen. Ungültige argv-Container liefern jetzt kontrolliert
Exit-Code 2. `tests/test_reactivate.py`: 58/58 bestanden; Mypy für Source und
Ruff für die betroffenen Dateien sauber.

## Runde 242: Profile-Layout validiert Transaktionscontainer und Flag

`profile_layout.ensure_profile_layout()` übernahm Fremdtypen für
`created_directories`/`created_files` bis zum rohen `.append()`; ein nicht-
boolesches `preserve_existing_metadata` wurde ebenfalls still interpretiert.
Optionale Container und Flag werden jetzt vor Layout-I/O typgeprüft.
`tests/test_profile_layout.py` und `tests/test_profile_jobs.py`: 83/83
fokussierte Tests bestanden; Mypy für Source und Ruff für die betroffene Datei
sauber.

## Runde 243: Abschlussverifikation nach Direct-/Private-IO-Härtung

Die Vollsuite bestätigt den aktuellen HEAD: `2530 bestanden, 1 übersprungen,
1 Warnung` in 81,79 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. `mypy src/codex_usage` ist in 35 Quelldateien
fehlerfrei; der aggregierte Ruff-Lauf über Produktion, Scripts, Launcher und
Tests ist ebenfalls sauber.

## Runde 244: State-APIs validieren optionale Verzeichnisse

`state.load_state_generation()`, `load_usage_snapshot()`,
`load_current_usage()`, `save_usage_snapshot()` und `save_current_usage()`
übernahmen Fremdtypen für Snapshot-/Current-Verzeichnisse bis zur rohen
Pfadkombination. Ein gemeinsamer Resolver weist ungültige Verzeichnisse jetzt
kontrolliert als `ValueError("state directory is invalid")` zurück.
`tests/test_state.py`: 252/252 bestanden; Mypy für Source und Ruff für die
betroffenen Dateien sauber.

## Runde 245: State-Provenance- und Merge-APIs validieren Usage-Typen

`backend_provenance_matches_configured()`,
`backend_provenance_matches()` und `backend_identity_matches()` griffen bei
Fremdobjekten direkt auf Usage-Felder zu. `merge_current_with_last_success()`
tat dasselbe für current/last-success. Predicates fail-closed; Merge weist
ungültige Usage jetzt kontrolliert als `ValueError` zurück.
`tests/test_state.py`: 263/263 bestanden; Mypy für Source und Ruff für die
betroffenen Dateien sauber.

## Runde 246: Abschlussverifikation nach State-Härtung

Die Vollsuite bestätigt den aktuellen HEAD: `2546 bestanden, 1 übersprungen,
1 Warnung` in 82,04 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. `mypy src/codex_usage` ist in 35 Quelldateien
fehlerfrei; der aggregierte Ruff-Lauf über Produktion, Scripts, Launcher und
Tests ist ebenfalls sauber.

## Runde 247: Scheduler-Watch-APIs validieren Config-Typ

`scheduler.watch()` griff bei Fremd-Configs direkt auf
`config.interval_seconds` zu; `watchdog()` konnte malformed Configs bis in
den Zyklus verarbeiten. Beide öffentlichen APIs weisen ungültige Configs jetzt
kontrolliert als `ValueError("config is invalid")` zurück.
`tests/test_scheduler.py`: 192/192 bestanden; Mypy für Source und Ruff für die
betroffenen Dateien sauber.

## Runde 248: Bridge-Server validiert Bind-/Datei-Eingaben

`bridge.run_bridge_server()` griff bei Fremdtypen für Config, Host, Port oder
optionale Snapshot-/Config-/TLS-Pfade vor Handler- und Socket-Aufbau direkt auf
Attribute zu bzw. reichte sie an I/O weiter. Guards prüfen diese Grenzen jetzt
vor TLS und Bind. `tests/test_bridge.py`: 254/254 bestanden; Mypy für Source
und Ruff für die betroffenen Dateien sauber.

## Runde 249: Scheduler validiert optionalen Auth-Pfad

`scheduler.fetch_all()`, `watch()` und `watchdog()` übernahmen Fremdtypen für
`auth_json_path`. `fetch_all()` brach dadurch roh mit `AttributeError` in der
Pfadauflösung ab; `watchdog()` versteckte denselben Fehler als Fehlerusage.
Ein gemeinsamer Guard weist ungültige Werte jetzt vor Scheduler-I/O als
`ValueError("auth_json_path is invalid")` zurück. `tests/test_scheduler.py`:
196/196 fokussierte Tests bestanden; Mypy für `scheduler.py` und Ruff für die
betroffenen Dateien sauber. Der bestehende Mypy-Fehler im Testmodul für den
absichtlich falsch typisierten `LimitWindow(remaining="97")`-Fixture bleibt
unverändert.

## Runde 250: Abschlussverifikation nach Scheduler-Härtung

Die Vollsuite bestätigt den Stand nach Scheduler-Runde 249: `2574 bestanden,
1 übersprungen, 1 Warnung` in 87,87 s. Die Warnung bleibt externe PyGObject-
Deprecation außerhalb des Repositories. `mypy src/codex_usage` ist in 35
Quelldateien fehlerfrei; der aggregierte Ruff-Lauf über Source, Tests und
Scripts ist ebenfalls sauber.

## Runde 251: Identity-Helpers verwerfen malformed URLs

`identity.backend_identity_from_candidates()` und
`backend_plan_type_from_candidates()` reichten einen nicht parsebaren,
nichtleeren URL-String wie `http://[::1` bis `urlsplit()` und brachen roh mit
`ValueError` ab. Die Kandidatenprüfung validiert URL-Syntax jetzt vor der
Priorisierung; solche Kandidaten werden wie andere unbrauchbare Kandidaten
verworfen. `tests/test_identity.py`: 28/28 bestanden; Mypy für Source und
Ruff für die betroffenen Dateien sauber.

## Runde 252: Strict-JSON validiert Eingabetyp

`json_utils.loads_strict()` reichte Fremdtypen an `memoryview()` bzw.
`json.loads()` weiter und lieferte dadurch rohen `TypeError`. Der Parser weist
Werte außerhalb `str | bytes | bytearray` jetzt kontrolliert als
`ValueError("JSON input is invalid")` zurück. `tests/test_json_utils.py`:
8/8 bestanden; Mypy für Source und Ruff für die betroffenen Dateien sauber.

## Runde 253: History-Store validiert Pfad und Dry-Run-Flag

`HistoryStore.__init__()` interpretierte falsche, falsy Pfade wie `[]` oder
`""` wegen `path or default_history_path()` als globale Default-Datei.
`record_usage_samples_batch()` konnte denselben falschen Pfad bei leerem
Sample-Satz still akzeptieren. Beide Grenzen validieren Pfadtypen jetzt
explizit. `HistoryStore.prune()` weist außerdem nicht-boolesche
`dry_run`-Werte zurück, statt sie truthy/falsy zu interpretieren.
`tests/test_history.py`: 72/72 fokussierte Tests bestanden; Mypy für Source
und Ruff für die betroffenen Dateien sauber.

## Runde 254: Reactivation validiert Manage-URL-Syntax

`reactivate.open_account_in_reactivation_browser()` ließ eine syntaktisch
kaputte URL wie `https://[::1` bis `urlsplit()` durch und gab rohen
`ValueError` statt `ReactivationError` zurück. Die URL-Prüfung fängt Parser-
Fehler jetzt kontrolliert ab. `tests/test_reactivate.py`: 59/59 fokussierte
Tests bestanden; Mypy für Source und Ruff für die betroffenen Dateien sauber.

## Runde 255: Reactivation validiert Account-Pfade

`reactivate` und `open_account_in_reactivation_browser` übernahmen malformed
`Account.profile_dir`/`auth_json_path` bis zu `Path()` und konnten dort roh mit
`TypeError` abbrechen. Gemeinsame Account-Pfadguards weisen falsche oder leere
Werte jetzt als `ReactivationError` zurück. `tests/test_reactivate.py`: 60/60
fokussierte Tests bestanden; Mypy für Source und Ruff für die betroffenen
Dateien sauber.

## Runde 256: Profile-Layout validiert Account-Profilpfad

`profile_layout.layout_for_account()` übernahm malformed oder leere
`Account.profile_dir`-Werte bis `Path()` und gab rohen `TypeError` zurück.
Ungültige bzw. relative Profilpfade werden jetzt vor Layout-I/O kontrolliert
abgewiesen. `tests/test_profile_layout.py`: 19/19 bestanden; Mypy für Source
und Ruff für die betroffenen Dateien sauber.

## Runde 257: App-Server validiert Account-Auth-Pfad

`app_server._auth_context()` reichte einen nicht-stringförmigen
`Account.auth_json_path` bis `Path()` weiter; `fetch_account_usage_app_server()`
konnte dadurch roh mit `TypeError` abbrechen statt Login-Required zu liefern.
Der Auth-Pfad wird jetzt als `DirectAuthError` fail-closed behandelt.
`tests/test_app_server.py`: 96/96 bestanden; Mypy für Source und Ruff für die
betroffenen Dateien sauber.

## Runde 258: Browser-Profil validiert Pfad und Browser

`browser._prepare_profile()` konnte malformed/relative `profile_dir`-Werte oder
unbekannte Browser bis Marker-/Verzeichnis-I/O übernehmen. Guards prüfen
Profilpfad und Browser jetzt vor dem ersten Schreibzugriff. `tests/test_browser_
profile.py` und `tests/test_browser_diagnose.py`: 161/161 fokussierte Tests
bestanden; Mypy für Source und Ruff für die betroffenen Dateien sauber.

## Runde 259: Direct-Fetch validiert Auth-Pfad und Override

`direct.fetch_account_usage_direct()` löste Account- und Override-
`auth_json_path` vor dem Fehlerfang auf. Fremdtypen wie `int` oder `object`
brachen deshalb roh mit `TypeError` ab. Auflösung liegt jetzt im kontrollierten
Auth-Fehlerpfad; beide Varianten liefern `LOGIN_REQUIRED` mit begrenztem
Fehlertext. `tests/test_direct.py`: 179/179 bestanden; Mypy für Source und
Ruff für die betroffenen Dateien sauber.

## Runde 260: CLI validiert explizites argv

`cli.main(argv=…)` übernahm Fremdcontainer oder nicht-stringförmige Argumente
vor `_default_root_command()` und konnte dadurch roh mit `TypeError` abbrechen.
Explizites argv wird jetzt vor Parser-Aufbau geprüft und liefert Exit-Code 2.
`tests/test_cli.py`: 114/114 bestanden; Mypy für Source und Ruff für die
betroffene Datei sauber.

## Runde 261: Abschlussverifikation nach Auth-/Profil-/CLI-Härtung

Die Vollsuite bestätigt den aktuellen HEAD: `2624 bestanden, 1 übersprungen,
1 Warnung` in 88,73 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. `mypy src/codex_usage` ist in 35 Quelldateien
fehlerfrei; der aggregierte Ruff-Lauf über Source, Tests und Scripts ist
ebenfalls sauber.

## Runde 262: Extractor verwirft malformed Kandidaten-URLs

`extractor.extract_windows()` rief für einen nicht parsebaren URL-String wie
`https://[::1` direkt `_wham_candidate_priority()`/`urlsplit()` auf und brach
roh mit `ValueError` ab. Eine gemeinsame Kandidatenprüfung validiert URL-Syntax
jetzt vor JSON-Window-Priorisierung und `load_json_candidate()` verwirft solche
URLs früh. `tests/test_extractor.py`: 195/195 bestanden; Mypy für Source und
Ruff für die betroffenen Dateien sauber.

## Runde 263: Config validiert optionale Callbacks

`config.add_or_update_account()` übernahm nicht-aufrufbare
`before_state_cleanup`-/`rollback_callback`-Werte bis zur tatsächlichen
Verwendung. Ein Listenwert brach dadurch roh mit `TypeError` ab. Beide
Callbacks werden jetzt vor Config-/Profil-I/O als aufrufbar geprüft.
`tests/test_config.py`: 110/110 bestanden; Mypy für Source und Ruff für die
betroffene Datei sauber.

## Runde 264: Consumption validiert Zeitbereich vor Lookback-Arithmetik

`consumption.calculate_consumption()` subtrahierte Lookback-Zeit von einem
aware, aber randständigen `datetime.min` und gab rohen `OverflowError` zurück.
Der Zeitbereich wird jetzt kontrolliert geprüft und als `ValueError("now is out
of range")` abgewiesen. `tests/test_consumption.py`: 25/25 bestanden; Mypy für
Source und Ruff für die betroffenen Dateien sauber.

## Runde 265: Usage-Limits fängt relative Reset-Zeitüberläufe

`usage_limits._reset_at()` addierte relative Reset-Sekunden zu einem
randständigen `datetime.max` und gab bei Überlauf rohen `OverflowError` zurück.
Der Reset wird bei nicht darstellbarem Ergebnis jetzt als unbekannt verworfen;
Usage-Werte bleiben nutzbar. `tests/test_usage_limits.py`: 123/123 bestanden;
Mypy für Source und Ruff für die betroffenen Dateien sauber.

## Runde 266: Routing validiert Policy-Quelle und Spark-Health-JSON

`routing.evaluate_routing()` übernahm eine leere oder fremdtypige
`policy_source`-Angabe bis in die Entscheidungsstruktur; ein nicht
serialisierbares Objekt brach erst beim JSON-Ausgeben roh ab. Zusätzlich wurde
ein fremdes `spark_health`-Dict unverändert in die Ausgabe kopiert, sodass
Objektwerte in bekannten Feldern oder unbekannten Zusatzfeldern ebenfalls
`json.dumps()` sprengten. Nichtleere String-Policy-Quellen werden jetzt am
Eingang verlangt; bekannte Spark-Health-Felder werden typgeprüft und in eine
JSON-sichere Struktur kopiert, Zusatzfelder verworfen. Routing bleibt bei
malformed Health-Daten fail-closed. `tests/test_routing.py`: 123/123
fokussierte Tests bestanden; Mypy für Source, Ruff und `git diff --check`
sauber.

## Runde 267: Vollsuite nach Routing-Härtung

Die Vollsuite bestätigt den aktuellen Routing-Stand: `2638 bestanden, 1
übersprungen, 1 Warnung` in 87,85 s. Die Warnung bleibt externe PyGObject-
Deprecation außerhalb des Repositories. `mypy src/codex_usage` ist in 35
Quelldateien fehlerfrei; der aggregierte Ruff-Lauf über Source, Tests und
Scripts sowie `git diff --check` sind sauber.

## Runde 268: CLI-Consumption schützt Zeitbereich

`cli._cmd_consumption()` subtrahierte Lookback-Sekunden von einem erlaubten,
aber randständigen `--now`-Zeitpunkt und gab rohes `OverflowError`-Verhalten
(`date value out of range`) an die CLI-Fehlerausgabe weiter. Zusätzlich konnte
`_parse_history_datetime()` bei extremem UTC-Offset bereits in
`astimezone()` überlaufen. Beide Grenzen liefern jetzt kontrolliert
`<label> is out of range`; der Consumption-Befehl öffnet die Historie erst nach
erfolgreicher Zeitbereichsprüfung. `tests/test_cli.py`: 117/117 fokussierte
Tests bestanden; Consumption-Suite: 25/25; Mypy für Source, Ruff und
`git diff --check` sauber.

## Runde 269: Vollsuite nach CLI-Zeitgrenzen-Fix

Die Vollsuite bestätigt den aktuellen HEAD: `2641 bestanden, 1 übersprungen,
1 Warnung` in 87,21 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. `mypy src/codex_usage` ist in 35 Quelldateien
fehlerfrei; der aggregierte Ruff-Lauf über Source, Tests und Scripts sowie
`git diff --check` sind sauber.

## Runde 270: Snapshot-Cost-Window-Konverter validiert

`integration_snapshot.build_schema1_document()` rief bei beliebigen
Cost-Window-Objekten ein vorhandenes `as_dict`-Attribut blind auf. Ein
nicht-aufrufbares Attribut oder eine fehlerwerfende Property konnte dadurch
rohen `TypeError` bzw. `RuntimeError` auslösen. Der Converter wird jetzt
kontrolliert gelesen und aufrufbar geprüft; Fehler werden als
`IntegrationInvalidSource` behandelt. `tests/test_integration_snapshot.py`:
48/48 fokussierte Tests bestanden; Mypy für Source, Ruff und
`git diff --check` sauber.

## Runde 271: History validiert UTC-Zeitbereich

`history.UsageSample` akzeptierte aware Randzeitpunkte, deren UTC-Konvertierung
außerhalb des darstellbaren `datetime`-Bereichs lag, etwa
`datetime.min+14:00`. `HistoryStore.record()` konnte danach in
`_to_millis()` roh mit `OverflowError` abbrechen. Die gemeinsame
Aware-Zeitprüfung testet jetzt zusätzlich UTC-Konvertierbarkeit und weist solche
Werte kontrolliert als `<label> is out of range` zurück. `tests/test_history.py`:
74/74 fokussierte Tests bestanden; Mypy für Source, Ruff und `git diff --check`
sauber.

## Runde 272: Health-Safe-Clock validiert Zeitzone und UTC-Bereich

`health.record_health_event()` behandelte naive `datetime`-Werte als lokale
Zeit und ließ aware Randwerte mit nicht darstellbarer UTC-Konvertierung roh als
`OverflowError` scheitern. Die Telemetrie prüft `tzinfo`/`utcoffset()` und
UTC-Konvertierung jetzt defensiv; malformed `now` fällt auf aktuelle UTC-Zeit
zurück. `tests/test_health.py`: 28/28 fokussierte Tests bestanden; Mypy für
Source, Ruff und `git diff --check` sauber.

## Runde 273: Vollsuite nach Snapshot-/History-/Health-Härtung

Die Vollsuite bestätigt den aktuellen HEAD: `2647 bestanden, 1 übersprungen,
1 Warnung` in 87,63 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. `mypy src/codex_usage` ist in 35 Quelldateien
fehlerfrei; der aggregierte Ruff-Lauf über Source, Tests und Scripts sowie
`git diff --check` sind sauber.

## Runde 274: Health-Token-Fallback gegen String-Konverterfehler

`health._safe_token()` rief `str()` blind auf Component-, Event- und
Error-Class-Werten auf. Ein Objekt mit fehlerwerfendem `__str__` konnte dadurch
Telemetrie mit rohem `RuntimeError` abbrechen. String-Konvertierung läuft jetzt
kontrolliert; bei Fehler oder ungültigem Inhalt bleibt der jeweilige sichere
Fallback erhalten. `tests/test_health.py`: 31/31 fokussierte Tests bestanden;
Mypy für Source, Ruff und `git diff --check` sauber.

## Runde 275: Profiljob-Pfade weisen unbekannte Home-Namen kontrolliert ab

`profile_jobs` ließ `Path.expanduser()` bei einem unbekannten `~user`-Namen als
rohes `RuntimeError` aus `_validate_create_arguments()` und bei `config_path`
aus `create_profile_job()` entkommen. Beide Eingaben liefern jetzt kontrolliert
`ValueError` und schreiben keinen Job. `tests/test_profile_jobs.py`: 71/71
fokussierte Tests bestanden; Mypy für Source, Ruff und `git diff --check`
sauber.

## Runde 276: Vollsuite nach Profiljob-Pfad-Härtung

Die Vollsuite bestätigt den aktuellen HEAD: `2652 bestanden, 1 übersprungen,
1 Warnung` in 88,08 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. `mypy src/codex_usage` ist in 35 Quelldateien
fehlerfrei; der aggregierte Ruff-Lauf über Source, Tests und Scripts sowie
`git diff --check` sind sauber.

## Runde 277: Service-Config-Pfade vor Seiteneffekten expandieren

`service_install()` und `service_enable()` akzeptierten einen unbekannten
`~user`-Config-Pfad zunächst, legten danach bereits das systemd-Unit-Verzeichnis
an und ließen `Path.expanduser()` als rohes `RuntimeError` entkommen. Die
gemeinsame Pfadauswahl expandiert jetzt vor Lock und Seiteneffekten und liefert
kontrolliert `ValueError("config path cannot be resolved")`. `tests/test_service.py`:
65/65 fokussierte Tests bestanden; Mypy für Source, Ruff und `git diff --check`
sauber.

## Runde 278: Account- und Auth-Pfade weisen unbekannte Home-Namen ab

`config._absolute_account_path()` und die abschließende Account-Validierung
ließen `Path.expanduser()` bei unbekannten `~user`-Namen als rohes
`RuntimeError` entkommen. Beide zentralen Pfadprüfungen liefern jetzt
kontrolliert `ValueError` für Profil- und Auth-Pfade. `tests/test_config.py`:
112/112 fokussierte Tests bestanden; Mypy für Source, Ruff und
`git diff --check` sauber.

## Runde 279: Browser-Diagnose ignoriert unbekannte CODEX_HOME-Namen

`browser._diagnose_auth_json()` ließ einen unbekannten `CODEX_HOME=~user/...`
als rohes `RuntimeError` entkommen, obwohl relative Homes bereits sicher auf
`~/.codex` zurückfallen. Nicht expandierbare Umgebungswerte werden jetzt wie
relative Werte ignoriert. `tests/test_browser_diagnose.py`: 39/39 fokussierte
Tests bestanden; Mypy für Source, Ruff und `git diff --check` sauber.

## Runde 280: Vollsuite nach Config-/Browser-Pfad-Härtung

Die aktuelle Vollsuite bestätigt den HEAD: `2657 bestanden, 1 übersprungen,
1 Warnung` in 87,51 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. `mypy src/codex_usage` ist in 35 Quelldateien
fehlerfrei; der aggregierte Ruff-Lauf über Source, Tests und Scripts sowie
`git diff --check` sind sauber.

## Runde 281: Direct-Auth-Pfade weisen unbekannte Home-Namen ab

`direct._resolve_auth_json_path()` ließ unbekannte `~user`-Overrides und
Account-Auth-Pfade als rohes `RuntimeError` entkommen. Beide Varianten liefern
jetzt `DirectAuthError("auth.json path is invalid")`, sodass der Fetch wie bei
anderen ungültigen Auth-Pfaden kontrolliert `LOGIN_REQUIRED` meldet.
`tests/test_direct.py`: 181/181 fokussierte Tests bestanden; Mypy für Source,
Ruff und `git diff --check` sauber.

## Runde 282: Browser-Profilpfad weist unbekannte Home-Namen ab

`browser._prepare_profile()` ließ einen unbekannten `~user`-Profilpfad als
rohes `RuntimeError` entkommen. Der Profilpfad wird jetzt kontrolliert als
ungültig gemeldet, bevor Marker oder Browser-Verzeichnisse angelegt werden.
`tests/test_browser_profile.py`: 124/124 fokussierte Tests bestanden; Mypy für
Source, Ruff und `git diff --check` sauber.

## Runde 283: Reactivation-Pfadresolver validieren Home-Expansion

`reactivate._validate_auth_target()` und `_account_profile_root()` ließen
unbekannte `~user`-Pfade ebenfalls als rohes `RuntimeError` entkommen. Beide
Resolver liefern jetzt kontrolliert `ReactivationError`, bevor Browser- oder
Auth-Zugriffe starten. `tests/test_reactivate.py`: 61/61 fokussierte Tests
bestanden; Mypy für Source, Ruff und `git diff --check` sauber.

## Runde 284: Profil-Layout validiert unbekannte Home-Namen

`profile_layout.layout_for_account()` ließ unbekannte `~user`-Profilpfade als
rohes `RuntimeError` entkommen. Der kanonische Layout-Resolver liefert jetzt
kontrolliert `ValueError`, bevor Pfadvorfahren oder Verzeichnisse geprüft
werden. `tests/test_profile_layout.py`: 20/20 fokussierte Tests bestanden;
Mypy für Source, Ruff und `git diff --check` sauber.

## Runde 285: Vollsuite nach Browser-/Reactivation-/Layout-Pfad-Härtung

Die Vollsuite bestätigt den aktuellen HEAD: `2662 bestanden, 1 übersprungen,
1 Warnung` in 93,59 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. `mypy src/codex_usage` ist in 35 Quelldateien
fehlerfrei; der aggregierte Ruff-Lauf über Source, Tests und Scripts sowie
`git diff --check` sind sauber.

## Runde 286: App-Server-Pfade weisen unbekannte Home-Namen ab

`app_server._auth_context()` und `_resolve_codex()` ließen unbekannte
`~user`-Pfade als rohes `RuntimeError` entkommen. Auth-Kontext und expliziter
Codex-Befehl liefern jetzt kontrolliert `DirectAuthError` bzw.
`AppServerUnavailableError`. `tests/test_app_server.py`: 98/98 fokussierte
Tests bestanden; Mypy für Source, Ruff und `git diff --check` sauber.

## Runde 287: Bridge-TLS-Pfade validieren Home-Expansion

`bridge._tls_context()` ließ unbekannte `~user`-Pfade für Zertifikat oder Key
als rohes `RuntimeError` entkommen. Zusätzlich expandierte
`run_bridge_server()` den Config-Pfad erst direkt vor Handler/Bind. TLS- und
Config-Pfade werden jetzt vor Zugriffen kontrolliert expandiert; Fehler liefern
`ValueError`. `tests/test_bridge.py`: 256/256 fokussierte Tests bestanden;
Mypy für Source, Ruff und `git diff --check` sauber.

## Runde 288: Scheduler-Auth-Override expandiert vor Ambiguitätsprüfung

`scheduler._validated_auth_json_path()` akzeptierte einen unbekannten
`~user`-Override, worauf die spätere gemeinsame Auth-Quellenprüfung roh in
`Path.expanduser()` scheitern konnte. Der Override wird jetzt zentral
expandiert; unbekannte Home-Namen liefern kontrolliert
`ValueError("auth_json_path is invalid")`. `tests/test_scheduler.py`: 197/197
fokussierte Tests bestanden; Mypy für Source, Ruff und `git diff --check`
sauber.

## Runde 289: Vollsuite nach App-Server-/Bridge-/Scheduler-Härtung

Die Vollsuite bestätigt den aktuellen HEAD: `2667 bestanden, 1 übersprungen,
1 Warnung` in 88,13 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. `mypy src/codex_usage` ist in 35 Quelldateien
fehlerfrei; der aggregierte Ruff-Lauf über Source, Tests und Scripts sowie
`git diff --check` sind sauber.


## Runde 290: Profil-Migration validiert explizite Auth-Quelle

`profile_migration._source_for_account()` ließ einen unbekannten `~user`-
Auth-Pfad als rohes `RuntimeError` entkommen. Explizite Auth-Quellen werden
jetzt kontrolliert expandiert; unbekannte Home-Namen liefern
`ValueError("auth source cannot be resolved")` vor Quellenklassifizierung und
Schreibzugriff. `tests/test_profile_migration.py`: 31/31 fokussierte Tests
bestanden; Mypy für Source, Ruff und `git diff --check` sauber.

## Runde 291: Vollsuite nach Pfad-Expansion-Härtung

Die Vollsuite bestätigt den aktuellen HEAD: `2668 bestanden, 1 übersprungen,
1 Warnung` in 88,75 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. `mypy src/codex_usage` ist in 35 Quelldateien
fehlerfrei; der aggregierte Ruff-Lauf über Source, Tests und Scripts sowie
`git diff --check` sind sauber.

## Runde 292: Migration validiert UTC-Darstellbarkeit vor Schreiben

`profile_migration._validate_migration_plan()` akzeptierte aware
Randzeitpunkte, deren UTC-Konvertierung außerhalb des `datetime`-Bereichs lag.
`apply_auth_migration()` konnte dadurch erst nach Profil-/Auth-Schreibvorgängen
mit `OverflowError` abbrechen. Die Validierung prüft UTC-Konvertierbarkeit jetzt
vor Seiteneffekten. `tests/test_profile_migration.py`: 32/32 fokussierte Tests
bestanden; Mypy für Source, Ruff und `git diff --check` sauber.

## Runde 293: Consumption validiert Baseline-Value-Zeitbereich

`consumption.calculate_consumption()` prüfte zunächst nur den normalen
Lookback. Die separate `baseline_value_minutes`-Subtraktion konnte bei
`datetime.min`-nahen Werten danach roh mit `OverflowError` abbrechen. Beide
Zeitpunkte werden jetzt gemeinsam validiert; ungültiger Bereich liefert
`ValueError("now is out of range")`. `tests/test_consumption.py`: 26/26
fokussierte Tests bestanden; Mypy für Source, Ruff und `git diff --check`
sauber.

## Runde 294: Vollsuite nach Migration-/Consumption-Zeitgrenzen

Die Vollsuite bestätigt den aktuellen HEAD: `2670 bestanden, 1 übersprungen,
1 Warnung` in 157,19 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. `mypy src/codex_usage` ist in 35 Quelldateien
fehlerfrei; der aggregierte Ruff-Lauf über Source, Tests und Scripts sowie
`git diff --check` sind sauber.

## Runde 295: Integration-Lookback validiert Zeitbereich vor History-Zugriff

`integration_entrypoint._load_cost_windows()` zog `now - 1h` direkt vor der
History-Abfrage ab. Bei randständigem `generated_at` konnte dadurch rohes
`OverflowError` entstehen; `execute()` fiel dann auf falschen Exit-Code 69.
Der Lookback wird jetzt vor Main-/Credits-Abfragen kontrolliert berechnet und
liefert bei Überlauf `ValueError("now is out of range")`.
`tests/test_integration_entrypoint.py`: 26/26 fokussierte Tests bestanden;
Mypy für Source, Ruff und `git diff --check` sauber.

## Runde 296: Vollsuite nach Integration-Lookback-Härtung

Die Vollsuite bestätigt den aktuellen HEAD: `2671 bestanden, 1 übersprungen,
1 Warnung` in 89,33 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. `mypy src/codex_usage` ist in 35 Quelldateien
fehlerfrei; der aggregierte Ruff-Lauf über Source, Tests und Scripts sowie
`git diff --check` sind sauber.

## Runde 297: Unbekannte XDG-Home-Namen fallen auf Standard zurück

`config._xdg_root()` akzeptierte absolute XDG-Werte, behandelte unbekannte
`~user`-Expansion aber nicht. `Path.expanduser()` konnte dadurch beim Aufbau
von Default-Statepfaden roh mit `RuntimeError` abbrechen. Unauflösbare
Expansion fällt jetzt wie relative XDG-Werte auf den Standardpfad zurück.
`tests/test_config.py`: 113/113 fokussierte Tests bestanden; Mypy für die
Konfigurationsdatei, Ruff und `git diff --check` sauber.

## Runde 298: Direct-Auth-Dateihelfer validieren Home-Expansion

`auth_identity_from_file()`, `auth_email_from_file()` und
`auth_plan_type_from_file()` expandierten Pfade außerhalb des geschützten
Fetch-Pfads. Ein unbekanntes `~user` konnte deshalb roh mit `RuntimeError`
abbrechen. Gemeinsame Pfadvalidierung liefert jetzt konsistent
`DirectAuthError("auth.json path is invalid")`. `tests/test_direct.py`:
182/182 fokussierte Tests bestanden; Mypy für Direct, Ruff und
`git diff --check` sauber.

## Runde 299: Managed-Service-Config-Pfad toleriert unbekannte Home-Namen

`managed_service_config_path()` las den `ExecStart --config`-Wert aus einer
verwalteten Unit und expandierte ihn ohne `RuntimeError`-Schutz. Ein unbekanntes
`~user` konnte Status-/Cleanup-Pfade roh abbrechen. Die Funktion behandelt
solche nicht auflösbaren Unit-Inhalte jetzt wie ungültige Konfiguration und
liefert `None`. `tests/test_service.py`: 66/66 fokussierte Tests bestanden;
Mypy für Service, Ruff und `git diff --check` sauber.

## Runde 300: Config-Test-Home und Profilprüfung validieren Home-Expansion

`add_or_update_account(test_home=True)` expandierte eine explizite
Auth-Quelle vor der normalen Pfadvalidierung. Zusätzlich lag die erste
`profile_dir`-Expansion in `_validate_profile_path()` außerhalb des
Fehlerfangs. Unbekannte `~user`-Werte liefern jetzt jeweils kontrolliertes
`ValueError` statt rohem `RuntimeError`. `tests/test_config.py`: 115/115
fokussierte Tests bestanden; Mypy für Config, Ruff und `git diff --check`
sauber.

## Runde 301: Account-Übersicht toleriert nicht auflösbare Pfade

`render_account_overview()` expandierte Profil- und Auth-Pfade aus einem
direkt übergebenen `AppConfig` ohne Schutz. Ein unbekanntes `~user` konnte die
Anzeige statt Statusausgabe abbrechen; zusätzlich wurde die Pfadspalte nicht
erreicht. Status zeigt jetzt `ungültig`, die Pfadspalte bleibt darstellbar.
`tests/test_render.py`: 62/62 fokussierte Tests bestanden; Mypy für Render,
Ruff und `git diff --check` sauber.

## Runde 302: Profiljob-Abschlussprüfung fängt ungültige Home-Expansion

`_verify_profile_job_completion()` verglich das gespeicherte Profilverzeichnis
direkt nach `expanduser()`. Ein beschädigtes Job-Manifest mit unbekanntem
`~user` lief am äußeren Fehlerfang vorbei und konnte die Worker-Prüfung
abbrechen. `RuntimeError` wird jetzt als fehlgeschlagene Postcondition
behandelt. `tests/test_profile_jobs.py`: 72/72 fokussierte Tests bestanden;
Mypy für Profile Jobs, Ruff und `git diff --check` sauber.

## Runde 303: Scheduler-Auth-Quellschlüssel bleibt bei Expand-Fehler stabil

`_shared_direct_auth_accounts()` expandierte einen unbekannten Auth-Override
im Fehler-Fallback ein zweites Mal. Der ursprüngliche `RuntimeError` konnte
dadurch erneut entkommen. Fallback nutzt jetzt den rohen Pfad als stabilen
Schlüssel. `tests/test_scheduler.py`: 198/198 fokussierte Tests bestanden;
Mypy für Scheduler, Ruff und `git diff --check` sauber.

## Runde 304: Bridge-Capture-Zeit toleriert unrepräsentierbare Offsets

`bridge._parse_captured_at()` normalisierte ISO-Zeitstempel mit Offset direkt
per `astimezone()`. Randwerte außerhalb des `datetime`-Bereichs ließen dabei
`OverflowError` entkommen. Strikte Eingabe liefert jetzt kontrolliert
`ValueError("invalid capture timestamp")`; nicht-strikte Eingabe fällt auf
Empfangszeit zurück. `tests/test_bridge.py`: 258/258 fokussierte Tests
bestanden; Mypy für Bridge, Ruff und `git diff --check` sauber.

## Runde 305: Spark-Health-Status fängt unrepräsentierbare Zeitstempel

`spark_health_status()` berechnete das Alter eines gespeicherten Health-
Zeitstempels ohne Fehlergrenze. Randständige Offset-Werte konnten bei UTC-
Normalisierung roh mit `OverflowError` abbrechen. Der Status fällt jetzt auf
`invalid_spark_health_record` zurück. `tests/test_spark_health.py`: 25/25
fokussierte Tests bestanden; Mypy für Spark Health, Ruff und
`git diff --check` sauber.

## Runde 306: Browser-Diagnose fängt Auth-Zeitformatierungsgrenzen

`browser._format_datetime()` normalisierte Auth-Metadaten ohne Schutz vor
unrepräsentierbaren UTC-Konvertierungen. Extremwerte aus `auth.json` konnten
Diagnoseausgabe mit `OverflowError` abbrechen. Die Funktion liefert jetzt
`None`. `tests/test_browser_profile.py`: 126/126 fokussierte Tests bestanden;
Mypy für Browser, Ruff und `git diff --check` sauber.

## Runde 307: Consumption validiert Offset-Konvertierung der Referenzzeit

`consumption._require_aware()` akzeptierte timezone-aware Randwerte, ließ aber
`astimezone(UTC)` bei `datetime.min/max` mit großem Offset roh scheitern. Die
Bereichsprüfung liefert jetzt `ValueError("now is out of range")`.
`tests/test_consumption.py`: 28/28 fokussierte Tests bestanden; Mypy für
Consumption, Ruff und `git diff --check` sauber.

## Runde 308: Vollsuite nach Boundary-Härtungen

Die Vollsuite bestätigt den aktuellen HEAD: `2687 bestanden, 1 übersprungen,
1 Warnung` in 87,57 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. Bridge-/Spark-Health-/Browser-/Consumption-
Zeitgrenzen sowie Profiljob- und Scheduler-Pfade sind integriert grün.

## Runde 309: Extractor-Resetzeit toleriert unrepräsentierbare Capture-Zeiten

`extractor._parse_time_today_or_next()` normalisierte timezone-aware
Capture-Zeiten außerhalb eines Fehlerfangs. Randwerte mit großem Offset
konnten Reset-Ermittlung per `OverflowError` abbrechen. Lokale Zeitbildung
und Folgetagaddition liefern jetzt `None` bei unrepräsentierbaren Werten.
`tests/test_extractor.py`: 197/197 fokussierte Tests bestanden; Mypy für
Extractor, Ruff und `git diff --check` sauber.

## Runde 310: Spark-Health-Schreiben validiert UTC-Darstellbarkeit

`set_spark_health()` prüfte timezone-awareness, serialisierte `now` aber ohne
Bereichsfang. Randwerte mit großem Offset konnten beim Schreiben roh mit
`OverflowError` scheitern. Das Schreiben liefert jetzt
`ValueError("spark health timestamp is out of range")`. `tests/test_spark_health.py`:
27/27 fokussierte Tests bestanden; Mypy für Spark Health, Ruff und
`git diff --check` sauber.

## Runde 311: Config-Test-Home validiert expliziten Profilpfad

`add_or_update_account(test_home=True)` expandierte einen expliziten
`profile_dir` beim Bau des kanonischen Auth-Pfads vor der zentralen
Pfadvalidierung. Unbekanntes `~user` konnte dadurch roh mit `RuntimeError`
abbrechen. Expansion liefert jetzt kontrolliertes
`ValueError("profile_dir must be an absolute path")`. `tests/test_config.py`:
116/116 fokussierte Tests bestanden; Mypy für Config, Ruff und
`git diff --check` sauber.

## Runde 312: Browser-Auth-Diagnose validiert unbekannte Pfade

`_diagnose_auth_json()` expandierte explizite Auth-Pfade ungefangen; auch
`diagnose_account()` expandierte konfigurierte Pfade vor der Diagnose. Ein
unbekanntes `~user` konnte die komplette Diagnose abbrechen. Der Diagnose-
Helper liefert jetzt strukturierten Fehler statt Exception. `tests/test_browser_diagnose.py`:
40/40 fokussierte Tests bestanden; Mypy für Browser, Ruff und
`git diff --check` sauber.

## Runde 313: Scheduler-Account-Authpfade expandieren geschützt

`_shared_direct_auth_accounts()` expandierte konfigurierte Authpfade bereits
beim Aufbau des lokalen Pfads, bevor der Fehler-Fallback griff. Unbekanntes
`~user` konnte die Mehrfachkonto-Prüfung roh abbrechen. Pfadobjekt bleibt jetzt
bis zum geschützten Expand/Resolve-Block unverändert. `tests/test_scheduler.py`:
199/199 fokussierte Tests bestanden; Mypy für Scheduler, Ruff und
`git diff --check` sauber.

## Runde 314: Vollsuite nach Config-/Diagnose-/Scheduler-Härtung

Die Vollsuite bestätigt den aktuellen HEAD: `2694 bestanden, 1 übersprungen,
1 Warnung` in 89,53 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. Config-Test-Home, Browser-Auth-Diagnose und
Scheduler-Auth-Quellpfade sind integriert grün.

## Runde 315: Reactivation-Executable validiert Home-Expansion

`reactivate._resolve_executable()` expandierte einen expliziten Executable-
Pfad ohne `RuntimeError`-Schutz. Unbekanntes `~user` konnte Reaktivierung roh
abbrechen. Der Pfad liefert jetzt kontrolliertes `ReactivationError`.
`tests/test_reactivate.py`: 62/62 fokussierte Tests bestanden; Mypy für
Reactivation, Ruff und `git diff --check` sauber.

## Runde 316: Vollsuite nach Reactivation-Executable-Härtung

Die Vollsuite bestätigt den aktuellen HEAD: `2695 bestanden, 1 übersprungen,
1 Warnung` in 95,07 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. Die Reactivation-Executable-Grenze ist integriert
grün.

## Runde 317: History-Zeitwerte schützen fehlerhafte Zeitzonen-Callbacks

`history._iter_usage_samples()` rief `tzinfo.utcoffset()` bei Capture- und
Reset-Zeitwerten ungefangen auf. Ein fehlerhaftes `tzinfo` konnte History-
Erzeugung mit einem beliebigen Callback-Fehler abbrechen. Eine zentrale
`_is_aware()`-Prüfung fängt solche Callbacks; ungültige `values_captured_at`
fallen auf `captured_at` zurück, ungültige Reset-Zeiten werden verworfen und
`UsageSample` normalisiert den Fehler zu `ValueError`. `tests/test_history.py`:
76/76 fokussierte Tests bestanden; Vollsuite: `2697 bestanden, 1 übersprungen,
1 Warnung` in 91,03 s. Ruff, Mypy und `git diff --check` sauber.

## Runde 318: Consumption-Zeitprüfung schützt fehlerhafte Zeitzonen

`consumption._require_aware()` ließ einen Fehler aus `tzinfo.utcoffset()` als
rohen Callback-Fehler entkommen. Die Awareness-Prüfung fängt jetzt beliebige
Zeitzonenfehler; die API liefert kontrolliert `ValueError`. `tests/test_consumption.py`:
29/29 fokussierte Tests bestanden; Ruff und Mypy sauber.

## Runde 319: State-Lokalisierung schützt fehlerhafte Zeitzonen

`state._localize_datetime()` behandelte nur `None`-Offsets, nicht aber einen
fehlerhaften `utcoffset()`-Callback. Solche Werte werden jetzt wie naive Zeiten
in `LOCAL_TZ` lokalisiert, damit Merge-/Expiry-Pfade fail-closed weiterlaufen.
`tests/test_state.py`: 264/264 fokussierte Tests bestanden; Ruff und Mypy sauber.

## Runde 320: Health-Clock schützt fehlerhafte Zeitzonen

`health.record_health_event()` fing Fehler aus `now.utcoffset()` und
`astimezone()` nicht vollständig ab. Die optionale Uhrzeit fällt bei jedem
gewöhnlichen Callback-Fehler auf sichere UTC-Systemzeit zurück.
`tests/test_health.py`: 32/32 fokussierte Tests bestanden; Ruff und Mypy sauber.

## Runde 321: Routing-Zeitpfade fail-closed bei Zeitzonenfehlern

Routing-Alters-, Reset- und Spark-Health-Berechnungen sowie Awareness- und
Zeittext-Helfer fingen fehlerhafte `tzinfo`-Callbacks bisher nur teilweise.
Die betroffenen Boundary-Fänge normalisieren jetzt solche Fehler zu
`usage_timestamp_invalid`, `False` oder `None`. `tests/test_routing.py`:
125/125 fokussierte Tests bestanden; Ruff und Mypy sauber.

## Runde 322: Vollsuite nach Zeitzonen-Callback-Härtungen

Die Vollsuite bestätigt den gemeinsamen Stand: `2702 bestanden, 1 übersprungen,
1 Warnung` in 91,78 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. History-, Consumption-, State-, Health- und
Routing-Zeitgrenzen sind integriert grün.

## Runde 323: Snapshot-Zeitwert nutzt gehärtete State-Lokalisierung

`state._saved_datetime()` prüfte `tzinfo.utcoffset()` beim Snapshot-Schreiben
ungefangen. Der Helper verwendet jetzt dieselbe sichere Lokalisierung wie
Expiry-Pfade; fehlerhafte Zeitzonen werden lokalisiert statt roh weitergereicht.
`tests/test_state.py`: 265/265 fokussierte Tests bestanden; Ruff und Mypy sauber.

## Runde 324: Usage-Limit-Reset schützt fehlerhafte Capture-Zeitzonen

`usage_limits._reset_at()` fing Fehler aus `captured_at.astimezone()` nur mit
Standardtypen. Relative Resetwerte mit fehlerhaftem `tzinfo` werden jetzt
kontrolliert verworfen. `tests/test_usage_limits.py`: 124/124 fokussierte Tests
bestanden; Ruff und Mypy sauber.

## Runde 325: Spark-Health-Clock schützt fehlerhafte Zeitzonen

Spark-Health-Status und -Schreiben fangen jetzt beliebige Fehler aus
`utcoffset()`/`astimezone()` der optionalen Clock. Ergebnis bleibt
`invalid_health_clock` beziehungsweise kontrolliertes `ValueError`.
`tests/test_spark_health.py`: 28/28 fokussierte Tests bestanden; Ruff und Mypy
sauber.

## Runde 326: Scheduler-Blockstatus verwirft fehlerhafte Reset-Zeitzonen

`scheduler._block_state()` ließ einen fehlerhaften Reset-Zeitzonen-Callback
entkommen. Der Reset gilt jetzt als unbekannt und blockiert mit erklärtem
Fail-Closed-Grund. `tests/test_scheduler.py`: 200/200 fokussierte Tests
bestanden; Ruff und Mypy sauber.

## Runde 327: Render-Authwert schützt fehlerhafte Ablauf-Zeitzonen

`render._auth_value()` fängt Fehler aus `expiry.utcoffset()` jetzt vollständig;
ungültige Auth-Abläufe bleiben als `-` verborgen. `tests/test_render.py`:
63/63 fokussierte Tests bestanden; Ruff und Mypy sauber.

## Runde 328: Extractor-Zeitparser fail-closed bei fehlerhaften Zeitzonen

Extractor-Parser und relative Resetberechnung fangen jetzt beliebige Fehler aus
Capture-Zeitzonen; `_display_timezone()` nutzt bei fehlerhaftem Offset lokale
Zone, und Parser geben `None` zurück. `tests/test_extractor.py`: 198/198
fokussierte Tests bestanden; Ruff und Mypy sauber.

## Runde 329: Integration-Snapshot verwirft fehlerhafte UTC-Zeitwerte

`integration_snapshot._utc_text()` normalisiert Fehler aus `utcoffset()` und
`astimezone()` jetzt zu `IntegrationInvalidSource`, statt Callback-Fehler nach
außen zu geben. `tests/test_integration_snapshot.py`: 49/49 fokussierte Tests
bestanden; Ruff und Mypy sauber.

## Runde 330: Auth-Migrationsplan schützt fehlerhafte Erstellungszeitzonen

`profile_migration._validate_migration_plan()` normalisiert beliebige Fehler
aus der `created_at`-Zeitzone zu `ValueError("migration plan is invalid")`.
`tests/test_profile_migration.py`: 33/33 fokussierte Tests bestanden; Ruff und
Mypy sauber.

## Runde 331: Vollsuite nach Zeit-/Zeitzonen-Boundary-Sweep

Die Vollsuite bestätigt den gemeinsamen Stand: `2710 bestanden, 1 übersprungen,
1 Warnung` in 92,74 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. Snapshot-, Usage-Limit-, Spark-Health-, Scheduler-,
Render-, Extractor-, Integration- und Migrations-Zeitpfade sind integriert grün.

## Runde 332: Dynamic-Series-Tabelle ignoriert kaputte Settings-Rows

`DynamicSeriesList._active_owners()` und `_series_options_for()` griffen bei
gekürzten oder nicht-listartigen externen Tabellenrows ungefangen per Index zu.
Malformed Rows werden jetzt ignoriert; gültige aktive Serien bleiben sichtbar.
`tests/test_dynamic_series_list.py`: 8/8 Tests bestanden; Ruff sauber. Mypy ist
für dieses Cinnamon-Modul wegen nicht installierter externer
`JsonSettingsWidgets`-/`TreeListWidgets`-Imports nicht ausführbar.

## Runde 333: Vollsuite nach Dynamic-Series-Settings-Härtung

Die Vollsuite bestätigt den gemeinsamen Stand: `2712 bestanden, 1 übersprungen,
1 Warnung` in 95,10 s. Die Warnung bleibt externe PyGObject-Deprecation
außerhalb des Repositories. Dynamic-Series-Settings und alle vorherigen Zeit-
und Pfadgrenzen sind integriert grün.

## Runde 334: Formatierungen als eine auswählbare Tabelle

Cinnamon verwirft `custom`-Settings bei einem normalen `bindProperty`; dadurch
blieben `fast-mode-icon` und `account-backends` ungebunden. Beide Settings lesen
und beobachten jetzt ihre Werte über einen sicheren Custom-Binding-Pfad.

Die aktive Settings-Struktur hat nun eine einzelne Page `Formatierungen`. Ein
zentriertes Dropdown schaltet innerhalb einer `Gtk.Stack` genau eine Tabelle
sichtbar; der zuletzt gewählte Tabellenschlüssel wird über JSON persistiert.
Die sechs Formatierungs- und Anzeigetabellen bleiben editierbar, ohne vertikal
ineinander zu laufen. Die Zieltabelle enthält zusätzlich `Verbrauch 5h` und
`Verbrauch 30d`; beide Ziele werden unabhängig in Request-, Panel- und
Renderpfaden ausgewertet. Das alte globale Baseline-Element 13 bleibt verworfen.

`pytest -q tests/test_applet.py tests/test_format_table_selector.py`: 31/31
bestanden. `node --test tests/applet_runtime.test.js`: 394/394 bestanden. Ruff,
Python-Kompilierung, JSON-Prüfung und `git diff --check` sauber.

## Runde 335: Tabellenwechsel ohne Crossfade-Überlagerung

Der Tabellen-Selector blendete beim Wechsel per `Gtk.Stack` über `CROSSFADE`
kurz alte und neue Tabelle gleichzeitig. Das widerspricht der Vorgabe „nur eine
Tabelle“ und erzeugt unnötige Animation. Der Stack schaltet jetzt ohne Übergang
auf genau ein sichtbares Kind. Konstruktor-/Selector-Regressionstest prüft
Transition-Typ und Initialauswahl; fokussierte Python-Suite: 32/32 bestanden.

## Runde 336: 5h-/30d-Rendering getrennt geprüft

Der Runtime-Test deckt zusätzlich ab, dass `consumption-short` und
`consumption-monthly` bei gleicher Verbrauchszeile unabhängig stylen: ein
deaktiviertes 5h-Ziel bleibt ungestylt, während ein aktiviertes 30d-Ziel
Markup erhält. `node --test tests/applet_runtime.test.js`: 395/395 bestanden.

## Runde 337: Browser-Installation dokumentiert

Der laufende Service meldete fehlende Playwright-Browser-Binaries, weil die
Installationsanleitung nur Chromium installierte, obwohl Firefox Standard und
beide Browser konfigurierbar sind. README installiert jetzt beide unterstützten
Playwright-Browser und beschreibt den Login korrekt als konfigurierten Browser.
Keine automatische Netzwerkinstallation wurde ausgeführt.

## Runde 338: App-Server-Vertrag geprüft

`src/codex_usage/app_server.py` wurde bounded auf Auth-Kontext, JSON-RPC-
Request/Response, Rate-Limit-Fenster, Modellkatalog und Prozess-/Reader-
Cleanup geprüft. Kein reproduzierbarer Repository-Fehler gefunden; der
laufende Service liefert für die `app-server`-Konten weiterhin gültige
5h-/Wochenwerte und Modellkatalogdaten. Die vorhandene Fail-Closed-Behandlung
für malformed payloads und Prozessgrenzen bleibt unverändert.

`pytest -q tests/test_app_server.py`: 98/98 bestanden. Coverage für das Modul:
83 % Branch-inklusive; Ruff, Python-Kompilierung und `git diff --check` sauber.

## Runde 339: Usage-Limit-Parser geprüft

`usage_limits.py` wurde für WHAM-/App-Server-Fenster, dynamische Dauer-
Identitäten, Spark-Duplikate, Modellkatalog und Reset-/Control-Flags geprüft.
Kein reproduzierbarer Parserfehler gefunden. Ein zusätzlicher Fuzz-Pass mit
5.000 JSON-artigen Payloads ergab keine ungefangene Exception.

`pytest -q tests/test_usage_limits.py`: 124/124 bestanden. Coverage für das
Modul: 91 % Branch-inklusive; Ruff und `git diff --check` sauber.

## Runde 340: Scheduler schützt malformed Account-Authpfade

`_shared_direct_auth_accounts()` rief bei einem nicht-stringartigen
`Account.auth_json_path` ungefangen `Path(...)` auf. `fetch_all()` brach dadurch
vor dem per-Account-Fehlerpfad mit `TypeError` ab. Solche Werte erhalten jetzt
einen isolierten internen Quellen-Schlüssel und laufen kontrolliert in den
bestehenden `LOGIN_REQUIRED`-/Usage-Fehlerpfad.

Regressionstest ergänzt. `pytest -q tests/test_scheduler.py`: 201/201
bestanden. Mypy für `scheduler.py`, Ruff und `git diff --check` sauber.

## Runde 341: Browser-Laufzeitpfad geprüft

`browser.py` behandelt fehlende Playwright-Binaries kontrolliert als
Account-Fehler. Ein automatischer Chromium-Fallback wäre hier falsch, weil
Browserwahl und persistente Profile konfigurationsabhängig sind. Laufzeitcheck:
Chromium-Binary vorhanden, Firefox-Binary fehlt weiterhin extern.

`pytest -q tests/test_browser_profile.py tests/test_browser_diagnose.py`:
166/166 bestanden. Ruff und `git diff --check` sauber.

## Runde 342: State-Expiry verwirft Modellfehler nicht mehr still

`expire_reset_windows()` entfernte malformed Model-Pools, ließ den Account
dabei aber als `OK` und frisch erscheinen. Ungültige Pool-/Fensterstrukturen
werden jetzt als `PARTIAL`, `stale` und mit `model pool catalog invalid`
markiert; reguläres Ablaufverhalten eines ausschließlich abgelaufenen Spark-
Pools bleibt unverändert.

Regressionstest ergänzt. `pytest -q tests/test_state.py`: 265/265 bestanden.
Mypy für `state.py`, Ruff und `git diff --check` sauber.

## Runde 343: Reload nach State-Fix geprüft

Service-Neustart nach `990a107` lädt neuen Code; Journal zeigt weiterhin
gültige `app-server`-Konten ohne State-/Parserfehler. Der Run endet bei den
Browser-Konten mit dem bereits bekannten externen Fehler: Firefox-Playwright-
Executable fehlt. Keine automatische Netzwerkinstallation ausgeführt.

## Runde 344: Render-Übersicht schützt malformed Pfade

`render_account_overview()` konnte bei nicht-stringartigen `profile_dir`- oder
`auth_json_path`-Werten mit `TypeError` aus `Path(...)` abbrechen. Die drei
lokalen Pfad-Helfer zeigen jetzt kontrolliert `ungültig` beziehungsweise den
rohen Wert an.

Regressionstest ergänzt. `pytest -q tests/test_render.py`: 65/65 bestanden.
Mypy für `render.py`, Ruff und `git diff --check` sauber.

## Runde 345: Usage-Reset-DTOs erneut geprüft

`usage_resets.py` wurde auf widersprüchliche Legacy-/kanonische Formen,
Grenzwerte, Boolean-als-Integer und ungültige Redemption-Zustände geprüft.
Kein reproduzierbarer Fehler gefunden; Parser und Formatter bleiben
fail-closed. 84 JSON-artige Einzelpayloads erzeugten keine ungefangene
Exception.

`pytest -q tests/test_usage_resets.py`: 5/5 bestanden. Mypy für
`usage_resets.py`, Ruff und `git diff --check` sauber.

## Runde 346: Routing-Entscheidungs-DTO typisiert

`evaluate_routing()` baute `base` ohne expliziten Mapping-Typ. Mypy inferierte
dadurch ein zu enges Union-Mapping und meldete beim späteren Einfügen des
`spark_health`-DTOs einen Typfehler in Zeile 288. `base` ist jetzt explizit
`dict[str, Any]`; Laufzeitverhalten bleibt unverändert.

`pytest -q tests/test_routing.py`: 125/125 bestanden. Mypy für `routing.py`,
Ruff und `git diff --check` sauber.

## Runde 347: Reload nach Routing-Typfix

Service-Neustart nach `cbdcd2a` lädt den neuen Routing-Code. Journal zeigt
gültige `app-server`-Ergebnisse inklusive Reset-/Limitdaten; der Lauf endet
wie zuvor ausschließlich an den Browser-Konten mit fehlender Firefox-
Playwright-Executable. Keine automatische Netzwerkinstallation ausgeführt.

## Runde 348: Snapshot-Serializer fängt Mapping-Callback-Fehler

`serialize_schema1_document()` ließ Exceptions aus einem formal gültigen, aber
fehlerhaften `Mapping`-Callback (z. B. `RuntimeError` aus `.get()`) ungefangen
nach außen. An dieser untrusted-DTO-Grenze werden solche Fehler jetzt in den
vorgesehenen `IntegrationInvalidSource`-Fehler übersetzt; bestehende
`IntegrationSnapshotError`-Typen bleiben unverändert.

Regressionstest ergänzt. `pytest -q tests/test_integration_snapshot.py`:
50/50 bestanden. Mypy für `integration_snapshot.py`, Ruff und
`git diff --check` sauber.

## Runde 349: Reload nach Snapshot-Serializer-Fix

Service-Neustart nach `a43027b` verarbeitet die `app-server`-Konten weiter
ohne Integration-/Parserfehler. Status 2 kommt weiterhin nur von den
Browser-Konten mit fehlender Firefox-Playwright-Executable; keine automatische
Netzwerkinstallation ausgeführt.

## Runde 350: Integration-Entrypoint geprüft

`integration_entrypoint.py` wurde bounded auf exakte Argumente, XDG-Pfade,
Verifier-Reihenfolge, Exitcode-/Token-Abbildung, UTC-Normalisierung und
History-Cost-Fenster geprüft. Kein neuer reproduzierbarer Fehler gefunden;
Fehlerdetails bleiben aus stdout/stderr ausgeschlossen.

`pytest -q tests/test_integration_entrypoint.py`: 26/26 bestanden. Mypy für
`integration_entrypoint.py`, Ruff und `git diff --check` sauber.

## Runde 351: Formatierungs-Selector und Einzeltabelle geprüft

`FormatTableSelector` wurde bounded auf deklarierte Tabellen, unbekannte
Auswahlwerte, Fallback beim Settings-Reload, persistierte Auswahl und
`Gtk.StackTransitionType.NONE` geprüft. Aktive Schema-Seite enthält weiterhin
eine zentrale Auswahl und genau eine sichtbare Tabelle; kein neuer GUI-Fehler
reproduziert.

`pytest -q tests/test_format_table_selector.py tests/test_applet.py`:
32/32 bestanden. Ruff, Python-Kompilierung und `git diff --check` sauber;
Warnungen sind bekannte PyGObject-/GTK-Deprecations.

## Runde 352: Verbrauchsberechnung geprüft

`consumption.py` wurde auf Lookback-/Baseline-Grenzen, Reset-Erkennung,
Lücken-/Stale-Coverage, Forecast-Obergrenze, EMA-Smoothing und ungültige
Parameter geprüft. Kein neuer reproduzierbarer Fehler gefunden; `UsageSample`
validiert Zeitstempel bereits beim Erzeugen.

`pytest -q tests/test_consumption.py`: 29/29 bestanden. Mypy für
`consumption.py`, Ruff und `git diff --check` sauber.

## Runde 353: History-Exporter typisiert malformed Window-Namen

`history.py` gab beim Fallback für einen nicht-stringartigen
`LimitWindow.name`-Wert `None` an `dict.get()` weiter. Runtime war der Pfad
bereits fail-closed, Mypy meldete aber einen echten Typfehler. Der Fallback
nutzt jetzt den semantisch identischen leeren String.

`pytest -q tests/test_history.py`: 76/76 bestanden. Mypy für `history.py`,
Ruff und `git diff --check` sauber.

## Runde 354: Reload nach History-Typfix

Service-Neustart nach `18183fd` ist aktiv (`codex-usage.service`, Exit 0 beim
Start). Journal enthält im Prüfintervall keine History-/Parser-/Browserfehler.

## Runde 355: Spark-Health-TTL und Persistenz geprüft

`spark_health.py` wurde auf Backend-ID-Validierung, UTC-/DST-Zeitgrenzen,
Stale-TTL, failed-vs-healthy-Zustände, JSON-/Dateirechte und bounded
Record-Rotation geprüft. Kein neuer reproduzierbarer Fehler gefunden;
malformed Records bleiben unbekannt beziehungsweise fail-closed.

`pytest -q tests/test_spark_health.py`: 28/28 bestanden. Mypy für
`spark_health.py`, Ruff und `git diff --check` sauber.

## Runde 356: Model-DTOs und Serialisierung geprüft

`models.py` wurde auf Fensteridentität, Prozent-/Zahlenvalidierung,
Pool-Verfügbarkeit, Exhaustion, Legacy-Fenster, Modellauflösung und JSON-
Serialisierung geprüft. 204 manipulierte DTO-Feldkombinationen erzeugten
keine ungefangene Exception; kein neuer reproduzierbarer Fehler.

`pytest -q tests/test_models.py`: 14/14 bestanden. Mypy für `models.py`,
Ruff und `git diff --check` sauber.

## Runde 357: Private-I/O-Locks und Atomic Writes geprüft

`private_io.py` wurde auf Pfadtypen, Symlink-Ancestors, geschützte Verzeichnisse,
Owner-/Link-/Mode-Prüfungen, `O_NOFOLLOW`, Lock-Timeouts, Atomic Replace,
Create-Only-Rollback und fsync geprüft. Kein neuer reproduzierbarer Fehler.

`pytest -q tests/test_private_io.py`: 32/32 bestanden. Mypy für
`private_io.py`, Ruff und `git diff --check` sauber.

## Runde 358: Strict-JSON-Parser geprüft

`json_utils.py` wurde auf Eingabetypen, Bytearray-Verarbeitung, Duplicate Keys,
NaN/Infinity-Konstanten, String-Escapes und maximale Verschachtelung geprüft.
Auch ungültige UTF-8-Bytes werden als `ValueError` (UnicodeDecodeError-
Untertyp) behandelt; kein neuer reproduzierbarer Fehler.

`pytest -q tests/test_json_utils.py`: 8/8 bestanden. Mypy für `json_utils.py`,
Ruff und `git diff --check` sauber.

## Runde 359: Gesamt-Typcheck nach Einzelpasses

Cross-File-Verifikation nach Routing-, Snapshot- und History-Anpassungen:
`mypy src/codex_usage` meldet in allen 35 Quelldateien keine Fehler.
Aggregierter Ruff auf `src/codex_usage` und `git diff --check` sind ebenfalls
sauber.

## Runde 360: Health-Event-Speicher geprüft

`health.py` wurde auf Token-/Account-Redaction, Event-Limit, Retention,
malformed JSON, strict Version, Dateirechte und Recovery geprüft. Kein neuer
reproduzierbarer Fehler gefunden.

`pytest -q tests/test_health.py`: 32/32 bestanden. Mypy für `health.py`, Ruff
und `git diff --check` sauber.

## Runde 616: Scheduler-Blockierung gegen fehlerhafte Resetvergleiche härten

`scheduler._block_state()` prüfte eine Reset-Datetime zwar auf vorhandene
Zeitzone und `utcoffset()`, führte danach `max()`, Gleichheitsvergleich,
`isoformat()` und `<= now` aber außerhalb eines Fehlerfangs aus. Eine
malformed `datetime`-Subclass konnte den Watchdog dadurch mit rohem
`RuntimeError` abbrechen, statt einen sicheren unbekannten Reset zu melden.

Die gesamte Auswahl- und Ausgabephase läuft jetzt durch einen begrenzten
`Exception`-Guard. Bei fehlerhaftem Vergleich werden Blockierungszeit und
Resetfreigabe verworfen; der Account bleibt mit „reset time unknown“ fail-closed.
Regression nutzt eine Datetime-Subclass mit fehlerhaftem `__le__`.
`pytest -q tests/test_scheduler.py`: 206/206; Ruff, Mypy, Python-Kompilierung
und `git diff --check` sauber.

## Runde 361: Config-Pfade und Account-Identitäten geprüft

`config.py` wurde auf XDG-/Tilde-/`file:`-Pfade, private Verzeichnisse,
Account-/Label-/Series-/Browser-/Backend-Typen, Ressourcen-Duplikate,
Auth-Pfade und Rollback-Grenzen geprüft. Kein neuer reproduzierbarer Fehler.

`pytest -q tests/test_config.py`: 116/116 bestanden. Mypy für `config.py`,
Ruff und `git diff --check` sauber.

## Runde 362: Auth-Profile-Migration geprüft

`profile_migration.py` wurde auf Source-Klassifikation, Manifest-/Target-
Disjointness, JSON-/Secret-Validierung, private Rechte, Atomic Apply und
Rollback-Identitäten geprüft. Kein neuer reproduzierbarer Fehler gefunden.

`pytest -q tests/test_profile_migration.py`: 33/33 bestanden. Mypy für
`profile_migration.py`, Ruff und `git diff --check` sauber.

## Runde 363: Profile-Layout geprüft

`profile_layout.py` wurde auf canonical `codex-home`/`auth.json`, private
Metadata, Symlink-/Protected-Path-Schutz, Locking und Rollback-Tracking geprüft.
Kein neuer reproduzierbarer Fehler gefunden.

`pytest -q tests/test_profile_layout.py`: 20/20 bestanden. Mypy für
`profile_layout.py`, Ruff und `git diff --check` sauber.

## Runde 364: Preserved Profile-Metadata bleibt privat

`ensure_profile_layout(..., preserve_existing_metadata=True)` akzeptierte
bisher ein vorhandenes `profile.json` mit Gruppen-/Weltzugriff. Das ist
inkonsistent zur privaten Metadata-Erzeugung und konnte sensible Account-
Labels offenlegen. Vor dem Preserve-Return werden jetzt Regular-File,
Single-Link, User-Owner und private Mode geprüft.

Regressionstest ergänzt. `pytest -q tests/test_profile_layout.py
tests/test_profile_migration.py`: 54/54 bestanden. Mypy für
`profile_layout.py`, Ruff und `git diff --check` sauber.

## Runde 365: Reload nach Profile-Metadata-Fix

Service lädt `d88cada`; `app-server`-Konten liefern unverändert valide
Ergebnisse. Exit 2 kommt weiterhin ausschließlich von den Browser-Konten mit
fehlender Firefox-Playwright-Executable. Kein automatischer Fallback und keine
Netzwerkinstallation ausgeführt.

## Runde 366: Profile-Job-Manifeste und Worker-Grenzen

`profile_jobs.py` wurde auf Manifest-Schema, Prozessstart/-Reaping,
Cancel-Races, Worker-Identität, Event-Dateien, Completion-Postconditions und
malformed Worker-DTOs geprüft. Dabei akzeptierte die Manifestvalidierung bisher
beliebige Strings mit abschließendem `Z` als `created_at`/`updated_at`.
Ungültige Zeitwerte konnten dadurch in Statusdaten gelangen.

Die Validierung parst beide Felder jetzt als UTC-ISO-8601 und weist
unparsebare oder timezone-lose Werte zurück. Vier Regressionstests decken beide
Felder und beide Fehlervarianten ab.

`pytest -q tests/test_profile_jobs.py`: 76/76 bestanden. Mypy für
`profile_jobs.py`, Ruff und `git diff --check` sauber.

## Runde 367: Systemd-Service-Status und Rollback-Pfade

`service.py` wurde auf Unit-Ownership, private Pfade, systemd-Ausgabegrenzen,
Timeout/Reaping, Aktivierungs-Rollbacks, Timer-Zustand und Status-DTOs geprüft.
Kein neuer reproduzierbarer Fehler.

`pytest -q tests/test_service.py`: 66/66 bestanden. Mypy für `service.py`,
Ruff und `git diff --check` sauber. Laufender Timer bleibt aktiv/geplant;
letzter One-Shot-Service-Lauf endet weiterhin wegen fehlender Firefox-
Playwright-Executable mit Exit 2.

## Runde 368: Formatierungs-Selector und Applet-GUI erneut verifiziert

Der `FormatTableSelector` zeigt über `Gtk.Stack` genau eine auswählbare Tabelle,
persistiert das Dropdown-Ziel ohne Rückschreibschleife und verwendet keinen
Crossfade. Das aktive Layout referenziert nur die Formatierungsseite; alte
Einzeltabellen bleiben aus dem Seitenbaum entfernt.

`pytest -q tests/test_format_table_selector.py tests/test_applet.py`: 32/32
bestanden. `node --test tests/applet_runtime.test.js`: 395/395 bestanden.
Keine neue reproduzierbare GUI-Regression.

## Runde 369: Profile-Job-Backend-ID mit Worker-Vertrag abgeglichen

`profile_jobs._validate_create_arguments()` erlaubte bisher Leerzeichen in
`expected_backend_account_id`, obwohl `profile_login`, `identity` und
`app_server` diese Identität ablehnen. Solche Jobs wurden erst im Worker
fehlerhaft. Die Job-Grenze nutzt jetzt dieselbe Whitespace-Prüfung und weist
den Auftrag vor Manifest-/Prozessstart zurück.

Zwei Regressionstests ergänzt. `pytest -q tests/test_profile_jobs.py`:
78/78 bestanden. Mypy für `profile_jobs.py`, Ruff und `git diff --check`
sauber.

## Runde 370: Reload nach Profile-Job-Identitätsfix

User-Timer nach `f54df54` neu aktiviert. `app-server`-Konten liefern weiterhin
valide Daten. Service bleibt wegen der zwei direkten Browser-Konten ohne
Firefox-Playwright-Executable bei Exit 2; Timer ist aktiv und geplant. Kein
Fallback und keine Netzwerkinstallation ausgeführt.

## Runde 371: Device-Login und Backend-Identitätshelfer

`profile_login.py` und `identity.py` wurden auf bounded subprocess output,
Stream-/Event-Trennung, Timeout-Reaping, staged-auth-Validierung,
Identitätskonsistenz und malformed Kandidaten geprüft. Kein neuer
reproduzierbarer Fehler.

`pytest -q tests/test_profile_login.py tests/test_identity.py`: 67/67
bestanden. Mypy für beide Module, Ruff und `git diff --check` sauber.

## Runde 372: App-Server-RPC und Usage-DTOs

`app_server.py` wurde auf Auth-Identitätswechsel, RPC-IDs/-Fehler,
bounded Nachrichten und stderr, Timeout-/Prozessgruppen-Reaping,
Modell-ID-Validierung sowie 5h-/Wochenfenster-Mapping geprüft. Kein neuer
reproduzierbarer Fehler; malformed Slots bleiben partiell oder fail-closed.

`pytest -q tests/test_app_server.py`: 98/98 bestanden. Mypy für
`app_server.py`, Ruff und `git diff --check` sauber.

## Runde 373: Usage-Pool-Parser und dynamische Fenster

`usage_limits.py` wurde auf 5h-/Woche-/30d-Identitäten, fehlende oder
unsupported Fensterdauern, Spark-Duplikate, Kontrollflags, Modellkatalog-
Grenzen, Reset-Zeiten und malformed Pool-DTOs geprüft. Kein neuer
reproduzierbarer Fehler; unklassifizierbare Pools bleiben unavailable.

`pytest -q tests/test_usage_limits.py`: 124/124 bestanden. Mypy für
`usage_limits.py`, Ruff und `git diff --check` sauber.

## Runde 374: Usage-Reset-Zustand

`usage_resets.py` wurde auf canonical/legacy Mapping, Konfliktauflösung,
unknown-vs-zero, bounded Werte und Redemption-Gates geprüft. Kein neuer
reproduzierbarer Fehler.

`pytest -q tests/test_usage_resets.py`: 5/5 bestanden. Mypy für
`usage_resets.py`, Ruff und `git diff --check` sauber.

## Runde 375: Browser-Bridge-Ingest und Extension-Grenzen

`bridge.py` wurde auf Token-/Account-Bindung, TLS-/Host-/Port-Grenzen,
Ingest-Identitäten, Capture-Zeitwerte, API-Response-Budgets, Streaming-
Backpressure, Debug-Redaction, Extension-Transaktionen und Rollback geprüft.
Kein neuer reproduzierbarer Fehler.

`pytest -q tests/test_bridge.py`: 258/258 bestanden. Mypy für `bridge.py`,
Ruff und `git diff --check` sauber.

## Runde 376: Scheduler-Backend- und Snapshot-Grenzen

`scheduler.py` wurde auf bounded Account-Iterables, Auth-Pfad-/Identitäts-
Attribution, Direct/App-Server-Fallback, globale Lock-Reihenfolge,
State-Generationen, Reset-Stabilisierung, Snapshot-/History-Rollback und
Watchdog-Blockzustände geprüft. Kein neuer reproduzierbarer Fehler.

`pytest -q tests/test_scheduler.py`: 201/201 bestanden. Mypy für
`scheduler.py`, Ruff und `git diff --check` sauber.

## Runde 377: Integration-Attestation-Testabdeckung

`integration_attestation.py` hat keine eigene Testdatei, wird aber durch
Installer-/Entrypoint-Tests direkt ausgeführt. Coverage-Nachweis mit
`pytest -q tests/test_integration_installer.py tests/test_integration_entrypoint.py`:
alle 17 Funktionen werden mindestens einmal ausgeführt, 310 Statements mit
78% Zeilen- und 100 Branch-Messung; die offenen Zeilen sind ausschließlich
zusätzliche Guard-/Fehlerzweige. Ruff, Mypy und `git diff --check` sauber.

## Runde 378: OAuth-Browser-Testabdeckung

`oauth_browser.py` hat keine eigene Testdatei, wird aber durch den
Reaktivierungs-Testpfad abgedeckt. `pytest -q tests/test_reactivate.py
-k 'oauth_browser'`: 14/14 bestanden; Modul-Coverage 84%, alle sechs
Funktionen mindestens einmal ausgeführt. Ruff, Mypy und `git diff --check`
sauber.

## Runde 379: Browser-Login-Erfolgspfad

`browser.login_account()` hatte bisher nur Validierungs- und indirekte
Fehlerpfadtests. Ein isolierter Test deckt jetzt Profilvorbereitung,
Playwright-Start, Analytics-Navigation, interaktive Eingabe und sicheres
Context-Schließen in Reihenfolge ab; kein echter Browser und kein Netzwerk.

`pytest -q tests/test_browser_diagnose.py tests/test_browser_profile.py`:
167/167 bestanden. Modul-Coverage für `browser.py` stieg auf 80%; offene
Zeilen sind zusätzliche Diagnose-/Fehler- und Guard-Zweige. Ruff, Mypy und
`git diff --check` sauber.

## Runde 380: Consumption-Grenzen und EMA-Pfad

`consumption.py` akzeptierte jeden positiven `stale_after_seconds`-Wert, baute
für sehr große Werte aber ein überlaufendes `timedelta` und warf unerwartet
`OverflowError`. Stale-Berechnung vergleicht jetzt direkt die Dauer in
Sekunden. Regressionstest deckt einen Wert von `10**15` ab. Zusätzlich deckt
ein Test den bisher ungetesteten zeitgewichteten EMA-Forecast ab.

`pytest -q tests/test_consumption.py`: 31/31 bestanden; Modul-Coverage 83%.
Offene Zeilen sind weitere Diagnose-/Guard- und EMA-Sonderzweige. Ruff, Mypy
und `git diff --check` sauber.

## Runde 381: Direct-Response-Header-Grenze

`direct._response_content_type()` griff bei einem malformed Response-Objekt
unmittelbar auf `headers.get()` zu und konnte dadurch `AttributeError` nach
außen geben. Die Funktion prüft jetzt den Header-Accessor und fällt sicher auf
`getheader()` oder leeren Content-Type zurück. Regressionstest deckt beide
Fallbacks ab.

`pytest -q tests/test_direct.py tests/test_live_direct.py`: 183 bestanden,
1 übersprungen. Modul-Coverage für `direct.py` 83%; offene Zeilen sind weitere
Netzwerk-, Auth- und Response-Sonderzweige. Ruff, Mypy und `git diff --check`
sauber.

## Runde 382: Service-Reload nach Direct-Fix

`codex-usage service enable --format json` nach `9aceac3` erfolgreich:
Service installiert/aktiv, Timer aktiv und geplant. Der letzte Lauf endet
weiterhin ausschließlich wegen fehlender Firefox-Playwright-Executable bei
Exit 2; `app-server`-Pfade bleiben unverändert. Kein Fallback und keine
Netzwerkinstallation ausgeführt.

## Runde 383: Terminal-Argumentpfade

`terminal.py` nutzt je Terminal-Emulator unterschiedliche Arbeitsverzeichnis-
und Kommandooptionen. Ein parametrischer Test deckt jetzt alle zehn
Argumentpfade (`gnome-terminal` bis `xterm`) ab und schützt die Account-Codex-
Home-Startgrenzen. Kein neuer reproduzierbarer Fehler.

`pytest -q tests/test_terminal.py`: 23/23 bestanden; Modul-Coverage 87%.
Offene Zeilen sind ausschließlich Start-/Fehler- und Resolver-Sonderzweige.
Ruff, Mypy und `git diff --check` sauber.

## Runde 384: Render-Pipeline

`render.py` wurde auf Tabellenbreiten, dynamische Main-/Spark-Limits,
Provenance-Redaction, Status-/Auth-Anzeige, malformed Zahlen und bounded
Iterables geprüft. Alle 26 Funktionen werden durch Render-/History-CLI-Tests
ausgeführt; kein neuer reproduzierbarer Darstellungsfehler.

`pytest -q tests/test_render.py tests/test_history_cli.py`: 68/68 bestanden;
Modul-Coverage 87%. Ruff, Mypy und `git diff --check` sauber.

## Runde 385: Profil-Layout-Grenzen

`profile_layout.py` wurde auf kanonisches `CODEX_HOME`, private Metadaten,
Symlink-/Hardlink-Grenzen, geschützte Ziele und Rollback-Erfassungen geprüft.
Kein neuer reproduzierbarer Fehler; bestehende Metadaten werden nur nach
strenger Eigentümer-/Mode-Prüfung bewahrt.

`pytest -q tests/test_profile_layout.py`: 21/21 bestanden; Modul-Coverage
73%. Ruff, Mypy und `git diff --check` sauber.

## Runde 386: Private-Write-Mode-Grenze

`private_io.write_private_text()` akzeptierte bisher beliebige Datei-Modi.
Ein versehentlicher Aufruf mit `0o640` hätte private Inhalte group-lesbar
geschrieben. Der Helper akzeptiert jetzt nur owner-only-Modi (`0o000` bis
`0o700` ohne Gruppen-/Welt-/Sonderbits); vier Regressionstests decken
Gruppenbits, Bool, String und negative Werte ab.

`pytest -q tests/test_private_io.py`: 36/36 bestanden. Abhängige
Konfigurations-, Profil-Layout- und Reaktivierungs-Tests: 235/235 bestanden.
Modul-Coverage 75%; Ruff, Mypy und `git diff --check` sauber.

## Runde 387: Service-Reload nach Private-I/O-Fix

`codex-usage service enable --format json` nach `58e08da` erfolgreich:
Service installiert/aktiv, Timer aktiv und geplant. Exit 2 bleibt der bekannte
Firefox-Playwright-Executable-Fehler der direkten Browser-Konten; Journal zeigt
keinen neuen Private-I/O-Fehler. Kein Fallback und keine Netzwerkinstallation.

## Runde 388: Integration-Snapshot-Eigentümergrenzen

`integration_snapshot.py` prüfte bei Integrationsverzeichnis und bestehendem
Cache bereits Pfad, Typ, Modus und Hardlink-Anzahl, verlangte aber nicht den
Eigentümer des aktuellen Users. Beide Prüfungen verlangen jetzt zusätzlich
`st_uid == os.getuid()`; zwei Regressionstests decken fremdes Verzeichnis und
fremde Cache-Datei ab.

`pytest -q tests/test_integration_snapshot.py`: 52/52 bestanden;
Modul-Coverage 76%. Ruff, Mypy und `git diff --check` sauber.

## Runde 389: Integration-Installer-Lesegrenze

`integration_installer._read_nofollow()` prüfte bisher regulären Dateityp,
Hardlink-Anzahl und Größenlimit, aber nicht den Eigentümer. Gelesene Source-,
Wheel- und Release-Artefakte müssen dem aktuellen User gehören; der Guard
verlangt jetzt zusätzlich `st_uid == os.getuid()`. Regressionstest deckt eine
fremde Datei ab.

`pytest -q tests/test_integration_installer.py`: 106/106 bestanden;
Modul-Coverage 83%. Ruff, Mypy und `git diff --check` sauber.

## Runde 390: Integration-Entrypoint-Clock-Fehler

`integration_entrypoint._require_aware_utc()` normalisierte Fehler aus
`utcoffset()`, ließ aber eine Exception aus `datetime.astimezone()` als
unbekannten Fehler bis Exit 69 durch. Beide Normalisierungsschritte liefern
jetzt `ValueError`, damit fehlerhafte Clock-Daten konsistent Exit 70 ergeben;
Regressionstest deckt den Astimezone-Fehler ab.

`pytest -q tests/test_integration_entrypoint.py`: 27/27 bestanden;
Modul-Coverage 91%. Ruff, Mypy und `git diff --check` sauber.

## Runde 391: Strict-JSON-Helfer

`json_utils.py` auf Eingabetypen, String-Scanner, Nesting-Limit,
Duplicate-Key- und Konstantenbehandlung geprüft. Keine neue reproduzierbare
Fehlfunktion; bestehende Fehler werden als `ValueError` normalisiert und die
Aufrufer begrenzen ihre Rohdaten separat.

`pytest -q tests/test_json_utils.py`: 8/8 bestanden; Modul-Coverage 93%.
Ruff, Mypy und `git diff --check` sauber.

## Runde 392: History-Reset-Zeitzonenfehler

`history._iter_usage_samples()` behandelte `utcoffset()`-Fehler defensiv, ließ
aber eine Exception aus `reset_at.astimezone()` aus dem Main- oder Credit-Pfad
entkommen. Beide Fensterpfade überspringen solche malformed Reset-Zeitzonen
jetzt kontrolliert; parametrischer Regressionstest deckt beide ab.

`pytest -q tests/test_history.py`: 78/78 bestanden; Modul-Coverage 81%.
Ruff, Mypy und `git diff --check` sauber.

## Runde 393: Service-Reload nach History-/Entrypoint-Fixes

`codex-usage service enable --format json` nach `f0ee977` erfolgreich:
Service installiert/aktiv, Timer aktiv und geplant. Der Lauf endet weiterhin
mit Exit 2 ausschließlich wegen fehlender Firefox-Playwright-Executable bei
Browser-Konten; Journal zeigt keinen neuen History-/Entrypoint-Fehler. Keine
Netzwerkinstallation und kein Fallback ausgeführt.

## Runde 394: Account-Lock-Eigentümergrenze

`account_lock()` prüfte bei bestehender Lockdatei bisher Typ und Hardlink-Anzahl,
aber nicht `st_uid`. Eine fremde, schreibbare Datei konnte dadurch bis
`fchmod()` gelangen und ein rohes `OSError` auslösen. Der Guard verlangt jetzt
aktuellen User-Eigentümer und liefert konsistent `AccountLockError`; Regressionstest
deckt fremde Lockdatei ab.

`pytest -q tests/test_account_lock.py`: 15/15 bestanden; Modul-Coverage 84%.
Ruff, Mypy und `git diff --check` sauber.

## Runde 395: Spark-Health-Datei

`spark_health.py` auf Backend-ID-/Reason-Validierung, private Datei, Record-
Rotation, Timestamp-/Stale-Grenzen und JSON-Fehlerpfade geprüft. Keine neue
reproduzierbare Fehlfunktion; fremde, gruppenlesbare, hardgelinkte und
übergroße/verschachtelte Health-Daten fallen auf `unknown` zurück.

`pytest -q tests/test_spark_health.py`: 28/28 bestanden; Modul-Coverage 89%.
Ruff, Mypy und `git diff --check` sauber.

## Runde 396: Backend-Identity-Auswahl

`identity.py` auf ID-/Plan-Typ-Validierung, Kandidaten-Bounds, URL-Prüfung,
Priorisierung und Account-Konsistenz geprüft. Keine neue reproduzierbare
Fehlfunktion; fremde oder mehrdeutige Backend-Gruppen werden weiter verworfen
oder als Fehler gemeldet.

`pytest -q tests/test_identity.py`: 28/28 bestanden; Modul-Coverage 81%.
Ruff, Mypy und `git diff --check` sauber.

## Runde 397: Extractor-Pipeline

`extractor.py` auf Kandidaten-/Text-Iterator-Bounds, JSON-Walk-Tiefe,
HTML-Hidden-Progress, Fensterpriorisierung, Prozent-/Zählerkonflikte und
Timestamp-/Zeitzonenfehler geprüft. Keine neue reproduzierbare Fehlfunktion;
mehrdeutige oder malformed Quellen bleiben verworfen bzw. liefern nur
unabhängige Reset-Metadaten.

`pytest -q tests/test_extractor.py`: 198/198 bestanden; Modul-Coverage 91%.
Ruff, Mypy und `git diff --check` sauber.

## Runde 398: Health-Event-Speicher

`health.py` auf Token-/Account-Redaktion, private Datei, JSON-Recovery,
Retention, Event-Limit und malformed Clock-/Version-/Hardlink-Pfade geprüft.
Keine neue reproduzierbare Fehlfunktion; ungültige Health-Daten werden
verworfen oder durch sicheren Recovery-Schreibpfad ersetzt.

`pytest -q tests/test_health.py`: 32/32 bestanden; Modul-Coverage 83%.
Ruff, Mypy und `git diff --check` sauber.

## Runde 399: Usage-Reset-State

`usage_resets.py` auf kanonische/legacy Payloads, Konfliktauflösung,
Bool-/Bereichsgrenzen, Anzeige und Redemption-Gate geprüft. Keine neue
reproduzierbare Fehlfunktion; unbekannte oder widersprüchliche Reset-Werte
bleiben fail-closed.

`pytest -q tests/test_usage_resets.py`: 5/5 bestanden; Modul-Coverage 79%.
Ruff, Mypy und `git diff --check` sauber.

## Runde 400: Usage-Limit-Pools

`usage_limits.py` auf WHAM-/App-Server-Payloads, Spark-Katalog, Fenster-
Identitäten, Dauer-/Prozent-/Reset-Grenzen und malformed Control-Flags geprüft.
Keine neue reproduzierbare Fehlfunktion; unsupported oder widersprüchliche
Buckets bleiben unavailable bzw. werden nicht als Nutzungswert verwendet.

`pytest -q tests/test_usage_limits.py`: 124/124 bestanden; Modul-Coverage 91%.
Ruff, Mypy und `git diff --check` sauber.

## Runde 401: Model-Serialization-Grenze

`models.AccountUsage.as_dict()` nutzt `_isoformat()` für alle Zeitfelder.
Malformed `datetime`-Subklassen konnten dort bisher mit einer Exception die
gesamte JSON-Serialisierung abbrechen. `_isoformat()` liefert bei fehlerhaftem
`isoformat()` jetzt `None`; Regressionstest deckt den Capture-Zeitpunkt ab.

`pytest -q tests/test_models.py`: 15/15 bestanden; Modul-Coverage 50% im
isolierten Model-Test. Ruff, Mypy und `git diff --check` sauber.

## Runde 402: Service-Reload nach Model-Serialization-Fix

`codex-usage service enable --format json` nach `8f133b4` erfolgreich:
Service installiert/aktiv, Timer aktiv und geplant. Exit 2 bleibt der bekannte
Firefox-Playwright-Executable-Fehler der Browser-Konten; Journal zeigt keinen
neuen Model-/Serialisierungsfehler. Keine Netzwerkinstallation und kein
Fallback ausgeführt.

## Runde 403: Config-Pfade und Account-Validierung

`config.py` erneut auf XDG-/Tilde-/`file:`-Pfade, private Konfigurationsdatei,
Account-/Ressourcen-Duplikate, Backend-/Browser-/Serienwerte, Test-Home-
Rollback und Analytics-URL geprüft. Keine neue reproduzierbare Fehlfunktion;
ungültige Pfade und Account-Daten bleiben vor Seiteneffekten abgewiesen.

`pytest -q tests/test_config.py`: 116/116 bestanden; Modul-Coverage 83%.
Ruff, Mypy und `git diff --check` sauber.

## Runde 404: State-Expiry-Zeitzonengrenze

`state._cached_window_expired()` fängt bei Reset-/Capture-Altersberechnung
jetzt beliebige `astimezone()`-Fehler und behandelt den Cache fail-closed als
abgelaufen. Eine malformed `datetime`-Subclass konnte zuvor
`expire_reset_windows()` abbrechen; Regressionstest deckt diesen Resetpfad ab.

`pytest -q tests/test_state.py`: 266/266 bestanden; Modul-Coverage 87%.
Ruff, Mypy und `git diff --check` sauber.

## Runde 406: Routing-Policy und Entscheidungslogik

`routing.py` auf private Policy-Datei, Scope-/Identifier-Validierung,
Credit-Limit-Overrides, Backend-/Identity-/Timestamp-Grenzen, Spark-Health
und Main-/Spark-/Credit-Entscheidungen geprüft. Keine neue reproduzierbare
Fehlfunktion; unbekannte oder stale Zustände bleiben blockiert.

`pytest -q tests/test_routing.py`: 125/125 bestanden; Modul-Coverage 87%.
Ruff, Mypy und `git diff --check` sauber.

## Runde 405: Service-Reload nach State-Expiry-Fix

`codex-usage service enable --format json` nach `61bb295` erfolgreich:
Service installiert/aktiv, Timer aktiv und geplant. Exit 2 bleibt der bekannte
Firefox-Playwright-Executable-Fehler der Browser-Konten; Journal zeigt keinen
neuen State-Expiry-Fehler. Keine Netzwerkinstallation und kein Fallback.

## Runde 407: App-Server-RPC und Prozessgrenzen erneut geprüft

`app_server.py` erneut auf Auth-Kontext, JSON-RPC-IDs/-Fehler, Rate-Limit-
Fenster, Modellkatalog, Deadline-/Write-Grenzen, bounded stdout/stderr-Reader
und Prozessgruppen-Cleanup geprüft. Keine neue reproduzierbare Fehlfunktion;
malformed oder unklassifizierbare Antworten bleiben Fehler beziehungsweise
partiell, Zeitüberschreitungen teilen dieselbe Deadline, und Reader-/Prozess-
Ressourcen werden im `finally`-Pfad geschlossen.

`pytest -q tests/test_app_server.py`: 98/98 bestanden; Modul-Coverage 83 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 452: Backend-Identitätsaggregation und Planbindung

`identity.py` auf Kandidaten-Limits, URL-/Payload-Validierung, Priorisierung
von WHAM-Usage-Antworten, partielle Identitäten, Alias-Konten, Mehrkonto-
Konflikte und Auth-/Planbindung geprüft. Identitätsfelder und Plan-Typen
bleiben bounded und fail-closed; unvollständige oder mehrdeutige Kandidaten
werden verworfen oder führen kontrolliert zu einem Attributionsfehler. Keine
neue reproduzierbare Fehlfunktion.

`pytest -q tests/test_identity.py`: 28/28 bestanden; Modul-Coverage 81 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 453: Browser-Kandidaten und Quellenpriorität

`browser.py` auf vertrauenswürdige Response-Hosts, Body-/Kandidaten-Budgets,
JSON-Parsing, DOM-/HTML-Quellen, Login-/Cloudflare-Erkennung, Identitäts-
Fallback, Profil-/Lock-Grenzen und private Diagnoseausgaben geprüft. Nicht
vertrauenswürdige oder übergroße Antworten werden verworfen; sichtbare DOM-
Quellen und versteckte HTML-Klone bleiben durch den Extractor getrennt, und
fehlende oder nicht bestätigte Identität autorisiert keine Nutzungswerte. Keine
neue reproduzierbare Fehlfunktion.

`pytest -q tests/test_browser_profile.py tests/test_browser_diagnose.py`:
174/174 bestanden; Modul-Coverage 81 % (Branch). Ruff, Mypy und
`git diff --check` sauber.

## Runde 454: Limit-Extractor und Quellenkonsistenz

`extractor.py` auf JSON-/DOM-Budgets, strukturelle 5h-/Wochenfenster,
WHAM-Haupt- und Zusatzlimits, Hidden-Progressbars, absolute und relative
Resetwerte, widersprüchliche Zähler/Prozentfelder sowie Quellenpriorität
geprüft. Unbekannte, doppelte oder widersprüchliche Fenster werden nicht per
Traversal-Reihenfolge autorisiert; sichtbare Werte behalten Vorrang vor
versteckten Klonen, und ungültige Felder bleiben ohne Nutzungswert. Keine neue
reproduzierbare Fehlfunktion.

`pytest -q tests/test_extractor.py`: 198/198 bestanden; Modul-Coverage 91 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 455: Private Datei-I/O und Lock-Grenzen

`private_io.py` auf Pfadtypen, Symlink-Ancestors, geschützte Verzeichnisse,
Eigentümer-/Hardlink-Prüfungen, Byte-/UTF-8-Budgets, atomare Ersetzungen,
Create-only-Rollback, Directory-Fsync und bounded Lock-Wartezeiten geprüft.
Vorhandene Ziele werden nicht unkontrolliert überschrieben; temporäre Dateien,
Locks und Rollbackpfade bleiben privat und regulär. Keine neue reproduzierbare
Fehlfunktion.

`pytest -q tests/test_private_io.py`: 44/44 bestanden; Modul-Coverage 75 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 456: Konfigurations- und Account-Ressourcen

`config.py` auf TOML-/Byte-/Typgrenzen, Analytics-URL-Allowlist, XDG-/Profil-
Pfade, Account-/Label-/Serienkonflikte, eindeutige Profile und Auth-Dateien,
private Test-CODEX_HOMEs sowie Add/Remove/Restore-Rollbacks geprüft. Relative,
fremde, geschützte oder symlinkbasierte Ressourcen werden nicht akzeptiert;
Konfigurations- und Zustandsänderungen bleiben unter Locks und mit kontrolliertem
Rollback. Keine neue reproduzierbare Fehlfunktion.

`pytest -q tests/test_config.py`: 117/117 bestanden; Modul-Coverage 82 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 457: Tabellen- und Statusrendering

`render.py` auf bounded Eingabemengen, Account-/Usage-Zuordnung, Backend-
Provenienz, Fehler-/Login-/Stale-Gates, Fenster- und Poolwerte, Reset-/Auth-
Zeitformatierung sowie dynamische Zusatzlimits geprüft. Nicht bestätigte oder
malformed Werte werden aus Tabellen und JSON-Ausgabe entfernt; Prozentwerte,
Restmengen und Status bleiben konsistent. Keine neue reproduzierbare
Fehlfunktion.

`pytest -q tests/test_render.py`: 66/66 bestanden; Modul-Coverage 87 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 451: Bridge-Debug-Secret-Redaction

`bridge._sanitize_debug_text()` redigierte bisher nur camelCase-Tokenfelder
im doppelten JSON-Format. Snake_case-Token, Single-Quote-/Assignment-Formen
und `Authorization: Bearer` konnten dadurch im lokalen Debug-Dump verbleiben.
Redaction deckt jetzt diese Formen ebenfalls ab; Inhalt bleibt bounded und
URL-/Identitätsredaction unverändert.

`pytest -q tests/test_bridge.py`: 263/263 bestanden; Modul-Coverage 83 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 408: Scheduler-Account-ID-Grenze

`scheduler._bounded_account_list()` akzeptierte bisher ein `Account`-Objekt
mit nicht-stringartiger `id`. Bei mehreren solchen Datensätzen brachen
Ambiguitäts- oder Snapshot-Sets mit rohem `TypeError` statt kontrolliertem
Entry-Point-Fehler ab. Die Bounded-Grenze weist solche Account-Datensätze jetzt
als `ValueError("account records are invalid")` zurück; reguläre Konfigurations-
IDs bleiben unverändert.

`pytest -q tests/test_scheduler.py`: 202/202 bestanden; Modul-Coverage 84 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 409: State-Expiry-Zeitvergleich

`state._values_capture_for_expiry()` fing bei der Gegenüberstellung von
`values_captured_at` und `captured_at` nur Standardvergleichsfehler. Eine
fehlerhafte `datetime`-Subclass konnte `expire_reset_windows()` deshalb mit
`RuntimeError` abbrechen. Der Vergleich fällt jetzt bei beliebigen Exceptions
auf den validierten Capture-Zeitpunkt zurück; der State bleibt fail-closed.

`pytest -q tests/test_state.py`: 267/267 bestanden; Modul-Coverage 88 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 410: Direct-Auth- und WHAM-Pipeline

`direct.py` auf Auth-Datei-/JWT-Parsing, Identitäts- und Planbindung,
Redirect-/Host-Grenzen, HTTP-Status-/Content-Type-/Body-Budgets,
Mehrfachantwort-Stabilisierung sowie Credit-/Fensterparser geprüft. Keine
neue reproduzierbare Fehlfunktion; fremde Identitäten, instabile oder
malformed Antworten und übergroße Inhalte bleiben fail-closed.

`pytest -q tests/test_direct.py`: 183/183 bestanden; Modul-Coverage 83 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Ergänzung 2026-08-22: Kopierte Leistenwert-Formatierungen und Hilfe

Die bisher unformatierbaren Leistenquellen `Resets`, `Kürzel`, `Label`,
`Account-ID`, `Abrufweg`, `Routing`, `Creditverbrauch aktiv`, die drei
Creditlimits, `Warnungen`, `Fehler`, `Login erfolgreich` und `Status` haben
jetzt je ein eigenes Formatierungsziel unter `Formatierungen`. Jede Tabelle
ist eine eigenständige Kopie der normalen Formatierungstabelle und wird je
Account gespeichert. Tokendelta, Prozent-, Datums-, Uhrzeit- und
Restlaufzeitwerte behalten ihre vorhandenen spezialisierten Tabellen.

Die neue Seite `Hilfe` rendert alle Beschreibungen und Tooltips des
Settings-Schemas in einer scrollbaren, gruppierten und aufklappbaren Ansicht.
Tabellenfelder, Auswahloptionen, Defaults und Grenzen werden dort ebenfalls
aufgeführt. Neue Schema-Felder erscheinen automatisch in dieser Sammlung.

`node --test tests/applet_runtime.test.js`: 406/406 bestanden.
`pytest -q tests/test_help_page.py tests/test_format_table_selector.py tests/test_applet.py`:
37/37 bestanden. `make applet-check` und `make install-local` erfolgreich;
Installer-Migration aktualisiert, laufendes Applet reload erfolgreich.

## Runde 450: Konfigurierbare Leistenwerte und Tokendelta

Die Leiste verwendet jetzt frei konfigurierbare, einzeln auswählbare
Wertquellen. `panel-value-count` ist ein Freitextfeld mit Default 20 und
bounded auf 1–64. Wertfelder oberhalb der bisherigen vier Slots werden
normalisiert, dedupliziert und im Gtk-Widget dynamisch aufgebaut. Die Quellen
umfassen Limits je Fenster, Restlaufzeit und Resetdatum je Fenster,
Tokendelta, Identität, Abrufweg, Routing-/Creditstatus, Warnungen, Fehler,
Loginstatus und Kontostatus. Sobald Wertfelder gesetzt sind, unterdrücken sie
die alten automatischen Leistenanhänge; alte `show-panel`-Daten bleiben nur
für Rückwärtskompatibilität in der Normalisierung.

`Tokendelta` ist ein eigenes Formatierungsziel. Die zusätzliche Option
`Dynamisch` extrapoliert das aktuelle Delta über den verbleibenden
Limit-/Reset-Horizont und aktiviert das Schwellenformat nur, wenn die
statistische Projektion das verbleibende Limit erreicht. Ohne belastbares
Fenster bleibt die Option fail-closed.

Die Settings-Seiten sind getrennt: `Einstellungen`, `Formatierungen`,
`Prognosen`, `Status` und `Accounts`. Prognosen enthält Tokenverbrauch,
Tokenende und Creditverbrauch; Status enthält Credits und Resets; Accounts
enthält Abrufwege und Reaktivierungsoptionen. Die alten
`show-panel`-Spalten sind nicht mehr editierbar.

`node --test tests/applet_runtime.test.js`: 404/404 bestanden.
`pytest -q tests/test_panel_settings_list.py tests/test_format_table_selector.py tests/test_applet.py`:
34/34 bestanden. Ruff, JSON-Parse, Python-Compile und `git diff --check`
sauber; verbleibende GTK-/PyGObject-Deprecation-Warnungen sind extern.

## Runde 458: Usage-Reset-Vertrag und Redemption-Gate

`usage_resets.py` erneut gegen den kanonischen/Legacy-Payload-Vertrag, die
Unknown-vs-Zero-Semantik, Typ-/Bereichsgrenzen, Formatierung und das
Redemption-Gate geprüft. Kanonische und verschachtelte Legacy-Formen bleiben
konfliktsicher; unbekannte, boolesche, negative und übergroße Werte fallen
fail-closed auf `unknown`. Die positive Redemption-Prüfung endet weiterhin
absichtlich mit `NotImplementedError`: README und v1-Spezifikation erklären
Redemption wegen fehlender Capability-, Nonce-/Replay-, Lock-, Bestätigungs-
und Postcondition-Gates als nicht verfügbar. CLI und Applet bleiben
read-only; keine neue reproduzierbare Fehlfunktion.

Grenzpfadtests für jede Funktion ergänzt. `pytest -q tests/test_usage_resets.py`:
17/17 bestanden; Modul-Coverage 100 % (Branch). Ruff, Mypy und
`git diff --check` sauber.

## Runde 459: Systemd-Linkauflösung und realer Service-Fehler

`service.py` auf den fehlgeschlagenen User-Timer-Lauf zurückgeführt. Journal,
Unit-`ExecStart` und Playwright-Dry-Run zeigen keinen neuen Repo- oder
Config-Fehler: Der Timer startet `/home/teladi/.local/bin/codex-usage`, aber
die konfigurierten Firefox-Browserkonten treffen auf eine fehlende
Playwright-Executable; der kontrollierte Watchdog-Exit 2 ist damit
umgebungsbedingt. Chromium ist im Cache vorhanden, Firefox nicht; kein
unsicherer Browser-Fallback und keine automatische Netzwerkinstallation.

Separat ließ `_cleanup_managed_timer_enable_link()` einen `RuntimeError` aus
`Path.resolve()` ungefangen entkommen. Ein synthetischer Auflösungsfehler
reproduziert jetzt `ServiceError` statt Roh-Exception; der Handler fängt
`OSError` und `RuntimeError` gemeinsam. `pytest -q tests/test_service.py`:
67/67 bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 460: Profile-Job-Status ohne Worker

`profile_job_status()` ließ ein Manifest mit `status="running"` und fehlender
oder ungültiger Worker-PID unverändert. Solche Jobs konnten dadurch dauerhaft
als laufend erscheinen, obwohl kein Worker mehr identifizierbar ist. Der
Status wird jetzt wie ein verlorener Worker kontrolliert auf `failed` mit
`profile_job_worker_lost` gesetzt; `queued` ohne PID bleibt weiterhin gültig,
und `cancel_requested` ohne PID wird weiterhin als `cancelled` abgeschlossen.

Regressionstest ergänzt. `pytest -q tests/test_profile_jobs.py`: 82/82
bestanden; Mypy, Ruff und `git diff --check` sauber.

## Runde 461: Optionale 5h-Ausblendung bei erschöpftem Langlimit

Neue globale Checkbox `hide-5h-when-long-limit-exhausted` ergänzt. Ist sie
aktiv, zeigt ein Account sein 5h-Prozent in Leiste, Klick-Menü und Hover als
`–`, sobald dessen Wochen- oder 30-Tage-Fenster 0 % verbleiben. Die Prüfung
läuft pro aktuellem Account-Datensatz; sobald ein Langlimit wieder Werte hat,
erscheint 5h wieder. Reset-, Verbrauchs- und Spark-Anzeigen bleiben
unverändert; Standard bleibt deaktiviert.

Schema-/Rendering-Regressionen ergänzt. `pytest -q tests/test_applet.py`:
27/27 bestanden; `node --test tests/applet_runtime.test.js`: 397/397
bestanden. `node --check` und `git diff --check` sauber.

## Runde 462: Integration-Installer-FD-Modusbindung

`integration_installer.py` setzte bei `_copy_regular()`,
`_safe_extract_wheel()` und `_write_exclusive()` den privaten Dateimodus erst
nach dem Schließen des Deskriptors über den Pfad. Ein Pfad-Race konnte diesen
Pfad zwischen `close()` und `chmod()` durch einen Symlink ersetzen und damit
den Modus eines fremden Ziels ändern. Die drei Schreibpfade verwenden jetzt
`os.fchmod()` am noch offenen Deskriptor; Pfad-`chmod()` entfällt.

Drei Race-Regressionen prüfen, dass Zieldatei und Fremddatei unverändert
bleiben; der bestehende Cleanup-Fehlertest deckt den neuen `fchmod()`-Fehlerast
für Kandidaten weiter ab. `pytest -q tests/test_integration_installer.py`:
109/109 bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 463: Private-I/O-Directory-FD-Modusbindung

`private_io.ensure_private_directory()` setzte Verzeichnisrechte bisher nach
der Identitätsprüfung über `Path.chmod()`. Ein Pfad-Race konnte das geprüfte
Verzeichnis durch einen Symlink ersetzen und dadurch Rechte am Ziel außerhalb
des geprüften Pfads ändern. Directory-Rechte werden jetzt über einen mit
`O_NOFOLLOW` geöffneten Verzeichnis-FD und `os.fchmod()` gesetzt; Typ- und
Ownerprüfung erfolgt nochmals am FD.

Race-Regression sowie abhängige State-/Spark-Fehlerpfade auf FD-Fehlerinjektion
umgestellt. `pytest -q tests/test_private_io.py tests/test_state.py
tests/test_spark_health.py`: 344/344 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 464: Service-Unit-Verzeichnis nutzt Directory-FD-Sicherung

`service._unit_directory(create=False)` setzte ein vorhandenes
systemd-Unit-Verzeichnis nach Pfadprüfung nochmals per `Path.chmod()`. Ein
Symlink-Race konnte dadurch ein Ziel außerhalb des geprüften Verzeichnisses
modifizieren. Der Pfad nutzt jetzt denselben `ensure_private_directory()`-
Vertrag mit Directory-FD und `O_NOFOLLOW`; fehlende Verzeichnisse bleiben bei
`create=False` unverändert fehlend.

Regression für den Race-Pfad ergänzt. `pytest -q tests/test_service.py
tests/test_private_io.py`: 113/113 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 465: Auth-Migrations-Manifest ohne Pfad-`chmod`

`profile_migration.apply_auth_migration()` setzte das bereits durch
`ensure_private_directory()` gesicherte Manifest-Verzeichnis nochmals per
`Path.chmod()`. Ein Symlink-Race konnte dadurch das geprüfte Verzeichnis
ersetzen und ein externes Ziel modifizieren. Redundanter Pfad-Aufruf entfernt;
die zentrale Directory-FD-Sicherung bleibt alleiniger Moduspfad.

Regression für das Manifest-Verzeichnis ergänzt. `pytest -q
tests/test_profile_migration.py tests/test_profile_layout.py
tests/test_profile_cli.py`: 67/67 bestanden. Ruff, Mypy und `git diff --check`
sauber.

## Runde 466: Device-Login-Staging per Directory-FD

`profile_login._create_staging_root()` prüfte das Staging-Verzeichnis,
erzeugte es und setzte den Modus danach über `Path.chmod()`. Ein
Symlink-Race konnte dadurch einen externen Pfad treffen; `tempfile.mkdtemp()`
lief anschließend sogar im umgeleiteten Verzeichnis. Der Pfad verwendet jetzt
`ensure_private_directory()` mit `O_NOFOLLOW`/`fchmod`.

Regression für Modus- und `mkdtemp`-Pfadbindung ergänzt. `pytest -q
tests/test_profile_login.py tests/test_profile_layout.py
tests/test_profile_jobs.py`: 147/147 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 467: Config-Profilverzeichnis per Directory-FD

`config._prepare_profile_dir()` setzte ein bestehendes Profilverzeichnis nach
der Prüfung per `Path.chmod()`. Ein Symlink-Race konnte danach Marker-Schreiben
und weitere Profil-I/O in ein externes Verzeichnis umleiten. Die Sicherung
verwendet jetzt erneut `ensure_private_directory()` mit Directory-FD und
`O_NOFOLLOW`; der bestehende Config-Directory-Fehlerpfad wurde auf
FD-Fehlerinjektion aktualisiert.

Profil-Race und Fehlerregressionen ergänzt. `pytest -q tests/test_config.py`:
118/118 bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 468: Installer-Venv-Verzeichnisse per Directory-FD

`integration_installer._install_release()` setzte die Modi von erzeugtem
`venv/` und `site-packages/` per `Path.chmod()`. Ein Pfad-Race konnte damit
einen Fremdpfad ändern. Beide Modusänderungen verwenden jetzt
`ensure_private_directory()` mit Directory-FD und `O_NOFOLLOW`.

Regression verbietet Pfad-`chmod` für die beiden Verzeichnisse und prüft Modus
und Symlink-Freiheit. `pytest -q tests/test_integration_installer.py`:
110/110 bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 469: Installer-Temporärverzeichnisse per Directory-FD

`integration_installer._create_private_directory()` setzte den Modus neu
angelegter Staging-, Build- und Wheel-Verzeichnisse noch per `Path.chmod()`.
Das war derselbe Pfad-Race wie bei den Venv-Verzeichnissen. Die Funktion nutzt
jetzt ebenfalls `ensure_private_directory()`; die Fehler-Injektionstests prüfen
FD-Fehler für alle vier erzeugten Ziele.

Direkter Modus-Test und Installer-Regression ergänzt. `pytest -q
tests/test_integration_installer.py`: 111/111 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 470: Installer-Aktivierungsdateien per Directory-FD löschen

`_remove_activation_files()` iterierte `venv/bin` und löschte Treffer per
Pfad. Ein Parent-Swap konnte dadurch Dateien aus einem Fremdverzeichnis
entfernen; `lib64` hatte denselben Fehler. Cleanup öffnet `venv/` und `bin/`
jetzt mit `O_DIRECTORY|O_NOFOLLOW`, scannt über den Directory-FD und löscht
relative Einträge per `dir_fd`.

Parent-Swap-Regression ergänzt. `pytest -q tests/test_integration_installer.py`:
112/112 bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 471: Installer-Candidate-Cleanup per Parent-FD

`_cleanup_owned_file()` prüfte bisher Parent- und Datei-Inode, löschte danach
aber per `Path.unlink()`. Ein Parent-Swap zwischen Prüfung und Löschung konnte
einen fremden Candidate-Eintrag treffen. Cleanup öffnet den erwarteten Parent
mit `O_DIRECTORY|O_NOFOLLOW`, revalidiert Identität und löscht per `dir_fd`.

Parent-Swap- und FD-Fehlerinjektion aktualisiert. `pytest -q
tests/test_integration_installer.py`: 113/113 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 472: Installer-Provisional-Cleanup per Parent-FD

`_cleanup_provisional()` entfernte neu angelegte Dateien/Verzeichnisse nach
Pfadprüfung per `unlink()`/`rmdir()`. Ein Parent-Swap konnte fremde leere
Einträge entfernen. Gemeinsamer `_remove_owned_entry()` öffnet den erwarteten
Parent descriptor-gebunden, prüft Inode/Typ und entfernt relativ per `dir_fd`.

Datei- und Verzeichnis-Parent-Swap-Regression ergänzt. `pytest -q
tests/test_integration_installer.py`: 114/114 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 473: Build-Pyproject-Austausch per Parent-FD

`_copy_source_into_project()` löschte kopiertes `pyproject.toml` per
`Path.unlink()`, bevor generierter Inhalt geschrieben wurde. Build-Directory-
Identität wird jetzt vom Caller gebunden; alter Eintrag wird über den
gemeinsamen FD-gebundenen Entry-Remover gelöscht und `_write_exclusive()` mit
derselben Parent-Identität aufgerufen.

Pfad-`unlink`- und Build-Replacement-Regression ergänzt. `pytest -q
tests/test_integration_installer.py`: 116/116 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 474: Finaler Release-Rename per Parent-FD

`_install_release()` verschob den Staging-Release zuletzt per
`staging.rename(final_release_dir)`. Trotz vorheriger Identitätsprüfung blieb
ein Parent-Swap bis zum Rename möglich. `_rename_owned_directory()` öffnet
`releases/` mit `O_DIRECTORY|O_NOFOLLOW`, prüft Parent-, Quell- und Ziel-Inode
und ruft `os.rename(..., src_dir_fd=..., dst_dir_fd=...)` auf.

Parent-Swap-Regression und Rename-Seam-Tests aktualisiert. `pytest -q
tests/test_integration_installer.py`: 117/117 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 475: Rekursiver Installer-Cleanup per Parent-FD

`_cleanup_owned_directory()` rief `shutil.rmtree(path)` nach einer
Pfadprüfung auf. Parent-Swap konnte dadurch fremde Build-/Wheel-/Staging-
Bäume löschen. Der gemeinsame Entry-Remover revalidiert Directory-Inode und
ruft `shutil.rmtree(name, dir_fd=parent_fd)` auf; `rmtree` ist auf diesem
System symlink-angriffssicher.

Parent-Swap-Regression ergänzt. `pytest -q tests/test_integration_installer.py`:
118/118 bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 476: Wheel-Output-Scan per Directory-FD

`_build_verified_wheel()` iterierte `wheel_dir` nach Builder-Ende per
`Path.iterdir()` und verwarf die Directory-Identität. Der Scan bindet jetzt
`wheel_identity`, öffnet das Verzeichnis mit `O_DIRECTORY|O_NOFOLLOW` und
prüft Einträge über `os.scandir(fd)`.

Descriptor-Scan-Regression ergänzt und Install-Aufruf an gespeicherte
Wheel-Identität gebunden. `pytest -q tests/test_integration_installer.py`:
119/119 bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 477: Wheel-Datei bis Lesen und Staging binden

Der Wheel-Directory-FD-Scan lieferte bisher nur einen Pfad; zwischen Scan,
`_wheel_details()` und Staging-Copy konnte die Datei ersetzt werden.
`_build_verified_wheel()` liefert jetzt Pfad plus Datei-Inode. `_read_nofollow`,
`_wheel_details` und `_copy_regular` akzeptieren und prüfen Parent- sowie
Datei-Identität über geöffnete Deskriptoren.

Datei-Austausch-Regression ergänzt. `pytest -q tests/test_integration_installer.py`:
120/120 bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 478: Wheel-Extraction relativ zu Destination-FD

`_safe_extract_wheel()` nutzte für Ziel-Dateien absolute Pfade mit
`O_NOFOLLOW`; das schützt keine Symlink-Ancestor-Komponenten. Ein Parent-Swap
vor `os.open()` konnte außerhalb des Release-Baums schreiben. Extraction
öffnet `destination` identitätsgebunden und traversiert jede Parent-Komponente
mit `O_DIRECTORY|O_NOFOLLOW`; Dateien werden per `dir_fd` exklusiv angelegt.

Parent-Symlink-Regression ergänzt. `pytest -q tests/test_integration_installer.py`:
121/121 bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 479: Installer-Release-Postwalk per Directory-FD

`_postwalk_release()` sammelte Directory-Namen über `scandir()` und führte
danach Pfad-`lstat()` aus. Ein Verzeichnis konnte zwischen beiden Schritten
ausgetauscht werden; der Postwalk hätte dann einen fremden Baum geprüft.
Root und alle Descendant-Verzeichnisse werden jetzt per
`O_DIRECTORY|O_NOFOLLOW` geöffnet, `DirEntry.stat()` wird direkt ausgewertet,
und Child-Inode muss vor dem Weiterlauf identisch bleiben. Caller binden Root-
Identität zusätzlich an.

Child-Directory-Swap-Regression ergänzt. `pytest -q
tests/test_integration_installer.py`: 122/122 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 480: Wheel-Extraction-Parent-Inodes binden

`_safe_extract_wheel()` validierte Ziel-Parent zuvor über
`Path.exists()`/`_require_private_dir()` und öffnete sie erst danach relativ
zum Destination-FD. Ein Austausch gegen ein anderes reguläres Verzeichnis
konnte dadurch fremde Zielpfade erreichen. Parent-Komponenten werden jetzt in
einem FD-relativen Validierungspass erstellt/geöffnet, mit `stat()`/`fstat()`-
Inode identifiziert und im Schreibpass erneut gegen diese Identität geprüft;
Target-Duplikate bleiben vor Materialisierung blockiert.

Ordinary-Directory-Swap-Regression ergänzt. `pytest -q
tests/test_integration_installer.py`: 123/123 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 481: Copy-Targets und Staged-Wheel-Datei binden

`_copy_regular()` öffnete Ziel-Dateien bisher über einen Pfad nach
`ensure_private_directory()`. Ein Parent-Swap konnte den Inhalt in fremdes
Verzeichnis schreiben. Ziel-Parent wird jetzt per Directory-FD und Inode
revalidiert; die Funktion liefert Ziel-Datei-Identität. Diese Identität samt
Staging-Parent wird bis `_safe_extract_wheel()` und `_read_nofollow()` gereicht,
damit ein Wheel-Austausch nach Staging nicht mehr gelesen wird.

Target-Parent-Swap-Regression ergänzt. `pytest -q
tests/test_integration_installer.py`: 124/124 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 482: Private Installer-Verzeichnisse per Parent-FD

`_create_private_directory()` prüfte Parent-Identität und legte das Ziel
danach per Pfad-`mkdir()` an. Ein Parent-Swap konnte ein neues Staging-, Build-
oder Wheel-Verzeichnis im falschen Baum erzeugen. Creation öffnet und
revalidiert den erwarteten Parent mit `O_DIRECTORY|O_NOFOLLOW`, erstellt relativ
per `dir_fd`, setzt den Modus am geöffneten Child-FD und prüft Inode/Parent vor
Rückgabe.

Parent-Swap- und Fehler-Injection-Tests aktualisiert. `pytest -q
tests/test_integration_installer.py`: 125/125 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 483: Venv-Site-Packages per Directory-FD finden

Die Venv-Auflösung nutzte `venv_root.glob("lib/python*/site-packages")` und
setzte danach den Modus des gefundenen Pfads. Ein Austausch von `python*`
zwischen Enumeration und Öffnen konnte fremdes `site-packages` auswählen oder
den Modus eines fremden Pfads ändern. `lib`, Python-Verzeichnis und
`site-packages` werden jetzt über
gebundene Directory-FDs gescannt, `stat()`/`fstat()`-Identitäten verglichen und
`site-packages` direkt am FD auf 700 gesetzt.

Python-Directory-Swap-Regression ergänzt. `pytest -q
tests/test_integration_installer.py`: 126/126 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 484: Exklusive Manifest-/Launcher-Dateien per Parent-FD

`_write_exclusive()` prüfte Ziel-Existenz per Pfad und erzeugte Manifest bzw.
Launcher danach ebenfalls pfadbasiert. Ein Parent-Swap konnte so in einen
fremden Candidate-/Release-Baum schreiben. Parent wird jetzt mit
`O_DIRECTORY|O_NOFOLLOW` geöffnet und identitätsgebunden; Existenzprüfung,
Creation, Schreiben, `fchmod()` und finale Datei-Identität laufen über diesen
Descriptor.

Parent-Swap-Regression ergänzt. `pytest -q
tests/test_integration_installer.py`: 127/127 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 485: Private-Verzeichnis-Creation mit erwartetem Parent

`_require_private_dir(create=True)` verwendete weiterhin Pfad-`exists()` und
`mkdir()`. Bootstrap-, Source-Copy- und Wheel-Output-Parents waren damit nicht
an den bereits geprüften Directory-Inode gebunden. Die Funktion akzeptiert
jetzt erwartete Parent-Identität, prüft Parent per FD und erstellt fehlende
Kinder relativ mit `dir_fd`; bekannte Call-Sites reichen ihre Parent-Inodes
durch. Concurrent-`FileExistsError` bleibt konvergent.

Parent-Swap- und Bootstrap-Race-Regression ergänzt. `pytest -q
tests/test_integration_installer.py`: 128/128 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 486: Redundante Release-Existenzpfade entfernt

`_install_release()` prüfte Staging- und Finalnamen zusätzlich per
`Path.exists()`/`is_symlink()`, obwohl `_create_private_directory()` und
`_rename_owned_directory()` bereits descriptor-gebunden und atomar prüfen.
Die doppelten Pfad-Checks sind entfernt; Entscheidung bleibt bei den
identitätsgebundenen Operationen.

Install-/Preexisting-Target-Tests: `pytest -q tests/test_integration_installer.py`:
128/128 bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 487: Release-Dateien bis Hash-Lesen identitätsgebunden

Nach Extraction und Launcher-Schreiben las der Installationspfad
Entry-Point, RECORD, Wheel und Launcher wieder nur über Pfade. `_safe_extract_wheel()`
liefert jetzt pro Member Parent-/Datei-Inode; `_write_launcher()` liefert die
Launcher-Inode. Alle späteren `_read_nofollow()`-Aufrufe prüfen diese Identität
zusätzlich, bevor Hashes und Manifestdaten entstehen.

Extraction-Identitätsassertion ergänzt. `pytest -q
tests/test_integration_installer.py`: 128/128 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 488: Attestation-Tree-Hash per Child-FD

`integration_attestation._release_tree_rows()` sammelte `DirEntry`-Namen,
führte danach Pfad-`lstat()` aus und las Dateien erneut über Pfade. Ein
Directory-Swap konnte so einen fremden Baum hashen. Tree-Walk öffnet Root und
alle Kinder jetzt mit `O_DIRECTORY|O_NOFOLLOW` bzw. `O_NOFOLLOW`, vergleicht
`stat()`/`fstat()`-Inodes und liest Dateien aus dem gebundenen FD; offene
Deskriptoren bleiben begrenzt auf die bereits gebundene Entry-Obergrenze.

Child-Directory-Swap-Regression ergänzt. `pytest -q
tests/test_integration_installer.py`: 129/129 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 489: Attestation-Dateilesen per Parent-FD

`integration_attestation._file_bytes()` und `_read_nofollow_bytes()` prüften
Dateien per Pfad und öffneten sie danach erneut. Parent-Swap konnte damit
fremde Bytes in Manifest-/RECORD-Prüfungen einbringen. Beide Leser öffnen
Parent mit `O_DIRECTORY|O_NOFOLLOW`, vergleichen Parent-Inode und Datei-Inode
gegen die erste Prüfung und lesen erst aus dem gebundenen Datei-FD;
`_record_rows()` reicht seine Datei-Identität weiter.

Datei-Parent-Swap-Regression ergänzt. `pytest -q
tests/test_integration_installer.py`: 130/130 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 519: Wochen-Einheit im Verbrauch durchgängig unterstützt

Die Cinnamon-Einstellungen und das Applet akzeptierten `unit=weeks`, während
`consumption.py` und der CLI-Parser die Konfiguration ablehnten. Dadurch
führte jede Wochen-Auswahl zu einem Abruffehler. Python, CLI-Hilfe und
Referenzdokumentation akzeptieren Wochen jetzt durchgängig.

Regressionen für Lookback-Berechnung und CLI-Aufruf ergänzt.
`pytest -q tests/test_consumption.py tests/test_history_cli.py`: 39/39
bestanden; passender CLI-Test: 4/4. Ruff, Mypy und `git diff --check` sauber.

## Runde 518: Unbekannte Verbrauchsfenster korrekt beschriftet

`applet.js` beschriftete jedes Verbrauchsfenster ohne 5h-, Wochen- oder
30-Tage-Dauer als `5h`. Spark-„sonstiges“ und weitere gültige Fenster waren
dadurch irreführend. Der Fallback lautet jetzt `sonstiges`; bekannte Dauern
bleiben unverändert.

Regression `consumption rendering labels unknown limit durations as other`
ergänzt. `node --test tests/applet_runtime.test.js`: 400/400 bestanden.
`git diff --check` sauber.

## Runde 517: Canonical-Auth-JSON validiert

Canonical-Items übersprangen bisher die Strict-JSON-/Objektprüfung, die für
zu migrierende Quellen bereits gilt. Korrupte oder zu große vorhandene
`auth.json` konnten deshalb als gültiges Canonical-Item im Manifest landen.
Canonical-Klassifizierung und direkter Apply lesen und validieren die Datei
jetzt ebenfalls; Fehler bleiben generisch und fail-closed.

Regressionen für Plan und Apply mit malformed Canonical-Auth ergänzt.
`pytest -q tests/test_profile_migration.py`: 64/64 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 516: Canonical-Auth-Datei auf Privatheit geprüft

Canonical-Items wurden bisher im Apply nur ins Manifest übernommen; eine
vorhandene Datei mit 0644, falschem Eigentümer, Hardlink oder Nicht-Regular-
Typ konnte dadurch als gültig erscheinen. Plan-Klassifizierung und direkter
Apply validieren Canonical-Ziel jetzt als user-owned, private reguläre
Single-Link-Datei, bevor sie den Status `canonical` akzeptieren.

Regressionen für Plan und direkten Apply mit nicht-privater Datei ergänzt.
`pytest -q tests/test_profile_migration.py`: 62/62 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 515: Broken-Auth-Symlink im Suchroot erkannt

`_source_for_account()` berücksichtigte Kandidaten bisher nur mit
`Path.exists()`. Ein kaputter finaler Symlink im Suchroot wurde dadurch als
fehlende Quelle gemeldet; beim tatsächlichen Lesen wäre es ein Symlink-
Konflikt. Kandidatensuche berücksichtigt jetzt auch `is_symlink()`, damit
broken links bereits im Dry-Run fail-closed als `conflict` erscheinen.

Regression ergänzt. `pytest -q tests/test_profile_migration.py`: 60/60
bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 514: Ressourcen-Aliase in Auth-Migration abgewiesen

Planner und Plan-Validator verglichen Quellen/Ziele bisher als rohe
`Path`-Objekte. `auth.json` oder Canonical-Ziel mit `..`-Alias konnte deshalb
mehrfach in einem Plan erscheinen. Beide Eindeutigkeitsprüfungen verwenden
jetzt lexikalisch normalisierte Keys; Symlink-Auflösung bleibt weiterhin
ausgeschlossen.

Regressionen für Alias-Quelle in zwei Accounts sowie Alias-Quelle/-Ziel im
direkten Plan ergänzt. `pytest -q tests/test_profile_migration.py`: 59/59
bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 513: Canonical-Auth-Pfadalias erkannt

`_classify_source()` verglich Quelle und Ziel bisher byteweise. Absolute
Konfigurationspfade mit `..` konnten dadurch auf dasselbe Canonical-
`auth.json` zeigen, wurden aber als vorhandener Konflikt markiert. Nach
Symlink-Prüfung vergleicht der Klassifizierer jetzt lexikalisch normalisierte
Pfade; Symlink-Auflösung bleibt ausgeschlossen.

Regression für ein vorhandenes Canonical-Ziel über `..` ergänzt. `pytest -q
tests/test_profile_migration.py`: 56/56 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 512: Lexikalische Suchroot-Aliase dedupliziert

`_source_for_account()` deduplizierte bisher nur bytegleiche `Path`-Objekte.
Zwei Suchroots wie `/root/data` und `/root/data/nested/..` meldeten dieselbe
`auth.json` deshalb als mehrere Quellen. Kandidaten werden jetzt mit
lexikalischem `normpath` dedupliziert, ohne Symlink-Auflösung; reale
verschiedene oder symlinkbasierte Quellen bleiben getrennt und fail-closed.

Regression für `..`-Alias ergänzt. `pytest -q tests/test_profile_migration.py`:
55/55 bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 511: Auth-Quellmodus beim Apply erneut geprüft

`apply_auth_migration()` vertraute bisher auf die Modusprüfung aus dem
Planungszeitpunkt. Wurde `auth.json` danach group-/world-lesbar, las und
kopierte Apply die Datei trotzdem. Der Modus wird jetzt direkt nach dem
geschützten Lesen erneut geprüft; nicht-private Quellen erzeugen kein Ziel.

TOCTOU-Regression ergänzt. `pytest -q tests/test_profile_migration.py`: 54/54
bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 510: Canonical-Ziel-Symlinks im Dry-Run fail-closed

`_classify_source()` prüfte bisher Symlinks am Canonical-Ziel erst beim
Apply. Ein symlinkendes Elternverzeichnis mit fehlendem Ziel wurde deshalb im
Dry-Run als `planned` ausgegeben, obwohl `_assert_migration_target_available()`
später abbrach. Ziel und Ziel-Ancestors werden jetzt bereits bei der
Klassifizierung geprüft; echte Ziele bleiben unverändert `conflict`.

Regression für symlinkendes Canonical-Elternverzeichnis ergänzt. `pytest -q
tests/test_profile_migration.py`: 53/53 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 509: Auth-Quellen-Symlinks im Dry-Run fail-closed

`_classify_source()` prüfte bisher nur den finalen Quellpfad und erkannte
einen Symlink-Elternpfad erst beim Apply-Lesen. Außerdem wurde ein symlinkendes
Canonical-Ziel bei identischem Pfad vor der Symlink-Prüfung als `canonical`
klassifiziert. Die bestehende Ancestor-Prüfung läuft jetzt vor der
Canonical-Entscheidung; beide Fälle erscheinen bereits im Dry-Run als
`conflict`.

Regressionen für Symlink-Ancestor und symlinkendes Ziel ergänzt. `pytest -q
tests/test_profile_migration.py`: 52/52 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 508: Doppelte Auth-Migrations-Ressourcen abgewiesen

`_validate_migration_plan()` erlaubte bisher dieselbe Auth-Quelle oder
dasselbe Canonical-Ziel in mehreren Items. Direkte Plan-Aufrufer konnten so
Credentials mehrfach verteilen; bei einem geteilten Ziel trat der Fehler erst
während des Schreibens auf. Quellen und Ziele werden jetzt vor jeder
Quellenprüfung eindeutig verlangt.

Regression für doppelte Quelle und doppeltes Ziel ergänzt. `pytest -q
tests/test_profile_migration.py`: 50/50 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 507: Doppelte Auth-Migrations-Account-IDs abgewiesen

`plan_auth_migration()` und `_validate_migration_plan()` akzeptierten bisher
mehrere Items mit derselben `account_id`. Die Konfigurationsprüfung verhindert
das im Normalpfad, direkte Plan-/API-Aufrufer konnten aber zwei Profile für
dieselbe Identität erzeugen oder in ein Manifest schreiben. Beide Pfade
verlangen jetzt eindeutige IDs vor Quellenprüfung bzw. Manifest-Schreibvorgang.

Regressionen für Planung und Apply ergänzt. `pytest -q
tests/test_profile_migration.py`: 48/48 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 505: Auth-Migrationsrollback validiert Item-Felder

`rollback_auth_migration()` übersprang bisher jeden unbekannten Item-Status
und prüfte `account_id` nicht. Ein manipuliertes Manifest konnte dadurch als
gültig markiert werden; bei einem ungültigen ID-Wert wurde ein passendes Ziel
vor der Fehlererkennung gelöscht. Rollback validiert jetzt jede Item-ID mit
der zentralen Account-Regel und akzeptiert nur `canonical` oder `applied`;
fehlerhafte Manifeste bleiben unverändert.

Regression für ungültige ID und unbekannten Status ergänzt. `pytest -q
tests/test_profile_migration.py`: 45/45 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 506: Identische Auth-Quellkandidaten dedupliziert

`_source_for_account()` behandelte dieselbe Datei zweimal als konkurrierende
Quelle, wenn `profile_dir` zugleich als einzelner `search_root` verwendet
wurde. Die Planung brach dadurch mit „multiple auth sources“ ab, obwohl kein
Konflikt vorlag. Existierende Kandidaten werden jetzt in Eingabereihenfolge
dedupliziert; echte verschiedene Quellen bleiben Konflikt.

Regression ergänzt. `pytest -q tests/test_profile_migration.py`: 46/46
bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 504: Auth-Migrationsplan-IDs validiert

`profile_migration._validate_migration_plan()` akzeptierte bisher ungültige
oder reservierte `AuthMigrationItem.account_id`-Werte, etwa bei
`status="canonical"`, und schrieb daraus ein Manifest. Validator nutzt jetzt
zentrale `_validate_account_id()`-Regel; fehlerhafte Pläne scheitern vor
Manifest-Schreibvorgang.

Regression ergänzt. `pytest -q tests/test_profile_migration.py`: 43/43 und
`tests/test_profile_layout.py`: 25/25 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 503: Profile-Layout-Account-ID validiert

`profile_layout.layout_for_account()` akzeptierte bisher ungültige oder
reservierte `account.id`-Werte und erzeugte dafür Layout-/Metadatenpfade.
Gemeinsamer `_validate_account_id()`-Guard ergänzt; direkte Aufrufer bleiben
damit unabhängig von vorgelagerter Config-Validierung sicher.

Regression ergänzt. `pytest -q tests/test_profile_layout.py`: 25/25,
`tests/test_profile_migration.py`: 42/42 sowie Profile-Login/Terminal: 72/72
bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 502: Consumption-Kernfunktionen direkt getestet

`ConsumptionWindow.as_dict()`, `consumption_lookback_seconds()` und
`_confirmed_reset()` direkt geprüft. Lookback-Einheiten, Forecast-Felder und
Reset-Übergang bleiben korrekt; keine Produktionsänderung erforderlich.

`pytest -q tests/test_consumption.py`: 35/35 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 501: LimitWindow-Eigenschaften direkt fail-closed getestet

`LimitWindow.has_known_identity`, `is_complete`, `has_invalid_usage_value`
und `has_usage_value` mit bekannten, dynamischen, ungültigen und fehlenden
Grenzwerten direkt geprüft. Bestehendes Verhalten fail-closed; keine
Produktionsänderung erforderlich.

`pytest -q tests/test_models.py`: 31/31 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 500: AccountUsage-Modelpool direkt fail-closed getestet

`AccountUsage.model_pool()` direkt geprüft: exakter eindeutiger Schlüssel
wird gefunden; Groß-/Kleinschreibung, Whitespace, ungültige Eingaben,
mehrdeutige und malformed Kataloge liefern `None`. Verhalten war bereits
korrekt; keine Produktionsänderung erforderlich.

`pytest -q tests/test_models.py`: 22/22 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 499: Routing-Fensteridentität explizit fail-closed getestet

`routing._window_identity_is_known()` für unbekannten Fensternamen ohne
`duration_seconds` geprüft. `_canonical_window_name(None)` liefert bereits
kontrolliert `""`; der Pfad ergibt `False` statt Ausnahme. Direkter
Fail-Closed-Test ergänzt, keine Produktionsänderung erforderlich.

`pytest -q tests/test_routing.py`: 126/126 bestanden. Ruff, Mypy und
`git diff --check` sauber.

## Runde 498: Profile-Job-Start-Cleanup mit strikter PID-Grenze

`profile_jobs.create_profile_job()` übergab bei fehlgeschlagenem Worker-
Tracking `process.pid` ungeprüft an `os.killpg()`. Boolesche oder ungültige
PIDs konnten damit falsche Prozessgruppen adressieren; bei `pid=True` fehlte
außerdem ein sicherer Prozess-Fallback. Der Pfad validiert PID jetzt strikt,
signalisiert nur gültige Gruppen und nutzt sonst `process.kill()`.

Regression ergänzt. `pytest -q tests/test_profile_jobs.py`: 85/85 bestanden.
Ruff, Mypy und `git diff --check` sauber.

## Runde 497: Installer-Prozesssignale mit strikter PID-Grenze

`integration_installer` übergab Preflight-PIDs und Builder-Gruppen-IDs
ungeprüft an `os.killpg()` bzw. `os.getpgid()`. Boolesche oder ungültige
Prozesswerte konnten damit falsche Prozessgruppen adressieren. Preflight-
Cleanup, Builder-Gruppenauflösung und Gruppen-Kill akzeptieren jetzt nur
positive, nicht-boolesche Ganzzahlen; ungültige Werte fallen auf
`process.kill()` oder werden übersprungen.

Drei Regressionen ergänzt. `pytest -q tests/test_integration_installer.py`:
133/133 bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 496: Systemctl-Cleanup mit strikter PID-Grenze

`service._terminate_systemctl_process()` übergab `process.pid` ungeprüft an
`os.killpg()`. Ein malformed Prozessobjekt konnte damit
`os.killpg(True, SIGKILL)` auslösen. Der Cleanup prüft PID jetzt als positive,
nicht-boolesche Ganzzahl und nutzt sonst den bestehenden `process.kill()`-
Fallback.

Regression ergänzt. `pytest -q tests/test_service.py`: 69/69 bestanden.
Ruff, Mypy und `git diff --check` sauber.

## Runde 495: Profile-Job-Abbruchsignal mit strikter PID-Grenze

`profile_jobs.cancel_profile_job()` behandelte `bool` wegen Python-
Integer-Vererbung als gültige Worker-PID und konnte damit
`os.killpg(True, SIGTERM)` aufrufen, wenn der aktualisierte Prozessdatensatz
malformed war. Der Guard weist Boolesche Werte jetzt ab; Manifestvalidierung
und normale Eigentümerprüfung bleiben unverändert.

Regression ergänzt. `pytest -q tests/test_profile_jobs.py`: 84/84 bestanden.
Ruff, Mypy und `git diff --check` sauber.

## Runde 494: Device-Login-Prozesssignal mit strikter PID-Grenze

`profile_login._terminate_bounded_process()` behandelte `bool` wegen Python-
Integer-Vererbung als gültige PID und konnte damit `os.killpg(True, SIGKILL)`
aufrufen. Der Guard weist Boolesche Werte jetzt ab und nutzt den bestehenden
`process.kill()`-/`process.wait()`-Fallback.

Regression ergänzt. `pytest -q tests/test_profile_login.py`: 45/45 bestanden.
Ruff, Mypy und `git diff --check` sauber.

## Runde 493: Reactivation-Prozesssignal mit strikter PID-Grenze

`reactivate._kill_login_process_group()` behandelte `bool` wegen Python-
Integer-Vererbung als gültige PID und konnte damit `os.killpg(True, SIGKILL)`
aufrufen. Der Guard weist Boolesche Werte jetzt ab und nutzt den bestehenden
`process.kill()`-/`process.wait()`-Fallback.

Regression ergänzt. `pytest -q tests/test_reactivate.py`: 68/68 bestanden.
Ruff, Mypy und `git diff --check` sauber.

## Runde 492: App-Server-Prozesssignal mit strikter PID-Grenze

`app_server._signal_process_group()` behandelte `bool` wegen Python-
Integer-Vererbung als gültige PID und konnte damit `os.killpg(True, ...)`
aufrufen. Der Guard weist Boolesche Werte jetzt ab und nutzt den bestehenden
`process.terminate()`-/`process.kill()`-Fallback. Normale PIDs und bereits
beendete Prozesse bleiben unverändert.

Regression ergänzt. `pytest -q tests/test_app_server.py`: 99/99 bestanden.
Ruff, Mypy und `git diff --check` sauber.

## Runde 491: Profile-Job-Reaping mit strikter PID-Grenze

`profile_jobs._reap_untracked_worker()` behandelte `bool` wegen Python-
Integer-Vererbung als gültige PID. Ein malformed Prozessobjekt konnte damit
`os.killpg(True, SIGKILL)` statt des sicheren Prozess-Fallbacks auslösen. Die
PID-Prüfung weist Boolesche Werte jetzt wie die übrigen Manifest-/Statuspfade
ab; bei `pid=True` wird nur `process.kill()` verwendet.

Regression ergänzt. `pytest -q tests/test_profile_jobs.py`: 83/83 bestanden.
Ruff, Mypy und `git diff --check` sauber.

## Runde 490: Opt-in-Leistenfilter für erschöpfte Langlimits

Die Leiste kann jetzt optional einen Account ausblenden, sobald dessen
Wochen- oder 30-Tage-Limit sicher 0 % erreicht. Der neue Switch
`hide-account-when-long-limit-exhausted` steht unter „Leiste“ und ist
standardmäßig deaktiviert. Unbekannte, veraltete, partielle oder fehlende
Werte bleiben sichtbar; Hover und Klick-Menü werden nicht gefiltert. Der
bestehende Switch für die 5h-Darstellung bleibt unabhängig.

`node --test tests/applet_runtime.test.js`: 399/399 bestanden.
`pytest -q tests/test_applet.py tests/test_format_table_selector.py`: 32/32
bestanden; die drei bekannten GTK-/PyGObject-Deprecation-Warnungen bleiben.
Ruff, Node-Syntaxcheck, JSON-Prüfung und `git diff --check` sauber.

## Runde 450: App-Server-RPC und Identitätsgrenzen

`app_server.py` auf RPC-ID-/Result-Prüfung, bounded Line-/Message-Queues,
CODEX_HOME-Symlink- und Prozessgrenzen, Auth-/Plan-/E-Mail-Konsistenz,
Window-Duration-Klassifikation sowie Pool-Provenienz geprüft. Ungültige
Protokollantworten, fremde Auth-Kontexte und unbrauchbare Limits bleiben
fail-closed; keine neue reproduzierbare Fehlfunktion.

`pytest -q tests/test_app_server.py`: 98/98 bestanden; Modul-Coverage 83 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 411: Browser-Diagnose-Outputpfade

`browser.py` setzte Account-IDs bei Diagnose-Screenshots und Probe-Dateien
direkt in Dateinamen ein. Obwohl geladene Configs IDs validieren, konnten
direkte Entry-Point-Aufrufe mit `../...` so den Zielordner verlassen. Beide
Output-Grenzen verwenden jetzt die bestehende Config-ID-Validierung; gültige
Account-Dateien bleiben unverändert.

`pytest -q tests/test_browser_profile.py tests/test_browser_diagnose.py`:
169/169 bestanden; Modul-Coverage 80 % (Branch). Ruff, Mypy und
`git diff --check` sauber.

## Runde 412: Render-Account-Grenze

`render_account_overview()` sortierte Config-Accounts direkt; zusammen mit
`render_account_values()` konnten nicht-stringartige `Account.id`-Werte bei
gemischten Datensätzen ungefangen `TypeError` auslösen. Der gemeinsame
Bounded-Account-Helper weist solche Datensätze jetzt kontrolliert zurück und
die Overview nutzt ihn ebenfalls.

`pytest -q tests/test_render.py`: 66/66 bestanden; Modul-Coverage 87 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 413: Private-I/O-Grenzen

`private_io.py` auf Pfadtyp-/Symlink-/Owner-/Hardlink-Prüfungen, geschützte
Verzeichnisse, bounded Reads, Atomic-/Create-Only-Writes, fsync, Lock-
Deadlines und Rollback geprüft. Keine neue reproduzierbare Fehlfunktion;
öffentliche Aufrufer sichern ihre Zielverzeichnisse vor den privaten Reads,
Writes und Locks, und bestehende Pfade bleiben unverändert.

`pytest -q tests/test_private_io.py`: 36/36 bestanden; Modul-Coverage 75 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 414: Profile-Job-Selektoren

`profile_jobs._validate_create_arguments()` prüfte Browser-, Backend- und
Reaktivierungs-Selektoren bisher direkt per Set-Mitgliedschaft. Unhashbare
Direktaufruf-Werte wie Liste oder Dict lösten daher rohes `TypeError` statt
kontrolliertem `ValueError` aus. Die drei Grenzen prüfen jetzt zuerst den
String-Typ; gültige Selektoren und Manifest-/Worker-Fluss bleiben unverändert.

`pytest -q tests/test_profile_jobs.py`: 81/81 bestanden; Modul-Coverage 78 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 415: Device-Login-Account-Grenze

`run_device_login()` akzeptierte ein direkt konstruiertes `Account` mit
ungültiger ID bis zum Lock-Aufruf. Listen konnten dort rohes `TypeError`
auslösen; Pfad-/Reservierungswerte wurden als `AccountLockError` statt als
Device-Login-Fehler sichtbar. Der Entry Point nutzt jetzt die bestehende
Account-ID-Validierung vor Layout und Lock; Config-basierte gültige Logins
bleiben unverändert.

`pytest -q tests/test_profile_login.py`: 43/43 bestanden; Modul-Coverage 85 %
(Branch). Aufrufer-Tests `tests/test_profile_jobs.py tests/test_profile_cli.py`:
85/85 bestanden. Ruff, Mypy und `git diff --check` sauber.

## Runde 416: Auth-Migrationsplan-Account-Grenzen

`plan_auth_migration()` ließ direkt konstruierte Accounts mit ungültiger ID
bis in Layout-/Quellenlogik laufen; ein truthy nicht-string
`auth_json_path` konnte dort außerdem rohes `TypeError` aus `Path()` liefern.
Plan-Entry-Point validiert Account-Typ und ID jetzt vor Seiteneffekten, und
explizite Auth-Quellen verlangen einen String; gültige Config-Accounts sowie
leere optionale Quelle bleiben unverändert.

`pytest -q tests/test_profile_migration.py`: 41/41 bestanden; Modul-Coverage
78 % (Branch). Nachbar-Tests `tests/test_profile_layout.py
tests/test_profile_cli.py`: 25/25 bestanden. Ruff, Mypy und `git diff --check`
sauber.

## Runde 417: OAuth-Browser-Eigentümergrenze

`oauth_browser._browser_configuration()` prüfte Profil und Marker bisher nur
auf private Rechte, Regular-File-/Link-Eigenschaften und Symlink-Freiheit.
Ein fremder Eigentümer konnte ein 0700-Profil beziehungsweise private Marker
als isoliertes OAuth-Profil einschleusen. Profilverzeichnis und beide
Markerarten verlangen jetzt zusätzlich aktuellen User-Owner; gültige
Reactivation-Profile bleiben unverändert.

`pytest -q tests/test_reactivate.py`: 63/63 bestanden; Modul-Coverage 84 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 418: Reactivation-Entry-Point- und Pfadgrenzen

`reactivate_account()` und `open_account_in_reactivation_browser()` ließen
direkte Accounts mit ungültiger ID bis zum Lock beziehungsweise bis in
Ausgabe-/Profilpfade laufen. Zusätzlich akzeptierten Auth- und Profilpfad-
Helper relative Pfade und konnten damit CWD-Dateien oder CWD-Profile berühren.
Beide Entry Points validieren IDs jetzt zentral; Auth- und Profilpfade müssen
absolut sein. Config-basierte gültige Accounts bleiben unverändert.

`pytest -q tests/test_reactivate.py`: 67/67 bestanden; Modul-Coverage 79 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 419: Integration-Attestierung

`integration_attestation.py` auf Manifest-Schema-/Version-Bindung,
State-/Data-/Release-Pfadcontainment, User-Owner und private Modi,
No-Follow-/TOCTOU-Lesen, Hash-/RECORD-Konsistenz, CSV-/Entry-/Byte-Limits,
Release-Tree-Digest und fail-closed Fehlerabbildung geprüft. Keine neue
reproduzierbare Fehlfunktion; Attestierung repariert oder mutiert keine
Dateien und verweigert Drift, Fremdpfade und malformed Daten.

`pytest -q tests/test_integration_installer.py`: 106/106 bestanden;
Modul-Coverage 78 % (Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 420: Account-Terminal-Isolation

`start_account_terminal()` kopierte bisher die komplette Parent-Umgebung in
den neuen Account-Terminal. `OPENAI_API_KEY` oder `CODEX_API_KEY` konnten damit
kanonische `CODEX_HOME`-Credentials übersteuern. Account-ID wird nun vor
Layout-/Prozessarbeit validiert; beide API-Key-Variablen werden aus der
Terminal-Umgebung entfernt, sonstige Desktop-/Locale-/Proxy-Variablen bleiben
erhalten.

`pytest -q tests/test_terminal.py`: 27/27 bestanden; Modul-Coverage 87 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 421: Formatierungs-Selector-GUI

`FormatTableSelector` auf genau ein sichtbares Gtk-Stack-Child, zentrale
Dropdown-Auswahl, schema-gefilterte Tabellen-IDs, fehlende-Definitionen-
Fallback, titel-/Selection-Synchronisation und unterdrückte Rückschreibungen
bei Settings-Reload geprüft. Tabellen bleiben getrennt; Stack-Transition ist
deaktiviert. Keine neue reproduzierbare Fehlfunktion.

`pytest -q tests/test_format_table_selector.py tests/test_applet.py`:
32/32 bestanden. `node --test tests/applet_runtime.test.js`: 395/395
bestanden. JSON-Schema, Ruff und `git diff --check` sauber. Bekannte GTK-
Deprecation-Warnungen bleiben extern/verhaltensneutral.

## Runde 422: CLI-Dispatch und Transaktionen

`cli.py` auf argv-/Default-Command-Normalisierung, Parser-Dispatch,
Account-/Auth-Mehrfachauswahl, Policy-/Usage-Provenienz, Bridge-Host-/Endpoint-
Grenzen, Ingest-Bytebudgets, Service-Synchronisation sowie Profile-/State-
Löschtransaktionen mit Rollback geprüft. Kein neuer reproduzierbarer Fehler;
malformed Eingaben bleiben kontrollierte Fehler, Identitäts- und Pfadbindung
bleiben erhalten.

`pytest -q tests/test_cli.py`: 117/117 bestanden; Modul-Coverage 71 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 423: Bridge-Token-Revoke-Grenze

`revoke_bridge_token()` löschte einen vorhandenen Tokenpfad bisher ohne
Regular-File-, User-Owner-, Linkzähler- oder Mode-Prüfung. Ein ersetzter
Hardlink konnte so trotz fail-closed Erzeuger-/Lesepfad verändert werden. Der
Revoke-Pfad akzeptiert Symlink-Aufräumen weiterhin, prüft reguläre Dateien
aber jetzt auf User-Owner, genau einen Link und private Modi.

`pytest -q tests/test_bridge.py`: 259/259 bestanden; Modul-Coverage 83 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 424: Account-Lock erneut geprüft

`account_lock.py` auf ID-/Timeout-Validierung vor Seiteneffekten,
private Lock-Verzeichnisse, `O_NOFOLLOW`, User-Owner, Single-Link-Dateien,
Nonblocking-Contention, Deadline und sichere Freigabe geprüft. Keine neue
reproduzierbare Fehlfunktion; bestehende Lockdateien werden vor Nutzung
gesichert und fehlerhafte Pfade kontrolliert abgewiesen.

`pytest -q tests/test_account_lock.py`: 15/15 bestanden; Modul-Coverage 84 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 425: History-SQLite und Retention

`history.py` auf absolute/private Datenbankpfade, FD-/Inode-Revalidierung,
`O_NOFOLLOW`, WAL-/SHM-Rechte, SQLite-Schema, Locking, UTC-/Millisekunden-
Grenzen, bounded Sample-Materialisierung, Account-/Pool-Isolation und
Dry-Run-/Delete-Prune geprüft. Keine neue reproduzierbare Fehlfunktion;
malformed Zeitwerte, Datenbanken und Samples bleiben fail-closed.

`pytest -q tests/test_history.py tests/test_history_cli.py`: 81/81 bestanden;
Modul-Coverage 82 % (Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 426: Vollsuite nach Audit-Fixes

Die Vollsuite bestätigt den aktuellen Branch nach Terminal-, Reactivation-,
Profile-, Bridge- und GUI-Audit-Fixes: `2785 bestanden, 1 übersprungen,
3 Warnungen` in 94,93 s. Warnungen bleiben externe GTK-/PyGObject-
Deprecations; keine Test- oder Integrationsregression.

## Runde 427: Systemd-Service erneut geprüft

`service.py` erneut auf Config-/XDG-Pfadgrenzen, Unit-Verzeichnis- und
Unit-Ownership, Symlink-/Hardlink-Schutz, private Modi, systemd-Ausgabe- und
Timeoutbudgets, Aktivierungs-/Installations-/Uninstall-Rollbacks sowie
Timer-Statusauswertung geprüft. Kein neuer reproduzierbarer Fehler; doppelte
Unit-Prüfung im Uninstall-Pfad ist redundant, verändert Verhalten aber nicht
und bleibt außerhalb dieses fokussierten Audits.

`pytest -q tests/test_service.py`: 66/66 bestanden; Modul-Coverage 86 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 428: Browser-Config- und Diagnose-Grenzen

Die öffentlichen Browser-Entry-Points prüften bisher nur den `AppConfig`-
Typ. Direkt konstruierte Configs konnten dadurch eine fremde
`analytics_url` bis zum eingeloggten Playwright-Browser durchreichen. Alle
vier Entry-Points validieren die vollständige Config jetzt vor Profil- oder
Browserarbeit; `diagnose --auth-json` weist Fremdtypen kontrolliert zurück.

Regressionen verhindern Navigation zu fremden Hosts sowie Profilanlage bei
ungültigen Eingaben. `pytest -q tests/test_browser_profile.py
tests/test_browser_diagnose.py`: 174/174 bestanden; Modul-Coverage 81 %
(Branch). Die aufrufenden Scheduler-/CLI-Tests bestanden mit 319/319.
Ruff, Mypy und `git diff --check` sauber.

## Runde 429: Service-Reload nach Browser-Config-Fix

`codex-usage service enable --format json` nach `8d94464` erfolgreich:
Units installiert, Timer aktiviert/geplant. Der letzte One-Shot-Lauf endet
weiterhin mit Exit 2 ausschließlich wegen fehlender Firefox-Playwright-
Executable; Journal zeigt keinen neuen Config- oder Browser-Validierungsfehler.

## Runde 430: Render-Tabellen und Anzeigegrenzen

`render.py` auf bounded Iteratoren, Zell-/Textnormalisierung, Tabellenbreiten,
malformed Status-/Zeit-/Fenster-/Pool-Werte, Backend-Provenienz und sichere
Wertausgabe geprüft. Ungültige oder nicht verifizierte Werte bleiben verborgen;
keine neue reproduzierbare GUI-/Tabellenfehlfunktion.

`pytest -q tests/test_render.py`: 66/66 bestanden; Modul-Coverage 87 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 431: Models-DTOs und Fensteridentität

`models.py` auf Fensterdauer-/Namensidentität, Prozent-/Zahlenvalidierung,
Pool-Erschöpfung, malformed Legacy-Felder und JSON-Serialisierung geprüft.
Ungültige optionale Container, Datetimes, Pool-Schlüssel und Fensterwerte
bleiben serialisierbar bzw. werden verworfen; keine neue reproduzierbare
Anzeige- oder State-Fehlfunktion.

`pytest -q tests/test_models.py`: 15/15 bestanden; Modul-Coverage 50 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 432: Strict-JSON erneut geprüft

`json_utils.py` auf Typgrenze, Byte-/String-Scanner, Escapes, strukturelle
Nesting-Grenze, Duplicate-Keys und nicht erlaubte JSON-Konstanten geprüft.
Malformed Eingaben bleiben kontrollierte `ValueError`; kein neuer
reproduzierbarer Fehler.

`pytest -q tests/test_json_utils.py`: 8/8 bestanden; Modul-Coverage 93 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 433: Test-Home-Auth-Rollback-Identität

`add_or_update_account(test_home=True)` verschob Auth-Datei vor Config-/State-
Rollback, prüfte beim Rückweg aber nur `is_file()`. Ein zwischenzeitlich
ersetztes Ziel, insbesondere ein Symlink, konnte dadurch als neue Quelle
zurückgeschoben werden. Verschiebevorgang merkt nun Device/Inode; Rollback
prüft Regular-File, User-Owner, Single-Link, private Modi sowie Symlink-freie
Elternpfade und verweigert geänderte Ziele fail-closed.

Regression reproduziert Zielersetzung. `pytest -q tests/test_config.py`:
117/117 bestanden; Modul-Coverage 82 % (Branch). Ruff, Mypy und
`git diff --check` sauber.

## Runde 434: Private-I/O-Eingabebudgets

`private_io.py` erneut auf Pfad-/Symlink-/Owner-/Hardlink-Grenzen, atomare
Writes, fsync und Locking geprüft. Ergänzt: `read_private_text()` weist
negative, boolesche und Fremdtyp-Bytebudgets vor Dateizugriff zurück;
`write_private_text()` weist Nicht-Strings kontrolliert zurück. Keine weitere
Pfad- oder Lock-Fehlfunktion reproduziert.

`pytest -q tests/test_private_io.py`: 44/44 bestanden; Modul-Coverage 75 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 435: Vollsuite nach Browser-/Config-/Private-I/O-Fixes

Die Integrationsprüfung nach `8d94464`, `9efa94b` und `8b79535` bestätigt
`2799 bestanden, 1 übersprungen, 3 Warnungen` in 94,43 s. Warnungen bleiben
externe GTK-/PyGObject-Deprecations; keine Test- oder GUI-Regression.

## Runde 436: Cinnamon-GUI-Runtime und Formatierungsseite

Die JavaScript-Runtime-Suite prüft Formatierungsziele, unabhängige
Verbrauchs-/Tokenend-/Credit-Tabellen, Element-Zielauflösung, Guard-/Queue-
Freigaben und wiederholtes Cleanup. `node --test tests/applet_runtime.test.js`:
395/395 bestanden. Keine neue GUI-/Element-Regression; GTK-Python-Tests sind
in Runde 435 enthalten.

## Runde 437: Health-Event-Speicher

`health.py` auf Token-/Account-Redaction, Event-/Byte-Limits, Retention,
malformed JSON, strikte Version, private Datei-/Verzeichnisrechte und
Recovery geprüft. Veraltete oder ungültige Events bleiben ausgeschlossen;
keine neue reproduzierbare Fehlfunktion.

`pytest -q tests/test_health.py`: 32/32 bestanden; Modul-Coverage 83 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 438: Usage-Resets-State

`usage_resets.py` auf kanonische/legacy Payloads, Unknown-vs-Zero-Semantik,
Konfliktauflösung, Bounded Counts und Redemption-Gate geprüft. Ungültige oder
widersprüchliche Werte werden unbekannt; Redemption bleibt ohne positive,
bekannte Capability blockiert. Keine neue reproduzierbare Fehlfunktion.

`pytest -q tests/test_usage_resets.py`: 5/5 bestanden; Modul-Coverage 79 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 439: Usage-Limit-Pools

`usage_limits.py` auf WHAM-/App-Server-Pools, Spark-Erkennung, Fenster-
Identität, Duration-/Reset-Budgets, malformed Control-Flags, Duplicate-Buckets
und bounded Model-Kataloge geprüft. Ungültige oder widersprüchliche Fenster
deaktivieren Pools; Katalog-Entitlement erfindet keine Nutzung. Keine neue
reproduzierbare Fehlfunktion.

`pytest -q tests/test_usage_limits.py`: 124/124 bestanden; Modul-Coverage 91 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 440: Consumption-Resetrotation

`consumption._confirmed_reset()` behandelte `reset_at` bisher nur als
vergangenen Eventzeitpunkt. Die Historie speichert dort jedoch den jeweils
nächsten Reset: Nach einer echten Rotation liegt der alte Reset zwischen den
Samples, während der neue wieder in der Zukunft liegt. Verbrauch wurde damit
als `partial` markiert und das Delta nach dem Reset unterschlagen. Die
Erkennung akzeptiert jetzt diesen belegten Übergang, bleibt bei bloßen
zukünftigen Resetverschiebungen konservativ und nutzt dieselbe Korrektur für
EMA-Prognosen.

`pytest -q tests/test_consumption.py`: 32/32 bestanden; Modul-Coverage 84 %
(Branch). Aufrufende History-/CLI-/Integrations-Tests: 309/309 bestanden.
Ruff, Mypy und `git diff --check` sauber.

## Runde 441: Integration-Snapshot-Entry-Point

`integration_entrypoint.py` auf exakte Argumente, XDG-Pfadgrenzen,
Attestierung vor/nach dem Lesen, Producer-Lock, UTC-Zeitbereich,
bounded History-Abfragen, Cost-Window-Projektion, Credits und
fehlerfreie stderr-Tokens geprüft. Keine neue reproduzierbare Fehlfunktion;
ungültige Quelle, Drift, fehlende Historie und externe Attestierungsfehler
bleiben fail-closed ohne Details.

`pytest -q tests/test_integration_entrypoint.py`: 27/27 bestanden;
Modul-Coverage 91 % (Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 442: Integration-Snapshot-Sanitizing

`integration_snapshot.py` auf Current-Reader-Races, private Eigentümer-/Mode-/
Linkgrenzen, Pool-/Fenster-Allowlist, Account-/Cost-Window-/Model-Budgets,
Secret-Scan, UTC-Normalisierung, Duplicate-Identitäten, Strict-JSON und
atomaren Cache-Publish geprüft. Keine neue reproduzierbare Fehlfunktion;
malformed Quellen und nicht kanonische DTOs bleiben `IntegrationInvalidSource`,
Cachepfad und bestehende Bytes bleiben bei Fehlern geschützt.

`pytest -q tests/test_integration_snapshot.py`: 52/52 bestanden;
Modul-Coverage 76 % (Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 443: Formatierungsseite-UI

`FormatTableSelector` und `settings-schema.json` erneut auf eine einzige
sichtbare Gtk-Stack-Tabelle, mittig ausgerichtetes Dropdown, persistente
Auswahl ohne Rückschreibschleife, statische Ziel-Allowlist und getrennte
editierbare Tabellen geprüft. Die sechs Ziele (Prozent, Datum, Uhrzeit,
Restlaufzeit, Account-Anzeige, Elemente/Formatierungsorte) bleiben isoliert;
zusätzliche Elemente 14/15 (Verbrauch 5h/30d) sind enthalten. Keine neue
Tabellenverschachtelung oder Auswahlregression.

`pytest -q tests/test_format_table_selector.py tests/test_applet.py`:
32/32 bestanden; bekannte GTK-/PyGObject-Deprecation-Warnungen bleiben.

## Runde 444: Vollsuite nach Consumption-Reset-Fix

Die Vollsuite nach `1ec2839` bestätigt den Resetrotations-Fix und den
aktuellen Formatierungsseitenstand: `2800 bestanden, 1 übersprungen,
3 Warnungen` in 94,09 s. Warnungen bleiben externe GTK-/PyGObject-
Deprecations; keine Test-, Snapshot- oder GUI-Regression.

## Runde 445: State-Modellpool-Merge

`state._merge_model_pools_with_last_success()` verwendete malformed
`UsagePool.key`-Werte direkt als Dictionary-Schlüssel. Ein unhashbarer
Modellschlüssel konnte den Current-/Last-Success-Merge mit rohem `TypeError`
abbrechen. Beide Katalogseiten verlangen jetzt Stringschlüssel und eindeutige
Current-Keys; ungültige Kataloge bleiben unverändert fail-closed.

Regressionen für unhashbare aktuelle und alte Modellpools ergänzt.
`pytest -q tests/test_state.py`: 269/269 bestanden; Modul-Coverage 88 %
(Branch). Aufrufende Scheduler-Suite: 202/202. Ruff, Mypy und
`git diff --check` sauber.

## Runde 446: State-Pool-Key-Invalidierung

Der ergänzende Expiry-Pfad entfernte malformed Modellpool-Keys zunächst zwar,
ließ den Accountstatus aber `OK`, weil die Kataloginvalidität nur Fensterdaten
prüfte. Nichtleere String-Keys werden jetzt vor Ablaufprüfung verlangt;
entfernte malformed Pools setzen kontrolliert `PARTIAL`, `stale` und den
Katalogfehler.

`pytest -q tests/test_state.py`: 271/271 bestanden; Modul-Coverage 88 %
(Branch). Aufrufende Scheduler-Suite: 202/202. Ruff, Mypy und
`git diff --check` sauber.

## Runde 447: Scheduler-Reset-Provenienz

`scheduler.py` auf Refresh-/Fallback-Entscheidungen, Reset-Discontinuity,
relative versus absolute Reset-Metadaten, Backend-Provenienz und Watchdog-
Gesundheitsprüfung geprüft. Ein Guard fehlte vor dem bestehenden
Fail-Closed-`try`: malformed Window-Objekte ohne `reset_at` konnten die
Stabilisierung mit `AttributeError` verlassen. Zugriff läuft jetzt durch
denselben Guard; ungültige Fenster autorisieren keine Fallback-Wiederverwendung.

`pytest -q tests/test_scheduler.py`: 205/205 bestanden; Modul-Coverage 84 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 448: Routing- und Pool-Auswahl

`routing.py` auf Policy-/Credit-Scope-Auflösung, Spark-vor-Main-Entscheidung,
Pool-/Fensteridentität, Nutzungsevidenz, Reset-/Age-Grenzen, Backend-
Provenienz und fail-closed JSON-Daten geprüft. Ungültige, katalog-only,
abgelaufene oder nicht attribuierte Nutzung bleibt blockiert; keine neue
reproduzierbare Fehlfunktion.

`pytest -q tests/test_routing.py`: 125/125 bestanden; Modul-Coverage 87 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 449: Direct-Auth-Identität und WHAM-Stabilisierung

`direct.py` im Schwerpunkt Auth-Identität/Planbindung und stabilisierte
WHAM-Samples erneut geprüft: Token-/Claim-Budgets, Identitätswechsel,
Account-ID-Ambiguität, Redirect-/Host-Grenzen, Reset-/Progressionsquoren,
Credit-Parsing und Auth-Datei-Eigentum bleiben fail-closed. Keine neue
reproduzierbare Fehlfunktion.

`pytest -q tests/test_direct.py`: 183/183 bestanden; Modul-Coverage 83 %
(Branch). Ruff, Mypy und `git diff --check` sauber.

## Runde 450: Cinnamon-Heap-Härtung für Hilfe und Panel

Die neue Hilfe-Seite erzeugte beim Öffnen alle 404 Feldbeschreibungen sofort als
GTK-Widgets: 1.066 Widgets pro Aufbau. Das belastet Cinnamon-Settings bei jedem
Seitenwechsel und hielt unnötige Actor-/Label-Strukturen bis zum Zerstören der
Seite. Einträge erzeugen ihren Inhalt jetzt erst beim Aufklappen; beim Zuklappen
wird der Inhalt zerstört. Der initiale Aufbau benötigt im aktuellen Schema 146
Widgets. GTK3 unterstützt kein `<br>`-Markup; Zeilenumbrüche werden als gültiges
XML-Zeichen `&#10;` gesetzt, wodurch die vorherige Warnungsflut entfällt.

Das Applet dedupliziert unveränderte Panel-Label-, Markup-, Tooltip- und
Icon-Schreibvorgänge. Display-Timer dürfen weiter rechnen und Warnklassen
prüfen, schreiben aber keine identischen Cinnamon-Oberflächen mehr in jedem
Tick. Der Safe-Mode verwirft den Cache, damit beim Wiederanlauf keine alte
Oberfläche übersprungen wird.

Regressionen:

- `pytest -q tests/test_help_page.py tests/test_format_table_selector.py tests/test_applet.py`: 38/38 bestanden.
- `node --test tests/applet_runtime.test.js`: 407/407 bestanden.
- `make applet-check`, Ruff, Python-Compile, JSON-Parse und `git diff --check` sauber.

## Runde 451: Prognosen durchschaltbar

Die drei Prognosentabellen lagen bisher gleichzeitig auf der Seite. Die Seite
zeigt jetzt einen mittig ausgerichteten Dropdown-Umschalter mit genau einer
sichtbaren Tabelle: Tokenverbrauch, Tokenende oder Creditverbrauch. Jede Tabelle
bleibt unter ihrem bisherigen JSON-Schlüssel gespeichert; bestehende Account-
Zeilen und Formatierungen brauchen keine Migration. Der Umschalter speichert
seine Auswahl unter `forecast-table-selector` und fällt bei unbekanntem Wert auf
Tokenverbrauch zurück. Stack-Animation ist deaktiviert, damit der Seitenwechsel
keine zusätzliche Cinnamon-Oberflächenlast erzeugt.

Die bestehende Tabellenbindung wird wiederverwendet. Installer und Hilfe-Schema
kennen die neue Widget-Datei; die Hilfe-Seite führt Umschalter und alle drei
Prognoseziele mit ihren Feldbeschreibungen auf.

Regressionen:

- `pytest -q tests/test_forecast_table_selector.py tests/test_format_table_selector.py tests/test_help_page.py tests/test_applet.py`: 41/41 bestanden.
- Ruff, Python-Compile, JSON-Parse und `git diff --check` sauber.

## Runde 452: Leisten-Editor nebeneinander

Der Leisten-Editor verwendete bisher den Cinnamon-Standarddialog mit einem
Feld pro Zeile. Bei vielen Wertfeldern wurde der Dialog dadurch höher als der
Monitor. `PanelSettingsList` verwendet jetzt für Doppelklick und Bearbeiten
einen begrenzten, vertikal scrollbaren GTK-Grid. Die Einstellung `Spalten im
Leisten-Editor` liegt auf der Seite Einstellungen unter Leiste, erlaubt 2, 3,
4 oder 5 Spalten und hat Default 3. Der Grid ändert nur die Anordnung; die
bestehenden Zeilen, Schlüssel, Reihenfolge und Speicherwerte bleiben gleich.

Die Hilfe beschreibt die neue Einstellung. Tests prüfen ungültige Werte,
Grenzen, echte Grid-Positionen und den Rückgabepfad geänderter Felder.

## Runde 453: DynamicSeriesList akzeptiert GTK-TreeModelRow

`DynamicSeriesList` prüfte Tabellenzeilen bisher zuerst auf `list` oder
`tuple`. Cinnamon liefert beim Bearbeiten jedoch `Gtk.TreeModelRow`: indexierbar,
aber kein `list`/`tuple` und ohne `len()`. Dadurch wurden aktive Serien in der
Besitzerkarte übersprungen; beim Doppelklick konnte die aktuelle Serie aus dem
Dropdown verschwinden. Besitzer- und Current-Assignment-Erkennung greifen nun
kontrolliert per Index zu, validieren Account-/Serienstrings und ignorieren nur
wirklich unbrauchbare Rows.

Regressionen decken echte TreeModelRow-Form ab; `tests/test_dynamic_series_list.py`
läuft mit 10/10 Tests. Ruff, Python-Compile und `git diff --check` sauber.

## Runde 454: Hilfe-Markup bewahrt primitive Werte

`help_page._markup()` machte alle falsy Werte über `text or ""` leer. Dadurch
wurden numerische oder boolesche Schemawerte `0` und `False` in generierten
Hilfetexten unterschlagen. Nur `None` gilt nun als leer; alle anderen Werte
werden vor GTK-Markup-Escaping als Text erhalten.

Regressionen prüfen `0` und `False` zusätzlich zum bestehenden Escaping und
Zeilenumbruch. `tests/test_help_page.py`: 5/5 bestanden; Ruff,
Python-Compile und `git diff --check` sauber.

## Runde 520: Integrations-Snapshot exportiert Custom-Limits

`integration_snapshot._pool_windows()` akzeptierte bisher nur die drei festen
Dauern 5h, Woche und 30 Tage. Frei konfigurierte Limitfenster wurden dadurch
aus `limits` entfernt, obwohl Modell, History, CLI, Snapshot-Kanonisierung und
Applet diese Dauern unterstützen.

Die Projektion akzeptiert jetzt jede positive, bekannte Fensterdauer bis zur
zentralen History-Obergrenze von 30 Tagen. Regression prüft ein 1-Tage-Fenster
mit Rest- und Verbrauchsprozent. `pytest -q tests/test_integration_snapshot.py
tests/test_integration_entrypoint.py`: 81/81; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 455: Selector-Tabellen lazy aufbauen

`FormatTableSelector` und `ForecastTableSelector` legten beim Seitenaufbau
bisher jede deklarierte Tabelle als vollständige GTK-`TreeView` mit Modell und
Toolbar an. Sichtbar war nur eine Tabelle, aber alle Formatierungsziele und
alle drei Prognoseziele blieben im Cinnamon-Heap. Die Selector-Seiten halten
jetzt zunächst nur Allowlist, Label und Schema-Definition; die gebundene
Tabelle entsteht erst bei Auswahl. Beim Umschalten werden bisherige
TreeViews samt JSON-Listener entfernt, bevor neue entstehen; beim Schließen
werden verbleibende Widgets ebenfalls freigegeben. Doppelte deklarierte
Schlüssel werden ignoriert.

Regressionen prüfen initial genau eine erzeugte Tabelle, vollständige leichte
Definitionen und Aufbau beim ersten Umschalten. `pytest -q
tests/test_format_table_selector.py tests/test_forecast_table_selector.py`:
10/10 bestanden; aufrufende Applet-, Hilfe-, Panel- und Serien-Suiten:
47/47 bestanden. Ruff, Python-Compile, `git diff --check`, JSON-/JS-Prüfung
und `make install-local` mit `reload=ok` sauber.

## Runde 456: Cinnamon-Loader-Pfad für Forecast-Selector

`xlet-settings` lädt Custom-Widgets per `spec_from_file_location()` und legt
deren Applet-Verzeichnis nicht in `sys.path`. `forecast_table_selector.py`
importierte trotzdem `_BoundFormatList` als Top-Level-Nachbarmodul. Dadurch
brach der echte Einstellungsstart mit
`ModuleNotFoundError: No module named 'format_table_selector'` ab; Python-Tests
hatten den Fehler verdeckt, weil sie den Applet-Pfad manuell voranstellten.
Der Forecast-Selector trägt seinen eigenen Verzeichnispfad jetzt kontrolliert
vor dem Schwesterimport ein.

Ein Loader-Regressionsfall entfernt den Applet-Pfad aus `sys.path` und lädt die
Datei wie Cinnamon. `pytest -q tests/test_forecast_table_selector.py
tests/test_format_table_selector.py tests/test_applet.py tests/test_help_page.py
tests/test_panel_settings_list.py tests/test_dynamic_series_list.py`:
58/58 bestanden. `xlet-settings applet codex-usage@H234598 -i 0` startet nach
Installation ohne Traceback und bleibt als GUI-Prozess offen; der Testlauf
wurde nach 8 Sekunden kontrolliert beendet. Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 457: Selector-Lifecycle explizit abgesichert

Der lazy Selector-Lifecycle wurde nach dem Loader-Fix nochmals isoliert
geprüft. Beim Destroy und beim Wechsel zwischen Tabellen muss genau der aktive
JSON-Listener verschwinden; sonst halten Cinnamon-Callbacks bereits zerstörte
GTK-TreeViews fest. Beide Selector-Implementierungen entfernen die Listener
und zerstören ihre letzte Tabelle idempotent.

Neue Regressionen prüfen Listener-Abmeldung und leeren Tabellenbestand nach
`destroy()`. `pytest -q tests/test_forecast_table_selector.py
tests/test_format_table_selector.py`: 13/13 bestanden; bekannte GTK- und
PyGObject-Deprecation-Warnungen bleiben extern.

## Runde 458: Fast-Mode-SVG-Lader bleibt fail-safe

`FastModeIconSelector` ließ `GLib.Error` aus
`GdkPixbuf.Pixbuf.new_from_file_at_scale()` hochlaufen. Eine beschädigte oder
unvollständige SVG-Datei konnte damit den gesamten Cinnamon-Settings-Aufbau
abbrechen, obwohl die Auswahl selbst weiterhin als Dateiname nutzbar ist.
Der Icon-Lader fängt `GLib.Error` jetzt zusammen mit den bisherigen lokalen
Dateifehlern ab und lässt die Zeile ohne Vorschau weiterlaufen.

Regression mit echtem kaputtem SVG: `pytest -q
tests/test_fast_mode_icon_selector.py`: 5/5 bestanden. Ruff, Python-Compile
und `git diff --check` sauber.

## Runde 459: Panel-Editor gibt Settings-Listener frei

`PanelSettingsList` registriert beim Aufbau einen Listener für die eigene
Tabelle und einen zweiten für `panel-value-count`. Beim Schließen der
Cinnamon-Settings-Seite wurden beide Callback-Referenzen bisher behalten;
jedes erneute Öffnen hielt damit eine alte GTK-Tabelle samt Modell im Heap.
Der Editor entfernt beide gebundenen Callbacks vor `destroy()` und bleibt bei
wiederholtem Destroy idempotent.

Zusätzliche Regressionen prüfen Listener-Abmeldung sowie den Rebuild-Pfad bei
Änderung der Wertfeldanzahl mit erhaltener Zeile. `pytest -q
tests/test_panel_settings_list.py`: 7/7 bestanden. Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 460: DynamicSeriesList gibt Listener frei

`DynamicSeriesList` registrierte über `JSONSettingsBackend.attach()` einen
Account-Tabellen-Listener, hielt ihn beim Schließen der Seite aber weiter im
Settings-Objekt. Wiederholtes Öffnen konnte so alte GTK-Tabellen und Modelle
festhalten. `detach()` entfernt den gebundenen Callback; `destroy()` ruft es
vor dem GTK-Teardown auf.

Regressionen prüfen echten Widget-Aufbau, Listener-Abmeldung und die bisher
ungetestete temporäre Serien-Spaltenfilterung samt Schema-Rücksetzung.
`pytest -q tests/test_dynamic_series_list.py`: 12/12 bestanden. Ruff,
Python-Compile und `git diff --check` sauber.

## Runde 461: Hilfe-Feldtexte enthalten Beschreibung und Tooltip

`help_page._field_text()` verwendete bisher nur `description` oder — falls
fehlend — `tooltip`. Bei `account-consumption-settings.custom-format` ging
dadurch die zusätzliche Platzhalter-/Coverage-Hilfe verloren. Feldtexte
führen jetzt beide bereinigten Texte zusammen und deduplizieren identische
Inhalte.

Regression prüft getrennte Beschreibung und Tooltip zusätzlich zu Optionen,
Defaults, Grenzen und Markup-Escaping. `pytest -q tests/test_help_page.py`:
5/5 bestanden; Ruff, Python-Compile und `git diff --check` sauber.

## Runde 462: Cinnamon-Installer-Migration idempotent

`scripts/install_cinnamon_applet.py` setzte bei jedem Lauf der
Schema-Aktualisierung `changed = True`, selbst wenn Cache-Schema und
`__md5__` bereits identisch waren. Jeder erneute Installationslauf schrieb
dadurch die Cinnamon-Settings-Datei atomar neu und meldete `updated`.
Die Migration berechnet den Schema-Digest nur einmal und schreibt nur bei
echter Schema-, Enum- oder Wertänderung.

Neue fokussierte Suite prüft alle Installer-Funktionen: Quell-/Zielpfad-
Validierung, atomaren Austausch, Cache-Migration, Enum-Konvertierung,
DBus-/Versionspfade und `main --dry-run`. `pytest -q
tests/test_install_cinnamon_applet.py`: 9/9 bestanden. Ruff, Python-Compile
und `git diff --check` sauber.

## Runde 463: Cinnamon-Uninstaller-CLI vollständig abgegrenzt

`uninstall_cinnamon_applet.py` hatte bisher nur indirekte Abdeckung über den
Installations-Roundtrip. Die fokussierte Suite prüft jetzt alle relevanten
Pfade: Dry-Run, wiederholtes Entfernen eines fehlenden Ziels, erfolgreiches
Entfernen, Symlink-/Datei-Schutz, unsichere Zielpfade, Löschfehler und die
Verzeichnisprüfung. Es wurde kein neuer Fehler im Uninstaller reproduziert;
unsichere Ziele bleiben unverändert.

`pytest -q tests/test_uninstall_cinnamon_applet.py`: 8/8 bestanden. Ruff,
Python-Compile und `git diff --check` sauber.

## Runde 464: DynamicSeries-Timeout-Cleanup bleibt bei Exit-Race stabil

`DynamicSeriesList._masterjet_series()` fing einen fehlgeschlagenen
`killpg()`-Aufruf ab, rief `process.kill()` im Fallback aber ungeschützt auf.
Wenn der Child-Prozess genau zwischen Poll und Signal bereits beendet war,
warf der Fallback selbst `ProcessLookupError`; damit konnte eine harmlose
Masterjet-Serienabfrage den Settings-Dialog-Callback verlassen.

Der Fallback ignoriert jetzt denselben Exit-Race-Fehler wie die übrigen
begrenzten Prozesspfade. Regression reproduziert den Race mit einem
beendeten Child-Doppel zuerst rot und besteht danach grün. `pytest -q
tests/test_dynamic_series_list.py`: 13/13 bestanden; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 465: Leisten-Limitquellen nutzen deklarierte Fenster

Die Quellen `Limit 30 Tage` und `Limit Spark sonstiges` wurden per einfachem
`source - 36` auf falsche Fenster abgebildet: 30 Tage wurde als Wochenlimit,
Spark sonstiges als Spark-Wochenlimit gelesen. Zusätzlich konnte
`main-other` das 30-Tage-Fenster als „sonstiges“ auswählen, wenn kein kleinerer
Wert vorlag. Explizite Quellenzuordnung für Render-, Fenster-, Warn- und
Slotwertpfad sowie ein monatliches Ausschlussflag trennen jetzt 30 Tage von
sonstigen Hauptfenstern; Spark behält sein kompatibles Other-Verhalten.

Regression prüft 18/19 sowie alle sechs Limitquellen numerisch und rendert
30-Tage-/Spark-Other-Werte. `node --test tests/applet_runtime.test.js`:
408/408 bestanden; `node --check` und `git diff --check` sauber.

## Runde 466: Serienzuordnung im Konto-Editor löschbar

Der Wert `Keine Serie` wurde beim Konto-Speichern nicht als Löschung
übertragen: Das Applet ließ `--series` bei leerem Wert weg, worauf die CLI die
bisherige Serie behielt; zusätzlich lehnte `add_or_update_account()` einen
explizit leeren Serienwert ab. Das Applet übergibt ein vorhandenes leeres
Serienfeld jetzt ausdrücklich, und die Konfigurationsvalidierung erlaubt leere
Serienwerte zum Entfernen einer Zuordnung. `series-active=false` bleibt dabei
erforderlich und wird vom Editor mitgesendet.

Regression reproduzierte zuerst den Validierungsfehler und den fehlenden
CLI-Schalter. Danach: `pytest -q tests/test_config.py -k
clear_existing_series` 1/1 und `node --test tests/applet_runtime.test.js`:
409/409 bestanden.

## Runde 467: Kürzel im Konto-Editor tatsächlich löschbar

Das gleiche Editor-Muster verhinderte das Leeren eines bestehenden Kürzels:
Ein explizit leerer Tabellenwert wurde vor dem CLI-Aufruf durch das kanonische
Kürzel ersetzt. Die Synchronisierung unterscheidet jetzt fehlendes Feld
(Legacy-Kompatibilität) von explizit leerem Feld; beim Speichern wird `--tag ""`
übertragen und entfernt das Kürzel.

Regression deckt Synchronisierungszeile und CLI-Argument ab. `node --test
tests/applet_runtime.test.js`: 411/411 bestanden; `node --check` und
`git diff --check` sauber.

## Runde 468: Einstellungsfenster wieder erreichbar

Der Einstellungslauncher selbst war korrekt; der Dialog brach beim Laden
eines Prognose-Widgets ab, weil Cinnamon Custom-Widgets per Dateipfad lädt
und `forecast_table_selector.py` die Schwesterdatei
`format_table_selector.py` ohne deren Verzeichnis im `sys.path` importierte.
Die Pfad-Härtung aus `94467a8` ist installiert und geprüft. Der echte Aufruf
`xlet-settings applet codex-usage@H234598 -i 14` öffnet wieder das Fenster
„Codex Usage“; der Loader-Test besteht. `pytest -q
tests/test_forecast_table_selector.py`: 5/5 und die Settings-Launcher-
Regressionen in `node --test tests/applet_runtime.test.js`: bestanden.
Applet danach installiert und Cinnamon-Applet neu geladen.

## Runde 469: Consumption-Reset zählt neuen Zyklus vollständig

Bei bestätigter Resetrotation prüfte `calculate_consumption()` den Reset erst
nach dem Vorzeichen des Deltas. Stieg der neue Zyklus bereits über den alten
Prozentwert (z. B. 90 % vor Reset, 95 % danach), wurden nur 5 statt 95
Verbrauchs-Punkte gezählt. Derselbe Fehler verfälschte die EMA-Prognose.

Reset-Erkennung läuft jetzt vor der Delta-Behandlung; bei bestätigtem Reset
wird der aktuelle Zykluswert in Roh- und EMA-Rate übernommen. Regression deckt
beide Pfade ab. `pytest -q tests/test_consumption.py`: 36/36; aufrufende
History-/Integrationstests: 15/15; Ruff und `git diff --check` sauber.

## Runde 470: 30-Tage-Historie aus Fenster-Aliasen speichern

`LimitWindow` akzeptiert `30d`, `30_day`, `month` und `monthly` als bekannte
30-Tage-Fenster. `history._iter_usage_samples()` hatte ohne explizites
`duration_seconds` jedoch nur 5h- und Wochen-Aliase abgebildet; valide
30-Tage-Werte verschwanden dadurch still aus der Historie.

Alle vier Aliase verwenden jetzt `MAX_HISTORY_WINDOW_SECONDS`. Regression:
`pytest -q tests/test_history.py`: 82/82; aufrufende History-/CLI-/
Integrationstests: 7/7; Ruff, Python-Compile und `git diff --check` sauber.

## Runde 471: Health-Test an Directory-FD-Modusbindung angepasst

Der bestehende Health-Fehlerpfad mockte nach der Private-I/O-Härtung noch
`Path.chmod()`, obwohl `ensure_private_directory()` Verzeichnisrechte nun
atomar über geöffneten Directory-FD und `os.fchmod()` setzt. Dadurch war die
Suite auf aktuellem HEAD rot und prüfte nicht mehr den realen Fehlerast. Der
Test injiziert jetzt `os.fchmod()` direkt; Produktionscode unverändert.

`pytest -q tests/test_health.py tests/test_private_io.py`: 77/77 bestanden;
Ruff, Python-Compile und `git diff --check` sauber.

## Runde 472: Dynamische Leistenwerte starten ihre Verbrauchsabfrage

Die Leiste unterstützt bis zu 64 Wertfelder, aber `_refreshConsumption()`
prüfte für Creditverbrauch nur die alten vier Slots. Ein Creditverbrauch in
Wert 5 oder höher wurde korrekt gerendert, löste jedoch keinen Credit-Request
aus und blieb deshalb bei `–`.

Die Request-Ermittlung iteriert jetzt über die konfigurierte Wertanzahl.
Regression setzt `Creditverbrauch` in Wert 20 und erwartet Pool `credits`.
`node --test tests/applet_runtime.test.js`: 412/412; `node --check` und
`git diff --check` sauber.

## Runde 473: Delta-/Tokenende-Leistenquellen fordern ihre Daten an

Die neuen Leistenquellen `TE`, `Δ`, `Δ5h`, `ΔW`, `ΔM`, `ΔSpark` und
`Δsonst.` wurden nur gerendert. Wenn die alten Tabellenziele deaktiviert
waren, startete keine Verbrauchsabfrage; bei festem Deltafenster wurde zudem
nicht garantiert das passende Poolfenster geladen. Die Leistenquelle selbst
aktiviert jetzt den Bedarf, fordert feste Deltafenster mit `all` an und lädt
bei Bedarf zusätzlich Main- oder Spark-Pool.

Regressionen decken Tokenende, konfiguriertes Delta, Main-/Spark-Delta und
beide Poolwechsel ab. `node --test tests/applet_runtime.test.js`: 415/415;
`node --check` und `git diff --check` sauber.

## Runde 474: Panel-Delta verwendet neuesten Fensterwert

Nach getrennten Verbrauchsabfragen können alte und neue Werte mit gleichem
Pool und Fenster gleichzeitig in `cost_windows` liegen. `_panelDeltaPart()`
nahm bisher immer den ersten Treffer und zeigte dadurch einen veralteten
Deltawert. Die Suche läuft jetzt vom zuletzt angehängten Wert rückwärts;
Fallback-Suche für konfiguriertes Delta folgt derselben Regel.

Regression mit altem `4 %` und neuem `9 %`: `node --test
tests/applet_runtime.test.js`: 416/416; `node --check` und `git diff --check`
sauber.

## Runde 475: `consumption --limit-window all` enthält 30 Tage

Der CLI-Modus `all` lieferte bislang nur 5-Stunden- und Wochenfenster.
Dadurch konnte die Leistenquelle `ΔM` trotz aktivierter Abfrage kein
monatliches `cost_window` erhalten. `all` umfasst jetzt 5h, Woche und 30
Tage; die CLI-Kurzübersicht nennt alle gültigen Fenster.

Regression prüft die drei ausgegebenen Fenster. `pytest -q
tests/test_history_cli.py`: 5/5; Ruff, Python-Compile und `git diff --check`
sauber.

## Runde 476: `all` enthält sonstige Historienfenster

`Δsonst.` kann ein frei konfiguriertes Fenster darstellen. Der CLI-Modus
`consumption --limit-window all` fragte bisher jedoch nur 5h, Woche und 30
Tage ab; historische Samples mit anderer Fensterdauer wurden unterschlagen.

`HistoryStore.consumption_window_seconds()` liefert jetzt bis zu 64 distinct
Fensterdauern aus dem begrenzten Abfragezeitraum. Das entspricht der maximalen
Anzahl konfigurierbarer Leistenwerte. `all` führt diese Werte in stabiler
Reihenfolge hinter den bekannten Fenstern zusammen. Regressionen decken
History-Auflistung, ein 1-Tage-Fenster und 33 konfigurierte Fenster ab.

`pytest -q tests/test_history.py tests/test_history_cli.py`: 90/90;
Ruff, Python-Compile und `git diff --check` sauber.

## Runde 477: Cinnamon akzeptiert alle konfigurierbaren Verbrauchsfenster

Der Backend-Query kann bis zu 64 konfigurierte Verbrauchsfenster liefern.
`applet.js` verwarf jedoch weiterhin jede `cost_windows`-Liste mit mehr als 32
Einträgen. Bei 33–64 Leistenwerten verschwanden damit Verbrauchs- und
Tokendelta-Daten trotz korrekter CLI-Antwort.

Der Sanitizer nutzt jetzt dieselbe Grenze 64 wie History, Panel und
Integrationsvertrag. Regression prüft 64 akzeptierte und 65 verworfene
Fenster. `node --test tests/applet_runtime.test.js`: 416/416; `node --check`
und `git diff --check` sauber.

## Runde 478: Tokendelta-Dynamik nutzt passendes Fenster

`Δsonst.` wählte ein bestimmtes Verbrauchsfenster aus `cost_windows`,
`_panelDeltaIsDynamic()` prüfte die Hochrechnung aber gegen das global
knappste sonstige Poolfenster. Bei mehreren frei konfigurierten Dauern konnte
dadurch ein anderes Restlimit die dynamische Schwelle auslösen.

Die Projektion sucht jetzt exakt Fensterdauer und Pool des angezeigten
Kandidaten; 5h/Woche behalten ihre Top-Level-Fallbacks. Regression mit
1-Tage- und 2-Tage-Fenster verhindert Verwechslung. `node --test
tests/applet_runtime.test.js`: 416/416; `node --check` und `git diff --check`
sauber.

## Runde 479: Dynamische Schwelle ignoriert stale/insufficient

`_panelDeltaIsDynamic()` prüfte bisher nur Delta, Rückblick und Restfenster.
Auch `coverage: "stale"` oder `"insufficient"` konnte dadurch die dynamische
Schwelle aktivieren. Der Verbrauchsvertrag behandelt solche Werte als
unbekannt; nur `complete` und frische `partial`-Daten sind für die Statistik
belastbar.

Die Dynamik bleibt bei stale/insufficient jetzt aus. Regression deckt stale
gegen complete ab. `node --test tests/applet_runtime.test.js`: 416/416;
`node --check` und `git diff --check` sauber.

## Runde 480: Dynamik benötigt bekanntes Restlimit

Bei `remaining=null` ersetzte `_panelDeltaIsDynamic()` das unbekannte
Restlimit bisher durch ein künstliches `100`. Ein ausreichend großes Delta
konnte dadurch trotz fehlender Limitinformation die dynamische Warnformatierung
aktivieren.

Unbekannte Restprozente bleiben jetzt fail-closed; die Projektion aktiviert
Dynamik nur bei belastbarem Restlimit. Regression deckt fehlendes `remaining`
gegen ein bekanntes Limit ab. `node --test tests/applet_runtime.test.js`:
416/416; `node --check` und `git diff --check` sauber.

## Runde 481: Integrations-Snapshot liefert Monats- und Custom-Verbrauch

`integration_entrypoint._load_cost_windows()` fragte für den Main-Pool bisher
nur 5h und Woche ab. Monatswerte und gespeicherte frei konfigurierte
Fensterdauern fehlten dadurch im Integrations-Snapshot, obwohl History, CLI,
Snapshot-Vertrag und Applet diese Fenster bereits unterstützen.

Der Producer nimmt jetzt 5h, Woche, 30 Tage und bis zu 64 distinct im
begrenzten Zeitraum gespeicherte Main-Fenster. Doppelte Dauern werden entfernt
und die bestehende Obergrenze vor der Snapshot-Erzeugung eingehalten.
Regression prüft 30 Tage und ein Custom-Fenster. `pytest -q
tests/test_integration_entrypoint.py tests/test_integration_snapshot.py
tests/test_history.py tests/test_history_cli.py`: 170/170; Ruff,
Python-Compile und `git diff --check` sauber.

## Runde 521: Integrationsvertrag dokumentiert Label-Allowlist korrekt

Der Integrationscode exportiert absichtlich nur Account-ID, Status,
Frischeinformationen, Limits, Verbrauch und Resets. Der Vertragstext nannte
zusätzlich Labels, obwohl der bestehende Allowlist-Regressionstest Labels und
Provider-Metadaten explizit ausschließt.

`docs/codex-usage-v1.md` beschreibt jetzt korrekt, dass Labels außerhalb des
Cross-Process-Vertrags bleiben. Keine Produktionsänderung erforderlich;
bestehende `tests/test_integration_snapshot.py`: 53/53 bestanden.

## Runde 522: Installer-Parent nach mkdir erneut gebunden

`integration_installer._require_private_dir(create=True)` erzeugte ein Ziel
relativ zum geprüften Parent-FD, prüfte danach aber nur den gleichnamigen
Pfad. Nach einem Parent-Swap mit einem fremden, ebenfalls privaten Ziel konnte
dieses fremde Verzeichnis als neu erzeugtes Ziel akzeptiert werden.

Nach der Creation werden Parent-Inode und aktueller Pfad-Parent erneut gegen
die erwartete Identität geprüft. Regression deckt ein gleichnamiges fremdes
Ziel ab. `pytest -q tests/test_integration_installer.py`: 134/134; Ruff,
Python-Compile und `git diff --check` sauber.

## Runde 523: Aktivierungsdatei vor Installer-Unlink revalidiert

`_remove_activation_files()` sammelte bisher nur Dateinamen. Ein Austausch
zwischen Enumeration und Unlink konnte dadurch eine fremde Datei gleichen
Namens löschen.

Vor jedem Unlink werden jetzt Typ, Inode, Gerät, Besitzer, Modus und Linkanzahl
gegen die Enumeration geprüft; Abweichung bricht fail-closed. Regression deckt
ein ersetztes `activate` ab. `pytest -q tests/test_integration_installer.py`:
135/135; Ruff, Python-Compile und `git diff --check` sauber.

## Runde 524: `lib64`-Symlink vor Installer-Unlink revalidiert

Auch der optionale `lib64`-Symlink wurde bisher nach einer einzelnen
`stat()`-Prüfung direkt per Dateiname gelöscht. Ein Austausch zwischen Prüfung
und Unlink konnte dadurch einen fremden Symlink treffen.

Vor dem Unlink werden jetzt Identität und Linkanzahl erneut geprüft;
Abweichungen brechen fail-closed. Regression deckt einen ersetzten `lib64`-
Symlink ab. `pytest -q tests/test_integration_installer.py`: 136/136; Ruff,
Python-Compile und `git diff --check` sauber.

## Runde 525: Cleanup-Ziel vor destruktiver Operation revalidiert

`_remove_owned_entry()` prüfte Parent und Ziel zunächst korrekt, führte danach
aber `unlink`, `rmdir` oder `rmtree` ohne zweite Zielprüfung aus. Ein Austausch
zwischen Prüfung und Cleanup konnte dadurch ein fremdes gleichnamiges Ziel
treffen.

Vor der destruktiven Operation werden Typ, Inode, Gerät, Besitzer und Modus
erneut verglichen; bei Dateien wird zusätzlich die Linkanzahl geprüft.
Regression deckt ersetztes `candidate.json` ab. `pytest -q
tests/test_integration_installer.py`: 137/137; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 526: Final-Release-Rename ohne Zielüberschreibung

`_rename_owned_directory()` verwendete bisher `os.rename()`. Das ersetzt ein
gleichnamiges Ziel, wenn es nach der Existenzprüfung, aber vor dem Rename
angelegt wird. Zusätzlich fehlte eine letzte Source-Identitätsprüfung.

Der Installer nutzt jetzt Linux `renameat2(RENAME_NOREPLACE)`, revalidiert den
Source-Eintrag direkt davor und bricht bei fehlender Unterstützung fail-closed
ab. Regressionen decken vorhandenes Ziel, Zielanlage am Rename-Seam und
ersetzte Source ab. `pytest -q tests/test_integration_installer.py`: 140/140;
Ruff, Python-Compile und `git diff --check` sauber.

## Runde 527: Source-Inode vor regulärem Copy gebunden

`_copy_regular()` prüfte Quelle zunächst per `lstat()`, rief
`_read_nofollow()` bei fehlender Caller-Identität danach aber ohne erwartete
Dateiidentität auf. Ein Austausch zwischen beiden Schritten konnte eine
fremde gleichnamige Datei in Build oder temporäre Kopie übernehmen.

Die beim ersten Check ermittelte Dateiidentität wird jetzt immer an den
geöffneten Read-FD gebunden. Regression deckt ersetzten Source ab. `pytest -q
tests/test_integration_installer.py`: 141/141; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 528: Fehlgeschlagenes reguläres Copy räumt Zielartefakt auf

`_copy_regular()` erzeugte das exklusive Ziel vor dem Lesen der Quelle. Schlug
der gebundene Read danach fehl, blieb eine leere oder partielle Zieldatei im
Build-/Temp-Baum zurück.

Das Ziel wird bei Fehler jetzt über seine Provisional-Identität und den
gebundenen Parent-FD entfernt; fremder Ersatz bleibt unangetastet. Regression
prüft Source-Race und Artefaktfreiheit. `pytest -q
tests/test_integration_installer.py`: 141/141; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 529: Exklusiver Write nach FD-Check erneut am Pfad gebunden

`_write_exclusive()` prüfte den erzeugten Eintrag nach `fsync()` einmal per
Pfad und validierte danach nur noch den offenen FD. Ein Rename des eigenen
Inodes auf einen anderen Namen plus fremder Ersatz am Zielpfad konnte dadurch
als erfolgreicher Write zurückkehren.

Nach der FD-Validierung wird der Pfad jetzt nochmals gegen die erwartete
Provisional-Identität geprüft. Regression deckt den Rename-/Ersatz-Seam ab.
`pytest -q tests/test_integration_installer.py`: 142/142; Ruff,
Python-Compile und `git diff --check` sauber.

## Runde 530: Interpreter-Identität nach Ausführbarkeitsprüfung revalidiert

`_resolve_python_executable()` prüfte den aufgelösten Interpreter per `lstat()`
und `os.access()`, gab danach aber ohne erneuten Identitätsvergleich zurück.
Ein Austausch zwischen diesen Schritten konnte einen fremden Interpreter
akzeptiert machen.

Nach `os.access()` werden jetzt Gerät, Inode, Besitzer, Typ, Modus und
Linkanzahl erneut verglichen. Regression deckt ersetzten Interpreter ab.
`pytest -q tests/test_integration_installer.py`: 143/143; Ruff,
Python-Compile und `git diff --check` sauber.

## Runde 531: Installer-Cleanup löscht nur eigene Aktivierungsobjekte

`_remove_activation_files()` löschte passende `activate`-/`python3`-Einträge
und `lib64`-Symlinks unabhängig von deren Besitzer. Ein fremdes Objekt mit
passendem Namen konnte dadurch entfernt werden.

Vor Enumeration und Unlink wird jetzt die aktuelle UID verlangt;
Fremdobjekte brechen fail-closed ab. Regressionen decken fremdes `activate`
und fremdes `lib64` ab. `pytest -q tests/test_integration_installer.py`:
145/145; Ruff, Python-Compile und `git diff --check` sauber.

## Runde 532: Postwalk akzeptiert nur eigene Release-Objekte

`_postwalk_release()` prüfte bisher Typ, Inode und Linkanzahl, aber nicht die
Besitzer-UID von Dateien und geöffneten Unterverzeichnissen. Fremde Objekte
konnten dadurch in den Release-Scan gelangen.

Dateien und Verzeichnisse werden jetzt während Enumeration und FD-Öffnung auf
aktuelle UID geprüft; Abweichung bricht fail-closed. Regression deckt fremde
Release-Datei ab. `pytest -q tests/test_integration_installer.py`: 146/146;
Ruff, Python-Compile und `git diff --check` sauber.

## Runde 533: Postwalk-Root ebenfalls auf Besitzer gebunden

Der Root-Descriptor von `_postwalk_release()` wurde bei fehlender übergebener
Identität nur auf Verzeichnis-/Symlink-Typ geprüft. Ein fremd besessenes Root-
Verzeichnis konnte den Scan dadurch passieren.

Auch der geöffnete Root wird jetzt auf aktuelle UID geprüft. Regression deckt
fremden Root-Descriptor ab. `pytest -q tests/test_integration_installer.py`:
147/147; Ruff, Python-Compile und `git diff --check` sauber.

## Runde 534: Directory-Cleanup löscht nur eigene Ziele

`_remove_owned_entry()` prüfte bei Directory-Identitäten bisher nur Typ,
Inode, Gerät und Modus. Ein fremd besessenes gleiches Directory konnte dadurch
an `rmtree()` gelangen.

Directory-Cleanup verlangt jetzt ebenfalls aktuelle UID vor jeder Entfernung.
Regression deckt fremden Besitzer ab. `pytest -q
tests/test_integration_installer.py`: 148/148; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 535: Installer-Reader verwirft Größen-Drift

`_read_nofollow()` begrenzte bisher nur die maximale Read-Länge. Schrumpfte
oder wuchs eine reguläre Datei zwischen `fstat()` und Read, konnte ein
verkürztes oder verändertes Payload trotzdem zurückgegeben werden.

Die gelesene Länge muss jetzt exakt der geprüften `st_size` entsprechen.
Regression deckt Größen-Drift nach Öffnung ab. `pytest -q
tests/test_integration_installer.py`: 149/149; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 536: Wheel-Reader erzwingt Header-Payload-Größe

`_read_bounded_wheel_member()` begrenzte bisher Read-Länge und Restdaten,
verglich die gelesene Payload aber nicht mit `ZipInfo.file_size`. Ein
inkonsistenter Header konnte dadurch als gültiges Member weiterlaufen.

Die Payload-Länge muss jetzt exakt der Headergröße entsprechen. Regression
deckt Header-Size-Drift ab. `pytest -q tests/test_integration_installer.py`:
150/150; Ruff, Python-Compile und `git diff --check` sauber.

## Runde 537: Launcher-Write an Parent-Identität gebunden

`_write_launcher()` rief `_write_exclusive()` bisher ohne erwartete
Parent-Identität auf. Ein Parent-Swap nach dem letzten Pfad-Check konnte
den Launcher in einen fremden gleichnamigen User-Ordner schreiben.

Der Parent wird jetzt vor dem exklusiven Write gebunden und beim FD-Open
erneut geprüft. Regression deckt den Parent-Swap ab. `pytest -q
tests/test_integration_installer.py`: 151/151; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 538: Prognose-Loader gegen Modulnamenskollision gehärtet

`forecast_table_selector.py` importierte `format_table_selector` bisher nur
über den globalen Modulnamen. Ein bereits von einem anderen Cinnamon-Xlet
geladenes gleichnamiges Modul konnte dadurch die Prognose-Seite beim Öffnen
der Einstellungen brechen.

Der gemeinsame Formatter wird jetzt aus dem lokalen Applet-Pfad unter einem
eindeutigen Modulnamen geladen. Regression simuliert ein kollidierendes
globales Modul. `pytest -q tests/test_forecast_table_selector.py
tests/test_format_table_selector.py tests/test_panel_settings_list.py
tests/test_help_page.py`: 26/26; Ruff, Python-Compile und `git diff --check`
sauber.

## Runde 539: Leisten-Editor respektiert Wertfeld-Anzahl

`panel-value-count` begrenzte bisher nur das Rendering. Die vier Legacy-
Slots blieben auch bei Wert `1` oder `2` im Editor sichtbar. Beim Umschalten
konnten bereits gespeicherte höhere Slots außerdem verloren gehen.

`panel_columns()` blendet Slots oberhalb der gewünschten Anzahl jetzt aus.
Rohwerte bleiben beim Umschalten und bei sichtbaren Edits erhalten. Regression
deckt Legacy-Slot-Ausblendung, Wiederherstellung und Bearbeitung ab. `pytest
-q tests/test_panel_settings_list.py`: 8/8; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 540: Panel-Anzahl validiert ganze Dezimalzahlen

Python-Editor und Applet behandelten ungewöhnliche Freitextwerte bisher
unterschiedlich: Das Applet akzeptierte etwa `"2.0"`, während der Editor auf
20 zurückfiel; numerische `2.5` konnte Python zudem auf 2 kürzen.

Beide Seiten akzeptieren jetzt nur ganze Dezimalzahlen im gültigen Bereich.
Regression deckt Leerzeichen, Dezimalstrings und gebrochene Zahlen ab.
`pytest -q tests/test_panel_settings_list.py`: 8/8; `node --test
tests/applet_runtime.test.js`: 416/416; Ruff, Node-Syntaxcheck, Python-Compile
und `git diff --check` sauber.

## Runde 541: Neue Leisten-Wertfelder starten mit „Aus“

`panel_columns()` erzeugte zusätzliche `slot`-Spalten ohne `default`. Beim
Aufbau des GTK-Editors wurden diese Felder dadurch als `None` angelegt; der
Add/Edit-Dialog speicherte denselben Wert. Die JavaScript-Normalisierung
verwirft eine solche Zeile, weil jede vorhandene Slotquelle eine ganze Zahl
0–51 sein muss.

Alle Slotspalten erhalten jetzt explizit `default: 0` („Aus“), einschließlich
der Legacy-Spalten. Regression prüft drei erzeugte Slots. `pytest -q
tests/test_panel_settings_list.py`: 10/10; zusätzlich prüft der GTK-Dialog-
Pfad fehlende Slotwerte als `0`. Ruff, Python-Compile und
`git diff --check` waren sauber; danach wurde installiert und Cinnamon neu
geladen.

## Runde 542: Panel-Anzahl verwirft boolesche Werte

Python-Editor und GJS validierten `panel-value-count` bisher nicht gleich:
Python verwirft boolesche Werte, GJS wandelte `true` mit `Number(true)` in
`1` um. Ein beschädigter JSON-Import konnte dadurch ungewollt nur ein
Wertfeld anzeigen.

`_panelValueCount()` verwirft boolesche Werte jetzt ebenfalls und fällt auf
20 zurück. Regression prüft `true` und `false`. `node --test
tests/applet_runtime.test.js`: 416/416; `pytest -q
tests/test_panel_settings_list.py tests/test_applet.py -k 'panel or metadata
or settings or list_columns'`: 14/14. Ruff, Node-Syntaxcheck, Python-Compile
und `git diff --check` sauber.

## Runde 543: Forecast-AW in kombinierten Verbrauchszeilen validiert

`_normalizeConsumptionRow()` validierte den eigenen Ausgangswert des
Verbrauchs, übernahm die optionalen Forecast-Felder aber teilweise direkt.
Legacy- oder beschädigte kombinierte Zeilen konnten dadurch einen String,
Bruchwert oder ungültige Minute bis in den Tokenende-Abfragepfad tragen.

`forecast-show-coverage-marker` sowie die beiden Forecast-AW-Felder werden
jetzt wie die eigenständige Prognosentabelle strikt als Boolean bzw. ganze
Minuten `0..9999` validiert. Regression prüft gültige 30 Minuten sowie falsche
Typen, Bruchwerte und Grenzverletzungen. `node --test
--test-name-pattern='forecast|token.end|metric-table switches|legacy consumption'
tests/applet_runtime.test.js`: 21/21; Ruff, Node-Syntaxcheck, Python-Compile
und `git diff --check` sauber.

## Runde 544: Fehlende Prognosentabelle erbt keine Verbrauchsanzeige

`_onConsumptionSettingsChanged()` übergab normalisierte Verbrauchszeilen als
Legacy-Prognosequelle. Der Normalizer ergänzt darin `forecast-show-panel` aus
dem Hauptschalter. Fehlt die separate Prognosentabelle, konnte Tokenende
dadurch ungewollt im Panel erscheinen.

Der Legacy-Merge nutzt jetzt die unveränderten Storage-Zeilen. Explizit
gespeicherte alte `forecast-*`-Felder werden weiter migriert; fehlende Felder
fallen auf den Prognose-Standard „Aus“. Regression prüft diesen Pfad sowie
Roundtrip und Forecast-Validierung. `node --test
--test-name-pattern='forecast|token.end|metric-table switches|legacy consumption|combined token'
tests/applet_runtime.test.js`: 23/23; Node-Syntaxcheck und `git diff --check`
sauber.

## Runde 545: Panel-Prognose nutzt eigene Formatierung

`_panelForecastPart()` übergab die kombinierte Verbrauchszeile direkt an
`_forecastWindowPart()`. Dadurch gewann das Hauptformat (`compact`) gegen ein
konfiguriertes Tokenende-Format (`verbose` oder `custom`). Eigener Forecast-AW
und Coverage wurden ebenfalls vom Hauptwert übernommen.

Der Panel-Adapter bildet Forecast-Format, Custom-Text, Coverage und AW jetzt
explizit auf die erwarteten unpräfixierten Felder ab. Regression prüft
`verbose` gegen `compact` sowie Custom-Text mit Forecast-AW. `node --test
--test-name-pattern='forecast|token.end|metric-table switches|legacy consumption|combined token|panel forecast'
tests/applet_runtime.test.js`: 24/24; Node-Syntaxcheck und `git diff --check`
sauber.

## Runde 546: Panel-Prognose-Sonderformate regressionsgesichert

Der fokussierte Audit des Panel-Prognosepfads bestätigt `compact-minutes` mit
Stunden-/Minutendarstellung. Forecast-Warnung und „Bei null ausblenden“ greifen
ebenfalls aus ihren eigenen Feldern; das Hauptformat bleibt ohne Einfluss.

Die Regression prüft `TE=2h 30m`, rote Warnmarkierung und Null-Ausblendung.
`node --test --test-name-pattern='panel forecast' tests/applet_runtime.test.js`:
2/2; der vollständige Lauf ist 420/420. Node-Syntaxcheck und
`git diff --check` sind sauber.

## Runde 547: Einstellungsfenster auf aktuellen Monitor holen

Der reale Cinnamon-/X11-Test zeigte das Problem: `xlet-settings` öffnete sich
auf gespeicherter Position des dritten Monitors (`x=3890`), während der
Einstellungsaufruf vom mittleren Monitor kam. Dadurch war der Dialog für den
Benutzer praktisch unsichtbar. `_scheduleSettingsMaximize()` verschiebt das
Fenster jetzt vor der Maximierung auf `Main.layoutManager.currentMonitor`.
Verschieben und Maximieren laufen in getrennten Timer-Ticks; der `wmctrl -e`
Parameter wird als korrektes kommagetrenntes Geometrieargument übergeben.

Regression prüft Monitorverschiebung vor Maximierung; Cinnamon-X11 bestätigte
real `x=3890 → x=1920`, danach `1920×964` maximiert. `node --test
--test-name-pattern='settings maximization moves|settings maximization retries|settings launcher'
tests/applet_runtime.test.js`: 3/3; Node-Syntaxcheck und `git diff --check`
sind sauber.

## Runde 548: Einstellungsseiten und Selector-Widgets real geprüft

Der direkte Cinnamon-Aufruf `xlet-settings applet codex-usage@H234598 -i 14 -t 1`
öffnet die Seite `Formatierungen` sichtbar; die Tabelle `Tokendelta` wird mit
ihren Accountzeilen gerendert. Der vollständige Applet-Aufruf bleibt durch die
Monitorplatzierung aus Runde 547 geschützt. Die Selector-Widgets bauen jeweils
nur die aktive Tabelle und trennen den alten Listener beim Wechsel.

Fokustests: `pytest -q tests/test_format_table_selector.py
tests/test_forecast_table_selector.py tests/test_panel_settings_list.py
tests/test_help_page.py`: 29/29. Keine neue reproduzierbare Fehlfunktion;
Worktree und installierte Applet-Datei bleiben synchron.

## Runde 549: Obsolete Panel-Felder in modernen Tabellen

Die Tabellen `Limitverbrauch`, `Creditstand`, `Creditverbrauch` und `Resets`
zeigen keine alten „In Liste anzeigen“-Felder mehr. Cinnamon `List.list_changed()`
schreibt deshalb Zeilen ohne `show-panel`. Die JavaScript-Normalizer für
Verbrauch, Credits und Resets verlangten das Feld trotzdem und lösten beim
Bearbeiten einen Reload der Accountdaten aus; gültige Änderungen konnten so
verschwinden.

Die Normalizer akzeptieren fehlendes `show-panel` jetzt als `false`, behalten
explizite alte Boolwerte aber unverändert. Regression prüft alle fünf modernen
Tabellenformen. Fokustest `node --test --test-name-pattern='credit|consumption|reset|metric-table|modern metric|account row mergers' tests/applet_runtime.test.js`:
67/67; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 550: Tokendelta-Hilfe vollständig materialisieren

`Tokendelta` erbt seine Formatierungsfelder im Selector aus
`account-percent-styles`, besitzt im Schema aber zusätzlich eigene Spalten
`Account-ID` und `Dynamisch`. Die schema-getriebene Hilfe sah deshalb nur diese
beiden Felder und verschwieg Modus, Schrift, Schwelle und Alternativformat.

Die Hilfe-Materialisierung führt für Tokendelta jetzt geerbte und eigene
Spalten in derselben Reihenfolge wie der Selector zusammen. Regression prüft
`Formatierungsmodus` und `Dynamisch`; `pytest -q tests/test_help_page.py`: 6/6.
Python-Kompilierung und `git diff --check` sauber.

## Runde 551: Native Cinnamon-Einstellungen mit Applet-Launcher verbinden

Cinnamon nutzt im Rechtsklick-Kontextmenü den virtuellen `configureApplet()`-
Pfad. Der Applet-eigene Menüpunkt verwendete bereits den geschützten Launcher
mit Monitorpositionierung; der native Pfad umging ihn und konnte das
Einstellungsfenster klein oder auf gespeicherter, unsichtbarer Position öffnen.
`configureApplet()` delegiert jetzt an denselben Launcher. Optionaler Tab-Index
wird weitergereicht.

Realer Cinnamon-X11-Test: linker Menüpunkt und Rechtsklick-„Einrichten…“ öffnen
beide `Codex Usage` maximiert auf `x=1920`, `1920×964`. Fokustest `node --test
--test-name-pattern='native Cinnamon configure action|settings launcher|settings
maximization' tests/applet_runtime.test.js`: 4/4; Node-Syntaxcheck und
`git diff --check` sauber.

## Runde 552: Leistenquellen schützen unbrauchbare Zusatzfenster

Die Leistenquellen `sonstiges` und `Spark sonstiges` griffen direkt auf ein
Poolfenster zu. Anders als 5h, Woche, Spark und 30 Tage prüften sie nicht, ob
der zugehörige Pool verfügbar, nicht erschöpft und eindeutig identifiziert
ist. Ein Provider konnte dadurch trotz `available=false` oder erschöpftem Pool
noch einen alten Zusatzwert anzeigen.

Wert-, Fenster- und Renderpfad prüfen jetzt vor Auswahl des Zusatzfensters
denselben `_poolIsUsable()`-Vertrag wie die übrigen Poolquellen. Regression
reproduziert beide Pools und den vollständigen Panel-Text zuerst rot und besteht
danach grün. Fokustest `node --test
--test-name-pattern='panel|pool|other-window' tests/applet_runtime.test.js`:
65/65; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 553: Warnungen ignorieren unbrauchbare Poolfenster

Die Poolprüfung war nach den Leistenfixes noch nicht überall einheitlich:
`_usageSeverity()` nahm das Main-30-Tage-Fenster ungeprüft, und
`_notifyForPayload()` erzeugte Monats- und Spark-Warnungen aus Pools mit
`available=false` oder widersprüchlicher Erschöpfung. Dadurch konnten alte
Grenzwerte als kritische Panelklasse oder Benachrichtigung erscheinen.

Severity und Benachrichtigungsaufbau verwenden jetzt `_poolIsUsable()` für
beide dynamischen Poolgruppen; direkte 5h-/Wochenwerte bleiben separat
nutzbar. Regression prüft unbrauchbare Main- und Spark-Pools sowie bestehende
Spark-Schwellen. Fokustest `node --test
--test-name-pattern='usage severity|limit notifications|Spark notification|panel|pool|other-window' tests/applet_runtime.test.js`:
68/68; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 554: Alle dynamischen Poolflächen gegen stale Monats-/Sparkwerte härten

Nach Severity und Benachrichtigungen blieben weitere direkte Zugriffe auf
Poolfenster: Alert-Defaults, Click-Zusammenfassung, Click-Resets,
Tokendelta-Dynamik, 5h-Ausblendung und Panel-Resetquellen. Bei
`available=false` konnten sie alte Monats-/Sparkwerte verwenden oder 5h
fälschlich ausblenden.

Monatswerte in diesen Pfaden verlangen jetzt einen nutzbaren Main-Pool.
Tokendelta verlangt zusätzlich einen nutzbaren Spark-Pool; bekannte
`available=true`-Erschöpfung bleibt für die 5h-Ausblendung und Resetdaten
sichtbar. Panel-Resetquellen verwerfen Fenster nicht verfügbarer Pools.
Regression deckt alle sechs Oberflächen und bestehende Erschöpfungsfälle ab.
Fokustest `node --test
--test-name-pattern='usage severity|alert settings|account click summary|account reset details|5h display|dynamic monthly delta|panel reset|panel|pool|other-window|limit notifications|Spark notification|long-limit exhaustion|monthly exhaustion|account menu adds' tests/applet_runtime.test.js`:
76/76; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 555: Nicht-Prozentquellen nicht als Warnwerte auswerten

`_panelValueForSource()` fiel für Resets, Identität, Tokendelta, Routing und
Status durch bis zum Durchschnitt von 5h/Woche. `_updatePanel()` behandelte
diesen Fallback als Prozentwert und konnte deshalb bei rein textlichen
Leistenfeldern Warnklasse und Minimum falsch setzen.

Nur echte Prozentquellen liefern wieder numerische Panelwerte; alle übrigen
formatierbaren Text-, Reset- und Deltaquellen liefern `null`. Regression prüft
alle Quellgruppen sowie unveränderte 5h-/Ø-Werte. Fokustest `node --test
--test-name-pattern='panel warning values ignore|panel|pool|other-window|extended panel sources|long-limit exhaustion|monthly exhaustion' tests/applet_runtime.test.js`:
75/75; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 556: Panel-Resetquellen nicht freigegebener Pools sperren

`_panelWindowForKey()` akzeptierte für Resetquellen `available=true` auch bei
`allowed=false`. Main- und Spark-Resetfelder konnten dadurch Fenster eines
Pools anzeigen, für den Nutzung ausdrücklich nicht freigegeben ist.

Poolfenster für Panel-Resetquellen verlangen jetzt zusätzlich
`allowed !== false`. Bekannte erschöpfte oder `limit_reached`-Pools behalten
ihre verwertbaren Resetdaten. Regression prüft Main- und Spark-Resetpfad;
Fokustest `node --test
--test-name-pattern='panel reset sources ignore|panel warning values ignore|panel|pool|other-window|extended panel sources|long-limit exhaustion|monthly exhaustion' tests/applet_runtime.test.js`:
76/76; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 557: Unbekannte „other“-Fenster nicht auswählen

`_poolOtherWindow()` übersprang zwar 5h/Woche/30d, akzeptierte aber unbekannte
Fensteridentitäten oder doppelte Identitäten. Panel- und Resetquellen für
„sonstiges“ konnten dadurch ein nicht eindeutig zuordenbares Fenster anzeigen.

Die Auswahl verlangt jetzt eindeutige, bekannte Fensteridentitäten und fällt
sonst auf kein Fenster zurück. Regression prüft unbekannte Main- und
Spark-Fenster sowie bestehende Pool-/Panelpfade. Fokustest `node --test
--test-name-pattern='other-window panel sources ignore unknown|panel reset sources ignore|panel limit sources|unusable main and Spark|Spark pools without|duplicate window identity|panel warning values ignore' tests/applet_runtime.test.js`:
9/9; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 558: Spark-Alertstatus an Poolvertrag binden

`_sparkLimitState()` prüfte Spark-Pools schwächer als Panel, Severity und
Benachrichtigungen. Ein Pool mit `allowed=false` oder `exhausted=true` konnte
deshalb in Alert-Einstellungen als `present` erscheinen und einen normalen
Spark-Schwellenwert anbieten.

Der Status verwendet jetzt zusätzlich `_poolIsUsable()`. Nicht nutzbare,
aber vorhandene Pools bleiben `unknown`; nur fehlende Pools sind `none`.
Regression prüft nicht freigegebenen Spark-Pool und bestehende Alert-/Panel-
Pfade. Fokustest `node --test
--test-name-pattern='Spark alert state ignores|alert helper matrix|legacy alert rows|usage severity|limit notifications|panel' tests/applet_runtime.test.js`:
55/55; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 559: Disallowed-Pool nicht als Langlimit-Erschöpfung werten

`_fiveHourDisplayWindow()` und `_longLimitExhausted()` prüften bisher nur
`available=true`. Bei `allowed=false, exhausted=true` konnte ein nicht
freigegebener Main-Pool deshalb 5h ausblenden oder den Account verstecken.

Beide Pfade verlangen jetzt zusätzlich `allowed !== false`. Bekannte
Erschöpfung freigegebener Pools bleibt unverändert. Regression prüft
unavailable/disallowed sowie bestehende Panel-, Pool- und Alarmfälle.
Fokustest `node --test
--test-name-pattern='5h display does not mask|long-limit exhaustion|monthly exhaustion|opt-in long-limit|panel|pool|other-window|usage severity|Spark alert state|limit notifications' tests/applet_runtime.test.js`:
79/79; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 560: Legacy-5h/Woche bei unbrauchbarem Main-Pool nicht alarmieren

`_usageSeverity()` und `_notifyForPayload()` werteten `five_hour` und `weekly`
direkt aus. Wenn parallel ein vorhandener, aber unbrauchbarer Main-Pool
vorlag, konnten gecachte Legacywerte trotzdem kritische Klassen oder Warnungen
erzeugen; Panelquellen verwarfen dieselben Werte bereits.

Beide Pfade verwenden jetzt den Main-Poolvertrag für Legacyfenster: Bei
unbrauchbarem Main-Pool werden sie ignoriert, ohne Main-Pool bleiben sie als
Legacyfallback nutzbar. Regression prüft Severity und Warnungen sowie alle
vorherigen Pool-/Panelpfade. Fokustest `node --test
--test-name-pattern='usage severity ignores legacy|limit notifications ignore legacy|usage severity|limit notifications|Spark notification|panel|pool|other-window|long-limit exhaustion|monthly exhaustion' tests/applet_runtime.test.js`:
82/82; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 561: Alert-Defaults für Legacyfenster synchronisieren

`_alertWindowAvailable()` erlaubte 5h- und Wochen-Schwellen weiterhin,
obwohl Panel, Severity und Benachrichtigungen Legacyfenster bei vorhandenem
unbrauchbarem Main-Pool bereits ignorierten. Alert-Tabellen konnten dadurch
„20“ statt „no 5h“/„no Woche“ zeigen.

Die Verfügbarkeitsprüfung verlangt jetzt für alle Main-Limitarten denselben
Poolvertrag; ohne Main-Pool bleiben Legacywerte nutzbar. Regression prüft
Defaults, Normalisierung sowie bestehende Alert-/Panelpfade. Fokustest
`node --test
--test-name-pattern='alert settings|alert helper matrix|legacy alert rows|accounts without a Spark|usage severity|limit notifications|panel|pool|other-window' tests/applet_runtime.test.js`:
84/84; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 562: Custom-Panelsetting korrekt binden

Cinnamon meldete beim Applet-Reload wiederholt `Invalid setting type 'custom'
for setting key 'account-panel-settings'`. Die Schema-Definition ist bewusst
`type=custom`, `_bindSettings()` verwendete dafür aber `bindProperty()`, das nur
Standardtypen akzeptiert. Dadurch blieb die Paneltabelle ungebunden und
Settings-Änderungen konnten aus dem Appletpfad verloren gehen.

`account-panel-settings` wird jetzt wie `account-backends` über
`_bindCustomSetting()` gelesen und am `changed::`-Signal aktualisiert.
Regression stellt sicher, dass es nicht mehr an Cinnamon-Standardbindung geht.
Fokustest `node --test
--test-name-pattern='panel custom setting bypasses|custom settings read|settings launcher|native Cinnamon configure action|settings maximization|panel settings' tests/applet_runtime.test.js`:
7/7; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 563: Command-Settings im Safe-Mode sperren

`_onCommandSettingsChanged()` startete bei Änderungen an `command-path` oder
`config-path` immer `_loadCached(true)`. Während des Safe-Mode konnte eine
Settings-Änderung dadurch trotz geöffneter Fehler-Sperre einen neuen
Hintergrundabruf starten. Andere Settings-Callbacks hatten diese Sperre schon.

Der Callback beendet sich jetzt bei `_removed` oder `_safeMode`; im normalen
Betrieb bleibt der erzwungene Cache-Ladevorgang unverändert. Regression prüft
Safe-Mode, normalen Betrieb und entfernte Appletinstanz. Fokustest
`node --test --test-name-pattern='safe mode|command setting|settings|refresh-on-open|automatic refresh' tests/applet_runtime.test.js`:
37/37; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 564: Menüaufbau nach Applet-Entfernung abbrechen

`_buildUsageMenu()` und `_buildLoadingMenu()` verwendeten `this.menu` ohne
Lifecycle-Prüfung. Ein verspäteter Callback nach `on_applet_removed_from_panel()`
konnte deshalb auf dem bereits zerstörten Menü `removeAll()` aufrufen.

Beide Funktionen kehren jetzt bei `_removed` oder fehlendem Menü sofort zurück.
Regression prüft den entfernten Zustand für Usage- und Loading-Menü. Fokustest
`node --test --test-name-pattern='usage menu rebuild is inert|panel click opens|settings launcher|native Cinnamon configure action|safe mode|command setting' tests/applet_runtime.test.js`:
15/15; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 565: Device-Login-Livechunks generationssicher machen

`_spawnAuxJson()` verwarf veraltete Ergebnis-Callbacks bereits über
`_auxGeneration`, leitete Device-Login-Livechunks aber ungeprüft weiter. Nach
Abbruch eines Logins und schnellem Start eines anderen konnten verspätete
Chunks des alten Prozesses deshalb unter dem neuen Account landen.

Der Livechunk-Callback prüft jetzt `_removed`, Generation und aktiven
`device-login`-Befehl, bevor er den Parser aufruft. Regression simuliert
Abbruch von Account A, Startzustand von Account B und verspäteten Chunk.
Fokustest `node --test --test-name-pattern='bounded reader|device login live|device login parser|stale device login live chunks|auxiliary timeout|auxiliary process' tests/applet_runtime.test.js`:
16/16; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 566: Abgebrochene Profiljob-Erkennung erneut erlauben

`_loadProfileJobs()` setzte `_profileJobsLoaded` vor dem Hilfsprozess auf
`true`. Wurde die Anfrage durch eine neue Auxiliary-Generation abgebrochen,
kam kein Ergebnis-Callback mehr und der nächste Statusabruf wurde dauerhaft
übersprungen.

`profile jobs` ist jetzt als eigener Auxiliary-Befehl markiert;
`_cancelAuxProcess()` setzt seinen Ladezustand bei Abbruch zurück. Eine neue
Erkennung kann danach wieder starten. Regression prüft Start, Abbruch und
erneuten Start. Fokustest `node --test
--test-name-pattern='cancelled profile job discovery can run again|persistent profile job|device login live|auxiliary' tests/applet_runtime.test.js`:
23/23; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 567: Consumption-Anfragen beim Safe-Mode invalidieren

`_enterSafeMode()` stoppte Primary-, Auxiliary- und Timer-Arbeit, ließ aber
`_consumptionCurrent` sowie die Consumption-Warteschlange bestehen. Nach einem
Retry konnte `_drainConsumptionRequests()` deshalb dauerhaft blockiert bleiben;
eine verspätete Antwort besaß weiterhin die alte Generation.

Safe-Mode erhöht jetzt `_consumptionGeneration`, leert aktive und wartende
Consumption-Anfragen und lässt neue Messungen erst aus dem nächsten Refresh
aufbauen. Regression erweitert den Safe-Mode-Lifecycle-Test; Fokustest
`node --test --test-name-pattern='safe mode cancels reactivation processes|late consumption response|consumption refresh|safe mode' tests/applet_runtime.test.js`:
14/14; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 568: Routing-Schreibzustand beim Safe-Mode zurücksetzen

Auch `_routingPolicyApplying` und `_pendingRoutingLimitCommands` überlebten
einen abgebrochenen Policy-Schreibvorgang. Nach dem Retry ignorierte
`_onRoutingSettingsChanged()` deshalb neue Änderungen, obwohl kein Prozess mehr
lief.

Safe-Mode verwirft jetzt den Routing-Schreibguard und die ausstehenden
Limit-Kommandos. Regression erweitert den Safe-Mode-Lifecycle-Test. Fokustest
`node --test --test-name-pattern='safe mode cancels reactivation processes|routing|safe mode|late consumption response' tests/applet_runtime.test.js`:
28/28; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 569: Account-Schreibqueue beim Safe-Mode leeren

`_accountChangeCurrent` und die Account-Schreibqueue blieben bei einem
abgebrochenen Account-Write gesetzt. Nach dem Retry blockierte
`_drainAccountChanges()` dadurch alle weiteren Accountänderungen; ein altes
`_accountChangePendingRows`-Snapshot konnte zusätzlich neue Settings überdecken.

Safe-Mode verwirft jetzt aktiven Account-Write, Queue und Snapshot. Die nächste
Synchronisierung liest die persistierten Settings erneut. Regression erweitert
den Safe-Mode-Test; Fokustest
`node --test --test-name-pattern='safe mode cancels reactivation processes|account controls|backend synchronization|account delete cancels|new account starts' tests/applet_runtime.test.js`:
16/16; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 570: Profiljob-Polling beim Safe-Mode invalidieren

`_profileJobPollingAccount` und der zugehörige Poll-Timer blieben nach Abbruch
eines Profiljob-Statusprozesses erhalten. `_pollNextProfileJob()` sah danach
weiterhin einen aktiven Poller und startete nach Retry keinen neuen Statusabruf.

Safe-Mode erhöht jetzt die Poll-Generation, entfernt den Poll-Timer und setzt
den aktiven Polling-Account zurück. Persistente Jobs bleiben erhalten und können
nach dem nächsten Refresh wieder aufgenommen werden. Fokustest
`node --test --test-name-pattern='safe mode cancels reactivation processes|persistent profile job|profile job|device login|safe mode' tests/applet_runtime.test.js`:
42/42; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 571: Profiljob-Discovery nach Safe-Mode erzwingen

Safe-Mode konnte während eines anderen Profilbefehls eintreten, während
`_profileJobsLoaded` noch `true` war. Der laufende Auxiliary-Prozess wurde zwar
abgebrochen, aber ein Retry übersprang wegen dieses Caches die erneute
`profile jobs`-Erkennung. Eine bereits laufende persistente Job-ID blieb dann
ohne Polling; bei wartender Account-Löschung konnte die Queue hängen.

Safe-Mode setzt jetzt `_profileJobsLoaded` und das Resume-Flag zurück und leert
die Resume-Warteschlange. Persistente Job-Maps bleiben erhalten, damit kein
aktiver Remote-Job fälschlich als beendet gilt; die nächste Auxiliary-
Synchronisierung liest sie autoritativ neu ein. Regression erweitert den
Safe-Mode-Lifecycle-Test. Fokustest `node --test
--test-name-pattern='safe mode cancels reactivation processes|persistent profile job|profile job resume|cancelled profile job discovery' tests/applet_runtime.test.js`:
10/10; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 572: Veraltete Profiljob-Maps aus Discovery entfernen

`profile jobs` listet ausschließlich nicht-terminale Jobs. Nach einem
Applet-Safe-Mode oder einer längeren Unterbrechung konnte ein Job aber remote
bereits beendet sein, während `_deviceLoginJobs`, `_deviceLoginActive`,
`_profilePendingAccounts` und der Lösch-Wartemarker lokal erhalten blieben.
Weitere Account-Aktionen sahen den Job dann fälschlich als aktiv.

Nach erfolgreicher Discovery werden lokale persistente Jobs jetzt gegen die
autoritative Accountliste abgeglichen. Fehlende Jobs verlieren ihre Job-,
Pending-, Lösch- und persistenten Login-/Event-Zustände; ein separater Live-
Device-Login bleibt unangetastet. Ein verwaister Poller wird ebenfalls
invalidiert. Regression prüft leere Discovery nach lokalem stale Job.
Fokustest `node --test
--test-name-pattern='profile job discovery clears locally stale completed jobs|persistent profile job|cancelled profile job discovery|safe mode cancels reactivation processes' tests/applet_runtime.test.js`:
10/10; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 573: Events bei ersetzter Profiljob-ID verwerfen

Wenn ein Account zwischen zwei Discoveries einen neuen persistenten Profiljob
bekam, übernahm `_loadProfileJobs()` zwar die neue Job-ID, ließ aber Events,
Live-Text und Fehler des alten Jobs stehen. Die GUI konnte dadurch Device-Code
oder Fehlermeldung eines anderen Jobs anzeigen.

Discovery löscht Fehler für aktive Jobs und verwirft Event-/Live-Text nur bei
geänderter Job-ID. Bei unveränderter ID bleiben bereits eingetroffene Events
erhalten. Regression prüft den ID-Wechsel. Fokustest `node --test
--test-name-pattern='profile job discovery drops events from replaced job|profile job discovery clears locally stale completed jobs|persistent profile job|cancelled profile job discovery|safe mode cancels reactivation processes' tests/applet_runtime.test.js`:
11/11; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 574: Anzeige nach leerer Profiljob-Discovery aktualisieren

Wenn die autoritative Discovery stale lokale Jobs entfernte und danach keine
aktiven Jobs übrig blieben, baute `_loadProfileJobs()` bisher weder Menü noch
Formatierungsflächen neu auf. Die alte „Login läuft“-Anzeige konnte dadurch bis
zum nächsten unabhängigen Refresh sichtbar bleiben.

Bei tatsächlich verändertem Profiljob-Zustand aktualisiert der Loader jetzt
Leisten-/Formatierungsflächen und Menü auch bei `jobs: []`; unveränderte leere
Discovery bleibt ohne Zusatzarbeit. Regression prüft stale Bereinigung und
genau einen Menüaufbau. Fokustest `node --test
--test-name-pattern='profile job discovery drops events from replaced job|profile job discovery clears locally stale completed jobs|persistent profile job|cancelled profile job discovery|safe mode cancels reactivation processes' tests/applet_runtime.test.js`:
11/11; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 575: Profil-Pending-Accounts beim Safe-Mode verwerfen

Beim Anlegen eines Accounts wird `_profilePendingAccounts` bereits vor dem
Profiljob gesetzt. Bricht Safe-Mode diesen Account-Write ab, existiert noch
keine Job-ID, die Discovery später bereinigen könnte. Der Account blieb lokal
als „Profil wird erstellt“ markiert und konnte weitere Steuerung blockieren.

Safe-Mode leert jetzt die Pending-Account-Map. Bereits laufende persistente
Job-IDs bleiben separat erhalten und werden nach Retry durch `profile jobs`
wieder eingetragen. Regression erweitert den Safe-Mode-Lifecycle-Test.
Fokustest `node --test
--test-name-pattern='safe mode cancels reactivation processes|profile job discovery clears locally stale completed jobs|persistent profile job|cancelled profile job discovery|device login live' tests/applet_runtime.test.js`:
18/18; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 576: Live-Device-Login-Events beim Abbruch löschen

`_cancelAuxProcess()` entfernte bei einem ephemeren `profile device-login`
Aktivstatus und Live-Text, ließ aber `_deviceLoginEvents` bestehen. Nach
Abbruch oder Safe-Mode konnte ein späterer Login daher den alten Device-Code
oder die alte URL anzeigen. Persistente Profiljobs dürfen diesen Zustand nicht
verlieren.

Cleanup löscht Events jetzt nur für den tatsächlich live abgebrochenen Account
ohne persistente Job-ID. Regression erweitert den persistenten-Job-Cleanup-Test.
Fokustest `node --test
--test-name-pattern='live device login cleanup preserves persistent profile job state|device login live|safe mode cancels reactivation processes|profile job discovery' tests/applet_runtime.test.js`:
13/13; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 577: Profiljob-Statuspoll nach Auxiliary-Abbruch fortsetzen

Ein laufender `profile job-status`-Abruf konnte durch Health-, Routing- oder
anderen Auxiliary-Befehl abgebrochen werden. Der gemeinsame Auxiliary-Guard
invalidierte zwar die Antwort, ließ aber `_profileJobPollingAccount` gesetzt;
der nächste Poll startete dadurch nicht mehr.

`profile job-status` wird jetzt als eigener Auxiliary-Befehl erkannt. Beim
Abbruch werden Account, Poll-Timer und Generation invalidiert und der Job
requeued. Nach Abschluss des unterbrechenden Hilfsbefehls nimmt der Loader den
nächsten Profiljob automatisch wieder auf. Regression prüft echten Status-
Spawn, Abbruch und Fortsetzung nach Auxiliary-Abschluss. Fokustest `node --test
--test-name-pattern='cancelled profile job status requeues its persistent poll|auxiliary completion resumes queued profile poll|profile job discovery|persistent profile job|live device login cleanup|safe mode cancels reactivation processes' tests/applet_runtime.test.js`:
12/12; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 578: Profiljob-Cancel nach Auxiliary-Abbruch fortsetzen

Auch `profile cancel` war bisher nicht als Profiljob-Command markiert. Wurde
der Cancel-Request durch einen anderen Auxiliary-Befehl ersetzt, blieben
Job-ID, Löschmarker und ggf. Poll-Account ohne Fortsetzung zurück.

Status- und Cancel-Commands verfolgen jetzt ihren Account separat. Abbruch
requeued den betroffenen persistenten Job, invalidiert Poll-Timer/Generation
und setzt Tracking zurück; der nächste Auxiliary-Abschluss startet den Status-
Poll erneut. Safe-Mode verwirft das Tracking gemeinsam mit seinen Queues.
Regression prüft echten Cancel-Spawn und Abbruch. Fokustest `node --test
--test-name-pattern='cancelled profile job status requeues its persistent poll|auxiliary completion resumes queued profile poll|cancelled profile job cancel request requeues target account|profile job discovery|persistent profile job|live device login cleanup|safe mode cancels reactivation processes' tests/applet_runtime.test.js`:
14/14; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 579: Profilpoll nicht hinter Account-Write-Queue verkeilen

Normale `profile job-status`-Polls wurden bei aktiver Account-/Backend-
Schreibqueue zwar deferred, setzten aber vorher `_profileJobPollingAccount`.
Nach Abschluss der Write-Queue sah `_pollNextProfileJob()` dadurch weiterhin
einen aktiven Poller und startete keinen Statusabruf mehr. Auch ein bereits
geplanter Timer konnte den Poll ohne Requeue verlieren.

`_pollNextProfileJob()` wartet jetzt explizit auf leere Write-Queues.
`_pollProfileJob()` requeued bei einem während des Timers eingetretenen Write
den Account und leert den Poller-Guard; erzwungene Cancel-/Statuspolls bleiben
ausgenommen. Regression prüft Warten, späteren Spawn und Timer-Requeue.
Fokustest `node --test
--test-name-pattern='profile polling waits for account writes before spawning status|profile job|device login live|safe mode cancels reactivation processes|auxiliary completion resumes queued profile poll' tests/applet_runtime.test.js`:
25/25; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 580: Einstellungsfenster nach langsamem Start sichtbar platzieren

Der Settings-Launcher verschob das `xlet-settings`-Fenster bisher genau einmal
250 ms nach dem Spawn. Auf diesem Cinnamon-System erscheint das Fenster erst
nach etwa 635 ms. Der erste `wmctrl -e`-Aufruf lief deshalb ins Leere; spätere
Maximierungsversuche konnten das Fenster auf seiner alten, unsichtbaren
Monitorposition lassen.

Der Verschiebeprozess wird jetzt per `wait_check_async()` ausgewertet. Ein
fehlgeschlagener `wmctrl`-Aufruf requeued die Platzierung bis zu zwölf Timer-
Ticks; erst nach erfolgreicher Platzierung (oder dem begrenzten Fallback) wird
maximiert. Regression reproduziert ein verspätetes Fenster mit erst
fehlgeschlagenem, dann erfolgreichem Verschiebeaufruf.

Fokustest `node --test --test-name-pattern='settings launcher|native Cinnamon
configure action|settings maximization|settings placement retries'
tests/applet_runtime.test.js`: 5/5. Node-Syntaxcheck und `git diff --check`
sauber.

## Runde 581: Ungültige Instanz-ID nicht an xlet-settings übergeben

`_openSettings()` erzeugte bei fehlender Instanz-ID bisher `-i undefined`.
`xlet-settings` lehnt diesen Wert per argparse ab und beendet sich, bevor ein
Fenster entsteht. Der Launcher übergibt `-i` jetzt nur für nichtnegative
Ganzzahlen; beim single-instance-Applet genügt UUID ohne Instanzschalter.

Regression prüft den bestehenden Instanzpfad und den fehlenden-ID-Fallback.
Der Settings-Fokuslauf bleibt bei 5/5; Node-Syntaxcheck und
`git diff --check` sauber.

## Runde 582: Hängende Settings-Platzierung begrenzen

Während `wmctrl -e` lief, blieb der Maximierungstimer bisher unbegrenzt in
`placementPending`. Ein festhängender X11-Hilfsprozess konnte damit einen
lebenden Timer und dessen Closure im Cinnamon-Prozess halten.

Pending-Ticks zählen jetzt ebenfalls zum Placement-Limit. Nach zwölf Ticks
geht der Launcher in den bereits vorhandenen begrenzten Maximierungs-Fallback;
Applet-Entfernung bleibt sofort wirksam. Regression simuliert einen
`wmctrl`-Prozess ohne Exit-Callback und prüft den anschließenden
Maximierungsaufruf.

Fokustest `node --test --test-name-pattern='settings placement retries|settings
placement does not wait|settings maximization' tests/applet_runtime.test.js`:
4/4. Node-Syntaxcheck und `git diff --check` sauber.

## Runde 583: Placement-Child bei Timeout und Applet-Entfernung beenden

Der neue Placement-Fallback begrenzte zwar den Timer, ließ das noch laufende
`wmctrl -e`-Child aber weiterlaufen. Ein verspäteter Exit konnte nach der
Maximierung erneut Geometrie anwenden; bei Applet-Entfernung blieb das Child
ebenfalls untracked.

Der Placement-Prozess wird jetzt referenziert, bei Timeout per `force_exit()`
beendet und bei Applet-Entfernung bereinigt. Abschluss-Callbacks lösen die
Referenz nur noch für ihren eigenen Prozess. Regression prüft Timeout-Abbruch
und Cleanup beim Entfernen.

Fokustest `node --test --test-name-pattern='settings placement|settings
maximization|settings launcher' tests/applet_runtime.test.js`: 6/6.
Node-Syntaxcheck und `git diff --check` sauber.

## Runde 584: Spawn- und Maximierungsfehler im Settings-Launcher trennen

`_openSettings()` behandelte bisher auch einen Fehler von
`_scheduleSettingsMaximize()` als fehlgeschlagenen Settings-Spawn. Das Fenster
war dann bereits gestartet, trotzdem erschien die irreführende Meldung
„Einstellungen konnten nicht geöffnet werden“.

Spawn und Nachbearbeitung sind jetzt getrennte `try`-Blöcke. Ein Timerfehler
wird nur begrenzt protokolliert; echter Spawn-Fehler bleibt als Benutzerfehler
sichtbar. Regression prüft erfolgreich gestarteten Child mit absichtlich
fehlgeschlagenem Maximierungstimer.

Fokustest `node --test --test-name-pattern='settings launcher|native Cinnamon
configure action|settings maximization|settings placement'
tests/applet_runtime.test.js`: 7/7. Node-Syntaxcheck und `git diff --check`
sauber.

## Runde 585: Stale Profilpoll-Timer nicht erneut starten lassen

`_scheduleProfileJobPoll()` prüfte die Generation nur beim Anlegen des
Timers. Ein bereits in die Mainloop eingereihter Callback konnte nach Cancel,
Safe-Mode oder anderer Generation trotzdem `_pollProfileJob()` starten und
einen neuen Statusprozess erzeugen.

Der Timer-Callback verwirft jetzt veraltete Generationen, entfernte/sichere
Appletzustände und fehlende Job-IDs vor jedem Spawn. Regression erhöht die
Generation nach dem Scheduling und erwartet keinen Statusaufruf.

Fokustest `node --test --test-name-pattern='stale profile poll timer|profile
polling waits|profile job|device login live|safe mode cancels'
tests/applet_runtime.test.js`: 25/25. Node-Syntaxcheck und `git diff --check`
sauber.

## Runde 586: Profilpoll-Timer nicht duplizieren

`_scheduleProfileJobPoll()` legte bisher einen neuen Mainloop-Timer an, ohne
einen bereits registrierten Poll-Timer vorher zu entfernen. Bei wiederholtem
Scheduling konnten dadurch mehrere Callbacks für denselben Profiljob leben;
ein alter Callback löschte zudem die ID des neueren Timers aus dem Tracking.

Vor dem Scheduling wird `_deviceLoginPollId` jetzt zentral entfernt. Damit
bleibt höchstens ein registrierter Profilpoll-Timer aktiv. Regression prüft
Entfernung des alten Timers und Tracking der neuen ID.

Fokustest `node --test --test-name-pattern='profile poll|profile job|device
login|settings launcher' tests/applet_runtime.test.js`: 42/42.
Node-Syntaxcheck und `git diff --check` sauber.

## Runde 587: Veraltete Settings-Maximierung invalidieren

`source_remove()` verhindert nicht zuverlässig einen Callback, der bereits in
der Cinnamon-Mainloop eingereiht wurde. Beim schnellen erneuten Öffnen der
Einstellungen konnte ein alter Maximierungs-Callback deshalb nach dem neuen
Scheduling noch `wmctrl` starten.

Der Settings-Platzierungs-/Maximierungspfad führt jetzt eine eigene Generation.
Timer- und `wmctrl`-Exit-Callbacks verwerfen ältere Generationen; bei
Applet-Entfernung wird die Generation ebenfalls invalidiert. Regression prüft
einen alten Callback nach direktem Rescheduling.

Fokustest `node --test --test-name-pattern='settings|stale settings'
tests/applet_runtime.test.js`: 29/29. Node-Syntaxcheck und `git diff --check`
sauber.

## Runde 588: Settings-Platzierung bei verzögerter Monitor-Erkennung

Wenn `Main.layoutManager.currentMonitor` beim ersten Maximierungstick noch
fehlt, setzte der Launcher die Platzierung bisher sofort als erledigt und
maximierte ohne `wmctrl -e`. Während Cinnamon-Start oder Monitor-Hotplug kann
das Fenster dadurch auf falschem oder unsichtbarem Monitor landen.

Fehlende/ungültige Monitor-Koordinaten werden jetzt bis zum bestehenden
12-Tick-Limit erneut geprüft. Erst danach greift der begrenzte Maximierungs-
Fallback; sobald Monitor-Daten eintreffen, wird zuerst verschoben und danach
maximiert. Regression simuliert fehlenden Monitor beim ersten Tick und
verzögerte Monitor-Verfügbarkeit.

Fokustest `node --test --test-name-pattern='settings launcher|native Cinnamon
configure action|settings maximization|settings placement|stale settings'
tests/applet_runtime.test.js`: 9/9. Node-Syntaxcheck und `git diff --check`
sauber.

## Runde 589: Vollständiger Leistenquellen-Renderpfad

Die 52 konfigurierbaren Leistenquellen (Aus plus Werte 1–51) sind über
Prozent-, Reset-, Verbrauchs- und eigene Statusformatierungen verteilt. Der
Audit fand keinen aktuellen Produktionsfehler, aber bisher keinen einzelnen
Laufzeittest, der jede Quelle durch `_panelValueForSource()`,
`_panelWindowForSource()` und `_panelSlotContent()` führt.

Der Test `every configured panel source has a safe render path` stellt für
alle Quellen vollständige Main-/Spark-, Verbrauchs-, Credit-, Routing- und
Resetdaten bereit und prüft, dass Plaintext und Markup jeweils sicher erzeugt
werden. Damit schlagen neue Branches mit `null`- oder Ausnahmefehlern direkt
im fokussierten Paneltest fehl.

Fokustest `node --test --test-name-pattern='panel sources|panel limit sources|extended panel sources|token delta' tests/applet_runtime.test.js`: 13/13. Node-Syntaxcheck und `git diff --check` sauber.

## Runde 590: Safe-Mode-Profilpoll-Cleanup

Der Profiljob-Statuspoll wird beim Eintritt in den Safe-Mode beendet. Dabei
werden Poll-Account und Command-Account vor `_cancelAuxProcess()` geleert;
damit darf der allgemeine Abbruchpfad den Job nicht erneut in die Resume-
Warteschlange legen.

Ein direkter Regressionstest deckt genau diese Reihenfolge mit aktivem
`profile-job-status`-Prozess ab und prüft Prozessabbruch, leere Resume-Queue
und leere Poll-Trackingfelder. Der aktuelle Produktionspfad besteht bereits;
es war keine Codeänderung nötig.

Fokustest `node --test --test-name-pattern='profile job|profile poll|auxiliary completion|safe mode' tests/applet_runtime.test.js`: 30/30. Node-Syntaxcheck und `git diff --check` sauber.

## Runde 591: Selector-Listener beim Settings-Seitenwechsel freigeben

`FormatTableSelector` und `ForecastTableSelector` registrieren ihren
Seitenwert über `JSONSettingsBackend.attach()`. Beim Zerstören wurden bisher
nur die jeweils angelegten Tabellenwidgets abgemeldet; der Listener des
Selectors selbst blieb im gemeinsamen `JSONSettingsHandler` erhalten.
Jedes erneute Öffnen der Settings-Seite konnte dadurch ein weiteres zerstörtes
Widget samt Callback im Speicher halten.

Beide Selector-Klassen entfernen beim `destroy()` jetzt ihren eigenen
`settings.listen()`-Callback. Regression prüft Tabellen- und Seitenlistener
nach dem Zerstören; damit ist der relevante Cinnamon-Heap-Pfad gebunden.

Fokustest `pytest -q tests/test_format_table_selector.py tests/test_forecast_table_selector.py`: 14/14. Python-Syntaxcheck und `git diff --check` sauber.

## Runde 592: Fast-Mode-Icon-Listener freigeben

`FastModeIconSelector` bindet den Wert `fast-mode-icon` über
`JSONSettingsBackend.attach()`, hatte aber keinen eigenen `destroy()`-Pfad.
Beim wiederholten Öffnen der Einstellungen blieb der Callback deshalb im
gemeinsamen JSON-Settings-Handler und hielt das alte GTK-Widget fest.

Der Selector entfernt seinen Listener jetzt beim Zerstören. Regression baut
den echten GTK-Selector mit leerer Icon-Liste, prüft Listener-Anmeldung und
bestätigt die vollständige Abmeldung nach `destroy()`.

Fokustest `pytest -q tests/test_fast_mode_icon_selector.py tests/test_format_table_selector.py tests/test_forecast_table_selector.py tests/test_panel_settings_list.py`: 30/30. Python-Syntaxcheck und `git diff --check` sauber.

## Runde 593: Masterjet-Serienprozess nach hartem Timeout reapen

`DynamicSeriesList._masterjet_series()` beendet einen hängenden
Masterjet-Prozess zunächst per Prozessgruppen-Kill und wiederholt den Kill
bei einem ersten `wait()`-Timeout. Danach fehlte ein letzter Reaping-Versuch;
bei langsamer Prozessbeendigung konnte ein Zombie zurückbleiben.

Nach dem zweiten Kill wartet der Cleanup-Pfad jetzt nochmals begrenzt auf
`process.wait()`. Regression simuliert zweimaliges Timeout und bestätigt den
dritten Reaping-Aufruf.

Fokustest `pytest -q tests/test_dynamic_series_list.py tests/test_fast_mode_icon_selector.py tests/test_format_table_selector.py tests/test_forecast_table_selector.py tests/test_panel_settings_list.py`: 44/44. Python-Syntaxcheck und `git diff --check` sauber.

## Runde 594: Live-Smoke-Test des Einstellungsmenüs

Die Meldung „Einstellungsmenü lässt sich nicht öffnen“ wurde am laufenden
Cinnamon-Applet reproduziert. Der aktive Eintrag ist vorhanden; der reale
Aufruf nutzt Instanz `14` und startet `xlet-settings applet
codex-usage@H234598 -i 14`. Das Fenster erscheint und wird auf den Monitor des
Applet-Panels maximiert. Direkter Start ohne Instanz-ID sowie alle acht
Tab-IDs bleiben ebenfalls aktiv.

Es gab dabei keinen reproduzierbaren Produktionsfehler und daher keine
zusätzliche Codeänderung. Ursache des beobachteten Zustands war ein veralteter
laufender Applet-Prozess bzw. ein nicht sichtbares Settings-Fenster; der
Installer-Reload wurde erneut ausgeführt und mit Menüaktivierung über Looking
Glass verifiziert.

Fokustest `node --test --test-name-pattern='settings launcher|native Cinnamon
configure action|settings maximization|settings placement|stale settings'
tests/applet_runtime.test.js`: 9/9. Live-Smoke `xlet-settings`/Cinnamon:
erfolgreich. `git diff --check` sauber.

## Runde 595: Auxiliary-Command nicht über Account-Argumente klassifizieren

`_spawnAuxJson()` erkannte Device-Login bisher mit
`argv.indexOf("device-login")`. Ein gültiger Account-Identifier oder ein
anderes Argument mit diesem Text setzte dadurch `_auxCommand` fälschlich auf
`device-login`; betroffen waren Timeouttext, Live-Event-Parser und der
Abbruchpfad. `_cancelDeviceLogin()` hatte dieselbe Übererkennung in der
Deferred-Queue.

Beide Pfade erkennen Device-Login jetzt ausschließlich über das strukturelle
Tokenpaar `profile device-login`. Regressionen prüfen sowohl Account-ID
`device-login` im normalen Auxiliary-Befehl als auch eine nicht-Profil-
Anfrage in der Queue; der echte Profilbefehl behält seinen Device-Login-
Timeouttext.

Fokustest `node --test --test-name-pattern='account id device-login|queued
device login cancellation|auxiliary timeout|profile job|bounded process output'
tests/applet_runtime.test.js`: grün. Vollständiger JS-Lauf `node --test
tests/applet_runtime.test.js`: 462/462. Zusätzlich 25 echte Auxiliary-Starts
unter laufendem Cinnamon: keine Zombies, `_auxProcess`/`_auxCommand` leer,
Timeoutquelle und Deferred-Queue freigegeben. Node-Syntaxcheck und
`git diff --check` sauber.

## Runde 596: Profiljob-Discovery gegen Duplikate und Backend-Limit härten

`_loadProfileJobs()` akzeptierte bisher doppelte Account- oder Job-ID-Zeilen.
Dabei konnte eine Job-ID überschrieben und derselbe Account mehrfach in die
Polling-Queue eingetragen werden. Zusätzlich lehnte der Applet-Client mehr als
acht Jobs ab, obwohl das Backend bis zu 64 aktive Profiljobs verwaltet.

Die Discovery validiert Account- und Job-ID-Eindeutigkeit vor jeder
Zustandsmutation und nutzt jetzt das Backend-Limit `MAX_PROFILE_JOBS = 64`.
Regressionen prüfen doppelte Accounts, doppelte Job-IDs und neun gültige Jobs;
letztere werden vollständig übernommen.

Fokustest `node --test --test-name-pattern='profile job|device login|auxiliary'
tests/applet_runtime.test.js`: 46/46. Node-Syntaxcheck und `git diff --check`
sauber. Vollständiger JS-Lauf `node --test tests/applet_runtime.test.js`:
465/465.

## Runde 597: Persistente Profiljobs bei Hilfsfehlern erhalten

Ein Timeout, ungültiges JSON oder eine ungültige Eventliste im
`profile job-status`-Abruf löschte bisher den lokalen Jobzustand, obwohl das
Backend-Manifest weiterlief. Bei einer laufenden Account-Löschung konnte ein
Fehler im `profile cancel`-Aufruf dadurch außerdem die Löschung freigeben,
ohne bestätigte Cancellation.

Status- und Cancel-Fehler behalten Job-ID, Aktivstatus und Warte-Marker jetzt
und planen einen generation-geschützten Retry. Status-Retries bleiben bei
laufenden Account-/Backend-Schreibvorgängen blockierend; Cancel-Retries
verwenden weiterhin den erzwungenen Pfad. Terminale Zustände (`completed`,
`failed`, `cancelled`) entfernen den Job unverändert. Fehlende Timerquellen
fallen auf erneute Discovery zurück, statt rekursiv zu pollen.

Regressionen prüfen Status-Retry und dass Account-Löschung bei Cancel-Fehlern
blockiert bleibt. Fokustest `node --test --test-name-pattern='profile status
failure|profile cancel failure|profile job|device login|auxiliary'
tests/applet_runtime.test.js`: 48/48. Vollständiger JS-Lauf:
`node --test tests/applet_runtime.test.js` 467/467. Node-Syntaxcheck und
`git diff --check` sauber.

## Runde 598: Ersetzten Leisten-TreeView zerstören

`PanelSettingsList._rebuild_tree()` entfernte beim Wechsel der Anzahl der
Wertfelder den alten `Gtk.TreeView` nur aus dem `ScrolledWindow` und ersetzte
ihn durch einen neuen. Der alte TreeView samt Renderern und Signalbindungen
wurde nicht explizit zerstört; wiederholte Änderungen konnten dadurch
GTK-/Cinnamon-Heap behalten.

Der alte TreeView wird vor dem Ersetzen jetzt explizit per `destroy()`
freigegeben. Regression lauscht auf das `destroy`-Signal beim Umschalten von
2 auf 3 Wertfelder.

Fokustest `pytest -q tests/test_panel_settings_list.py`: 11/11. Python-
Syntaxcheck und `git diff --check` sauber.

## Runde 599: Masterjet-Reaping-Race im Cleanup abfangen

`DynamicSeriesList._masterjet_series()` behandelte beim ersten Cleanup-
`process.wait()` nur `subprocess.TimeoutExpired`. Wenn der Child-Prozess in
der Zwischenzeit bereits reapte oder sein Status verloren ging, konnte ein
`OSError` aus dem `finally`-Block bis in die Settings-GUI steigen.

Der erste Cleanup-Wait ignoriert jetzt neben `TimeoutExpired` auch `OSError`;
der nachgelagerte Kill-/Reap-Pfad bleibt unverändert. Regression simuliert
genau diesen Reaping-Race und erwartet fail-closed `()` statt einer Exception.

Fokustest `pytest -q tests/test_dynamic_series_list.py`: 15/15. Python-
Syntaxcheck und `git diff --check` sauber.

## Runde 600: Settings-Fenster per Prozess-PID adressieren

Mehrere gleichzeitig geöffnete `xlet-settings`-Fenster tragen denselben
Titel `Codex Usage`. `_scheduleSettingsMaximize()` adressierte sie bisher
mit `wmctrl -r "Codex Usage"`; `wmctrl` nahm dadurch ein altes oder anderes
Fenster. Live standen drei alte Fenster offen: das erste war maximiert, das
zuletzt fokussierte nicht. Das erklärte den Eindruck, dass Einstellungen
nicht öffnen bzw. unsichtbar bleiben.

`_openSettings()` liest jetzt die PID des gestarteten
`xlet-settings`-Subprozesses. Der begrenzte Lookup `wmctrl -lp` ordnet diese
PID exakt einer Fenster-ID zu. Verschieben und Maximieren nutzen danach
`wmctrl -i -r <window-id>`. Lookup und Placement bleiben generation- und
entfernungsfest; nach zwölf 250-ms-Versuchen wird ohne unsicheres Titel-
Fallback beendet. Direkte interne Aufrufe ohne PID behalten den bisherigen
Titel-Fallback.

Regressionen prüfen PID-Weitergabe, exakte PID-/Fenster-ID-Zuordnung und
beide `wmctrl -i`-Aufträge. Fokustest `node --test
--test-name-pattern='settings (launcher|window lookup|maximization|placement)|stale settings|native Cinnamon configure'
tests/applet_runtime.test.js`: 12/12. Live-Reload erfolgreich; neues Fenster
PID `3455941`, Fenster-ID `0x08600007`, `_NET_WM_STATE` enthält
`MAXIMIZED_HORZ`, `MAXIMIZED_VERT` und `FOCUSED`. Node-Syntaxcheck und
`git diff --check` sauber. `pytest -q tests/test_applet.py` hat eine
vorbestehende, unabhängige Assertion zur alten `bind`-Form (`1 failed,
26 passed`).

## Runde 601: Veraltete Panel-Binding-Test-Erwartung korrigieren

Der fokussierte Python-Test prüfte noch den alten Ausdruck
`bind("account-panel-settings", ...)`, obwohl die Produktionsänderung aus
`d95b2a6` diese Einstellung absichtlich über `_bindCustomSetting(...)`
registriert. Dadurch blieb die Testsuite trotz korrektem Code rot.

Die Assertion folgt jetzt der tatsächlichen Custom-Binding-Form. Keine
Produktionslogik geändert. `pytest -q tests/test_applet.py`: 27/27.
Python-Kompilierung der betroffenen Settings-Widgets und `git diff --check`
sind sauber.

## Runde 602: Hilfe-Seite Ruff-Fehler beheben

`help_page.py` hatte in der Tabellenmaterialisierung eine 108 Zeichen lange
Zeile. Die verschachtelte `_definition_entry(...)`-Zeile ist jetzt ohne
Logikänderung formatiert.

`ruff check files/codex-usage@H234598/help_page.py`,
`pytest -q tests/test_help_page.py` (6/6), Python-Kompilierung und
`git diff --check` sind sauber.

## Runde 603: App-Server-Hilfsfunktionen direkt abdecken

`app_server.py` hatte für mehrere reine Hilfsfunktionen keinen direkten
Regressionstest: Deadline-/Primitive-Validierung, Umgebungsfilter,
Rate-Limit- und Model-RPC-Aufträge, RPC-Fehlerklassifikation,
Response-ID-Typprüfung, Kopier-/Fehlerbegrenzung, Stream-Cleanup und das
Queue-Replacement des Line-Readers.

Gezielte Tests prüfen diese Verträge einschließlich ungültiger Model-IDs,
strikter Integer-/Boolean-Typen, Auth-/Unavailable-/Fetch-/Protocol-
Klassifikation und begrenzter Fehlermeldungen. Produktionslogik blieb
unverändert. `pytest -q tests/test_app_server.py`: 108/108. Ruff,
Python-Kompilierung und `git diff --check` sauber.

## Runde 604: Usage-Limit-Helfer direkt testen

`usage_limits.py` verarbeitet WHAM-/App-Server-Fenster fail-closed. Ein
begrenzter Fuzzlauf mit 3000 zufällig verschachtelten malformed Payloads
produzierte keine unerwartete Exception.

Zusätzlich deckt ein direkter Regressionstest bisher nur indirekt geprüfte
Helfer ab: Pool-/Fensterkonstruktion, Fensternamen, Reset-Zeit, Spark-
Identität, Normalisierung, Integer-/Boolean-Grenzen und Unique-Filter.
Produktionslogik blieb unverändert. `pytest -q tests/test_usage_limits.py`:
125/125. Ruff, Python-Kompilierung und `git diff --check` sauber.

## Runde 605: Ambige Modell-Pools im Serializer verwerfen

`AccountUsage.model_pool()` arbeitet bei case-ambigen Pool-Keys bewusst
fail-closed. `AccountUsage.as_dict()` serialisierte solche Pools bisher
trotzdem; bei identischem Key konnte ein späterer Pool den früheren ohne
Warnung überschreiben, bei unterschiedlicher Großschreibung entstand ein
später unlesbarer Snapshot.

Der Serializer erkennt jetzt case-insensitive Duplikate, entfernt beide
ambigen Einträge und behält nur eindeutige String-Keys. Regression prüft
ambigen Spark-Key neben einem gültigen Pool. `pytest -q tests/test_models.py`:
32/32; relevante State-Tests: 77/77. Ruff, Python-Kompilierung und
`git diff --check` sauber.

## Runde 606: Models-Malformed-Fuzz nach Serializer-Fix

Nach dem Serializer-Fix wurden die Model-Eigenschaften und
`AccountUsage.as_dict()` mit 2.000 zufällig typfremden Fenster-/Poolwerten
ausgeführt. Zusätzlich wurden 5.000 zufällige `AccountUsage`-Objekte mit
`json.dumps(..., allow_nan=False)` geprüft. Kein Lauf erzeugte eine
unerwartete Exception oder nicht-JSON-sichere Ausgabe.

Damit ist für diesen Modulbereich kein weiterer reproduzierbarer Fehler
offen. Produktionslogik blieb unverändert; Regression bleibt durch die
vorhandenen Model- und State-Tests abgedeckt.

## Runde 607: `account manage` in Root-Hilfe ergänzen

Der Parser registriert `codex-usage account manage`, die ausführliche
Root-Hilfe listete den Befehl bisher nicht. Dadurch fehlte gerade die
Account-Funktion zum Öffnen des isolierten Reaktivierungsbrowsers in der
CLI-Dokumentation.

Die Hilfe enthält jetzt die vollständige Syntax einschließlich Browserwahl
und Ausgabeformat. Der bestehende Root-Hilfe-Test prüft die Zeile direkt;
der Test fiel vor der Änderung erwartungsgemäß fehl und ist danach grün.
`pytest -q tests/test_cli.py`: 117/117. Ruff, Python-Kompilierung und
`git diff --check` sauber.

## Runde 608: Root-Hilfe mit Parser-Optionen synchronisieren

Der automatische Abgleich von `_build_parser()` mit `COMMAND_OVERVIEW`
fand weitere sichtbare Optionen, die Nutzer bisher nicht in der
Root-Hilfe sahen: Account-Tags/Auth-Löschung/Serien, `policy set-limits`,
Historienfilter und JSON-Aliase, Consumption-Baselines/EMA/Pool/Pfad,
Snapshot-Pfade sowie Profil-Serien und Migrationsmanifest.

`COMMAND_OVERVIEW` enthält jetzt diese Syntax einschließlich des fehlenden
`policy evaluate --format`. Der Root-Hilfe-Regressionstest prüft die neuen
Einträge; Produktionshandler blieben unverändert. `pytest -q
tests/test_cli.py`: 117/117. Ruff, Python-Kompilierung und
`git diff --check` sauber.

## Runde 609: FIFO-Blockade beim Sidecar-Schutz verhindern

`_secure_related_files()` prüft WAL-/SHM-Sidecars über
`_chmod_private_regular()`. Die Funktion öffnete Sonderdateien bisher nur
mit `O_RDONLY|O_NOFOLLOW|O_CLOEXEC`. Ein als Sidecar platzierter FIFO konnte
den Prozess dadurch beim Öffnen blockieren, bevor der Nicht-Regular-Dateityp
erkannt wurde.

`_chmod_private_regular()` verwendet jetzt zusätzlich `O_NONBLOCK`, sofern
die Plattform es anbietet. Ein FIFO wird weiterhin sicher als ungültige
Sidecar-Datei abgewiesen, blockiert aber nicht. Der Regressionstest prüft das
Flag vor dem echten Öffnen; er war vor dem Fix rot und ist danach grün.
`pytest -q tests/test_history.py`: 84/84. Ruff, Python-Kompilierung und
`git diff --check` sauber.

## Runde 610: String-Status nicht in History übernehmen

`_iter_usage_samples()` prüfte den Accountstatus bisher mit `!=` gegen
`AccountStatus.OK`. Weil `AccountStatus` ein `StrEnum` ist, passierte ein
malformed Status-String `"ok"` diesen Gate und wurde als gültige Historie
gespeichert.

Der Gate verwendet jetzt Identitätsvergleich (`is not`), wie die übrigen
Fail-Closed-Prüfungen. Ein echter `AccountStatus.OK` bleibt gültig, ein
gleichwertiger String wird verworfen. Regression testet genau diesen
Boundary-Fall. `pytest -q tests/test_history.py`: 85/85. Ruff, Python-
Kompilierung und `git diff --check` sauber.

## Runde 611: Relative History-Pfade auch beim Empty-Shortcut ablehnen

`record_usage_samples_batch()` validierte bisher nur den Pfadtyp. Bei leerem
Usage-Batch kehrte die Funktion vor `HistoryStore._prepare_path()` zurück;
`Path("relative-history.sqlite3")` lieferte dadurch fälschlich `0`, obwohl
History-Datenbanken ausschließlich absolute Pfade akzeptieren.

`_validated_history_path()` prüft jetzt zusätzlich `Path.is_absolute()`.
Damit verhalten sich leere und nichtleere Aufrufe gleich und erzeugen keine
unbeabsichtigte relative Pfadsemantik. Regression deckt den Empty-Shortcut
ab. `pytest -q tests/test_history.py`: 86/86. Ruff, Python-Kompilierung und
`git diff --check` sauber.

## Runde 612: EMA-Namen in Consumption strikt validieren

`calculate_consumption()` wandelte den Suffix von `smoothing` bisher mit
`int()` um. Dadurch wurden nicht-kanonische Namen wie `ema-05` und
`ema-0005` als `ema-5` akzeptiert, obwohl CLI und Vertrag nur die exakt
aufgelisteten EMA-Werte erlauben.

Die Validierung verlangt jetzt zusätzlich die kanonische Schreibweise
`ema-{minutes}`. Gültige Werte bleiben unverändert; führende Nullen und
andere alternative Schreibweisen werden verworfen. Drei Regressionfälle
decken die Grenze ab. `pytest -q tests/test_consumption.py`: 40/40. Ruff,
Python-Kompilierung und `git diff --check` sauber.

## Runde 613: Zero-Zeit-Duplikate nicht als Verbrauch zählen

`calculate_consumption()` addierte bisher auch ein Delta zwischen zwei
Samples mit identischem Zeitstempel. Ein Sprung von 10 auf 90 Prozent am
selben Zeitpunkt wurde dadurch als echter Verbrauch gezählt; im Beispiel
stieg der Verbrauch fälschlich auf 90 statt 10 Prozentpunkte.

Nicht-positive Zeitabstände werden jetzt übersprungen und markieren die
Abdeckung als `partial`. Positive Deltas bleiben unverändert. Der
Regressionstest verwendet zwei identische Zeitstempel und prüft, dass kein
Verbrauch erfunden wird. `pytest -q tests/test_consumption.py`: 41/41.
Ruff, Python-Kompilierung und `git diff --check` sauber.

## Runde 614: Settings-Fenster am Applet-Monitor platzieren

Der Settings-Launcher wählte die Zielposition bisher über
`Main.layoutManager.currentMonitor`. Dieser Wert folgt dem aktuell fokussierten
Monitor, nicht zwingend dem Monitor des geklickten Applets. Bei mehreren
Monitoren konnte `xlet-settings` deshalb korrekt starten, aber außerhalb des
sichtbaren Arbeitsmonitors erscheinen; das wirkte wie „Einstellungen öffnet
nicht“.

`_scheduleSettingsMaximize()` verwendet jetzt zuerst
`findMonitorForActor(this.actor)` und fällt nur bei fehlendem Monitor auf
`currentMonitor` zurück. Verschieben und Maximieren bleiben unverändert
begrenzt und PID-sicher. Regression simuliert fokussierten Monitor `x=3840`
bei Applet-Monitor `x=0` und prüft den `wmctrl -e`-Aufruf. Der fokussierte
Settings-Lauf `node --test --test-name-pattern='settings (launcher|window
lookup|maximization|placement)|native Cinnamon configure|stale settings'
tests/applet_runtime.test.js`: 13/13.

## Runde 615: Routing-Policy verlangt exakt 0600

`load_policy()` meldete bei zu offenen Policy-Dateien zwar „0600“, prüfte
aber nur Gruppen-/Andere-Bits. Dateien mit `0700` oder `0400` wurden deshalb
akzeptiert. Für eine Datei, die bezahltes Credit-Routing und Modellfreigaben
steuert, war das ein inkonsistenter Schutzvertrag.

Die Prüfung verwendet jetzt `stat.S_IMODE(...) == 0o600` und verwirft jede
abweichende Rechtekombination weiterhin vor JSON-Verarbeitung. Regression
setzt eine gültige Policy auf `0700` und erwartet die kontrollierte Ablehnung.
`pytest -q tests/test_routing.py`: 127/127; Ruff, Mypy, Python-Kompilierung
und `git diff --check` sauber.

## Runde 616: Scheduler-Blockzustand bei fehlerhaften Reset-Vergleichen fail-closed

`_block_state()` fing Fehler aus Zeitzonen- und Reset-Vergleichen zunächst
nicht vollständig ab. Ein fehlerhaftes `datetime`-Objekt konnte bei Auswahl,
Gleichheitsprüfung, `isoformat()` oder dem Vergleich mit `now` einen
`RuntimeError` bis in den Watchdog durchreichen.

Die Auswahl- und Vergleichsphase ist jetzt vollständig geschützt. Bei einem
unbekannten Reset wird konservativ „Limit erreicht; Resetzeit unbekannt“
zurückgegeben. Regression deckt einen fehlerhaften `<=`-Vergleich ab.
`pytest -q tests/test_scheduler.py`: 206/206; Ruff, Mypy,
Python-Kompilierung und `git diff --check` sauber.

## Runde 617: Scheduler-Watchdog bei fehlerhaften Datetime-Vergleichen fail-closed

`_watch_core_resets_current()`, `_blocked_until_active()` und
`_capture_is_too_far_in_future()` fingen bisher nur ausgewählte Standard-
Exceptions ab. Ein fehlerhaftes `datetime`-Objekt konnte bei `<=` oder `>`
deshalb einen `RuntimeError` nach außen geben und den Watchdog-Zyklus
abbrechen.

Die drei Validierungspfade behandeln jetzt jeden normalen Vergleichsfehler
als ungültige bzw. zu unsichere Zeit: Resetprüfung und Blockaktivität werden
verworfen, ein fehlerhafter Zukunftsvergleich gilt als zu weit in der Zukunft.
Regressionstests decken alle drei Grenzen mit absichtlich fehlerhaften
Datetime-Vergleichen ab. `pytest -q tests/test_scheduler.py`: 209/209;
Ruff, Mypy, Python-Kompilierung und `git diff --check` sauber.

## Runde 618: Watchdog-Re-Fetch bei fehlerhafter Capture-Zeit fail-closed

`_current_supersedes_blocked_snapshot()` verglich die Capture-Zeit des
aktuellen Werts mit dem Block-Snapshot nur gegen einige Standardfehler. Ein
fehlerhafter Vergleich konnte den Watchdog beim Re-Fetch-Entscheid abbrechen.

Der Vergleich behandelt jetzt jeden normalen Fehler als „nicht neuer“ und
lässt den bestehenden Block-Snapshot sicher bestehen. Regression deckt einen
fehlerhaften `>`-Vergleich ab. `pytest -q tests/test_scheduler.py`: 210/210;
Ruff, Mypy, Python-Kompilierung und `git diff --check` sauber.

## Runde 619: Authentisierte Stabilisierung bei fehlerhafter Capture-Differenz fail-closed

`_stabilize_authenticated_usage()` berechnete das Alter zwischen aktueller
und vorheriger Capture-Zeit. Ein fehlerhaftes Datetime-Objekt konnte bei der
Subtraktion einen unerwarteten `RuntimeError` auslösen und den Abrufzyklus
abbrechen.

Die Altersprüfung behandelt jetzt jeden normalen Fehler als unbrauchbaren
Stabilisierungsvergleich und verwendet unverändert den aktuellen Abruf.
Regression deckt einen fehlerhaften `__sub__`-Vergleich ab. `pytest -q
tests/test_scheduler.py`: 211/211; Ruff, Mypy, Python-Kompilierung und
`git diff --check` sauber.

## Runde 620: Reset-Diskontinuität bei fehlerhaftem Datetime-Vergleich fail-closed

`_has_unexpired_window_reset_discontinuity()` prüfte alte und aktuelle
Reset-Zeitpunkte nur gegen ausgewählte Standardfehler. Ein fehlerhafter
Datetime-Operator konnte die authentisierte Fenster-Stabilisierung abbrechen.

Die Diskontinuitätsprüfung verwirft jetzt jeden normalen Vergleichsfehler und
behält dadurch keine möglicherweise erschöpften alten Limits. Regression
deckt einen fehlerhaften `<=`-Vergleich ab. `pytest -q tests/test_scheduler.py`:
212/212; Ruff, Mypy, Python-Kompilierung und `git diff --check` sauber.

## Runde 621: State-Generation-Lesefehler nach Abruf regressionsgesichert

Der zweite `load_state_generation()`-Aufruf in `fetch_all()` schützt vor
einem State-Rennen nach dem Abruf. Dieser Fehlerpfad löschte Werte bereits
fail-closed, hatte aber keine Regression für den Fall eines Lese-`OSError`
mit bestehender Abruf-Fehlermeldung.

Der Test prüft jetzt Fehlerverkettung, geleerte Limits, `stale` und
`cache_invalidated`. Produktionslogik blieb unverändert. `pytest -q
tests/test_scheduler.py`: 213/213; Ruff, Mypy, Python-Kompilierung und
`git diff --check` sauber.

## Runde 622: Konservativer Direct-Vergleich bei fehlerhaftem Reset-Zeitpunkt fail-closed

`_is_more_conservative_direct_usage()` verglich Reset-Zeitpunkte bisher nur
gegen ausgewählte Standardfehler. Ein fehlerhafter `datetime`-Operator konnte
die Entscheidung über konservativere Direct-Werte abbrechen.

Der Vergleich verwirft jetzt jeden normalen Operatorfehler und liefert
konservativ `False`. Regression deckt einen fehlerhaften `<=`-Vergleich ab.
`pytest -q tests/test_scheduler.py`: 214/214; Ruff, Mypy,
Python-Kompilierung und `git diff --check` sauber.

## Runde 623: Watchdog-Pool-/Fenster-Properties bei Laufzeitfehlern fail-closed

`_pool_forces_watchdog_block()` und `_window_is_exhausted()` behandelten nur
ausgewählte Standardfehler. Fehlerhafte Properties konnten dadurch den
Watchdog statt einer konservativen Blockentscheidung abbrechen.

Beide Gates werten normale Property-Fehler jetzt als erschöpft bzw. blockierend.
Regressionen prüfen fehlerhafte Pool- und Restwert-Properties. `pytest -q
tests/test_scheduler.py`: 216/216; Ruff, Mypy, Python-Kompilierung und
`git diff --check` sauber.

## Runde 624: Watch-Cycle-Health bei fehlerhafter Pool-Evidenz fail-closed

`_watch_cycle_is_healthy()` wertete Pool-Evidenz außerhalb eines vollständig
geschützten Fehler-Gates aus. Eine fehlerhafte `availability_sources`-Property
konnte den Watchdog-Zyklus mit `RuntimeError` abbrechen.

Der Health-Gate behandelt normale Laufzeitfehler jetzt als ungesunden Zyklus.
Regression prüft eine fehlerhafte Pool-Evidenz-Property. `pytest -q
tests/test_scheduler.py`: 217/217; Ruff, Mypy, Python-Kompilierung und
`git diff --check` sauber.

## Runde 625: History-Batch-Fehler regressionsgesichert

`fetch_all(save_snapshots=True)` behandelt Fehler beim gemeinsamen History-
Schreiben bewusst getrennt von Current-/Snapshot-Fehlern. Dieser Schutzpfad
war bisher nicht direkt getestet.

Der Test prüft, dass gültige aktuelle Nutzung unverändert zurückkommt und
genau ein `history/sample_save_failed`-Health-Event entsteht. Produktionslogik
blieb unverändert. `pytest -q tests/test_scheduler.py`: 218/218; Ruff, Mypy,
Python-Kompilierung und `git diff --check` sauber.

## Runde 626: App-Server-Fallback bewahrt Ambiguitäts-Schutz

Fällt der App-Server wegen Nichtverfügbarkeit auf Direct zurück, muss der
Fallback dieselbe Identitätsprüfung wie ein normaler Direct-Abruf erhalten.
Ohne das Flag konnte eine gemeinsame Benutzeridentität mehrere konfigurierte
Accounts falsch zuordnen.

Regression mit zwei Accounts und gemeinsamer Benutzeridentität prüft, dass
beide Direct-Fallbacks `reject_ambiguous_backend_identity=True` erhalten und
den Fallback-Grund unverändert melden. Produktionslogik blieb unverändert.
`pytest -q tests/test_scheduler.py`: 219/219; Ruff, Mypy,
Python-Kompilierung und `git diff --check` sauber.

## Runde 627: Browser-Fehler meldet tatsächliche Transport-Provenienz

`_fetch_one()` meldete bei einem Fehler im headed Browser-Pfad den
konfigurierten authentisierten Backend-Namen als `backend_used`. Dadurch
erschien ein fehlgeschlagener Browser-Abruf fälschlich als Direct-Abruf.

Der Pfad führt jetzt tatsächliche Transport-Provenienz separat und setzt bei
Browser-Fehlern `backend_used="browser"`; Direct-/App-Server-Fallbacks bleiben
unverändert. Regression prüft Status, konfigurierte Provenienz, Transport und
Cache-Invalidierung. `pytest -q tests/test_scheduler.py`: 220/220; Ruff,
Mypy, Python-Kompilierung und `git diff --check` sauber.

## Runde 628: Invalidierte GUI-Daten verbergen Credits

Die Applet-Validierung und der Invalidierungs-Merge löschten Fenster und
Modellpools, ließen aber `credits` stehen. `_creditParts()` renderte diesen
Wert trotz `cache_invalidated=true`; dadurch konnten veraltete Creditstände
nach einem fehlgeschlagenen Abruf sichtbar bleiben.

Invalidierte Payloads verwerfen jetzt Credits und Creditverbrauch-Daten,
der Invalidierungs-Merge leert zusätzlich `credits`, und die Credit-Ausgabe
verweigert invalidierte Werte. Regressionen prüfen Validierung, Merge und
direkten Renderer-Pfad. `node --test tests/applet_runtime.test.js`: 472/472
(inklusive bestehender Runtime-Tests) sauber.

## Runde 629: Alle invalidierten dynamischen Anzeigeziele bleiben leer

Nach dem Credit-Fix konnten direkte Renderer-Aufrufe mit einem bereits
invalidierten Objekt weiterhin Credit-Prozentwert, Verbrauchsfenster oder
Resetdaten ausgeben. Der normale Merge deckte diese Fälle meist ab, aber
Renderer-Gates durften Invalidierung nicht voraussetzen.

`_panelValueForSource`, Verbrauchs-, Creditverbrauch- und Reset-Renderer
brechen bei invalidierten Accounts jetzt früh ab. `_clearInvalidatedUsage`
setzt zusätzlich `usage_resets` auf unbekannt. Regression prüft Renderer und
Invalidierungs-Merge. `node --test tests/applet_runtime.test.js`: 472/472
sauber.

## Runde 630: Resetdaten und direkte Invalidierungs-Renderer abgesichert

Der Invalidierungs-Merge entfernte zwar Limits, Modelle und Verbrauchsfenster,
übernahm aber mögliche `usage_resets` unverändert. Zusätzlich konnten direkte
Aufrufe von Verbrauchs- und Reset-Renderern ein invalidiertes Objekt umgehen.

Der Merge setzt Resetdaten jetzt auf unbekannt; alle betroffenen Renderer
brechen für invalidierte Accounts früh ab. Regression deckt diese direkten
Pfade mit gültigen Altwerten ab. `node --test tests/applet_runtime.test.js`:
472/472 sauber.

## Runde 631: Invalidierte Accounts erzeugen keine Verbrauchs-Requests

`_refreshConsumption()` stellte bisher auch für Accounts mit
`cache_invalidated=true` Token- und Creditverbrauchs-Requests zusammen. Die
zugehörigen Renderer verwerfen solche Werte bereits; die Requests konnten
deshalb nur unnötige I/O- und Queue-Last erzeugen.

Die Verbrauchsplanung überspringt invalidierte Accounts jetzt vollständig.
Regression prüft, dass trotz aktivierter Verbrauchseinstellungen keine Queue-
Einträge entstehen. `node --test tests/applet_runtime.test.js`: 473/473;
`make applet-check` inklusive JSON-Validierung sauber.

## Runde 632: Tokendelta begrenzt widersprüchlichen Reset-Horizont

`_panelDeltaIsDynamic()` nutzte einen zukünftigen `reset_at` direkt als
Projektionshorizont. Ein widersprüchlicher Reset weit hinter der deklarierten
Fensterdauer konnte dadurch eine dynamische Warnung auslösen, obwohl die
Fensterdauer diesen Horizont nicht zulässt.

Die Hochrechnung nutzt jetzt höchstens die deklarierte Fensterdauer; ein
kürzerer gültiger Reset bleibt wirksam. Ungültige oder fehlende Dauerwerte
beenden die Prüfung fail-closed. Regression prüft einen 5h-Kandidaten mit
Reset in 24 Stunden. `node --test tests/applet_runtime.test.js`: 474/474;
`make applet-check` inklusive JSON-Validierung sauber.

## Runde 633: Verbrauchsfenster mit Dauer null verworfen

`_safeConsumptionWindows()` akzeptierte bisher `limit_window_seconds: 0`.
Das widerspricht dem Backend-Vertrag positiver Fensterdauern und konnte bei
`Δsonst.` als gültiges sonstiges Fenster erscheinen, obwohl keine Zeitspanne
existiert.

Die Applet-Validierung verlangt jetzt ebenfalls eine strikt positive Dauer.
Regression erweitert die DTO-Validierung um den Nullfall.

## Runde 634: Coverage und Samplezahl konsistent validiert

`_safeConsumptionWindows()` akzeptierte bisher `coverage: "complete"` oder
`"partial"` mit weniger als zwei Samples. Umgekehrt konnte `"insufficient"`
mit mindestens zwei Samples durchgelassen werden. `_panelDeltaIsDynamic()`
konnte dadurch statistische Warnungen aus einem unbrauchbaren Messdatensatz
ableiten.

Die DTO-Prüfung verlangt jetzt mindestens zwei Samples für `complete`,
`partial` und `stale`; `insufficient` ist nur mit null oder einem Sample
gültig. Regression deckt den widersprüchlichen Complete-Fall ab.
`node --test --test-name-pattern='(consumption|Tokendelta|panel delta|dynamic delta|forecast)' tests/applet_runtime.test.js`:
37/37; `make applet-check` inklusive JSON-Validierung: 474/474 sauber.

## Runde 635: Leere Consumption-Pool-ID verworfen

`_safeConsumptionWindows()` übernahm bisher eine leere `pool`-ID. Der
Backend-Snapshotvertrag verlangt nichtleere Token; eine leere ID konnte
dadurch als scheinbar validiertes, später aber nicht auswählbares
Verbrauchsfenster im Cache landen.

Die DTO-Prüfung verlangt jetzt eine nichtleere Pool-ID. Regression erweitert
die Consumption-Validierung um den leeren Token.

## Runde 636: Consumption-Pool-Token an Backend-Format gebunden

Auch nichtleere Pool-IDs mit Leerzeichen wurden bisher akzeptiert. Der
Backend-Snapshotvertrag erlaubt für Pool-Token ausschließlich druckbare
ASCII-Zeichen ohne Leerzeichen; solche IDs sind weder gültige Vertragsdaten
noch auswählbare Applet-Pools.

Die DTO-Prüfung akzeptiert jetzt nur noch nichtleere ASCII-Token im erlaubten
Bereich `!` bis `~`. Regression ergänzt eine Pool-ID mit Leerzeichen.
`node --test --test-name-pattern='(consumption|Tokendelta|panel delta|dynamic delta|forecast)' tests/applet_runtime.test.js`:
37/37; `make applet-check` inklusive JSON-Validierung: 474/474 sauber.

## Runde 637: Unbekannte Verbrauchsdaten nicht ausblenden

`_consumptionWindowPart()` und `_creditConsumptionParts()` prüften
`hide-when-zero` vor der Coverage-Auswertung. Backend-Daten mit
`coverage: "insufficient"` tragen konstruktionsbedingt Verbrauch `0`; die
Einstellung konnte dadurch den Hinweis „nicht genügend Messdaten“ ausblenden.

Die Null-Unterdrückung greift jetzt nur bei sicher bekannten Coverage-Werten.
`insufficient` bleibt sichtbar, auch wenn Nullwerte ausgeblendet werden soll.
Regressionen decken Token- und Creditverbrauch ab. Fokus 37/37;
`make applet-check`: 474/474 sauber.

## Runde 638: Inflight-Verbrauchsantworten respektieren Invalidierung

Ein bereits laufender Verbrauchs-Request konnte nach einer
`cache_invalidated`-Antwort noch `cost_windows` in denselben Account schreiben.
Der nächste Renderer-Aufruf sah damit wieder veraltete Verbrauchsdaten, und
die Invalidierung war im Datenzustand nicht dauerhaft.

Der Callback übernimmt Fenster jetzt nur noch für nicht invalidierte Accounts.
Regression simuliert die Invalidierung zwischen Requeststart und Callback.
`node --test --test-name-pattern='(consumption|Tokendelta|panel delta|dynamic delta|forecast)' tests/applet_runtime.test.js`:
38/38; `make applet-check` inklusive JSON-Validierung: 475/475 sauber.

## Runde 639: Leisten-Tokendelta verbirgt unzureichende Messdaten

`_panelDeltaPart()` zeigte bei `coverage: "insufficient"` den Backend-Wert
`0` als `0,0%`. Die normale Verbrauchsanzeige kennzeichnet denselben Zustand
bereits als „nicht genügend Messdaten“; die Leiste konnte dagegen einen
scheinbar belastbaren Wert vortäuschen.

Leisten-Tokendelta rendert unzureichende Coverage jetzt als `–`; belastbare
Coverage bleibt unverändert numerisch. Regression ergänzt diesen Panelpfad.
`node --test --test-name-pattern='(consumption|Tokendelta|panel delta|dynamic delta|forecast)' tests/applet_runtime.test.js`:
38/38; `make applet-check` inklusive JSON-Validierung: 475/475 sauber.

## Runde 640: Direkte Panelpfade respektieren Cache-Invalidierung

Direkte Aufrufe der konfigurierbaren Panelquellen konnten bei einem
`cache_invalidated`-Objekt noch alte Prozent-, Pool-, Tokenende- und
Tokendelta-Werte verwenden, wenn diese Felder im Objekt verblieben. Der
normale Merge leert sie zwar, direkte Renderer-Grenzen müssen trotzdem
fail-closed sein.

`_panelValueForSource`, `_panelWindowForSource`, `_panelWindowForKey`,
`_panelForecastPart` und `_panelDeltaPart` verweigern invalidierte Daten jetzt
früh. Regression speist alte Werte in alle betroffenen Panelpfade ein.
Fokus 116/116; `make applet-check`: 475/475 sauber.

## Runde 641: Credit-Renderer akzeptiert keinen Null-Account

`_creditParts(null, ...)` griff trotz vorhandener Teilprüfung direkt auf
`usage.account` zu und warf bei einem fehlenden Usage-Objekt einen
`TypeError`. Renderer-Grenzen sollen bei fehlenden Daten sicher leer bleiben.

Der Credit-Renderer beendet sich jetzt bei fehlendem Usage-Objekt früh mit
`null`. Regression ergänzt diesen direkten Nullpfad. Fokus 116/116;
`make applet-check`: 475/475 sauber.

## Runde 642: Invalidierter Loginstatus ist nicht erfolgreich

Das Leistenfeld `Login erfolgreich` behandelte `status: "partial"` als
`ja`. Der Invalidierungs-Merge verwendet genau diesen Status für verworfene
Caches; alte oder ungültige Daten konnten dadurch einen erfolgreichen Login
vortäuschen.

Die Anzeige verlangt jetzt zusätzlich `cache_invalidated !== true` für `ja`.
Regression prüft invalidierten `partial`-Status und erwartet `Login nein`.
Fokus 116/116; `make applet-check`: 475/475 sauber.

## Runde 643: Langsamer Settings-Start bleibt sichtbar

Der PID-gezielte Lookup des `xlet-settings`-Fensters brach bisher nach zwölf
250-ms-Ticks ab. Das sind nur drei Sekunden. Beim kalten Start der großen
Cinnamon-Settings-GUI kann das Fenster später erscheinen; danach blieb es auf
seiner gespeicherten Position außerhalb des sichtbaren Monitors. Für den
Benutzer sah das wie ein nicht öffnendes Einstellungsmenü aus.

Der Lookup wartet jetzt bis zu 40 Ticks (zehn Sekunden), ohne das sichere
PID-Matching oder die Generation-/Cleanup-Guards zu ändern. Eine Regression
simuliert ein Fenster, das erst beim zwölften Lookup auftaucht, und prüft
anschließendes Verschieben per Fenster-ID.

Fokustest `node --test --test-name-pattern='settings (launcher|window
lookup|maximization|placement)|stale settings|native Cinnamon configure'
tests/applet_runtime.test.js`: 14/14. `git diff --check` sauber.

## Runde 644: Beschädigte Formatierungstabelle öffnet Settings nicht mehr kaputt

`_BoundFormatList` übernahm bisher jeden gespeicherten Tabellenwert direkt an
`TreeListWidgets.List.on_setting_changed()`. `null` löste dort beim Öffnen
`TypeError: 'NoneType' object is not iterable` aus; nicht-dict-Zeilen oder
falsche Zelltypen konnten ebenfalls die gesamte `xlet-settings`-GUI abbrechen.

Der Formatierungslisten-Adapter akzeptiert jetzt nur Listen und
Zeilenobjekte. Ungültige Zeilen bzw. nicht passende Zelltypen werden
übersprungen; gültige Zeilen bleiben unverändert. Persistierte Daten werden
nicht automatisch überschrieben, damit kein stiller Verlust entsteht.

Regression deckt `null`, Nicht-Objekt-Zeilen und falsche Zelltypen ab.
`pytest -q tests/test_format_table_selector.py`: 9/9; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 645: Beschädigte Account-Serienzeilen brechen Settings nicht mehr ab

`DynamicSeriesList` nutzte ebenfalls direkt die ungeschützte
`TreeListWidgets.List.on_setting_changed()`-Implementierung. Ein beschädigter
`account-series-settings`-Wert (`null`, Nicht-Objekt-Zeile oder falscher
Zelltyp) konnte dadurch den gesamten Einstellungsdialog beim Aufbau beenden.

Die Account-Serienliste lädt jetzt nur Listen mit Objektzeilen. Ungültige
Zeilen und nicht passende GTK-Zellwerte werden verworfen; gespeicherte Daten
bleiben unangetastet.

Regression deckt alle drei Fälle ab. `pytest -q
tests/test_dynamic_series_list.py`: 16/16; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 646: Beschädigte Leistenzeilen brechen Settings nicht mehr ab

`PanelSettingsList` erbte für den ersten Settings-Load weiterhin die
ungeprüfte `TreeListWidgets.List.on_setting_changed()`-Implementierung. Ein
beschädigter `account-panel-settings`-Wert (`null`, eine Nicht-Objekt-Zeile,
ein falscher Zelltyp oder ein Integer außerhalb des GTK-Bereichs) konnte den
gesamten Einstellungsdialog beim Aufbau beenden. Derselbe Abbruch war beim
Ändern der Wertanzahl über `_rebuild_tree()` möglich.

Die Leistenliste akzeptiert jetzt nur Listen mit Objektzeilen und fängt
`OverflowError`, `TypeError` und `ValueError` beim Einfügen in das GTK-Modell
ab. Ungültige Zeilen werden übersprungen; persistierte Einstellungen werden
nicht automatisch überschrieben. Regression deckt Initial-Load und
Wertanzahl-Rebuild ab. `pytest -q tests/test_panel_settings_list.py`: 15/15;
Ruff, Python-Compile und `git diff --check` sauber.

## Runde 647: Formatierungs- und Prognosentabellen fangen GTK-Overflow ab

Der gemeinsame `_BoundFormatList`-Adapter der Formatierungs- und
Prognosenseite übersprang falsche Zelltypen bereits bei `TypeError` und
`ValueError`, aber nicht bei `OverflowError`. Ein persistierter Integer
außerhalb des GTK-`gint`-Bereichs konnte deshalb beim Aufbau der ausgewählten
Tabelle den Einstellungsdialog erneut abbrechen.

Der Adapter fängt jetzt auch `OverflowError` und lässt die beschädigte Zeile
leer aus. Persistierte Werte werden nicht automatisch überschrieben. Eine
Regression erzeugt eine Integer-Zeile mit `2**31`; sie deckt damit den echten
GTK-GValue-Fehler ab. `pytest -q tests/test_format_table_selector.py
tests/test_forecast_table_selector.py`: 16/16; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 648: Ungültige Selector-Werte bleiben hash-sicher

`FormatTableSelector.on_setting_changed()` und die duplizierte
`ForecastTableSelector`-Methode prüften den gespeicherten Tabellenwert direkt
als Dictionary-Schlüssel. Eine beschädigte JSON-Auswahl als Liste oder Dict
führte dadurch zu `TypeError: unhashable type` und verhinderte den Aufbau der
Settings-Seite.

Beide Selector-Pfade akzeptieren jetzt nur String-IDs; alle anderen Werte
fallen auf die erste deklarierte Tabelle zurück, ohne den beschädigten Wert zu
überschreiben. Regressionen decken Liste und Dict für Formatierungen und
Prognosen ab. `pytest -q tests/test_format_table_selector.py
tests/test_forecast_table_selector.py`: 20/20; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 649: Hilfe-Builder ignoriert beschädigte Schema-Schlüssel

Der schema-getriebene Hilfe-Builder nahm bisher an, dass alle Einträge in
`layout.pages` und `sections` String-Schlüssel sind. Eine Liste oder ein Dict
als Schlüssel führte beim Lesen zu einem unhashable-`TypeError`. Zusätzlich
konnte eine kopierte Tokendelta-Tabelle mit einer nicht-stringartigen Basis-
Spalten-ID denselben Abbruch auslösen.

Der Builder verwirft jetzt ungültige Seitensektionen und kopierte Basis-
Spalten-IDs fail-closed; gültige Hilfeeinträge bleiben unverändert. Regression
deckt verschachtelte Layoutfehler und eine unhashable Basis-ID ab.
`pytest -q tests/test_help_page.py`: 9/9; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 650: Account-Serienliste fängt GTK-Integer-Overflow ab

`DynamicSeriesList` übersprang falsche Zelltypen beim Laden gespeicherter
Accountzeilen bereits für `TypeError` und `ValueError`, aber nicht für
`OverflowError`. Die Schemafelder `browser` und `backend` sind GTK-
`gint`-Spalten; ein persistierter Wert wie `2**31` konnte deshalb den
Einstellungsdialog beim Aufbau abbrechen.

Der Loader fängt jetzt auch `OverflowError` und lässt die beschädigte Zeile
leer aus. Persistierte Daten werden nicht automatisch überschrieben.
Regression deckt den Integer-Overflow ab. `pytest -q
tests/test_dynamic_series_list.py`: 17/17; Ruff, Python-Compile und
`git diff --check` sauber.

## Runde 651: Backend-Overview verwirft nicht-stringartige Account-Labels

`_loadAccountBackends()` rief `_safeText(item.label, 120)` bisher außerhalb
des geschützten Parsing-Blocks auf. Ein malformed Overview-Label (zum Beispiel
eine Zahl) warf dadurch aus dem asynchronen Callback heraus. Die umgebende
Hilfsprozessschicht konnte den Fehler zwar protokollieren, aber der Account-
Sync wurde ohne kontrollierte Ablehnung abgebrochen.

Die Labelvalidierung läuft jetzt im selben fail-closed Block wie Account-ID,
Backend, Browser und Pfade. Ungültige Overview-Daten ersetzen keinen alten
Zustand und verlassen den Callback kontrolliert. Regression prüft genau diesen
Fall. Account-/Panel-Fokus: 15/15 Runtime-Tests; Node-Syntaxcheck und
`git diff --check` sauber.

## Runde 652: Strikte GJS-Ausführung blockierte Einstellungsmenü

Der neue geschützte Account-Overview-Parser schrieb `label` ohne lokale
Deklaration. Node-Test-Sandbox lief dadurch zufällig weiter, GJS behandelt
Applet-Module jedoch strikt und warf bei jedem gültigen Account einen
`ReferenceError`. Der Callback brach vor `_backendRowsReady` ab; dadurch
blieb die Synchronisierung unvollständig und der Einstellungszugriff wirkte
defekt.

`label` ist jetzt vor dem Parsing-Block lokal deklariert. Regression führt
denselben Overview-Pfad in strikt ausgeführter Sandbox aus und prüft, dass
gültige Zeilen synchronisiert werden. `node --test tests/applet_runtime.test.js`:
478/478; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 653: Beschädigtes Label im Account-Settings-Callback

`_onAccountBackendsChanged()` normalisierte `row.label` bisher außerhalb eines
Fehlerguards. Ein beschädigter gespeicherter Wert, etwa eine Zahl statt Text,
warf deshalb aus dem Settings-Callback. Der äußere Schutz protokollierte den
Fehler, stellte aber weder den autoritativen Backend-Stand wieder her noch
verhinderte wiederholte Callback-Fehler.

Die Label-Normalisierung fällt jetzt bei ungültigem Typ kontrolliert auf
`_loadAccountBackends()` zurück. Regression prüft, dass der Callback nicht
wirft und genau einen Reload anfordert. Fokussierte Account-/Backend-Tests:
24/24; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 654: Textfelder im Account-Settings-Callback fail-closed

Neben `label` konnten auch `tag`, `auth-json`, `profile-dir` und `series` als
falsche gespeicherte Typen aus `_onAccountBackendsChanged()` werfen. Damit
blieb derselbe Settings-Callback bei beschädigten Einzelzeilen instabil.

Alle vier Textnormalisierungen laufen jetzt gemeinsam in einem Guard und
fordern bei Fehlern einen autoritativen Backend-Reload an. Ein parametrischer
Regressionstest deckt alle vier Felder ab. Fokussierte Account-/Backend-Tests:
25/25; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 655: Reaktivierungsbrowser wurde vom Anzeige-Browser überschrieben

Beim Speichern eines Accounts und beim Starten eines neuen Profiljobs leitete
der Applet-Code `--reactivation-browser` fälschlich aus `browser` ab. Eine
separate Einstellung wie `vivaldi` wurde dadurch bei jedem Account-Write durch
`firefox` oder `chromium` ersetzt; bestehende Reaktivierungswerte wirkten nur
bei direkter Menüaktion korrekt.

Die CLI-Argumente verwenden jetzt ausdrücklich `reactivation-browser` aus der
Accountzeile, mit begrenztem Mapping `auto`/`vivaldi`/`chromium`/`firefox` und
Fail-Closed-Fallback. Regression prüft Account-Write und Profiljob mit
Firefox-Anzeige, aber Vivaldi-Reaktivierung, sowie ungültige Mappingwerte.
Fokussierte Account-/Reaktivierungs-Tests: 26/26; Node-Syntaxcheck und
`git diff --check` sauber.

## Runde 656: Reaktivierungs-Mapping zentralisiert

Die vier zulässigen Reaktivierungsbrowser standen nach dem Fix noch einmal
als getrennte Objektzuordnung im Overview-Parser. Das Mapping nutzt jetzt
dieselbe zentrale, begrenzte Namensliste wie Account-Write und Profiljob.
Damit können Parser- und Schreibpfad nicht mehr still unterschiedliche
Indexwerte vergeben. Die fokussierten 26/26 Tests bleiben grün.

## Runde 657: Einstellungsstarter blockierte am AT-SPI-Bridge-Start

Der Einstellungsstarter rief `xlet-settings` ohne eigene Prozessumgebung auf.
Auf diesem Cinnamon-Desktop wartet GTK dabei auf den nicht funktionierenden
AT-SPI-Bridge-Dienst. Die Prozesse liefen weiter, aber das Fenster erschien
erst nach vielen Sekunden; fünf liegengebliebene `xlet-settings`-Prozesse und
die Cinnamon-Warnungen `AT-SPI: Could not obtain desktop path or name` sowie
`GetRegisteredEvents returned message with unknown signature` bestätigten den
Pfad. Ein direkter Start ohne Bridge öffnete das Fenster innerhalb einer
Sekunde, der normale Start blieb über acht Sekunden unsichtbar.

`_openSettings()` startet `xlet-settings` jetzt über
`Gio.SubprocessLauncher` mit `NO_AT_BRIDGE=1`. Das betrifft nur diesen
Einstellungs-Hilfsprozess; die Applet-Laufzeit bleibt unverändert. Der
Launcherpfad ist durch Regressionen für Argumente, Umgebungsvariable,
Fenster-PID und verzögerte Maximierung abgedeckt. Fokussierte
Einstellungs-Tests: 10/10; Node-Syntaxcheck und `git diff --check` sauber.

## Runde 658: Leisten-Editor zeigte bei unbekannter Quelle einen alten Wert

Der GTK-Renderer für Optionsfelder wird zwischen Tabellenzeilen wiederverwendet.
Bei einem unbekannten gespeicherten Quellenwert, etwa `99`, setzte die
Mapping-Funktion keinen neuen Text. Der Renderer behielt dadurch den Text der
vorherigen Zeile; reproduzierbar zeigte eine Zeile mit `99` weiterhin `5h`.
Zusätzlich konnten solche Werte beim Wechsel der Anzahl der Leistenfelder über
den Rebuild-Pfad wieder in das Modell gelangen.

`PanelSettingsList` verwirft jetzt unbekannte Optionswerte sowohl beim Laden
als auch beim Rebuild. Die Mapping-Funktion leert den Renderer vor jeder
Zuordnung, damit auch direkte Modelländerungen keinen alten Text übernehmen.
Regression deckt unbekannten Wert `99`, Rebuild und Renderer-Reuse ab.
`tests/test_panel_settings_list.py`: 17/17; `tests/test_help_page.py`: 9/9;
Python-Kompilierung und `git diff --check` sauber.

## Runde 659: Leisten-Rebuild verlor Hidden-Slots bei gemischten Zeilen

Beim Ändern der Anzahl der Leistenfelder prüfte `_on_count_changed()` bisher,
ob alle gespeicherten Zeilen Dictionaries sind. Eine einzelne beschädigte
Zeile zwang dadurch den Fallback auf das aktuell sichtbare GTK-Modell. Dieses
Modell enthält absichtlich keine vorübergehend ausgeblendeten `slotN`-Felder;
deren Werte gingen beim Rebuild verloren.

Der Rebuild übernimmt jetzt alle gültigen gespeicherten Dictionary-Zeilen und
ignoriert nur nicht-dictionary Zeilen. Der sichtbare Modell-Fallback bleibt für
leere oder vollständig unbrauchbare Listen erhalten. Regression mit gültiger
Zeile, verborgenem `slot3` und zusätzlicher beschädigter Zeile prüft, dass der
Hidden-Slot nach Count-Wechsel erhalten bleibt. `tests/test_panel_settings_list.py`:
18/18; Python-Kompilierung und `git diff --check` sauber.

## Runde 660: `list_changed()` behält Hidden-Slots trotz beschädigter Zeilen

Beim Speichern sichtbarer Leistenänderungen verwendete `list_changed()` die
gesamte gespeicherte Liste für den Positions-Fallback. Stand eine beschädigte
Nicht-Dictionary-Zeile vor einer gültigen Zeile, wurde beim Umbenennen des
sichtbaren Accounts kein passender Vorgänger gefunden und der Fallback traf auf
die beschädigte Zeile. Versteckte `slotN`-Werte der gültigen Zeile verschwanden.

Der Positions-Fallback arbeitet jetzt nur noch mit gespeicherten Dictionary-
Zeilen. Damit bleiben Hidden-Slots auch bei beschädigten Fremdzeilen erhalten;
der Account-Match bleibt vorrangig. Regression setzt eine beschädigte Zeile vor
eine gültige Zeile, benennt den sichtbaren Account um und prüft `slot3`.
`tests/test_panel_settings_list.py`: 19/19; Python-Kompilierung und
`git diff --check` sauber.

## Runde 661: Formatierungstabelle zeigte bei unbekannter Option alten Text

`_BoundFormatList.on_setting_changed()` übernahm gespeicherte Optionswerte
ohne Prüfung. GTK verwendet Renderer zwischen Zeilen wieder; ein unbekannter
Wert wie `mode=99` setzte daher kein neues Label und zeigte weiterhin das Label
der vorherigen Zeile, reproduzierbar `Immer`.

Der Formatierungslisten-Loader verwirft jetzt Zeilen mit Optionswerten, die im
Schema nicht definiert sind. Damit bleibt die Tabelle fail-closed und kann
keinen falschen Formatierungsmodus anzeigen. Regression prüft gültige Zeile
plus `mode=99`; `tests/test_format_table_selector.py`: 13/13. Python-
Kompilierung und `git diff --check` sauber.

## Runde 662: Formatierungs-Selector verwirft beschädigte Schema-Einträge

Der Selector iterierte `info["tables"]` ungeprüft. `None`, ein String oder
eine Liste mit `None` ließ den Konstruktor beim Öffnen der Formatierungsseite
mit `TypeError` beziehungsweise `AttributeError` abbrechen. Ebenso konnte ein
nicht-dictionary Tabellen-Definition später `_BoundFormatList` crashen lassen.

Der Konstruktor akzeptiert jetzt nur Listen, Mapping-Einträge, String-Schlüssel
und dictionary-Definitionen, die tatsächlich im Settings-Schema existieren.
Beschädigte Einträge werden übersprungen; die Seite bleibt leer oder zeigt
verfügbare Tabellen. Regression deckt `None`, String, gemischte Listen und
nicht-dictionary Definitionen ab. Zusätzlich bleibt der unbekannte Optionswert
`mode=99` fail-closed. `tests/test_format_table_selector.py`: 17/17;
Python-Kompilierung und `git diff --check` sauber.

## Runde 663: Tokendelta verwirft beschädigte Copy-Spalten

`Tokendelta` übernimmt die Spalten der Prozenttabelle. Bei einer beschädigten
Basisdefinition wie `columns: null` oder unbekannten Spaltentypen brach der
Aufbau vor GTK mit `AttributeError` beziehungsweise `KeyError` ab; damit blieb
das Einstellungsmenü unöffnbar.

Der Copy-Loader behandelt Basisdefinitionen und Spalten jetzt nur bei passendem
Mapping-/Listen-Schema. Ungültige Spalten werden übersprungen, die dynamische
Tokendelta-Spalte wird trotzdem ergänzt. Regression deckt `columns: null` und
gemischte beschädigte Spalten ab. `tests/test_format_table_selector.py`:
19/19; Python-Kompilierung und `git diff --check` sauber.

## Runde 664: Tabellenmetadaten crashen Settings-Seite nicht mehr

`_BoundFormatList` reichte beschädigte Metadaten ungeprüft an Cinnamon-Widgets
weiter. Nicht-string `description` oder `tooltip`, nicht-numerisches `height`
und `hidden-buttons: null` führten beim Öffnen zu `TypeError` oder einem
nicht-iterierbaren Wert; `show-buttons` konnte stillschweigend falsche UI
erzeugen.

Der Loader verwendet jetzt sichere Defaults: Label/Tooltip leer, Höhe 300,
Buttons sichtbar und Hidden-Buttons leer. Gültige Definitionen bleiben
unverändert. Regression deckt alle fünf Metadatenfelder ab;
`tests/test_format_table_selector.py`: 24/24; Python-Kompilierung und
`git diff --check` sauber.

## Runde 665: Hilfe materialisiert beschädigte Format-Copies konsistent

Die Hilfe-Seite behandelte Copy-Definitionen anders als der Format-Selector.
Bei `columns: null` oder einem String unterschlug sie geerbte Formatfelder;
bei Tokendelta fehlte dann auch die dynamische Schwelle. Die GUI blieb zwar
offen, dokumentierte aber nicht das tatsächlich bearbeitbare Format.

`_help_definition()` übernimmt Copy-Spalten jetzt unabhängig von einem
beschädigten lokalen `columns`-Wert und ergänzt bei Tokendelta immer `Dynamisch`.
Gültige Überschreibungen bleiben erhalten. Regression ergänzt Copy- und
Tokendelta-Fälle; `tests/test_help_page.py`: 10/10; Python-Kompilierung und
`git diff --check` sauber.

## Runde 666: Leisten-Editor verwirft kaputte Schema- und Account-Zeilen

`panel_columns()` rief bei `columns: null`, einem String oder `None`-Einträgen
`.get()` auf und ließ damit die Einstellungen abstürzen. Zusätzlich verwendete
der Positions-Fallback beim Speichern jede Dictionary-Zeile; eine Zeile mit
`account: 123` konnte dadurch Hidden-Slots einer gültigen Zeile überschreiben.

Der Panel-Loader akzeptiert nur vollständige Cinnamon-Spalten und normalisiert
Widget-Metadaten auf sichere Defaults. Rebuild und Edit-Fallback verwenden nur
Zeilen mit nicht-leerer String-Account-ID. Regression deckt vier kaputte
Schemas, fünf Metadatenfelder und den ungültigen Account-Fallback ab;
`tests/test_panel_settings_list.py`: 29/29; Python-Kompilierung und
`git diff --check` sauber.

## Runde 667: Leisten-Dialog toleriert verkürzte Edit-Daten

`open_add_edit_dialog()` indexierte `info` blind. Verkürzte Listen, Dictionaries
und Strings führten beim Doppelklick auf einen Eintrag zu `IndexError` oder
`KeyError`, statt fehlende Felder mit Schema-Defaults zu öffnen.

Der Dialog liest Positionswerte jetzt geschützt; nicht vorhandene oder falsch
geformte Werte gelten als leer und verwenden vorhandene Defaults. Regression
deckt leere, verkürzte und nicht-sequenzielle `info`-Werte ab;
`tests/test_panel_settings_list.py`: 33/33; Python-Kompilierung und
`git diff --check` sauber.

## Runde 668: Leisten-Dialog verwirft falsche Feldtypen

`open_add_edit_dialog()` reichte vorhandene Werte ungeprüft an Cinnamon-
Editorwidgets weiter. Ein String in einem Integer-Feld führte zu `TypeError`;
numerische Spalten ohne `min/max` konnten bereits beim Editoraufbau abstürzen.

Der Dialog verwirft nicht passende Werte und nutzt Defaults. Der Schemafilter
akzeptiert numerische Spalten ohne Optionsmap nur noch mit gültigem Zahlenbereich;
ungültige Felder werden übersprungen. Regression deckt falschen Integerwert und
fehlenden Zahlenbereich ab; `tests/test_panel_settings_list.py`: 35/35;
Python-Kompilierung und `git diff --check` sauber.

## Runde 669: Leisten-Slots bleiben innerhalb Count und eindeutig

`panel_columns()` übernahm ungültige IDs wie `slot0` und `slotfoo` sowie
doppelte `slot1`-Definitionen. Dadurch konnte die Oberfläche mehr Felder als
der gewählte Count zeigen; beim Speichern überschrieben gleiche IDs einander.

Slot-Spalten werden jetzt strikt auf `slot1` bis `slotN` begrenzt und jede
Spalten-ID erscheint höchstens einmal. Regression prüft ungültige und doppelte
Slots; `tests/test_panel_settings_list.py`: 36/36; Python-Kompilierung und
`git diff --check` sauber.

## Runde 670: Leisten-Editor verwirft unbrauchbare Zahlenbereiche

Numerische Schemafelder mit `min > max`, `NaN` oder unendlichen Grenzen wurden
an GTK weitergereicht. Das erzeugte Warnungen und unbrauchbare SpinButtons.

Der Panel-Loader akzeptiert nur endliche, geordnete Zahlenbereiche. Ungültige
Felder werden übersprungen; Slot-Erzeugung bleibt verfügbar. Regression deckt
umgekehrte und nicht-endliche Grenzen ab; `tests/test_panel_settings_list.py`:
38/38; Python-Kompilierung und `git diff --check` sauber.

## Runde 671: Mehrdeutige Account-Zeilen behalten ihre Hidden-Slots

Der Speicher-Fallback nutzte bei doppelten Account-IDs den letzten Eintrag im
`by_account`-Mapping. Beim Umbenennen einer der Duplikatzeilen konnten dadurch
Hidden-Slots des anderen Eintrags übernommen werden.

Mehrdeutige Accounts werden jetzt aus dem ID-Match entfernt; für sie gilt der
positionsgebundene Fallback. Eindeutige Accounts behalten weiterhin robustes
ID-Matching bei Sortieränderungen. Regression deckt zwei gleiche Account-IDs
und unterschiedliche Hidden-Slots ab; `tests/test_panel_settings_list.py`:
39/39; Python-Kompilierung und `git diff --check` sauber.

## Runde 672: Panel-JS verwirft kaputte Bestandszeilen sicher

`_updateAccountPanelSetting()` lief beim Menü-Update blind über
`accountPanelSettings`. `null`, Arrays oder andere beschädigte Rows führten bei
`row.account` zum Callback-Crash; nicht-objektartige `changes` waren ebenfalls
ungeprüft.

Die Funktion akzeptiert nur gültige Account-Strings und Objektänderungen und
filtert beschädigte Bestandszeilen vor Mapping und Persistenz. Regression deckt
`null`, Array sowie ungültige Änderungen ab; fokussierter Node-Test grün,
Syntaxcheck und `git diff --check` sauber.

## Runde 673: Panel-Settings-Mapper verwirft kaputte Eingaben

`_panelSettingsMap()` erwartete ungeprüft ein Array aus Objekt-Rows. `null`
führte zu einem Längen-Crash; Arrays, leere oder nicht-stringartige Accounts
konnten unbrauchbare Map-Schlüssel erzeugen.

Der Mapper akzeptiert nur nicht-leere String-Accounts in Objekt-Rows und gibt
bei Nicht-Arrays eine leere Map zurück. Prototype-sichere Account-IDs bleiben
erhalten. Regression erweitert den Mapping-Test um alle beschädigten Formen;
fokussierter Node-Test und Syntaxcheck sauber.

## Runde 677: Settings-Launcher fällt bei alter Gio-API zurück

Der Settings-Start verwendete `Gio.SubprocessLauncher.new()` ohne
Kompatibilitätspfad. Wenn Cinnamon diese API nicht bereitstellt oder deren
Konstruktor fehlschlägt, wurde `xlet-settings` gar nicht gestartet.

Der Launcher prüft API und Methoden jetzt defensiv, setzt `NO_AT_BRIDGE` nur
best-effort und verwendet sonst `Gio.Subprocess.new()`. Regression prüft den
Fallback inklusive PID-Weitergabe; fokussierter Settings-Block: 14/14,
JavaScript-Syntax und `git diff --check` sauber. Nach Install/Reload öffnete
der echte Cinnamon-Aufruf das Settings-Fenster in 265 ms.

## Runde 678: Settings-Cleanup nach Applet-Entfernung verifiziert

Der Settings-Maximierer besitzt eigene Timer-, Placement- und Lookup-Prozesse.
Ohne explizite Regression blieb möglich, dass beim Cinnamon-Reload nur der
Timer, nicht aber beide Kinder freigegeben wurden.

Der bestehende Removal-Test prüft jetzt zusätzlich `null` für beide Prozesse
und `0` für den Timer. Fokussierter Settings-Block: 14/14; Cleanup-Test grün,
Syntaxcheck und `git diff --check` sauber. Keine Produktcodeänderung nötig.

## Runde 679: Settings-Fenster akzeptiert keine PID 0

Der Settings-Launcher und die `wmctrl`-Zuordnung akzeptierten jede Ziffernfolge
als Prozess-ID. Ein fehlerhaftes `0` konnte dadurch eine fremde Desktop-Zeile
mit PID 0 auswählen und verschieben oder maximieren.

Launcher und Lookup akzeptieren jetzt nur positive Dezimal-PIDs. Regression
deckt PID 0 beim Start und bei der Fensterzuordnung ab; Settings-Fokusblock:
15/15, Syntaxcheck und `git diff --check` sauber.

## Runde 680: Leisten-Listener überlebt kaputten Settings-Read

`PanelSettingsList.on_setting_changed()` rief `get_value()` ungefangen auf.
Ein fehlender oder defekter JSON-Wert konnte damit aus dem Cinnamon-Listener
herauslaufen und den Leisten-Editor abbrechen.

Der Handler verwendet bei typischen Settings-Lese-Fehlern jetzt eine leere
Zeilenliste und bleibt bedienbar. Regression deckt den Fehler bereits beim
Widget-Aufbau ab; `tests/test_panel_settings_list.py`: 40/40, Python-Compile
und `git diff --check` sauber.

## Runde 681: Leisten-Schema verwirft nicht-iterierbare Optionen

Eine Spalte mit `options: 1` oder `options: null` passierte den Schemafilter.
`list_edit_factory()` versucht solche Werte beim Dialogaufbau zu iterieren und
ließ den Leisten-Editor abstürzen.

`panel_columns()` akzeptiert Optionswerte jetzt nur noch als Dict, Liste oder
Tuple. Regression ergänzt den kaputten Optionsfall;
`tests/test_panel_settings_list.py`: 41/41, Python-Compile und
`git diff --check` sauber.

## Runde 682: Leisten-Schema verwirft leere Spaltenidentitäten

Leere Spalten-IDs oder reine Leerzeichen als Titel passierten den Filter.
Leere IDs erzeugen beim Speichern kollidierende Dictionary-Schlüssel; leere
Titel machen den GTK-Editor unbrauchbar.

`panel_columns()` akzeptiert nur noch nicht-leere IDs und Titel. Regression
deckt beide Formen ab; `tests/test_panel_settings_list.py`: 43/43,
Python-Compile und `git diff --check` sauber.

## Runde 683: Leisten-Slot-Parsing überlebt überlange IDs

Eine manipulierte ID wie `slot` plus tausende Ziffern konnte in `panel_columns()`
`int()` mit Python-Digit-Limit-Fehler abbrechen lassen. Das passierte beim
Aufbau der Settings-Seite.

Slotnummern werden jetzt einmalig geschützt geparst; ungültige oder überlange
IDs fallen weg. Regression deckt 5000 Ziffern ab;
`tests/test_panel_settings_list.py`: 44/44, Python-Compile und
`git diff --check` sauber.

## Runde 684: Leisten-Höhe wird GTK-sicher normalisiert

Die Editor-Metadaten akzeptierten `NaN`, Unendlich, negative Höhen und
gebrochene Zahlen. Diese Werte konnten `Gtk.set_size_request()` beim Aufbau
der Einstellungen mit Typ- oder Wertefehler abbrechen lassen.

Höhe wird jetzt auf endliche, nicht-negative Integer bis 10000 begrenzt.
Regression deckt alle vier Formen ab; `tests/test_panel_settings_list.py`:
48/48, Python-Compile und `git diff --check` sauber.

## Runde 685: Leisten-Ausrichtung wird vor GTK validiert

`align` wurde ungeprüft an `Gtk.CellRenderer.set_alignment()` übergeben.
Strings warfen `TypeError`; `NaN`, Unendlich und Werte außerhalb von 0 bis 1
erzeugten GTK-Criticals. Der Fehler trat bereits im Basiskonstruktor auf.

Der Schemafilter entfernt ungültige Ausrichtungen und normalisiert gültige auf
Float; der Rebuild-Pfad prüft zusätzlich defensiv. Regression deckt sieben
kaputte Werte ab; `tests/test_panel_settings_list.py`: 55/55,
Python-Compile und `git diff --check` sauber.

## Runde 686: Leisten-Numerik-Reads fangen Overflow ab

`_read_count()` und `_read_edit_columns()` ließen einen `OverflowError` aus
dem Settings-Backend herauslaufen. Damit konnte die Leisten-Seite beim Aufbau
oder der Editordialog beim Öffnen abbrechen.

Beide Reads verwenden jetzt ihre sicheren Defaults auch bei Overflow.
Regression prüft beide Pfade; `tests/test_panel_settings_list.py`: 56/56,
Python-Compile und `git diff --check` sauber.

## Runde 687: Leisten-Slot-IDs bleiben kanonisch

`slot01` und `slot001` wurden neben `slot1` akzeptiert. Dadurch konnten
semantisch doppelte Wert-Spalten entstehen und beim Speichern unterschiedliche
Schlüssel für denselben Slot erzeugen.

Slot-IDs mit führenden Nullen werden jetzt verworfen. Der bestehende Schema-
Regressionstest deckt den Fall ab; `tests/test_panel_settings_list.py`: 56/56,
Python-Compile und `git diff --check` sauber.

## Runde 688: Leisten-Spalten verwerfen NUL-Metadaten

Spalten-ID und Titel konnten eingebettete NUL-Zeichen enthalten. Solche Werte
sind für GTK-Labels und JSON-Schlüssel nicht sicher und konnten Aufbau oder
Speicherung der Tabelle abbrechen lassen.

Der Schemafilter verwirft NUL-verunreinigte IDs und Titel. Regression deckt
beide Felder ab; `tests/test_panel_settings_list.py`: 58/58, Python-Compile und
`git diff --check` sauber.

## Runde 689: Leisten-ComboBoxen validieren Options-Typen

`list_edit_factory()` übergibt Optionswerte ungeprüft an `Gtk.ListStore`.
Falsche Dict-Werte oder numerische Listenwerte konnten den Editordialog mit
`TypeError` abbrechen lassen.

`panel_columns()` prüft jetzt Labels und Werte gegen den deklarierten GTK-Typ;
ungültige ComboBox-Spalten werden entfernt. Regression deckt vier falsche
Formen im echten Dialogpfad ab; `tests/test_panel_settings_list.py`: 62/62,
Python-Compile und `git diff --check` sauber.

## Runde 690: Leisten-Integer-Optionen bleiben im GTK-Bereich

`Gtk.ListStore(int)` akzeptiert nur signed-32-bit-Werte. ComboBox-Optionen
außerhalb dieses Bereichs wurden bislang erst beim Dialogaufbau erkannt und
warfen dort `OverflowError`.

Integer-Optionen werden jetzt auf `-2**31` bis `2**31-1` geprüft. Regression
deckt beide Grenzverletzungen im echten Dialogpfad ab;
`tests/test_panel_settings_list.py`: 64/64, Python-Compile und
`git diff --check` sauber.

## Runde 691: Leisten-Widget-Metadaten bleiben kompatibel

SpinButton-Metadaten mit ungültigem `step` (`0`, Text, `NaN`, Unendlich)
führten beim Dialogaufbau zu GTK-Fehlern. ComboBox-Spalten übernahmen zudem
`min/max/step/units/expand-width`, die ihr Widget nicht unterstützt.

Ungültige Schritte werden entfernt; nicht passende Spin-Eigenschaften werden
bei ComboBoxen gestrichen. Regression deckt den echten Dialogpfad ab;
`tests/test_panel_settings_list.py`: 70/70, Python-Compile und
`git diff --check` sauber.

## Runde 692: Leisten-Widget-Properties bleiben typgerecht

`min/max/step/units` wurden auch an String-, File-, Icon- und Sound-Widgets
weitergereicht. Deren Konstruktoren kennen diese Argumente nicht; der
Editordialog brach mit `TypeError` ab.

Der Schemafilter behält je Widgettyp nur unterstützte Properties und prüft
Bool-Properties strikt. Regression deckt vier Widgettypen im Dialogpfad ab;
`tests/test_panel_settings_list.py`: 74/74, Python-Compile und
`git diff --check` sauber.

## Runde 693: Leisten-Add/Edit bleibt bei ungültigen Widgetwerten stabil

Die geerbten `List.add_item()`- und `edit_item()`-Pfade reichten Dialogwerte
ungeprüft an `Gtk.ListStore` weiter. Ein kaputter Default konnte dadurch beim
Hinzufügen oder Doppelklick-Editieren `TypeError` oder `OverflowError` werfen.

Panel-Add/Edit fängt diese Modellfehler jetzt ab; Edit stellt die Originalzeile
wieder her. Regression deckt beide echten Bedienpfade ab;
`tests/test_panel_settings_list.py`: 76/76, Python-Compile und
`git diff --check` sauber.

## Runde 694: Leisten-Callbacks überleben Settings-Read-Fehler

`list_changed()` und `_on_count_changed()` lasen persistierte Leistenwerte
noch ungefangen. Ein Backend-Fehler beim Speichern oder beim Ändern der
Wertanzahl konnte deshalb aus einem GTK-Callback herausbrechen und das
Einstellungsfenster instabil machen.

Beide Pfade verwenden jetzt denselben leeren Listen-Fallback wie die übrigen
Panel-Reads. Regression deckt Speichern und Wertanzahl-Änderung mit defektem
Backend ab; `tests/test_panel_settings_list.py`: 77/77, Python-Compile und
`git diff --check` sauber.

## Runde 695: Leisten-Schreibfehler setzen Callback-Zustand zurück

`JSONSettingsBackend.set_value()` setzt vor dem Backend-Schreiben `_saving`
auf `True`, setzt es bei `OSError` oder ungültigen Daten aber nicht zurück.
Ein fehlgeschlagener Leisten-Write konnte dadurch spätere externe
Settings-Updates dauerhaft unterdrücken.

`list_changed()` fängt die erwartbaren Schreibfehler ab, setzt `_saving`
wieder auf `False` und beendet den Callback kontrolliert. Regression deckt
einen nicht verfügbaren Settings-Write ab; `tests/test_panel_settings_list.py`:
78/78, Python-Compile und `git diff --check` sauber.

## Runde 696: Leisten-Initialisierung überlebt Read-Overflow

`on_setting_changed()` behandelte `OverflowError` beim Laden der gespeicherten
Leistenzeilen noch nicht. Ein überlaufender Backend-Read konnte damit direkt
beim Öffnen des Einstellungsmenüs den Widgetaufbau abbrechen.

Der Read nutzt jetzt denselben leeren Listen-Fallback wie die übrigen
Leistenpfade. Regression deckt den Overflow bereits während der Widget-
Initialisierung ab; `tests/test_panel_settings_list.py`: 79/79,
Python-Compile und `git diff --check` sauber.

## Runde 697: Leisten-Listener-Registrierung blockiert Menü nicht

`PanelSettingsList.__init__()` ließ Fehler aus `settings.listen()` ungefangen.
Ein Backend-Problem beim Registrieren der Änderungslistener konnte damit den
gesamten Einstellungsseitenaufbau abbrechen, obwohl Tabellen-Schema und
gespeicherte Werte verwendbar waren.

Die Listener-Registrierung ist jetzt fehlertolerant; das Panel bleibt ohne
Live-Updates bedienbar. Regression deckt einen fehlgeschlagenen Listener-Write
ab; `tests/test_panel_settings_list.py`: 80/80, Python-Compile und
`git diff --check` sauber.

## Runde 698: Leisten-Read-Fehler überschreiben keine Hidden-Slots

Der leere Read-Fallback in `list_changed()` hätte bei einem temporär
unlesbaren Backend alle nicht sichtbaren Slotwerte verworfen, sobald ein
sichtbarer Wert geändert wurde.

Der Schreib-Callback beendet sich bei Read-Fehlern jetzt vor dem Persistieren.
Der Count-Rebuild nutzt seinen Modell-Fallback weiterhin nur für die Anzeige.
Regression bestätigt, dass ein fehlgeschlagener Read gespeicherte Zeilen
unverändert lässt; `tests/test_panel_settings_list.py`: 80/80,
Python-Compile und `git diff --check` sauber.

## Runde 699: Leisten-Slots erzwingen Integer-Optionen

Ein vorhandenes `slot1` mit falschem Typ (z. B. `string`) bekam später die
Integer-Quellenliste zugewiesen. GTK-Modell und Edit-ComboBox hatten dadurch
unterschiedliche Werttypen; Einträge konnten verschwinden oder der Dialog
fehlschlagen.

Alle vorhandenen und neu erzeugten Slotspalten werden bei der Normalisierung
jetzt als Integer mit deaktiviertem Standardwert angelegt. Regression prüft
einen falsch typisierten Legacy-Slot; `tests/test_panel_settings_list.py`:
81/81, Python-Compile und `git diff --check` sauber.

## Runde 700: Leisten-Stringoptionen verwerfen NUL-Werte

ComboBox-Labels wurden bereits auf eingebettete NUL-Zeichen geprüft, die
zugehörigen Stringwerte aber nicht. Ein solcher Wert konnte beim Erzeugen des
GTK-Optionenmodells den Dialogaufbau abbrechen.

Stringoptionen akzeptieren jetzt nur noch NUL-freie Werte. Regression deckt
den echten Schemafilterpfad ab; `tests/test_panel_settings_list.py`: 82/82,
Python-Compile und `git diff --check` sauber.

## Runde 701: Leisten-SpinButton-Einheiten bleiben GTK-sicher

`units` wurde für Integer-/Float-Editoren ohne Textvalidierung an den
SpinButton-Konstruktor weitergereicht. NUL-haltige Einheiten konnten dadurch
beim Öffnen des Bearbeitungsdialogs einen GTK-Fehler auslösen.

Ungültige `units`-Metadaten werden jetzt entfernt; gültige Einheiten bleiben
unverändert. Regression prüft den Schemafilter; `tests/test_panel_settings_list.py`:
83/83, Python-Compile und `git diff --check` sauber.

## Runde 702: Cinnamon-Settings startet Panel-Seite ohne Importfehler

Der reale Cinnamon-Settings-Prozess wurde mit
`xlet-settings.py applet codex-usage@H234598 -i 0` acht Sekunden laufen
gelassen. Prozess blieb ohne Importtraceback oder GTK-Fehler aktiv; der
Timeout beendete nur die absichtlich nicht-interaktive GUI-Prüfung.

Zusätzlich: `cinnamon-settings applets` startete erfolgreich. Working tree
blieb nach Installation und Reload sauber.

## Runde 703: Panel mit echter Settingsdatei lädt alle Nutzerzeilen

Die installierte `PanelSettingsList` wurde mit der realen Datei
`~/.config/cinnamon/spices/codex-usage@H234598/codex-usage@H234598.json`
und dem aktuellen Schema instanziiert. Ergebnis: 23 Spalten (Account,
Metadaten und 20 Wertfelder), 8 gespeicherte Zeilen, ein korrekter Listener;
kein GTK-/Importfehler. Widget und File-Monitor wurden anschließend sauber
geschlossen.

## Runde 704: Panel-Aufbau-Fuzzcheck ohne ungefangene GTK-Fehler

330 gezielt variierte Schema-/Settings-Kombinationen (kaputte Typen,
Grenzwerte, Optionen, Zeilen und Metadaten) wurden in-process instanziiert
und wieder zerstört. Kein Aufbaufehler reproduziert; Änderungen waren für
diese Runde nicht nötig.

## Runde 705: Backend-Exceptions setzen Leisten-Saving-Zustand zurück

Ein Settings-Write kann neben `OSError` auch eine normale Exception aus
DBus-/Notify- oder Backend-Code weiterreichen. Der Leisten-Callback fing nur
einzelne Built-in-Fehler; `_saving` konnte dadurch dauerhaft `True` bleiben.

Der Write-Grenzpunkt fängt jetzt `Exception` (nicht `BaseException`) ab,
setzt `_saving` zurück und beendet den GTK-Callback kontrolliert. Regression
deckt `OSError` und `RuntimeError` ab; `tests/test_panel_settings_list.py`:
84/84, Python-Compile, Ruff und `git diff --check` sauber.

## Runde 706: Listener-Exceptions blockieren Panel-Aufbau nicht

Auch `settings.listen()` liegt an einer externen Backend-Grenze und kann
normale Runtime-/DBus-Exceptions liefern. Die bisherige Built-in-Typ-Liste
ließ solche Fehler beim `PanelSettingsList`-Aufbau durch.

Beide Listener-Registrierungen behandeln jetzt `Exception` fehlertolerant;
das Panel bleibt ohne Live-Updates bedienbar. Regression deckt
`OSError` und `RuntimeError` ab; `tests/test_panel_settings_list.py`: 85/85,
Python-Compile, Ruff und `git diff --check` sauber.

## Runde 707: Listener-Cleanup überlebt kaputten Container

Beim fehlgeschlagenen `attach()` und beim Schließen greift
`_remove_listener()` auf den externen `settings.listeners`-Container zu.
Eine Exception dort konnte den gerade fehlertolerant gemachten Aufbau oder
Destroy-Pfad wieder abbrechen.

Cleanup behandelt Containerfehler jetzt kontrolliert als bereits nicht
verfügbaren Listener-Zustand. Regression deckt einen fehlerwerfenden
Containerzugriff ab; `tests/test_panel_settings_list.py`: 86/86,
Python-Compile, Ruff und `git diff --check` sauber.

## Runde 708: Leisten-Dialoge werden auch bei GTK-Fehlern zerstört

`open_add_edit_dialog()` rief `dialog.destroy()` nur auf normalen Return-
Pfaden auf. Ein Fehler im Dialoglauf oder beim Widgetaufbau konnte dadurch
ein Fensterobjekt und zugehörige GTK-Ressourcen liegen lassen.

Dialogzerstörung liegt jetzt in einem `finally`-Block; auch ein Fehler beim
Zerstören selbst wird nicht weitergereicht. Regression erzwingt einen Fehler
aus `run()` und prüft die Zerstörung; `tests/test_panel_settings_list.py`:
87/87, Python-Compile, Ruff und `git diff --check` sauber.

## Runde 709: Leisten-Zeilenaktionen behandeln fehlende Auswahl

Die geerbten Remove-/Move-Callbacks riefen GTK mit `None` auf, wenn keine
Zeile selektiert war. Das passierte reproduzierbar trotz deaktivierter Buttons
bei stale Selection-Zustand und endete in `TypeError`/`AttributeError`.

Panel überschreibt die drei Aktionen jetzt mit Auswahl-/Grenzprüfungen und
aktualisiert Button-Sensitivität nach jedem Abbruch. Regression deckt leere
Auswahl sowie Move/Remove einer echten Auswahl ab; `tests/test_panel_settings_list.py`:
89/89, Python-Compile, Ruff und `git diff --check` sauber.

## Runde 710: Formatierungs-Selector überlebt Settings-Reads

`FormatTableSelector.on_setting_changed()` und die eingebettete
`_BoundFormatList` lasen Backendwerte ungefangen. Ein Read-/DBus-Fehler beim
Öffnen der Formatierungsseite konnte dadurch den gesamten Settings-Aufbau
abbrechen.

Selector fällt jetzt auf die erste Tabelle, Listen auf leere Zeilen zurück;
kein Write erfolgt. Regression deckt Selector- und Tabellen-Readfehler ab;
`tests/test_format_table_selector.py`: 26/26, Python-Compile, Ruff und
`git diff --check` sauber.

## Runde 711: Formatierungslisten begrenzen GTK-Höhe

`_BoundFormatList` übergab `NaN`, Unendlich, negative oder übergroße
`height`-Metadaten direkt an `Gtk.ScrolledWindow`; das reproduzierte
`TypeError`/`OverflowError` bereits beim Öffnen der Formatierungsseite.

Höhe wird jetzt endlich, nichtnegativ und auf 10000 Pixel begrenzt;
ungültige Werte nutzen 300 Pixel. Regression deckt alle Grenzformen ab;
`tests/test_format_table_selector.py`: 31/31, Python-Compile, Ruff und
`git diff --check` sauber.

## Runde 712: Formatierungs-Listener blockieren Fallback nicht

`FormatTableSelector` und `_BoundFormatList` ließen Fehler aus
`settings.listen()` ungefangen. Bei einem Backend-/DBus-Fehler konnte die
Formatierungsseite deshalb abbrechen, statt wenigstens die erste Tabelle zu
zeigen; Cleanup war ebenfalls nicht fehlertolerant.

Attach-/Detach-Pfade behandeln solche Exceptions jetzt kontrolliert und
rendern die Fallback-Tabelle ohne Live-Listener. Regression deckt die
Listener-Registrierung am Selector und an der Tabelle ab;
`tests/test_format_table_selector.py`: 32/32, Python-Compile, Ruff und
`git diff --check` sauber.

## Runde 713: Formatierungs-Writes setzen Saving-Zustand zurück

Dropdown-Wechsel und geerbtes Listen-Speichern reichten Backend-Exceptions
ungefangen weiter. Ein fehlgeschlagener Write konnte dadurch den GTK-Callback
brechen und `_saving` dauerhaft auf `True` lassen.

Selector und gebundene Formatierungsliste fangen normale Backend-Exceptions
jetzt ab, setzen `_saving` zurück und aktualisieren die UI kontrolliert.
Regression deckt Dropdown- und Tabellen-Write ab;
`tests/test_format_table_selector.py`: 34/34, Python-Compile, Ruff und
`git diff --check` sauber.

## Runde 714: Formatierungs-Spalten validieren GTK-Ausrichtung

`_BoundFormatList` reichte `align`-Metadaten ungeprüft an
`Gtk.CellRenderer.set_alignment()` weiter. Text/`None` brachen den
Seitenaufbau mit `TypeError`; ungültige Zahlen erzeugten GTK-Criticals.
Schemaobjekte wurden außerdem direkt mutiert.

Spaltenschema wird jetzt kopiert, NUL-Identitäten werden verworfen und
`align` auf finite Werte zwischen 0 und 1 normalisiert. Regression deckt alle
Grenzformen ab; `tests/test_format_table_selector.py`: 42/42,
Python-Compile, Ruff und `git diff --check` sauber.

## Runde 715: Formatierungs-Optionen bleiben im GTK-Wertebereich

Formatierungs-ComboBoxen übernahmen Optionswerte ungeprüft. Ein Integerwert
größer als signed 32-bit reproduzierte beim Doppelklick einen
`OverflowError`; NUL-Werte und falsche Optionsformen waren ebenfalls nicht
GTK-sicher.

Ungültige Optionen werden vor dem Widgetaufbau verworfen. Regression deckt
Overflow, NUL-Label/-Wert, numerische Listen und fehlende Optionen ab;
`tests/test_format_table_selector.py`: 47/47, Python-Compile, Ruff und
`git diff --check` sauber.

## Runde 716: Formatierungs-SpinButtons benötigen gültige Bereiche

Numerische Formatspalten ohne vollständige `min/max`-Grenzen oder mit
umgekehrten/nichtendlichen Grenzen brachen beim Doppelklick im
`SpinButton`-Konstruktor mit `TypeError` ab.

Der Spaltenfilter verwirft unvollständige Bereiche und bereinigt ungültige
Schritte; leere Spaltenschemata erzeugen keine Phantomzeilen mehr. Regression
deckt fünf Bereichsformen ab; `tests/test_format_table_selector.py`: 52/52,
Python-Compile, Ruff und `git diff --check` sauber.

## Runde 717: Formatierungs-Defaults bleiben widgettypgerecht

Der echte Dialogpfad reichte ungültige `default`-Werte ungefangen an
`Gtk.Entry`-/SpinButton-/File-Widgets weiter. Falsche Typen reproduzierten
`TypeError`; sehr große oder nichtendliche Zahlen konnten ebenfalls brechen.

Defaults werden jetzt gegen Widgettyp, Optionswerte, signed-32-bit und
Numerikbereich geprüft; ungültige Defaults entfallen. Regression deckt fünf
Widgettypen ab; `tests/test_format_table_selector.py`: 57/57,
Python-Compile, Ruff und `git diff --check` sauber.

## Runde 718: Format-Listenaktionen behandeln fehlende Auswahl

Gebundene Formatierungslisten erbten Remove-/Move-Callbacks, die bei leerer
Selection `Gtk.TreeModel` mit `None` aufriefen. Das reproduzierte
`TypeError`/`AttributeError` trotz deaktivierter Buttons.

Die drei Aktionen prüfen Auswahl und Randpositionen jetzt vor GTK-Aufrufen;
Button-Sensitivität wird bei Abbruch aktualisiert. Regression deckt leere
Auswahl ab; `tests/test_format_table_selector.py`: 58/58, Python-Compile,
Ruff und `git diff --check` sauber.

## Runde 719: Reale Cinnamon-Settings bleiben nach Format-Härtung aktiv

`xlet-settings.py applet codex-usage@H234598 -i 0` lief nach den
Formatierungsänderungen acht Sekunden ohne Importtraceback oder GTK-Fehler.
Der erwartete Timeout beendete nur die nicht-interaktive GUI-Prüfung.

## Runde 720: Reale Formatierungstabellen und Dialogtypen geprüft

180 variierte Dialog-Schemata liefen nach der Sanitizing-Runde ohne Fehler für
alle im Projekt verwendeten Widgettypen. Der künstliche `keybinding`-Typ bleibt
Cinnamon-intern backendabhängig und wird im Format-Schema nicht verwendet.

Zusätzlich wurden alle 21 realen Formatierungstabellen mit der Nutzer-
Settingsdatei nacheinander aufgebaut und verworfen: keine Exception, kein
GTK-Fehler.

## Runde 721: Prognosen-Selector bekommt dieselben Backend-Grenzen

Der Prognosen-Selector behandelte `settings.listen()`, Tabellen-Reads und
Dropdown-Writes noch ungefangen. Backend-/DBus-Fehler konnten die
Prognosenseite beim Öffnen oder Wechseln abbrechen.

Info-/Definitionstypen, Attach-/Detach-, Read- und Write-Pfade sind jetzt
fehlertolerant; die erste Tabelle bleibt als Fallback sichtbar. Regression
deckt Listener-, Read- und Write-Fehler ab; `tests/test_forecast_table_selector.py`:
10/10, Python-Compile, Ruff und `git diff --check` sauber.

## Runde 722: Reale Prognosentabellen laden fehlerfrei

`ForecastTableSelector` wurde mit der echten Nutzer-Settingsdatei instanziiert;
alle drei Prognosentabellen wurden nacheinander aufgebaut und wieder entfernt.
Ergebnis: 3 Tabellen, 0 Exceptions, 0 GTK-Fehler.

## Runde 724: Fast-Mode-Icon-Widget überlebt Backend-Fehler

`FastModeIconSelector` behandelte Listener-Registrierung, Settings-Read und
Auswahl-Write ungefangen. Backend-/DBus-Fehler konnten dadurch das
Einstellungsmenü oder den ComboBox-Callback abbrechen.

Attach-/Detach-, Read- und Write-Pfade sind jetzt fehlertolerant; der
aktuelle/erste Iconwert bleibt sichtbar. Regression deckt Read-/Write- und
Listenerfehler ab; `tests/test_fast_mode_icon_selector.py`: 8/8,
Python-Compile, Ruff und `git diff --check` sauber.

## Runde 725: Fast-Mode-Icons bleiben auf Applet-SVGs begrenzt

Optionen wurden als Pfad direkt unter `icons/` zusammengesetzt. NUL-Werte
konnten `Path.is_file()` mit `ValueError` brechen; `../`-Pfade konnten auf
Dateien außerhalb des Icon-Verzeichnisses zeigen.

Nur NUL-freie SVG-Basenames werden jetzt akzeptiert; Loader-`ValueError` wird
abgefangen. Regression deckt Traversal, NUL und gültiges Icon ab;
`tests/test_fast_mode_icon_selector.py`: 9/9, Python-Compile, Ruff und
`git diff --check` sauber.

## Runde 726: Fast-Mode-Widget mit echter Settingsdatei geprüft

Reale Nutzer-Settingsdatei und Schema wurden direkt geladen: 9 Icons,
gültige aktuelle Auswahl und genau ein Settings-Listener. Widget und
File-Monitor wurden anschließend sauber zerstört/pausiert; keine GTK- oder
Importfehler.

## Runde 723: Selector-Mappings überleben fehlendes Backend-Attribut

Forecast- und Format-Selector dereferenzierten `settings.settings` direkt.
Ein unvollständiges Backend konnte dadurch vor jedem Listener-/Read-Fallback
mit `AttributeError` abbrechen.

Beide Konstruktoren lesen das Mapping jetzt über `getattr(..., {})` und
bleiben mit leerem Selector nutzbar. Regression deckt beide fehlenden
Mappings ab; kombinierter Fokustest `tests/test_format_table_selector.py`
und `tests/test_forecast_table_selector.py`: 70/70, Python-Compile, Ruff und
`git diff --check` sauber.

## Runde 727: Einstellungslauncher nach Reload erreichbar

Der gemeldete Öffnungsfehler ließ sich zuerst direkt mit
`xlet-settings applet codex-usage@H234598 -i 0` und anschließend über den
laufenden Cinnamon-Applet-Callback reproduzierbar prüfen. Beide Pfade laden
die acht Seiten, inklusive Hilfe-, Formatierungs- und Prognosen-Widgets, ohne
Import- oder GTK-Fehler; das Fenster erscheint innerhalb einer Sekunde und
wird auf den Applet-Monitor maximiert. Alte Sitzungslogs enthielten nur
AT-SPI-Timeouts früherer Settings-Prozesse. Das installierte Applet wurde mit
`scripts/install_cinnamon_applet.py --reload-running` synchronisiert; danach
öffnet `configureApplet()` erneut ein sichtbares, maximiertes Fenster.

Regression: `pytest -q tests/test_help_page.py` 10/10 und der fokussierte
Settings-Launcher-Block in `node --test tests/applet_runtime.test.js` 10/10.
Kein Produktcode-Fix erforderlich; die Ursache war im aktuellen Stand nicht
mehr reproduzierbar. Von diesem Lauf gestartete Testprozesse wurden beendet.

## Runde 728: Hilfe-Materialisierung mutiert Schema nicht

`_help_definition()` ergänzte beim Materialisieren des dynamischen
Tokendelta-Felds `dynamic` direkt die originale `columns`-Liste, wenn das
referenzierte Basisschema fehlte oder beschädigt war. Ein erneutes Öffnen der
Hilfe konnte dadurch das geladene Schema schrittweise verändern.

Die dynamische Spaltenliste wird vor dem Ergänzen flach kopiert. Regression
prüft, dass die Eingabe unverändert bleibt und nur die zurückgegebene
Materialisierung `dynamic` enthält. `pytest -q tests/test_help_page.py`:
11/11; Python-Compile, Ruff und `git diff --check` sauber. Applet installiert
und mit `--reload-running` neu geladen.

## Runde 729: Hilfe-Schema-Fuzz ohne weitere Abbrüche

Der fokussierte Help-Builder wurde mit 5000 kleinen, gezielt malformed
Schema-/Spalten-/Option-Kombinationen sowie nicht-stringartigen Layoutwerten
ausgeführt. `_clean_text()`, `_option_text()`, `_field_text()`,
`_definition_entry()`, `_help_definition()`, `build_help_groups()` und
`_markup()` lieferten jeweils kontrollierte Ergebnisse; keine Exception und
kein GTK-Aufbaufehler wurde reproduziert. Es war keine weitere Änderung nötig.

## Runde 730: Dynamic-Series-Backend-Fehler blockieren Settings nicht

`DynamicSeriesList` ließ einen Fehler aus `settings.get_value()` oder
`settings.listen()` bis in den Cinnamon-Konstruktor steigen. Ein temporärer
Backend-/DBus-Fehler konnte dadurch die gesamte Accounts-Seite abbrechen.

Read-Fehler liefern jetzt eine leere Anzeige ohne Persistenzänderung.
Listener-Fehler werden abgemeldet, danach wird der lokale Tabellenstand noch
einmal ohne Live-Listener geladen; auch ein fehlerhafter Fallback räumt das
GTK-Modell kontrolliert. Regression: `pytest -q
tests/test_dynamic_series_list.py` 19/19; Python-Compile, Ruff und
`git diff --check` sauber. Reale `xlet-settings`-Smoke nach Installation und
Reload lief acht Sekunden ohne Traceback/GTK-Fehler.

## Runde 731: Numerische Optionsspalten ignorieren irrelevante Grenzen

`_BoundFormatList` übersprang bei numerischen ComboBox-Spalten zwar die
Bereichsvalidierung, verglich den gültigen `default` danach aber trotzdem mit
`min/max`. Malformed Metadaten wie `min="bad"` lösten beim Aufbau der
Formatierungs- oder Prognosenseite einen `TypeError` aus.

Die Bereichsprüfung läuft für Optionsspalten nicht mehr; dort definiert die
Optionsmenge bereits alle zulässigen Werte. Regression deckt integer-
Optionsspalte mit ungültigen Grenzen ab. Kombinierter Fokustest
`tests/test_format_table_selector.py tests/test_forecast_table_selector.py`:
71/71; Python-Compile, Ruff und `git diff --check` sauber. Applet installiert
und reloaded.

## Runde 732: Einstellungslauncher erneut live geprüft

Der gemeldete Fehler „Einstellungsmenü lässt sich nicht öffnen“ wurde über
beide realen Cinnamon-Wege geprüft: `xlet-settings applet
codex-usage@H234598 -i 14` baut acht Seiten inklusive aller Custom-Widgets auf;
der native `configureApplet`-Callback und die tatsächliche Popup-Aktion
„Einstellungen“ starten jeweils ein sichtbares `Codex Usage`-Fenster. Der
Fensterprozess erscheint in `wmctrl -lp`; der laufende Launcher positioniert
und maximiert das Fenster auf dem Applet-Monitor. Keine Python-/GTK-/GJS-
Exception reproduziert. Von diesem Lauf gestartete Testfenster wurden beendet;
ein vorher vorhandenes Nutzerfenster blieb unangetastet.

Regressionen: fokussierter Settings-Launcher-/Configure-Block in
`tests/applet_runtime.test.js` 6/6; Format-/Forecast-/Hilfe-/Dynamic-Series-
Tests 101/101; Python-Compile und `git diff --check` sauber. Es war kein
weiterer Produktcode-Fix begründet. Installiertes Applet bleibt nach Reload
auf aktuellem Stand.

## Runde 733: Einstellungsfenster nach Start aktiv fokussieren

Der Launcher erzeugte das Einstellungsfenster zwar korrekt, garantierte aber
keine Aktivierung. Nach dem Aufbau/Maximieren konnte der Fokus wieder auf ein
anderes Fenster springen; dadurch wirkte „Einstellungen“ wie ein No-op,
obwohl `xlet-settings` bereits lief.

Nach dem gezielten `wmctrl`-Maximieren aktiviert `_scheduleSettingsMaximize()`
das erkannte Fenster jetzt einmal per `wmctrl -i -a <window-id>` (ohne PID-
Treffer per Titel-Fallback). Aktivierungsfehler bleiben best effort und
blockieren den Launcher nicht. Regression ergänzt die echte Ziel-ID und deckt
Retry-, Monitor-, Late-Window- und Timeout-Pfade ab: Settings-Launcher-Block
15/15; Node-Syntaxcheck und `git diff --check` sauber. Nach Installation und
Reload öffnet die echte Popup-Aktion ein maximiertes, fokussiertes Fenster;
Testfenster wurden beendet, das vorhandene Nutzerfenster blieb erhalten.

## Runde 734: Settings-Launcher fällt bei `spawnv()`-Fehler zurück

`_openSettings()` fiel bisher nur dann auf `Gio.Subprocess.new()` zurück, wenn
`Gio.SubprocessLauncher.new()` nicht verfügbar war. War die Launcher-API
vorhanden, aber `launcher.spawnv(argv)` schlug beim Start fehl, wurde sofort
„Einstellungen konnten nicht geöffnet werden“ gemeldet. Damit war ein
plattform-/runtimeabhängiger Launcher-Fehler unnötig fatal.

Der Spawn wird jetzt separat abgefangen; bei Fehlschlag läuft derselbe
`Gio.Subprocess.new()`-Fallback wie bei fehlender Launcher-API. Nur wenn beide
Pfade keinen Prozess liefern, bleibt die Fehlermeldung bestehen. Regression
prüft Spawnfehler, Prozessargumente, PID-Weitergabe und dass kein falscher
Fehlerdialog erscheint. Settings-Launcher-Block: 16/16; Node-Syntaxcheck und
`git diff --check` sauber.

## Runde 735: Launcher-Spawn-Fallback erneut verifiziert

Der bereits implementierte Fallback wurde gegen das nächste Fehlerfenster
geprüft: Eine vorhandene `Gio.SubprocessLauncher`-API kann trotz erfolgreicher
Konstruktion bei `spawnv()` scheitern. `_openSettings()` verwendet dann
`Gio.Subprocess.new()`; nur wenn auch dieser Pfad keinen Prozess liefert,
erscheint der Fehler.

Regression deckt Launcher-Spawnfehler, unveränderte Argumente, PID-Weitergabe
und Fehlerdialog-Unterdrückung ab. Settings-Launcher-Block: 16/16;
Node-Syntaxcheck und `git diff --check` sauber. Live-Popup-Smoke nach Reload
bleibt fokussiert und maximiert. In dieser Runde war kein weiterer
Produktcode-Fix erforderlich.

## Runde 736: Format-Selector überlebt Widget-Aufbaufehler

`FormatTableSelector._ensure_table()` ließ Fehler aus `_BoundFormatList`,
`Gtk.Stack.add_named()` oder `show_all()` bis in den ComboBox-Callback steigen.
Ein beschädigtes Schema oder ein temporärer GTK-Fehler beim Umschalten konnte
damit die gesamte Formatierungsseite abbrechen; bei teilweise angehängten
Widgets blieben Listener und GTK-Kinder zurück.

Der Aufbau läuft jetzt in einem Guard. Fehlgeschlagene Widgets werden, soweit
vorhanden, detached, aus dem Stack entfernt und zerstört; `_ensure_table()`
liefert dann `None`, sodass die vorhandene aktive Tabelle erhalten bleibt.
Regression deckt den Konstruktorfehler ab. Format-/Forecast-Fokustests:
72/72; Python-Compile und `git diff --check` sauber. Reale `xlet-settings`-
Smoke nach Installation und Reload lief ohne Traceback.

## Runde 737: Optionsspalten reichen keine SpinButton-Parameter weiter

`_BoundFormatList` akzeptierte bei Optionsspalten weiterhin `min`, `max`,
`step` und `units`. Beim eigentlichen Doppelklick-Editor übergibt
`TreeListWidgets.list_edit_factory()` diese Felder jedoch an `ComboBox`;
`ComboBox.__init__()` kennt sie nicht und bricht mit `TypeError` ab. Der
Fehler war beim Tabellenaufbau unsichtbar und trat erst beim Bearbeiten auf.

Für jede Spalte mit `options` werden diese Renderer-fremden Eigenschaften nun
entfernt. Regression baut die echte `list_edit_factory`-ComboBox und prüft die
Bereinigung. Format-/Forecast-Fokustests: 73/73; Python-Compile und
`git diff --check` sauber. Reale `xlet-settings`-Smoke nach Reload lief ohne
Traceback.

## Runde 740: Leere/NUL-Selector-Keys verworfen

`FormatTableSelector` akzeptierte bislang leere oder NUL-haltige Tabellenkeys.
GTK kürzte etwa `bad\0key` beim ComboBox-Active-ID auf `bad`, während der
interne Stack-Key vollständig blieb; Auswahl, sichtbare Tabelle und
Persistenz konnten dadurch auseinanderlaufen.

Leere Keys und Keys mit NUL werden beim Schemaeinlesen jetzt verworfen.
Regression deckt beide Varianten ab. Format-/Forecast-Fokustests: 78/78;
Python-Compile und `git diff --check` sauber. Reale `xlet-settings`-Smoke nach
Reload lief ohne Traceback.

## Runde 741: NUL-Labels auf sicheren Tabellenkey zurückfallen lassen

Markup-Escaping allein reicht für NUL im Tabellenlabel nicht: GTK beendet den
Text am NUL trotzdem vorzeitig (`A\0B` wurde als `A` angezeigt). Dadurch konnte
die sichtbare Bezeichnung von der Schemaangabe abweichen.

Labels mit NUL werden beim Einlesen jetzt auf den bereits validierten
Tabellenkey zurückgesetzt. Regression deckt diesen Fallback ab. Format-/
Forecast-Fokustests: 79/79; Python-Compile und `git diff --check` sauber.
Reale `xlet-settings`-Smoke nach Reload lief ohne Traceback.

## Runde 742: Sequenz-Optionen in der Leiste konsistent validiert

`panel_columns()` akzeptiert bei konfigurierbaren Leistenfeldern neben
Dictionaries auch Listen und Tupel als Optionsmenge. `PanelSettingsList` prüfte
beim initialen Laden und beim Neuaufbau nach einer Wertfeldänderung bisher nur
`dict.values()`. Ein gespeicherter Sequenzwert außerhalb der erlaubten Menge
wurde deshalb als gültige Zeile angezeigt.

Beide Ladepfade leiten die erlaubten Werte jetzt für Dictionary-, Listen- und
Tupel-Optionen ab und verwerfen unbekannte Werte vor dem Einfügen ins
`Gtk.ListStore`. Regression prüft jeweils Liste und Tupel sowie Initial- und
Rebuild-Pfad.

Verifikation: `tests/test_panel_settings_list.py` 91/91; danach separat
`tests/test_format_table_selector.py tests/test_forecast_table_selector.py`
79/79; Node-Syntax, Ruff und `git diff --check` sauber. Ein kombinierter
Panel-erst-Run löst im PyGObject-Testprozess einen bekannten GTK-Segfault bei
`Gtk.main_iteration` aus; umgekehrte Reihenfolge und getrennte Fokustests sind
stabil. Kein Produktlauf von `xlet-settings` reproduziert diesen Test-Harness-
Fehler.

## Runde 738: Sequenz-Optionswerte beim Laden validiert

`_BoundFormatList` unterstützte für `options` neben Dictionaries auch Listen
und Tupel, prüfte gespeicherte Zeilenwerte aber ausschließlich gegen
`dict.values()`. Ein persistierter Wert außerhalb einer Sequenz wurde dadurch
als gültige Tabellenzeile angezeigt und konnte später in einen ungültigen
Editorzustand gelangen.

Die Zeilenvalidierung bildet jetzt für Dict, Liste und Tupel dieselbe erlaubte
Wertmenge. Regression nutzt echte String-Optionen als Liste und Tupel und
verwirft jeweils unbekannte Werte. Format-/Forecast-Fokustests: 75/75;
Python-Compile und `git diff --check` sauber. Reale `xlet-settings`-Smoke nach
Reload lief ohne Traceback.

## Runde 739: Tabellenlabels werden vor Pango-Markup escaped

`FormatTableSelector._show_table()` setzte Tabellenlabels ungefiltert in
`Gtk.Label.set_markup()`. Labels mit `&` oder `<` erzeugten GTK-Warnungen und
blieben leer, statt ihren Text anzuzeigen. Das war bei statischen deutschen
Labels unsichtbar, blieb aber ein fehlerhafter Metadatenpfad.

Die Labels werden jetzt mit `GLib.markup_escape_text()` escaped und erst dann
fett markiert. Regression prüft `&`, `<` und `>` über den echten Selector-
Pfad. Format-/Forecast-Fokustests: 76/76; Python-Compile und
`git diff --check` sauber. Reale `xlet-settings`-Smoke nach Reload lief ohne
Traceback.

## Runde 743: Panel-Editor überlebt Widget-Factory-Fehler

`PanelSettingsList.open_add_edit_dialog()` ließ Ausnahmen aus
`list_edit_factory()` bis in den GTK-Callback steigen. Ein einzelnes
inkompatibles oder beschädigtes Feld konnte dadurch den kompletten
Leisten-Editor abbrechen, obwohl die Tabelle selbst noch angezeigt werden
konnte.

Der Factory-Aufbau ist jetzt pro Feld geschützt. Ausnahme oder ein leeres
Widget beendet nur den Dialog mit `None`; der bestehende `finally`-Pfad räumt
das Dialogfenster auf. Regression deckt den Factory-Fehler ab.

Verifikation: Panel-Tests 92/92; Format-/Forecast-Tests 79/79; relevante
Settings-Launcher-Tests 39/39; Python-Compile, Ruff und `git diff --check`
sauber.

## Runde 744: Masterjet-Seriencache an Befehl binden

`DynamicSeriesList._masterjet_series()` verwendete einen klassenweiten
30-Sekunden-Cache ohne Bezug zum tatsächlich verwendeten
`CODEX_MASTER_MCP`-Befehl. Nach einem Befehls-/Pfadwechsel blieb deshalb die
alte Serienliste sichtbar, obwohl der neue Backend-Prozess bereits andere
Serien lieferte.

Der Cache-Key enthält jetzt das vollständige `argv`. Ein anderer Befehl
umgeht den alten Eintrag sofort; gleicher Befehl nutzt weiterhin den
zeitbegrenzten Cache. Regression startet zwei kleine Masterjet-Dummies und
prüft den Wechsel von `A` auf `B`.

Verifikation: `tests/test_dynamic_series_list.py` 20/20; Python-Compile,
Ruff und `git diff --check` sauber.

## Runde 745: Dynamic-Series-Editor fängt Basisdialogfehler ab

`DynamicSeriesList.open_add_edit_dialog()` filterte die Serienoptionen und
rief danach den Cinnamon-Basisdialog ungeguardet auf. Fehler beim Aufbau eines
einzelnen Feldeditors konnten damit den Accounts-Editor bis in den
Settings-Eventloop abbrechen. Das zuvor geschützte `PanelSettingsList`-Muster
war hier nicht wirksam, weil diese Klasse direkt von `List` erbt.

Der Basisdialog läuft jetzt in einem Fehlerguard und liefert bei einer
Ausnahme `None`; der `finally`-Pfad stellt die unveränderte Spaltenschemareferenz
immer wieder her. Regression deckt Fehler und Schema-Restore ab.

Verifikation: `tests/test_dynamic_series_list.py` 21/21; Python-Compile,
Ruff und `git diff --check` sauber.

## Runde 746: Konfligierende aktive Serien bleiben reparierbar

Die Accounts-Tabelle kann durch alte oder extern geschriebene Einstellungen
vorübergehend zwei aktive Accounts mit derselben Serie enthalten. Der
Anwendungs-Sync weist diesen Konflikt beim Speichern korrekt zurück. Im
Editor wurde die Serie des zuerst gelisteten Konflikt-Accounts jedoch aus dem
Dropdown entfernt, weil `_active_owners()` den zuletzt gelesenen Besitzer
führte. Das aktuelle Feld konnte dadurch nicht mehr unverändert geöffnet oder
auf `Keine Serie` korrigiert werden.

`DynamicSeriesList._series_options_for()` prüft deshalb zusätzlich, ob die
aktuelle Serie im bearbeiteten Datensatz selbst aktiv dem aktuellen Account
gehört. In diesem Fall bleibt sie als `A (aktuell)` auswählbar, ohne die Serie
für andere Accounts freizugeben. Accountnamen werden für diesen Vergleich
nur an den Rändern normalisiert; gespeicherte Tabellenwerte werden nicht
mutiert.

Regression deckt einen doppelten aktiven Serienwert ab und verlangt, dass der
erste Konflikt-Account `A (aktuell)` behalten kann. Verifikation:
`tests/test_dynamic_series_list.py` 22/22; Python-Compile, Ruff und
`git diff --check` sauber.

## Runde 747: Listener-Cleanup darf Settings-Start nicht abbrechen

Wenn die Listener-Registrierung des Cinnamon-Backends fehlschlug, rief der
Konstruktor zur Bereinigung `detach()` auf. Ein fehlerhaftes oder bereits
abgebautes `settings.listeners`-Objekt konnte dabei selbst eine Ausnahme
werfen. Damit wurde aus einem abgefangenen Backendfehler erneut ein
ungefangener Fehler im Settings-Eventloop.

`DynamicSeriesList.detach()` behandelt den gesamten Cleanup-Pfad jetzt als
best-effort. Ein nicht verfügbares Listener-Register oder eine fehlgeschlagene
Entfernung bleibt lokal; der Settings-Editor kann die Tabelle weiterhin leer
anzeigen und später sauber zerstört werden. `BaseException` wird nicht
abgefangen.

Regression verwendet ein Backend, dessen `listeners`-Property beim Cleanup
fehlschlägt. Verifikation: `tests/test_dynamic_series_list.py` 23/23;
Python-Compile, Ruff und `git diff --check` sauber.

## Runde 748: Formatierungs-Selector überlebt abgehängte Tabellenwidgets

Beim Umschalten einer Formatierungs- oder Prognosetabelle entfernt der
Selector zuerst das bisher aktive Widget aus dem `Gtk.Stack`. War dieses
Widget bereits extern abgehängt oder zerstört, konnte `Stack.remove()` eine
Ausnahme werfen. Der Auswahl-Callback brach dann vor `destroy()` und vor der
neuen Anzeige ab.

`FormatTableSelector._discard_table()` behandelt `detach()`, `Stack.remove()`
und `destroy()` jetzt einzeln als best-effort. Der interne Tabellenindex wird
vorher entfernt; ein Fehler in einem GTK-Cleanup-Schritt blockiert daher weder
den nächsten Tabellenwechsel noch das Schließen der Settings-Seite.

Regression simuliert ein bereits abgehängtes Stack-Widget. Verifikation:
Format-/Forecast-Fokustests 80/80; Python-Compile, Ruff und `git diff --check`
sauber.

## Runde 749: Prognose-Selector mit robustem Tabellen-Cleanup

`ForecastTableSelector._discard_table()` hatte noch den alten direkten
Cleanup-Pfad: `detach()`, `Gtk.Stack.remove()` und `destroy()` konnten jeweils
eine Ausnahme bis in den Auswahl-Callback weiterreichen. Damit blieb der
Forecast-Selector beim Wechsel hängen, wenn GTK ein Tabellenwidget bereits
entfernt hatte.

Der Prognose-Selector behandelt alle drei Cleanup-Schritte jetzt wie der
Formatierungs-Selector als best-effort und entfernt den internen Tabelleneintrag
vorher. Ein beschädigtes altes Widget blockiert weder die neue Prognosetabelle
noch das Schließen der Seite.

Regression simuliert ein bereits abgehängtes Stack-Widget. Verifikation:
Format-/Forecast-Fokustests 81/81; Python-Compile, Ruff und `git diff --check`
sauber.

## Runde 750: Prognose-Tabellenaufbau fail-closed

`ForecastTableSelector._ensure_table()` rief `_BoundFormatList` und die
anschließende GTK-Registrierung ungeguardet auf. Ein beschädigtes
Tabellenschema oder ein einzelner GTK-Aufbaufehler konnte dadurch aus dem
Auswahl-Callback bis in den Settings-Eventloop steigen. Der
Formatierungs-Selector hatte diesen Schutz bereits.

Der Prognose-Tabellenaufbau nutzt jetzt denselben Guard: Erst nach erfolgreichem
`add_named()` und `show_all()` wird das Widget registriert. Bei einem Fehler
werden bereits erzeugtes Widget, Listener und Stack-Eintrag best-effort
aufgeräumt; der Selector liefert `None` und bleibt bedienbar.

Regression erzwingt einen Fehler in `_BoundFormatList` und erwartet einen
leeren Tabellenindex statt einer Ausnahme. Verifikation:
Format-/Forecast-Fokustests 82/82; Python-Compile, Ruff und `git diff --check`
sauber.

## Runde 751: Prognosetitel sicher als Pango-Markup setzen

`ForecastTableSelector._show_table()` setzte den dynamischen Tabellentitel
direkt in `Gtk.Label.set_markup()`. Ein Label mit `&`, `<` oder `>` erzeugte
GTK-Warnungen und wurde leer angezeigt. Das war im statischen deutschen
Schema unsichtbar, blieb aber ein fehlerhafter Metadatenpfad und wich vom
bereits gehärteten Formatierungs-Selector ab.

Der Forecast-Titel wird jetzt mit `GLib.markup_escape_text()` escaped und erst
dann fett markiert. Der sichtbare Titel bleibt unverändert, Pango interpretiert
nur die absichtlich gesetzten `<b>`-Tags.

Regression nutzt `A & B <C>` und prüft den echten GTK-Titeltext. Verifikation:
Format-/Forecast-Fokustests 83/83; Python-Compile, Ruff und `git diff --check`
sauber.

## Runde 752: Forecast-Tabellenmetadaten ohne NUL-Werte

Der Prognose-Selector akzeptierte bislang leere oder NUL-haltige Tabellen-Keys
und Labels. Solche Werte konnten in `ComboBoxText`, Listener-Schlüsseln oder
Pango-Markup landen und den Selector mit abgeschnittenen GTK-Strings oder
Fehlern zurücklassen. `FormatTableSelector` filterte diese Metadaten bereits.

Forecast ignoriert jetzt leere/NUL-Keys und fällt bei einem NUL-Label auf den
validierten Tabellen-Key zurück. Gültige Tabellen bleiben unverändert; keine
persistierte Auswahl wird durch diese Metadatenprüfung geschrieben.

Regression deckt leeren Key, NUL-Key und NUL-Label ab. Verifikation:
Format-/Forecast-Fokustests 86/86; Python-Compile, Ruff und `git diff --check`
sauber.

## Runde 753: Permission-Regressionstests an FD-Härtung angepasst

Der Vollsuite-Lauf fand drei rote Tests in den Bridge-/Browser-Diagnosepfaden.
Die Tests simulierten fehlgeschlagene Verzeichnissicherung über
`Path.chmod()`. Die gemeinsame Private-IO-Implementierung setzt Verzeichnis-
rechte inzwischen absichtlich über geöffnete Deskriptoren und `os.fchmod()`,
damit ein TOCTOU-Swap zwischen Prüfung und Modusänderung nicht auf einen
Symlink folgt. Der alte Test-Seam konnte diesen Fehler daher nicht mehr
auslösen; Produktionscode war nicht regressiert.

Die Bridge- und Browser-Tests patchen jetzt direkt den jeweiligen
`ensure_private_directory`-Vertrag und prüfen weiterhin fail-closed-Fehler in
den öffentlichen Wrappern. Ein zusätzlicher Private-IO-Test deckt echten
`os.fchmod()`-Fehler ab. Damit testen sie aktuelle Sicherheitsgrenze statt
einer veralteten Implementierungsdetails.

Verifikation: fokussierte Permission-Tests 49/49; Vollsuite **3198 bestanden,
1 übersprungen, 41 externe PyGObject-/GTK-Warnungen**; Python-Compile, Ruff
und `git diff --check` sauber.

## Runde 754: Settings-Fallback behält AT-SPI-Schutz

Der Settings-Launcher setzte `NO_AT_BRIDGE=1` bisher nur über
`Gio.SubprocessLauncher.setenv()`. Wenn die Launcher-API fehlte, `setenv()`
nicht vorhanden war oder `spawnv()` scheiterte, fiel der Code auf
`Gio.Subprocess.new()` zurück und startete `xlet-settings` ohne den Schutz.
Auf dieser Maschine kann der AT-SPI-Bridge-Aufbau den Prozess dadurch lange
blockieren; für Nutzer wirkt die Einstellungen-Aktion dann wie ein No-op.

Der Launcher wird jetzt nur verwendet, wenn die Kindumgebung erfolgreich
gesetzt wurde. Jeder `Gio.Subprocess.new()`-Fallback startet stattdessen
`/usr/bin/env NO_AT_BRIDGE=1 xlet-settings …`. Die bestehende PID-Weitergabe
und Fensterplatzierung bleiben unverändert. Regression deckt fehlende
Launcher-API, `spawnv()`-Fehler und fehlendes `setenv()` ab.

Verifikation: kompletter Node-Runtime-Test `486/486`; Settings-Launcher-
Fokustest `8/8`; Node-Syntaxcheck und `git diff --check` sauber. Das Applet
wurde installiert und mit `--reload-running` neu geladen. Nach Nutzerwunsch
wurden keine weiteren Settings-Fenster für diesen Lauf gestartet; gestartete
Audit-Prozesse und -Fenster sind beendet.

## Runde 755: Leisten-Editor räumt GTK-Dialogbaum vollständig auf

Der Leisten-Editor zerstörte seine Eingabefelder bisher nur indirekt über
`Gtk.Dialog.destroy()`. Bei einem fehlenden oder abweichenden Dialog-Cleanup
blieben Frame, ScrolledWindow, Grid und die dynamisch erzeugten Eingabefelder
referenziert. Nach vielen Doppelklicks konnte Cinnamon dadurch unnötig GTK-Heap
behalten; der Fehler zeigte sich im Test als Segfault beim späteren Abarbeiten
der globalen GTK-Eventqueue.

`PanelSettingsList.open_add_edit_dialog()` hält Editor-Widgets und den
Editor-Frame jetzt bis zum `finally` fest. Der komplette Frame wird explizit
zerstört, verbliebene Widgets werden zusätzlich best-effort zerstört, danach
folgt der Dialog-Cleanup. Damit greift Bereinigung auch bei einem Dialog-Doppel
oder einem fehlerhaften Dialogpfad.

Die GTK-Selector-Regressionen für Formatierungs- und Prognosetabellen drainen
keine globale Eventqueue mehr. Sie verwenden den normalen `changed`-Signalpfad
und rufen den Handler nur als synchronen Fallback auf, falls eine GTK-Version
den Signalversand verzögert. Dadurch verarbeitet ein Test nicht fremde,
bereits zerstörte Widgets aus anderen Settings-Seiten.

Regression: Neuer Test erzwingt Zerstörung aller Leisten-Editor-Widgets trotz
Dialog-Doppel ohne Cleanup. Kombinierter GTK-Fokuslauf
`tests/test_panel_settings_list.py tests/test_format_table_selector.py
tests/test_forecast_table_selector.py`: **179 bestanden**, kein Segfault.
Python-Compile, Ruff und `git diff --check` sauber. Keine Settings-Fenster
gestartet.

## Runde 756: Fehlgeschlagene Formatierungs-Tabelle nicht persistieren

Beim Umschalten des Formatierungs-Selectors schrieb `_on_table_changed()` die
Auswahl bisher auch dann in die Einstellungen, wenn `_ensure_table()` den
Widget-Aufbau nicht herstellen konnte. Dadurch blieb die alte oder keine
Tabelle sichtbar, während beim nächsten Reload eine nicht ladbare Auswahl
erneut verwendet wurde.

`_show_table()` liefert jetzt einen Erfolgswert zurück. Der Selector speichert
die Auswahl nur nach erfolgreichem Tabellenaufbau; bei Aufbaufehler bleibt die
bisherige persistierte Auswahl unverändert. Regression prüft den
fehlgeschlagenen Aufbau direkt.

Verifikation: kombinierter GTK-Fokuslauf
`tests/test_panel_settings_list.py tests/test_format_table_selector.py
tests/test_forecast_table_selector.py`: **180 bestanden**, kein Segfault.
Python-Compile, Ruff und `git diff --check` sauber. Keine Settings-Fenster
gestartet.

## Runde 757: Formatierungs-Selector fällt bei defekter Auswahl zurück

Beim Settings-Reload wurde ein gültiger, aber nicht baubarer Tabellen-Key als
ausgewählt behandelt. `_ensure_table()` konnte den Widget-Aufbau ablehnen,
danach blieb die Auswahl auf der defekten Tabelle und die Formatierungsseite
zeigte keine nutzbare Tabelle. Eine andere gültige Tabelle wurde nicht
probiert.

`on_setting_changed()` probiert jetzt zuerst den gespeicherten Key und danach
die übrigen Tabellen in deklarierter Reihenfolge. Combo-Auswahl und sichtbare
Tabelle werden erst nach erfolgreichem `_show_table()` gesetzt; bei einem
Aufbaufehler bleibt die persistierte Auswahl unverändert. Regression erzwingt
den Fehler der gespeicherten Tabelle und prüft den Wechsel auf die nächste
funktionierende Tabelle.

Verifikation: kombinierter GTK-Fokuslauf
`tests/test_panel_settings_list.py tests/test_format_table_selector.py
tests/test_forecast_table_selector.py`: **181 bestanden**, kein Segfault.
Python-Compile, Ruff und `git diff --check` sauber. Keine Settings-Fenster
gestartet.

## Runde 758: Prognosen-Selector erhält denselben Aufbau-Fallback

Der Prognosen-Selector hatte den gleichen Fehlerpfad wie der bereits gehärtete
Formatierungs-Selector: Nach einem fehlgeschlagenen Tabellenaufbau wurde die
Auswahl trotzdem persistiert. Beim Reload wurde eine gültige, aber nicht
baubare Tabelle nicht übersprungen; die Prognosen-Seite konnte dadurch leer
bleiben.

`ForecastTableSelector` wertet `_show_table()` jetzt als Erfolgswert aus und
schreibt nur nach erfolgreichem Aufbau. Beim Settings-Reload werden zuerst die
gespeicherte Tabelle und danach die übrigen Tabellen in deklarierter Reihenfolge
probiert. Combo-Auswahl und sichtbare Tabelle werden erst bei Erfolg gesetzt.
Zwei Regressionen decken Umschalten und Reload mit defekter Tabelle ab.

Verifikation: kombinierter GTK-Fokuslauf
`tests/test_panel_settings_list.py tests/test_format_table_selector.py
tests/test_forecast_table_selector.py`: **183 bestanden**, kein Segfault.
Python-Compile, Ruff und `git diff --check` sauber. Keine Settings-Fenster
gestartet.

## Runde 759: Serienbesitzer trimmt Account-ID konsistent

`DynamicSeriesList._series_options_for()` trimmt Account-IDs vor dem Vergleich,
`_active_owners()` speicherte den Besitzer bisher jedoch mit führenden oder
folgenden Leerzeichen. Ein Eintrag wie `"  alpha  "` wurde dadurch beim Filtern
als anderer Besitzer behandelt; belegte Serien konnten für denselben Account
falsch als frei erscheinen oder umgekehrt.

`_active_owners()` speichert den bereits validierten Besitzer jetzt getrimmt.
Die Normalisierung betrifft nur den internen Vergleich; persistierte
Account-Daten bleiben unverändert. Regression deckt Whitespace-Besitzer ab.

Verifikation: `tests/test_dynamic_series_list.py`: **24 bestanden**.
Python-Compile, Ruff und `git diff --check` sauber. Keine Settings-Fenster
gestartet.

## Runde 760: Ungültige aktuelle Serie nicht in GTK-Combo übernehmen

Der dynamische Serieneditor übernahm jeden gespeicherten String als
`(aktuell)`-Option, sobald die Serie nicht aus dem Masterjet-Angebot kam. Ein
beschädigter Wert wie `A\x00` konnte dadurch als Label und Wert in die GTK-
ComboBox gelangen. Das machte den Editor unnötig fragil und konnte beim
Rendern/Weiterreichen der Option fehlschlagen.

Der aktuelle Wert wird vor dem Legacy-Erhalt jetzt gegen dasselbe ASCII-
Serienpräfix geprüft wie die Masterjet-Antwort. Ungültige Werte werden nicht
als Option angeboten; gültige, nicht mehr verfügbare Serien bleiben für ihren
Account reparierbar. Regression deckt einen NUL-haltigen Serienwert ab.

Verifikation: `tests/test_dynamic_series_list.py`: **25 bestanden**.
Python-Compile, Ruff und `git diff --check` sauber. Keine Settings-Fenster
gestartet.

## Runde 761: `series-active` im JS-Reconcile strikt boolean prüfen

Der Accounts-Reconcile behandelte jeden vorhandenen Wert außer `true` als
`false`. Ein beschädigter Settings-Wert wie `"true"` konnte dadurch eine
aktive Serienzuordnung still deaktivieren und als legitime Änderung an die
CLI weitergereicht werden. Andere boolesche Felder werden bei falschem Typ
bereits verworfen.

`_onAccountBackendsChanged()` akzeptiert `series-active` jetzt nur als echtes
Boolean. Bei jedem anderen vorhandenen Typ wird der Backend-Stand neu geladen;
kein Account-Update und keine stille Deaktivierung erfolgt. Regression deckt
den Stringwert `"true"` ab.

Verifikation: fokussierte Node-Tests für Serien-/Backend-Settings: **9
bestanden**; Node-Syntaxcheck und `git diff --check` sauber. Keine
Settings-Fenster gestartet.

## Runde 762: `series_active` aus Backend-Overview strikt prüfen

Die Backend-Overview setzte `seriesActive` bisher mit `item.series_active ===
true`. Ein fehlerhafter Payload-Wert wie der String `"true"` wurde damit
lautlos zu `false`, obwohl die Antwort anschließend als gültiger Account-Stand
weiterverarbeitet werden konnte.

`_loadAccountBackends()` lehnt vorhandene `series_active`-Werte jetzt ab, wenn
sie kein echtes Boolean sind. Der bisherige Zustand bleibt erhalten; weder
Settings-Sync noch Account-Update laufen mit einem verfälschten Wert.
Regression prüft den String-Payload.

Verifikation: fokussierte Node-Tests für Backend-Overview, Backend-Settings
und Serien: **15 bestanden**; Node-Syntaxcheck und `git diff --check` sauber.
Keine Settings-Fenster gestartet.

## Runde 763: Legacy-Reactivation-Migration nicht mit malformed Serienflag

Die Migration des alten Reactivation-Browser-Felds baute
`series-active` bisher erneut per `=== true` auf. Ein beschädigter Stringwert
`"true"` wurde dadurch zu `false` und konnte beim nachfolgenden Account-Update
die aktive Serienzuordnung abschalten.

Die Migration bricht bei einem vorhandenen Nicht-Boolean jetzt fail-closed ab
und setzt keinen Migrationsstand. Echte Boolean-Werte sowie fehlende Legacy-
Felder bleiben unverändert kompatibel. Regression deckt den malformed String
ab.

Verifikation: fokussierte Node-Tests für Legacy-Migration, Backend-Overview,
Backend-Settings und Serien: **17 bestanden**; Node-Syntaxcheck und
`git diff --check` sauber. Keine Settings-Fenster gestartet.

## Runde 764: Tokendelta nicht mit unbekanntem Pool als `main` markieren

`_panelDeltaIsDynamic()` leitete aus jedem Poolnamen außer
`gpt-5.3-codex-spark` automatisch den Pool `main` ab. Ein unbekannter oder
künftiger pool-only-Pool konnte dadurch gegen das 5h-/Main-Fenster geprüft
und fälschlich als dynamisch formatiert werden.

Die Zuordnung akzeptiert jetzt ausschließlich die bekannten Pool-IDs `main`
und `gpt-5.3-codex-spark`; unbekannte IDs liefern konservativ `false`. Das
ändert keine bestehenden Main-/Spark-Pfade und hält neue pool-only IDs von
falschen Schwellenwerten fern. Regression deckt `pool-only` ab.

Verifikation: fokussierte Pool-/Delta-Tests in `tests/applet_runtime.test.js`:
**49 bestanden**; Node-Syntaxcheck und `git diff --check` sauber. Keine
Settings-Fenster gestartet.

## Runde 765: Tokendelta bei abgelaufenem Reset nicht dynamisch markieren

Die dynamische Delta-Projektion verwendete bei einem bereits abgelaufenen
`reset_at` weiterhin die komplette Fensterdauer als Horizont. Ein altes Delta
konnte dadurch nach dem Reset fälschlich den dynamischen Schwellenwert
überschreiten.

Ein bekanntes `reset_at` in der Vergangenheit beendet die Projektion jetzt
konservativ mit `false`; ein unbekannter Reset bleibt wie zuvor über die
konfigurierte Fensterdauer berechenbar. Regression deckt den abgelaufenen
Reset ab.

Verifikation: fokussierte Tokendelta-/Pool-Tests: **4 bestanden**;
Node-Syntaxcheck und `git diff --check` sauber. Keine Settings-Fenster
gestartet.

## Runde 766: Forecast-Nullausblendung auch im kombinierten Verbrauchspfad

`_consumptionWindowPart()` erzeugte für den Tokenende-Anteil eine Forecast-
Zeile mit dem generischen Feld `hide-when-zero`. `_forecastWindowPart()` prüft
jedoch korrekt das Forecast-Feld `forecast-hide-when-zero`. Im kombinierten
Verbrauchs-/Tokenende-Rendering wurde ein Schätzwert `0` deshalb trotz
aktivierter Nullausblendung als `TE=0,0h` angezeigt.

Die erzeugte Forecast-Zeile trägt jetzt zusätzlich das erwartete
`forecast-hide-when-zero`-Feld. Der Verbrauchs-Haken bleibt davon getrennt;
die Regression prüft, dass nur TE verschwindet, Delta und AW aber sichtbar
bleiben.

Verifikation: fokussierte Consumption-/Forecast-Tests in
`tests/applet_runtime.test.js`: **39 bestanden**; Node-Syntaxcheck und
`git diff --check` sauber. Keine Settings-Fenster gestartet.

## Runde 767: Tokenende nicht aus fremder Consumption-Query übernehmen

`_consumptionWindowPart()` nutzte bei fehlender Forecast-Antwort immer das
aktuelle Verbrauchsfenster als TE-Fallback, sobald das Limitfenster gleich
war. Bei unterschiedlicher Glättung (oder Baseline) stammte das Fenster damit
aus einer anderen Query und zeigte ein falsches Tokenende.

Der Fallback bleibt nur für ungetaggte Legacy-Fenster oder identische
Consumption-/Forecast-Queryparameter aktiv. Getaggte Ergebnisse einer
anderen Query liefern ohne eigene Forecast-Antwort kein TE. Regression deckt
unterschiedliche Glättung ab und erhält den Legacy-Fallback.

Verifikation: fokussierte Consumption-/Forecast-Tests:
**40 bestanden**; Node-Syntaxcheck und `git diff --check` sauber. Keine
Settings-Fenster gestartet.

## Runde 768: Consumption-Antworten nicht in fremden Pool übernehmen

`_drainConsumptionRequests()` prüfte bisher nur Account und Generation. Eine
gültige Antwort mit fremdem `pool` wurde deshalb unter dem Query-Schlüssel der
laufenden Anfrage gespeichert. Bei pool-only-Quellen konnte so ein Main-
Fenster als Credits-/Spark-Ergebnis im Cache landen und spätere Darstellung
oder Ersetzung beeinflussen.

Die Antwort wird jetzt nach DTO-Validierung verworfen, sobald eines ihrer
Fenster nicht exakt zum angefragten Pool gehört. Regression deckt eine
Credits-Anfrage mit Main-Antwort ab; Serienpfade bleiben Legacy-Kompatibilität.

Verifikation: fokussierte Consumption-/Forecast-Tests:
**41 bestanden**; Node-Syntaxcheck und `git diff --check` sauber. Keine
Settings-Fenster gestartet.

## Runde 769: Formatierungsziele je Wert und lesbare Hilfe

Die Hilfe-Seite hatte keinen Mindestplatz und war im kleinen Settings-Rahmen
kaum lesbar. `HelpPage` fordert jetzt mindestens 720 × 560 Pixel an; der
Scrollbereich erhält mindestens 640 × 480 Pixel. Labels expandieren horizontal
und bleiben zeilenumbruchsicher. Lazy-Expander bleiben erhalten, damit nicht
alle Feldwidgets gleichzeitig Cinnamon-Heap belegen.

Die aktive Tabelle `Elemente und Formatierungsorte` ist aus Layout und
Formatierungs-Dropdown entfernt. Ihre Definition bleibt ausschließlich als
unsichtbare Legacy-Migrationsquelle erhalten. Die alten Hover-/Klickwerte
werden beim Stylesync in die zuständigen Formatierungszeilen übernommen:
Prozent, Resetdatum, Resetzeit, Restlaufzeit sowie Reset-/Kürzel-/Label- und
Account-ID-Leistenwerte. Panel-Sichtbarkeit bleibt ausschließlich Aufgabe der
Tabelle `Leiste`.

Alle aktiven Formatierungsziele erhalten `In Hovermenü anzeigen`,
`In Klickmenü anzeigen` und `Bei Null ausblenden`; Leisten-Kopien erben die
Felder, Tokendelta behält zusätzlich `Dynamisch`. Nullausblendung meint
fehlende/unbekannte Werte mit Platzhalter `–`; ein echter numerischer `0`-Wert
bleibt sichtbar. Prozent-, Reset-, Delta- und Leisten-Renderer setzen diese
Semantik um.

Verifikation: **494 Node-Tests** und **111 fokussierte Python-Tests** bestanden;
Node-Syntaxcheck, JSON-Parse und `git diff --check` sauber. Keine
Settings-Fenster gestartet.

## Runde 770: NULL-/ungültige Werte standardmäßig ausblenden

Die Schalter `Bei null ausblenden` hatten in mehreren Tabellen noch `false`
als Schema- oder Laufzeit-Default. Das betraf Prozent-, Reset-, Tokenende-,
Credit- und Creditverbrauchszeilen sowie deren geklonte Formatierungszeilen.
Alle neun aktiven Tabellen mit diesem Schalter verwenden jetzt `true` als
Default. Fehlende Felder aus älteren gespeicherten Zeilen werden bei der
Normalisierung ebenfalls auf `true` gesetzt; ein ausdrücklich gespeichertes
`false` bleibt eine gültige Nutzerentscheidung.

Forecast- und Credit-Renderer unterdrücken bei aktivem Default nun auch
fehlende, nicht numerische oder negative Kernwerte, statt `—` auszugeben.
Bereits vorhandene gültige Werte und die bisherige explizite Nullausblendung
bleiben unverändert. Die Formatierungs-Renderer behalten ihre Trennung:
fehlende/ungültige Werte verschwinden, ein echter numerischer 0-Wert bleibt
sichtbar.

Regressionen prüfen alle Default-/Normalisierungspfade, die Beibehaltung von
`false`, Schema-Defaults sowie das Ausblenden ungültiger Forecast-/Creditwerte.

Verifikation: **496 Node-Tests**, **131 fokussierte Python-Tests** und die
vollständige Python-Suite mit **3208 bestanden, 1 übersprungen**; Node-
Syntaxcheck, JSON-Parse und `git diff --check` sauber. Keine Settings-Fenster
gestartet.

## Runde 771: Leistenwerte accountübergreifend kopieren und vertikal bearbeiten

Der Leisten-Editor bietet jetzt zwei Toolbar-Aktionen: Werte kopieren legt
die sichtbaren Wert-1-bis-N-Felder des ausgewählten Accounts als internen
Snapshot ab; Werte einfügen übernimmt diesen Snapshot in den ausgewählten
Zielaccount. Account-ID, Reihenfolge, Stumm und sonstige Metadaten werden
nicht überschrieben. Nicht sichtbare Legacy-Slots bleiben beim Speichern
weiterhin erhalten.

Die Editorpositionen trennen Account-Metadaten von den Wertfeldern. Wert 1–N
werden spaltenweise von oben nach unten verteilt: bei 15 Werten und drei
Spalten entstehen 1–5, 6–10 und 11–15. Dialogbreite und -höhe werden aus
Wertanzahl, Metadaten und Spaltenzahl berechnet; große Wertlisten behalten
vertikalen Bildlauf, horizontale Spalten bleiben sichtbar. Schema-Tooltip und
gesammelte Hilfe dokumentieren Bedienfolge und unveränderte Felder.

Regressionen decken Snapshot-Isolation, Toolbar-Aktionen, 15-Werte-Layout,
Dialoggröße und Schema-Metadaten ab.

Verifikation: **3212 Python-Tests bestanden, 1 übersprungen**; Node-
Syntaxcheck, JSON-Parse und `git diff --check` sauber. Keine Settings-Fenster
gestartet.

## Runde 772: Snapshot-Helfer gegen defekte Spaltendaten härten

Die neuen Leisten-Kopierfunktionen erwarteten bei `panel_value_settings()` und
`panel_apply_value_settings()` stillschweigend eine gültige Spaltenliste. Ein
defekter oder noch nicht geladener Schemawert `columns=None` führte deshalb zu
`TypeError`. Beide Helfer liefern jetzt bei ungültiger Spaltenliste einen
leeren beziehungsweise unveränderten Ziel-Snapshot zurück.

Regression deckt `None`, leere und fehlerhafte Snapshot-Eingaben ab; ein
kleiner Fuzz-Lauf über kaputte Typen bleibt fehlerfrei. Ruff und Panel-Fokuslauf
bleiben sauber.

Verifikation: **34 fokussierte Panel-Tests bestanden**; Ruff, Python-Compile
und `git diff --check` sauber. Keine Settings-Fenster gestartet.

## Runde 773: Nichtkanonische Leisten-Slot-IDs verwerfen

Die Slot-Erkennung verwendete `str.isdecimal()` ohne ASCII-Prüfung. Unicode-
Ziffern konnten dadurch als `slot1`-ähnliche IDs in Schema und Layout gelangen
und logische Wertfelder duplizieren. Slot-Suffixe sind jetzt ausschließlich
ASCII-Ziffern ohne führende Null; ungültige IDs werden wie andere beschädigte
Schemafelder verworfen und durch kanonische `slot1…slotN`-Felder ersetzt.

Regressionen prüfen arabische und vollbreite Ziffern sowie `slot0`, `slot01`
und Textsuffixe. Ruff, fokussierte Tests, Compile und Diff-Check sind sauber.

Verifikation: **8 fokussierte Slot-Tests bestanden**; keine Settings-Fenster
gestartet.

## Runde 774: Legacy-Slots numerisch sortieren

Gültige, aber historisch falsch angeordnete Slotfelder (`slot3`, `slot1`,
`slot2`) blieben bisher in Schema-Reihenfolge. Dadurch konnte Wert 3 vor Wert
1 im Leisten-Editor und in Snapshot-Reihenfolge landen. `panel_columns()` hält
Metadatenreihenfolge, sortiert danach alle kanonischen `slotN`-Felder numerisch
und ergänzt fehlende Slots wie bisher.

Regression deckt eine gültige, absichtlich falsch sortierte Legacy-Liste ab.
Panel-Spalten-, Editor- und Snapshot-Tests sowie Ruff, Compile und Diff-Check
bleiben sauber.

Verifikation: **54 fokussierte Panel-Tests bestanden**; keine Settings-Fenster
gestartet.

## Runde 775: Dialoggrößenberechnung ohne Float-Overflow

`panel_editor_dimensions()` teilte Wert- und Metadatenanzahl bisher mit
Float-Division auf. Extrem große, direkt übergebene Integer konnten dadurch
`OverflowError` auslösen. Die Aufrundung verwendet jetzt ganzzahlige Division;
die bestehende Höhenbegrenzung bleibt unverändert.

Regression prüft eine extrem große Wert- und Metadatenanzahl. Ruff, fokussierte
Tests, Compile und Diff-Check sind sauber.

Verifikation: **2 fokussierte Größen-Tests bestanden**; keine Settings-Fenster
gestartet.

## Runden 776–779: Persistenz- und JS-Leistenabgleich

Die gespeicherten Leistenzeilen wurden bei Kontoänderung, Löschen, Verschieben,
Duplikaten und versteckten Legacy-Slots geprüft. Zufällige Schemas bestätigten
für jede Wertanzahl 1–64 eindeutige, numerisch vollständige `slotN`-Felder.
Der JS-Pfad wurde gegen Python-Slotlayout, Normalisierung, Duplikatfilter,
Quellen 0–51 und dynamische 64er-Wertlisten abgeglichen. Keine belastbare neue
Fehlstelle.

Verifikation: **105 Panel-Tests** und **496 Node-Runtime-Tests** bestanden;
Ruff und Invarianten-Fuzzing sauber. Keine Settings-Fenster gestartet.

## Runde 783: Kopier-Toolbar nach Leisten-Rebuild zurücksetzen

Beim Ändern der Wertanzahl ersetzt der Leisten-Editor seine `TreeView`. Die
neue Ansicht hatte keine Auswahl, während die Kopier-/Einfüge-Toolbar ihren
alten Sensitivitätszustand behielt. Einfügen blieb dadurch sichtbar aktiv,
obwohl kein Zielaccount ausgewählt war; der Datenpfad blieb geschützt und
änderte ohne Auswahl nichts.

`on_setting_changed()` und `_rebuild_tree()` synchronisieren die Toolbar jetzt
nach jedem Modell- oder TreeView-Rebuild. Kopieren und Einfügen sind nur bei
aktueller Auswahl beziehungsweise bei Auswahl plus Snapshot aktiv.

Regression deckt den Wertanzahlwechsel nach einem Kopiervorgang ab und prüft,
dass die neue TreeView ohne Auswahl beide Aktionen deaktiviert. Ruff,
Python-Compile und Diff-Check bleiben sauber; keine Settings-Fenster gestartet.

Verifikation: **107 fokussierte Panel-Tests bestanden**.

## Runde 784: Leisten-Dialogbreite auf belegte Spalten begrenzen

Die automatische Breite des Leisten-Editors reservierte bisher immer alle
konfigurierten Spalten. Bei wenigen Wert- und Metadatenfeldern blieb ein
unnötig breites Fenster, obwohl weniger Spalten tatsächlich belegt waren.
Die Breite richtet sich jetzt nach der maximal belegten Spaltenzahl, bleibt
aber mindestens eine Spalte breit. Bei vielen Werten bleiben alle gewählten
Spalten sichtbar; nur vertikale Höhe kann weiterhin scrollen.

Regression prüft ein Wertfeld mit fünf konfigurierten Spalten und drei
Metadatenfeldern: drei belegte Spalten, nicht fünf. **108 fokussierte
Panel-Tests**, Ruff, Python-Compile und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 785: Unsaved-Leistenwerte bei Wertanzahlwechsel erhalten

Wenn ein Settings-Schreiben fehlschlug, blieb die Änderung zunächst korrekt
im GTK-Modell. Ein anschließender Wechsel der Wertanzahl baute die Tabelle
bisher jedoch ausschließlich aus der alten gespeicherten Zeile neu auf und
verwarf damit sichtbare, noch nicht persistierte Werte.

Der Rebuild übernimmt jetzt aktuelle sichtbare Modellwerte accountbezogen in
die gespeicherten Zeilen. Nicht sichtbare Legacy-Slots bleiben aus der
gespeicherten Zeile erhalten; gelöschte Modellzeilen werden nicht künstlich
wiederhergestellt. Regression prüft genau diesen Fehler nach einem
fehlgeschlagenen Schreiben.

Verifikation: **109 fokussierte Panel-Tests** sowie Ruff, Python-Compile und
Diff-Check sauber; keine Settings-Fenster gestartet.

## Runde 786: Hilfe zeigt vollständige Feld-Metadaten

Die gesammelte Hilfe zeigte bei Einzelwerten bisher nur Beschreibung und
Tooltip. Standardwert, Grenzen und Schrittweite aus dem Schema fehlten; bei
Listenfeldern wurden außerdem `step`, Einheiten, Ordnerauswahl und horizontale
Breite nicht erklärt. Optionslisten als Sequenz wurden ebenfalls verschluckt.

`_definition_entry()` und `_field_text()` geben diese Metadaten jetzt lesbar
aus. `_option_text()` unterstützt neben Dictionaries auch Listen und Tupel.
Die bestehende HTML-Escapelogik bleibt unverändert. Zusätzlich wurde ein
mehrdeutiger Gedankenstrich in der Einführung durch klare Textformulierung
ersetzt, damit der Help-Code lint-sauber bleibt.

Verifikation: **13 fokussierte Hilfe-Tests**, Ruff, Python-Compile und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 787: Doppelte Formatspalten-IDs verwerfen

Formatierungstabellen akzeptierten bisher doppelte Spalten-IDs. GTK zeigte
dann mehrere Spalten für denselben Schlüssel; beim Speichern erzeugte der
JSON-Aufbau denselben Schlüssel mehrfach, sodass der spätere Wert den früheren
überschrieb.

`_BoundFormatList` dedupliziert gültige Spalten jetzt deterministisch: Die
erste gültige Definition gewinnt, weitere gleiche IDs werden verworfen. Die
Schema-Kopie bleibt dank bestehender Deep-Copy-Isolation unverändert.

Regression prüft Titel und Reihenfolge der übrig gebliebenen ersten Spalte.
Verifikation: **72 fokussierte Format-Tests**, Ruff, Python-Compile und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 788: Ungültige UTF-8-Texte im Format-Selector abweisen

Tabellenkeys und Labels mit unpaired Unicode-Surrogaten passierten bisher die
NUL-Prüfung. Beim Einfügen in GTK-ComboBox oder GLib-Markup entstand dadurch
`UnicodeEncodeError`; die Einstellungsseite konnte beim Aufbau abbrechen.

`_valid_text()` prüft jetzt NUL-freie, UTF-8-kodierbare Texte. Die Prüfung gilt
für Tabellenkey/-label sowie relevante Spalten-, Options-, Einheiten-,
Default-, Beschreibung- und Tooltiptexte. Ungültige Labels fallen auf den
Key zurück; ungültige Keys werden verworfen.

Regression deckt Key, Label und die Validierungsfunktion ab. Verifikation:
**75 fokussierte Format-Tests**, Ruff, Python-Compile und Diff-Check bestanden;
keine Settings-Fenster gestartet.

## Runde 789: Forecast-Selector übernimmt UTF-8-Validierung

Der Prognosen-Selector hatte noch die alte NUL-/Typprüfung. Tabellenkey oder
Label mit unpaired Unicode-Surrogat konnten beim ComboBox-/Markup-Aufbau
`UnicodeEncodeError` auslösen, obwohl der Format-Selector bereits gehärtet war.

`ForecastTableSelector` verwendet jetzt den gemeinsamen `_valid_text()`-Validator
des Format-Selectors für Key und Label. Ungültige Keys werden verworfen,
ungültige Labels fallen auf den validierten Key zurück.

Regression deckt beide Textpfade ab. Verifikation: **21 fokussierte
Forecast-Tests**, Ruff, Python-Compile und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 790: Pool-Listen bleiben nach Schreibfehler reaktiv

`DynamicSeriesList` erbte die rohe `List.list_changed()`-Implementierung. Wenn
das JSON-Backend beim Speichern einen Fehler meldete, blieb
`JSONSettingsBackend._saving` auf `True`. Nachfolgende externe
Account-Änderungen wurden dadurch vom Listener ignoriert, bis das Widget neu
geladen wurde.

Die Liste baut ihre Zeilen nun selbst in `list_changed()` auf, setzt `_saving`
bei Schreibfehlern zurück und aktualisiert die Button-Sensitivität. Eine
Regression erzwingt einen fehlgeschlagenen Pool-Write und prüft anschließend,
dass derselbe Listener eine externe Änderung noch einliest.

Verifikation: **26 fokussierte Dynamic-Series-Tests**, Ruff, Python-Compile und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 791: Fast-Mode-Icons bei fehlerhaftem Schema robust laden

Unpaired Unicode-Surrogate in einem Icon-Label konnten beim Einfügen in den
GTK-`ListStore` einen `UnicodeEncodeError` auslösen und die Einstellungsseite
abbrechen. Außerdem begrenzte die alte Schleife die ersten 32 rohen
Schemaeinträge statt 32 gültiger Icons; viele ungültige Einträge konnten damit
gültige spätere Icons verstecken.

Der Selector prüft Label und Dateiname jetzt auf UTF-8-Kodierbarkeit und zählt
das Limit erst nach erfolgreicher Validierung. Eine Regression deckt beide
Fälle ab: fehlerhafter Text wird übersprungen, ein gültiges Icon nach 32
ungültigen Einträgen bleibt sichtbar.

Verifikation: **10 fokussierte Fast-Mode-Icon-Tests**, Ruff, Python-Compile und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 792: Hilfe-Seite gegen ungültige GTK-Texte härten

Ein unpaired Unicode-Surrogate in einem Schema-Titel ließ den Help-Expander
beim direkten GTK-Label-Aufbau mit `UnicodeEncodeError` abbrechen. Derselbe
Fehlerpfad war bei Beschreibungen und anderen Markup-Texten möglich; NUL-Bytes
waren ebenfalls nicht für GTK-Markup bereinigt.

`_markup()` ersetzt NUL-Bytes und nicht kodierbare Zeichen jetzt sicher. Der
Expander-Titel erhält dieselbe UTF-8-Bereinigung vor dem GTK-Konstruktor. Eine
Regression baut die Seite mit beschädigtem Titel und Beschreibung vollständig
auf und prüft den bereinigten Markup-Text.

Verifikation: **14 fokussierte Help-Page-Tests**, Ruff, Python-Compile und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 793: Leisten-Schema vor ungültigen GTK-Texten schützen

Eine unpaired Unicode-Surrogate in einer Leisten-Spaltenüberschrift ließ den
GTK-TreeView-Aufbau abbrechen. Derselbe Eintrittspfad bestand für
Optionslabels, String-Optionswerte, Einheiten sowie Beschreibung und Tooltip.

`_panel_text_valid()` prüft diese Schema-Texte jetzt auf NUL-Freiheit und
UTF-8-Kodierbarkeit. Ungültige Spalten/Optionen werden verworfen; Beschreibung
und Tooltip fallen auf sichere leere Werte zurück. Die Wertkopie,
Wertreihenfolge und Editor-Spalten bleiben unverändert.

Regression deckt Validator, ungültige Spalten und Widget-Aufbau ab.
Verifikation: **112 fokussierte Panel-Tests**, Ruff, Python-Compile,
Diff-Check sowie 3000-Fälle-Schema-Fuzz bestanden; keine Settings-Fenster
gestartet.

## Runde 794: Leere Formatspalten verwerfen

Der Format-Selector akzeptierte bisher leere Spalten-IDs oder -Titel. Solche
Definitionen erzeugten zwar keinen unmittelbaren GTK-Crash, aber nicht
adressierbare bzw. unsichtbare Formatfelder und konnten beim JSON-Speichern
leere Schlüssel verwenden.

`_BoundFormatList` verlangt für Spalten-ID und -Titel jetzt nichtleere,
NUL-freie, UTF-8-kodierbare Texte. Beide Fälle sind als Regression abgedeckt.
Der bestehende 500-Fälle-Konstruktions-Fuzz blieb ebenfalls fehlerfrei.

Verifikation: **77 fokussierte Format-Selector-Tests**, Ruff, Python-Compile und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 796: Leisten-Quellzuordnung geprüft

Die Panel-Quellzuordnung in `applet.js` wurde gegen die 52 konfigurierbaren
Quellen (0 bis 51) geprüft. `_panelItems`, Quellfenster, Prozent-/Resetpfade,
Textwerte, Credits, Routing und Status besitzen jeweils einen Renderpfad;
Duplikate und ungültige Slotwerte werden vor der Darstellung verworfen.

Kein neuer belastbarer Fehler. Der fokussierte Runtime-Filter deckt 62
Panel-/Quelltests ab; die vollständige headless Installationsprüfung meldete
496 von 496 Runtime-Tests erfolgreich. Keine Codeänderung erforderlich.

## Runde 797: Profil-Layout validiert Account-Label vor Seiteneffekten

`ensure_profile_layout()` erzeugte bei einem nicht-stringförmigen
`Account.label` zunächst Profilverzeichnisse und scheiterte erst beim
`json.dumps()` mit `TypeError`. Damit blieb Teilzustand trotz ungültiger
Eingabe zurück.

`layout_for_account()` lehnt leere oder nicht-stringförmige Labels jetzt vor
jeder Verzeichnisprüfung/-erzeugung mit `ValueError` ab. Regression prüft
Fehler und das Ausbleiben des Profilverzeichnisses.

Verifikation: **26 fokussierte Profile-Layout-Tests**, **90 Profile-Layout- und
-Migrations-Tests**, Ruff, Python-Compile und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 798: Profil-Job nicht durch zweiten Worker übernehmen

`run_profile_job()` behandelte das Ergebnis eines fehlgeschlagenen
`expected_status="queued"`-Claims mit bereits vorhandenem Status
`running` trotzdem als eigenen Start. Ein zweiter Worker konnte dadurch
denselben Login parallel ausführen und Job-Metadaten beziehungsweise
Accountzustand überschreiben.

Der Worker akzeptiert den Übergang zu `running` jetzt nur, wenn die gespeicherte
`worker_pid` der eigenen Prozess-ID entspricht. Fremde laufende Jobs werden mit
Status `1` verlassen; Manifest und erster Worker bleiben unverändert.
Regression deckt den Fremd-PID-Pfad ab und stellt sicher, dass kein Login
gestartet wird.

Verifikation: **86 fokussierte Profile-Job-Tests**, **135 Profile-Job-, CLI-
und Login-Tests**, Ruff, Python-Compile und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 799: Codex-Befehl auf gültiges UTF-8 begrenzen

`profile_login._validate_codex_command()` prüfte bisher nur Typ, Leerstring
und Steuerzeichen. Ein unpaired Unicode-Surrogat passierte diese Prüfung und
führte beim echten `subprocess.Popen()` zu einem rohen `UnicodeEncodeError`
statt zu kontrollierter Eingabeablehnung.

Der Befehlsname muss jetzt zusätzlich UTF-8-kodierbar sein; ungültige Werte
ergeben weiterhin `DeviceLoginError("codex command is invalid")` vor jedem
Runner- oder Prozessstart. Regression ergänzt den Surrogatfall.

Verifikation: **46 fokussierte Profile-Login-Tests**, **132 Profile-Login-
und Job-Tests**, Ruff, Python-Compile und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 800: `created_paths` vor Verzeichnis-I/O validieren

`private_io.ensure_private_directory()` akzeptierte für den optionalen
`created_paths`-Transaktionscontainer beliebige Typen. Bei einem Tupel oder
Dictionary wurden bereits Parent-Verzeichnisse erzeugt, bevor ein rohes
`AttributeError` auf `.append()` den Lauf abbrach; Partial-State blieb zurück.

Der Container muss jetzt vor jeder Pfadprüfung und jedem I/O eine `list` sein.
Ungültige Werte liefern `ValueError("created_paths is invalid")`; Regression
prüft zusätzlich, dass weder Ziel noch Parent angelegt werden.

Verifikation: **50 fokussierte Private-IO-Tests**, **411 Private-IO-, Layout-,
Migrations- und State-Tests**, Ruff, Python-Compile und Diff-Check bestanden;
keine Settings-Fenster gestartet.

## Runde 801: Account-Lock-I/O-Fehler kontrolliert melden

`account_lock()` kapselte Fehler beim Öffnen der Lockdatei, aber nicht den
`os.fchmod()`-Fehler beim Sichern ihrer Rechte. Ein solcher Setup-Fehler leakte
als rohes `OSError` statt als erwarteter `AccountLockError`.

Der Rechte-Setup-Pfad wandelt `OSError` jetzt in
`AccountLockError("could not secure account lock")` mit Cause um. Regression
prüft Fehlertyp, Meldung und Ursache vor dem Lock-Body.

Verifikation: **16 fokussierte Account-Lock-Tests**, **267 Account-Lock-,
Config-, Profile-Job- und Profile-Login-Tests**, Ruff, Python-Compile und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 802: Migrations-Rollback auf echte Manifestziele begrenzen

`rollback_auth_migration()` vertraute bisher jedem absoluten `target`-Pfad
aus einem Manifest. Ein handgebautes oder beschädigtes Manifest konnte damit
eine beliebige private Datei löschen, sobald ihr Hash angegeben war. Die
Rollback-Prüfung ignorierte außerdem Manifest-Rechte und das Schema.

Rollback akzeptiert jetzt nur Schema 1, private Manifestdateien, vollständige
`source`-/SHA-256-Felder und Ziele unter `.../codex-home/auth.json`. Ein
Regressionstest reproduziert den fremden Zielpfad und stellt sicher, dass die
Datei erhalten bleibt; ein weiterer prüft gruppenlesbare Manifestdateien.

Verifikation: **66 fokussierte Profile-Migrations-Tests**, Ruff,
Python-Compile und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 803: Doppelte Reset-Kanonik vollständig abgleichen

`parse_usage_resets()` erkannte ein vollständiges Top-Level-Resetobjekt, prüfte
ein zusätzliches vollständiges `usage_resets`-Objekt aber nur über dessen
`available`-Wert. Widersprüchliche Werte für `known` oder
`redeem_capability` wurden dadurch als gültiger Zustand übernommen; ein
malformierter boolescher Wert konnte ebenfalls durchrutschen.

Vollständige doppelte Kanonik wird jetzt erneut als `UsageResetState` validiert
und muss exakt dem bereits erkannten Zustand entsprechen. Abweichungen oder
ungültige Felder fallen geschlossen auf den unbekannten Zustand zurück;
partielle Legacy-Mappings behalten bisherige Kompatibilität. Regression deckt
abweichendes `known`, abweichende Fähigkeit und ungültigen Typ ab.

Verifikation: **20 fokussierte Usage-Reset-Tests**, **376 Reset-, State-,
Model- und Snapshot-Tests**, Ruff, Python-Compile und Diff-Check bestanden;
keine Settings-Fenster gestartet.

## Runde 804: Fehlerhafte Candidate-Iteratoren kontrolliert verwerfen

`identity._usable_candidates()` fing bei der begrenzten Iterator-Erstellung
nur `TypeError`. Ein formal iterierbarer, aber fehlerhafter Container, dessen
`__iter__()` `ValueError` wirft, ließ alle drei Identitäts-/Plan-Helfer roh
abbrechen.

Der Guard behandelt solche `ValueError` jetzt wie andere ungültige Candidate-
Container und liefert eine leere Menge. Regression deckt den fehlerhaften
Iterator über Identitäts-, Plan- und Konsistenzpfad ab.

Verifikation: **29 fokussierte Identity-Tests**, **466 Identity-, Bridge- und
Browser-Tests**, Ruff, Python-Compile und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 805: Health-Clock gegen Fremdtyp-Rückgabe sichern

`record_health_event()` validierte `now` zwar als `datetime`, übernahm aber
blind das Ergebnis von `now.astimezone(UTC)`. Eine manipulierte
`datetime`-Subclass konnte dort einen Fremdtyp zurückgeben; der anschließende
`isoformat()`-Aufruf brach die Health-Telemetrie mit `AttributeError` ab.

Der normalisierte Clock-Wert wird jetzt nur übernommen, wenn er ein exakter
`datetime`-Wert ist; sonst bleibt die sichere aktuelle UTC-Zeit aktiv.
Regression deckt die Fremdtyp-Rückgabe ab.

Verifikation: **33 fokussierte Health-Tests**, **306 Health-, State- und
CI-Workflow-Tests**, Ruff, Python-Compile und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 806: Consumption-Clock gegen manipulierte Datetime-Subklassen sichern

`calculate_consumption()` validierte `now` nur als timezone-aware
`datetime` und führte danach direkt Zeitrechnungen mit dem Objekt aus. Eine
`datetime`-Subclass konnte `__sub__()` überschreiben und die Berechnung damit
in einen rohen `TypeError` laufen lassen; auch eine überschriebene
`timestamp()`-Methode durfte nicht in den Normalisierungspfad gelangen.

Nach der Awareness-Prüfung wird `now` jetzt über die Basisklassenmethode
`datetime.timestamp()` in einen exakten UTC-`datetime` normalisiert. Nicht
darstellbare Werte liefern kontrolliert `ValueError("now is out of range")`;
anschließende Fenster-, Delta- und EMA-Arithmetik arbeitet nur noch mit dem
normalisierten Basistyp. Regression deckt manipulierte Subclass-Arithmetik
und Timestamp-Dispatch ab.

Verifikation: **42 fokussierte Consumption-Tests**, **273 Consumption- und
abhängige CLI-, History- und Integrationstests**, Ruff, Python-Compile und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 807: Integer-Subklassen in Usage-Limit-Fenstern fail-closed behandeln

`usage_limits._strict_int()` akzeptierte bisher jede `int`-Subclass. Eine
manipulierte `windowDurationMins`-Subclass konnte dadurch ihre Multiplikation
überschreiben; der App-Server-Parser brach anschließend mit einem rohen
`TypeError` statt mit einem verworfenen Fenster ab.

Die Prüfung akzeptiert jetzt ausschließlich den exakten Built-in-Typ
`int`. Nicht vertrauenswürdige Integer-Subklassen werden wie andere ungültige
Limitwerte verworfen; Boolean-Werte bleiben weiterhin ausgeschlossen.
Regression deckt den App-Server-Pfad mit überschriebenem `__mul__()` und
`__mod__()` ab.

Verifikation: **126 fokussierte Usage-Limit-Tests**, **417 Usage-Limit-, Direct-
und App-Server-Tests**, Ruff, Python-Compile und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 808: Fensteridentität gegen manipulierte Dauerwerte härten

`LimitWindow.has_known_identity` akzeptierte bisher `int`-Subklassen und
führte für unbekannte Fenster deren überschriebenen Operator `%` aus. Eine
fehlerhafte Dauer konnte dadurch die Modellprüfung mit einem rohen Fremd-
Exception abbrechen, statt die Fensteridentität abzulehnen.

Die Identitätsprüfung akzeptiert jetzt nur den exakten Built-in-Typ `int`.
Damit bleiben Boolean-, Float-, String- und Integer-Subclass-Dauern
fail-closed ungültig, bevor Kanonisierung oder Fenstervergleich rechnen.
Regression deckt eine Dauer-Subclass mit fehlerhaftem Modulo-Operator ab.

Verifikation: **33 fokussierte Model-Tests**, **496 Model-, Usage-Limit-,
State- und Render-Tests**, Ruff, Python-Compile und Diff-Check bestanden;
keine Settings-Fenster gestartet.

## Runde 809: Render-Iteratoren kontrolliert abbrechen

`render._bounded_usage_list()` und `_bounded_account_list()` kapselten nur
`TypeError` beim begrenzten Einlesen ihrer Iteratoren. Ein formal iterierbarer
Container, dessen `__next__()` `ValueError` wirft, leakte damit den Fremdfehler
aus JSON-, Tabellen- und Account-Ausgabe.

Beide gemeinsamen Eingangsgrenzen behandeln solche `ValueError` jetzt wie
andere ungültige Record-Container und liefern die dokumentierten
`ValueError("… records are invalid")`-Fehler. Regression deckt Usage- und
Account-Iteratoren inklusive JSON- und Tabellenpfad ab.

Verifikation: **68 fokussierte Render-Tests**, **405 Render-, Scheduler- und
CLI-Tests**, Ruff, Python-Compile und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 810: Scheduler-Usage-Iteratoren fail-closed prüfen

`_watch_cycle_is_healthy()` und `_usage_map_for_accounts()` begrenzten ihre
Usage-Iteratoren zwar per `islice`, kapselten aber keine Fehler aus einem
formal iterierbaren Container. Ein `ValueError` aus `__next__()` konnte damit
Watchdog- oder Usage-Zuordnungspfad roh abbrechen.

Beide Validierungspfade fangen solche `TypeError`-/`ValueError`-Fehler jetzt
direkt am Iterator-Eingang und liefern kontrolliert `False` beziehungsweise
`None`. Die bestehende Overflow-Grenze bleibt unverändert und liest nicht
über erwartete Anzahl plus einen Prüfwert hinaus. Regression deckt beide
Pfade mit demselben fehlerhaften Usage-Iterator ab.

Verifikation: **221 fokussierte Scheduler-Tests**, **601 Scheduler-, Bridge-
und CLI-Tests**, Ruff, Python-Compile und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 811: Reset-Anzahl gegen Integer-Subklassen absichern

`UsageResetState` und die Legacy-Zweige von `parse_usage_resets()` akzeptierten
`int`-Subklassen. Ein manipuliertes Vergleichsverhalten konnte die
Bereichsprüfung für Reset-Anzahlen mit einem rohen Fremd-Exception abbrechen,
statt den Zustand als unbekannt zu verwerfen.

Alle Reset-Anzahl-Grenzen akzeptieren jetzt ausschließlich den exakten
Built-in-Typ `int`; Boolean- und sonstige Integer-Subklassen bleiben ungültig.
Regression prüft Konstruktor und Legacy-Parser mit überschriebenem
Vergleichsoperator.

Verifikation: **21 fokussierte Usage-Reset-Tests**, **451 Reset-, Model-,
Usage-Limit- und State-Tests**, Ruff, Python-Compile und Diff-Check bestanden;
keine Settings-Fenster gestartet.

## Runde 812: App-Server-Integer nur als Built-in akzeptieren

`app_server._strict_int()` akzeptierte bisher beliebige `int`-Subklassen.
Manipulierte Vergleichs- oder Rechenoperatoren konnten dadurch die
Used-Percent-/Fensterprüfung mit einem rohen Fremd-Exception abbrechen.

Die Grenze akzeptiert jetzt ausschließlich den exakten Built-in-Typ `int`.
Boolean-Werte und Integer-Subklassen werden wie andere ungültige
App-Server-Payloadwerte verworfen. Regression deckt direkten Strict-Int- und
Used-Percent-Pfad ab.

Verifikation: **109 fokussierte App-Server-Tests**, **256 App-Server-,
Usage-Limit- und Usage-Reset-Tests**, Ruff, Python-Compile und Diff-Check
bestanden; keine Settings-Fenster gestartet.

## Ergänzung 2026-08-23: Delta-Formatierung für Token- und Creditverbrauch

`Tokendelta` formatiert jetzt nicht nur die auswählbaren Leisten-Deltas,
sondern auch die normale Tokenverbrauch-Ausgabe in Hover- und Klickmenü. Die
Schwelle ist dort eindeutig ein Verbrauchswert: Formatierung wird ab
`Verbrauch >= Schwelle` aktiv; die bisherige Restlimit-Richtung bleibt nur für
Nicht-Delta-Tabellen bestehen.

Für Creditverbrauch gibt es unter `Formatierungen` die neue, unabhängige
Tabelle `Δ Creditverbrauch`. Sie besitzt dieselben Format-, Null- und
Hover/Klick-Felder wie `Tokendelta` plus `Dynamisch`. Die dynamische Prüfung
projiziert Creditverbrauch auf den kürzeren Wert aus Reset-Horizont und
Fensterdauer und markiert nur bei Erreichen des verbleibenden Creditlimits.
Alte Creditverbrauch-Abfragen und Leistenquelle `Creditverbrauch` bleiben
kompatibel; fehlende oder ungültige Werte fallen weiter auf `–`/Ausblenden.

Verifikation: **499 Node-Tests**, **3.263 Python-Tests** (1 übersprungen),
JSON-Parse, Python-Compile und Node-Syntaxcheck bestanden; keine
Settings-Fenster gestartet.

## Runde 813: Private-I/O-Grenzen gegen numerische Subklassen härten

`private_io` akzeptierte bei Lock-Timeout, Bytebudget und Dateimodus beliebige
`int`-/`float`-Subklassen. Deren überschreibbare Konvertierungs-, Vergleichs-
oder Bitoperatoren konnten vor der eigentlichen Dateiprüfung rohe
Fremdfehler auslösen. Die drei Grenzen akzeptieren jetzt ausschließlich
Built-in-`int` beziehungsweise Built-in-`float`; ungültige Subklassen werden
kontrolliert als `ValueError` abgewiesen.

Regression deckt Timeout, `max_bytes` und Dateimodus mit absichtlich fehlerhaften
numerischen Subklassen ab. Verifikation: **538 direkte Private-I/O-, State-,
Service-, Spark-Health- und Config-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 814: Verbrauchsparameter gegen Integer-Subklassen absichern

`consumption_lookback_seconds()` und `calculate_consumption()` akzeptierten
bei Menge, Baseline, Stale-Grenze und Lückenlimit beliebige `int`-Subklassen.
Vergleiche und Multiplikationen konnten dadurch überschreibbare Operatoren
ausführen und rohe Fremdfehler in CLI- oder Integrationspfade leaken.
Alle fünf Eingangsgrenzen akzeptieren jetzt ausschließlich Built-in-`int`.

Regression prüft jede Grenze mit einer absichtlich fehlerhaften Integer-
Subclass. Verifikation: **188 Consumption-, CLI- und Integration-Entrypoint-
Tests**, Ruff und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 815: History-Zeitstempel und Samples strikt numerisch prüfen

`history.py` akzeptierte bei Fensterdauer, `used_percent` und Millisekunden-
Konvertierung numerische Subklassen. Überschriebene Vergleichs-, Divisions-
oder Float-Konvertierungsoperatoren konnten dadurch rohe Fremdfehler aus
History-, CLI- und Scheduler-Pfaden auslösen. Auch eine Credit-Fensterdauer aus
einer Subclass wurde vor dem Fallback verglichen.

Die Grenzen akzeptieren jetzt nur Built-in-`int` beziehungsweise Built-in-
`float`; ungültige Credit-Dauern fallen kontrolliert auf das 30-Tage-Fenster
zurück. Regression deckt Sample-, Millisekunden- und Creditpfad ab.

Verifikation: **344 History-, History-CLI-, Scheduler- und Integration-
Entrypoint-Tests**, Ruff und Diff-Check bestanden; keine Settings-Fenster
gestartet.

## Runde 816: Model-Zahlen normalisieren und fail-closed serialisieren

`models.py` akzeptierte bei `UsagePool.window_for_duration()` Integer-
Subklassen. Zusätzlich reichten `_finite_number()`, `_safe_int()` und
`_safe_number()` geprüfte, aber weiterhin fremde numerische Objekte zurück.
Überschriebene Operatoren konnten dadurch Vergleiche, Serialisierung oder
nachgelagerte JSON-Ausgabe beeinflussen.

Alle numerischen Model-Grenzen akzeptieren jetzt ausschließlich Built-in-
`int`/`float`; abgewiesene Werte werden als ungültig behandelt und nicht in
Payloads zurückgegeben. Regression deckt Dauervergleich, Prozentberechnung und
JSON-sichere Account-Ausgabe ab.

Verifikation: **535 Model-, Usage-Limit-, Render- und State-Tests**, Ruff und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 817: Spark-Health-Grenzen gegen primitive Subklassen sichern

`spark_health.py` akzeptierte bei `max_age_seconds`, Backend-Account-ID und
State noch `int`-/`str`-Subklassen. Überschriebene Vergleichs-,
`.encode()`- oder Gleichheitsoperatoren konnten dadurch rohe Fremdfehler vor
der Health-Prüfung oder beim Schreiben auslösen.

Die öffentlichen Grenzen akzeptieren jetzt ausschließlich Built-in-`int` und
`str`; ungültige Werte werden kontrolliert verworfen. Regression deckt alle
drei Eingänge ab.

Verifikation: **302 Spark-Health-, Routing- und CLI-Tests**, Ruff und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 818: Routing-Numerik gegen Subklassen absichern

`routing.py` akzeptierte bei maximalem Usage-Alter, Fensterdauer,
Restprozentsatz und Credit-Limits noch numerische Subklassen. Dadurch konnten
Hash-, Vergleichs-, Rechen- oder Float-Konvertierungsoperatoren in Routing-
Entscheidungen und Policy-Validierung gelangen.

Die vier numerischen Grenzen akzeptieren jetzt ausschließlich Built-in-`int`
und `float`; ungültige Werte führen kontrolliert zu `unknown` beziehungsweise
`ValueError`. Regression deckt alle vier Eingänge ab.

Verifikation: **402 Routing-, Spark-Health- und CLI-Tests**, Ruff und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 819: Usage-Limit-Prozente nur aus Built-in-Zahlen bilden

`usage_limits.py` akzeptierte bei `used_percent` weiterhin `int`- und
`float`-Subklassen. Die anschließende `float()`-Konvertierung konnte dadurch
einen fremden Konvertierungs-Hook ausführen und den Parser mit einem rohen
Fehler abbrechen lassen.

`_percent()` akzeptiert jetzt ausschließlich Built-in-`int` und `float`.
Numerische Subklassen werden vor jeder Konvertierung verworfen. Regression
deckt den Hook-Aufruf direkt sowie alle Parser-Aufrufer ab.

Verifikation: **886 Usage-Limit-, App-Server-, Direct-, State-, Render- und
Routing-Tests**, Ruff und Diff-Check bestanden; keine Settings-Fenster
gestartet.

## Runde 820: Usage-Reset-Mapping-Callbacks fail-closed behandeln

`parse_usage_resets()` akzeptierte `Mapping`-Objekte, ließ aber Exceptions aus
deren `__contains__()`, `get()` oder `__getitem__()` nach außen leaken. Ein
formal gültiges, fehlerhaftes DTO konnte dadurch Direct-, App-Server-, Bridge-
oder State-Verarbeitung abbrechen.

Der Mapping-Parser läuft jetzt unter einer Exception-Grenze und liefert bei
Callback-Fehlern den unbekannten Reset-Zustand. Regression deckt ein
explodierendes Mapping direkt sowie alle vier Aufruferpfade ab.

Verifikation: **22 fokussierte Usage-Reset-Tests**, **848 Reset-, Direct-,
App-Server-, Bridge- und State-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 821: State-Snapshot-Numerik auf Built-in-Typen begrenzen

`state.py` akzeptierte bei Generationswerten, Fensterdauern und Snapshot-
Zahlen noch `int`-/`float`-Subklassen. Überschriebene Vergleichs- oder
`float()`-Operatoren konnten dadurch Cache-, Expiry- und State-Generation-
Pfade mit rohen Fremdfehlern abbrechen.

Alle sechs State-Numerikgrenzen akzeptieren jetzt ausschließlich Built-in-
`int`/`float`; ungültige Werte werden kontrolliert verworfen. Regression deckt
direkte Helper, State-Generation und die Bridge-/Scheduler-/CLI-Aufrufer ab.

Verifikation: **272 State-Tests**, **873 State-, Bridge-, Scheduler- und
CLI-Tests**, Ruff und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 822: Direct-Numerik vor Fremdoperatoren schützen

`direct.py` akzeptierte bei Timeout-/Deadline-Werten, HTTP-Status, WHAM-
Fensterdaten, Credits, JWT-Ablaufzeiten und Antwortsignaturen noch numerische
Subklassen. `float()`- oder Vergleichsoperatoren aus solchen Werten konnten
Fetch-, Auth- und Stabilitätsprüfungen mit rohen Exceptions abbrechen.

Alle Direct-Numerikgrenzen akzeptieren jetzt ausschließlich Built-in-`int`/
`float` (Credits zusätzlich Built-in-`str`). Ungültige Werte werden kontrolliert
verworfen. Regression deckt alle zentralen Normalisierer sowie HTTP-/JWT-
Aufrufer ab.

Verifikation: **185 Direct-Tests**, **448 Direct-, Scheduler- und
Browser-Diagnose-Tests**, Ruff und Diff-Check bestanden; keine Settings-Fenster
gestartet.

## Runde 823: Browser-Numerik vor Fremdoperatoren schützen

`browser.py` akzeptierte bei Timeout, Diagnosewerten und HTTP-Status noch
`int`-/`float`-Subklassen. Überschriebene Vergleichs- oder Konvertierungs-
operatoren konnten Profil-, Diagnose- und Statuspfade mit rohen Exceptions
abbrechen oder fremde Zahlen in Diagnose-DTOs zurückgeben.

Die drei Browser-Grenzen akzeptieren jetzt nur Built-in-`int`/`float`; fremde
Zahlen werden kontrolliert verworfen beziehungsweise als Typname diagnostisch
ausgegeben. Regression deckt direkte Helper und Browser-/Scheduler-/CLI-
Aufrufer ab.

Verifikation: **178 fokussierte Browser-Tests**, **516 Browser-, Scheduler- und
CLI-Tests**, Ruff und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 824: Integration-Snapshot-Numerik strikt kanonisieren

`integration_snapshot.py` akzeptierte bei Fensterdauer, Restprozent, Kosten,
Sample-/Forecast-Werten, Schema-Version und Reset-Anzahl noch numerische
Subklassen. Fremde Vergleichs- oder `float()`-Operatoren konnten dadurch die
Snapshot-Kanonisierung und den Export mit rohen Exceptions abbrechen.

Alle Snapshot-Numerikgrenzen akzeptieren jetzt ausschließlich Built-in-`int`
und `float`; ungültige Werte werden als `IntegrationInvalidSource` verworfen.
Regression deckt Projektion, Kanonisierung und die Integration-Entrypoint-/
Installer-Aufrufer ab.

Verifikation: **54 fokussierte Snapshot-Tests**, **233 Snapshot-, Entrypoint-
und Installer-Tests**, Ruff und Diff-Check bestanden; keine Settings-Fenster
gestartet.

## Runde 825: Config-Integergrenzen strikt prüfen

`config.py` akzeptierte bei `restore_account(index)` und dem zentralen
`_strict_int()` noch Integer-Subklassen. Fremde Vergleichsoperatoren konnten
Restore-Position oder Intervallvalidierung mit rohen Exceptions abbrechen.

Beide Grenzen akzeptieren jetzt ausschließlich Built-in-`int`; ungültige Werte
werden kontrolliert als Konfigurationsfehler verworfen. Regression deckt
direkte Helper sowie CLI-, Scheduler- und Service-Aufrufer ab.

Verifikation: **120 fokussierte Config-Tests**, **527 Config-, CLI-, Scheduler-
und Service-Tests**, Ruff und Diff-Check bestanden; keine Settings-Fenster
gestartet.

## Runde 826: Extractor-Numerik vor Subklassen-Hooks abschirmen

`extractor.py` akzeptierte bei Zeitstempeln, relativen Resetzeiten und der
allgemeinen Zahlenkonvertierung noch `int`-/`float`-Subklassen. Überschriebene
`__float__`-Hooks konnten dadurch Zeit-, Limit- und Resetpfade mit rohen
Exceptions abbrechen oder fremde Zahlenwerte einschleusen.

Die Extraktor-Grenzen akzeptieren jetzt ausschließlich Built-in-`int`/`float`;
`_finite_float()` ist der gemeinsame fail-closed Konvertierungspunkt. Regression
deckt Float- und Integer-Subklassen mit absichtlich fehlerhaften Hooks sowie
Zeitstempel-, Reset- und Prozentpfade ab.

Verifikation: **200 fokussierte Extractor-Tests**, **534 Extractor-, Browser-,
Identity- und Usage-Limit-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 827: Scheduler-Numerik strikt validieren

`scheduler.py` akzeptierte bei Prozent-/Limitwerten, Fensterdauern und
Watch-Intervallen noch numerische Subklassen. Überschriebene Konvertierungs-,
Vergleichs- oder Additionsoperatoren konnten Stabilisierung, Resetprüfung und
Watch-Zyklen mit rohen Exceptions abbrechen.

Die Scheduler-Grenzen akzeptieren jetzt ausschließlich Built-in-`int`/`float`.
`fetch_all()` und `watch()` teilen dieselbe Mindestintervallprüfung; ungültige
Konfigurationswerte werden vor jeder weiteren Verarbeitung verworfen.
Regression deckt direkte Numerik-Helper, Reset-Fallback und Watch-/Fetch-
Einstiegspunkte ab.

Verifikation: **225 fokussierte Scheduler-Tests**, **489 Scheduler-, CLI-,
Health-, Integration-Entrypoint- und Profile-Job-Tests**, Ruff und Diff-Check
bestanden; keine Settings-Fenster gestartet.

## Runde 828: Render-Numerik vor Fremd-Konvertierung schützen

`render.py` akzeptierte bei Zahlenformatierung und Prozentanzeige noch
`int`-/`float`-Subklassen. Überschriebene `__float__`-Hooks konnten dadurch
Darstellung und Restprozentberechnung mit rohen Exceptions abbrechen.

`_fmt_number()` und `_is_finite_number()` akzeptieren jetzt ausschließlich
Built-in-`int`/`float`; ungültige Werte bleiben bei `-` beziehungsweise
unsichtbar. Regression deckt beide Subklassen sowie Tabellen-/CLI-Anzeige ab.

Verifikation: **70 fokussierte Render-Tests**, **187 Render- und CLI-Tests**,
Ruff und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 829: Bridge-Numerik strikt an Trust-Grenzen prüfen

`bridge.py` akzeptierte bei API-Status, Request-Sequenzen, Intervallen,
Debugzahlen und Serverports noch numerische Subklassen. Fremde Vergleichs-,
Konvertierungs- oder Intervalloperatoren konnten ingest-, Sanitizer-,
Frische- und Serverpfade mit rohen Exceptions abbrechen.

Alle Bridge-Grenzen akzeptieren jetzt ausschließlich Built-in-`int`/`float`;
Frischeberechnung nutzt denselben validierten Intervallwert. Ungültige Werte
werden vor Vergleich, `int()` oder Zeitfensterarithmetik verworfen. Regression
deckt Ingest-Metadaten, Debug-Sanitizer, Port-/Intervallvalidierung und
Browser-/Authenticated-Frische ab.

Verifikation: **266 fokussierte Bridge-Tests**, **531 Bridge-, CLI-, Config-
und Integration-Entrypoint-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 830: Service-PID strikt prüfen

`service.py` akzeptierte beim Beenden gebundener `systemctl`-Prozesse noch
Integer-Subklassen. Ein überschriebenes Vergleichsoperator konnte den
Aufräumpfad beim Kill-Handling mit einer rohen Exception abbrechen.

Die PID-Grenze akzeptiert jetzt ausschließlich Built-in-`int`; fremde oder
boolesche PIDs werden wie unbekannte PIDs verworfen, der normale Prozess-Kill
läuft trotzdem weiter. Regression deckt Boolean- und Integer-Subklassen sowie
Service-/CLI-/Integration-Aufrufer ab.

Verifikation: **70 fokussierte Service-Tests**, **301 Service-, CLI-,
Integration-Entrypoint- und Profile-Job-Tests**, Ruff und Diff-Check bestanden;
keine Settings-Fenster gestartet.

## Runde 831: Device-Login-Prozessgrenzen strikt prüfen

`profile_login.py` akzeptierte beim Device-Login-Timeout und beim Beenden
gebundener Prozesse noch Integer-Subklassen. Fremde Vergleichsoperatoren
konnten Validierung und Prozessgruppen-Kill mit rohen Exceptions oder falscher
Signalweitergabe abbrechen.

Timeout und PID akzeptieren jetzt ausschließlich Built-in-`int`; Boolean- und
Subklassenwerte werden kontrolliert verworfen, danach bleibt der normale
Prozess-Killpfad aktiv. Regression deckt Login-, Cleanup-, CLI- und Job-Aufrufer
ab.

Verifikation: **48 fokussierte Profile-Login-Tests**, **138 Profile-Login-,
Profile-CLI- und Profile-Job-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 832: Reaktivierungsprozessgrenzen strikt validieren

`reactivate.py` akzeptierte bei Browser-Reaktivierungs-Timeouts und beim
Beenden des Login-Prozessgroups noch Integer-Subklassen. Fremde Vergleichs-
operatoren konnten Timeoutprüfung oder Kill-Pfad mit rohen Exceptions und
unerwarteter Signalweitergabe abbrechen.

Timeout und PID akzeptieren jetzt ausschließlich Built-in-`int`; ungültige
Werte werden kontrolliert verworfen, danach läuft der sichere Einzelprozess-
Kill weiter. Regression deckt Reaktivierungs-Timeout, Boolean-/Subklassen-PID
und den vollständigen Reaktivierungspfad ab.

Verifikation: **70 fokussierte Reaktivierungs-Tests**, Ruff und Diff-Check
bestanden; keine Settings-Fenster gestartet.

## Runde 833: Profile-Job-PIDs strikt kanonisieren

`profile_jobs.py` akzeptierte an Worker-Erzeugung, Reaping, Status-/Cancel-
Prüfung und Manifestgrenze noch Integer-Subklassen. Fremde Vergleichsoperatoren
konnten Cleanup, Worker-Signalisierung oder Manifestvalidierung mit rohen
Exceptions abbrechen.

Alle Worker-PID-Grenzen akzeptieren jetzt ausschließlich Built-in-`int`;
ungültige PIDs fallen auf normalen Einzelprozess-Cleanup beziehungsweise
kontrollierte Manifestfehler zurück. Regression deckt Startfehler, Reaping,
Cancel und Manifestvalidierung ab.

Verifikation: **89 fokussierte Profile-Job-Tests**, **93 Profile-Job- und
Profile-CLI-Tests**, Ruff und Diff-Check bestanden; keine Settings-Fenster
gestartet.

## Runde 834: App-Server-Prozessgruppen-PID strikt prüfen

`app_server.py` akzeptierte beim Signalisieren isolierter Prozessgruppen noch
Integer-Subklassen. Ein überschriebenes Vergleichsoperator konnte den Stop-/
Fallbackpfad mit einer rohen Exception abbrechen.

Die PID-Grenze akzeptiert jetzt ausschließlich Built-in-`int`; unbekannte,
boolesche oder fremde PIDs verwenden kontrolliert den Einzelprozess-Fallback.
Regression deckt Subklassen-PID sowie bestehende Stop-/Timeout-/Protocol-
Aufrufer ab.

Verifikation: **110 fokussierte App-Server-Tests**, **762 App-Server-,
Integration-Entrypoint-, Scheduler-, State- und Usage-Limit-Tests**, Ruff und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 835: Integration-Installer-Prozess- und Größenwerte strikt prüfen

`integration_installer.py` akzeptierte bei Preflight-/Builder-PIDs,
Prozessgruppen-IDs und Wheel-Mitgliedsgrößen noch Integer-Subklassen. Fremde
Vergleichsoperatoren konnten Cleanup, Nachfahren-Signalisierung oder
Archivprüfung mit rohen Exceptions abbrechen.

Alle betroffenen Grenzen akzeptieren jetzt ausschließlich Built-in-`int`;
ungültige Werte fallen auf Einzelprozess-Cleanup oder kontrollierte
`IntegrationInstallError`-Pfade zurück. Regression deckt Preflight, Builder,
Gruppen-Cleanup und Wheel-Reader ab.

Verifikation: **155 fokussierte Integration-Installer-Tests**, **28 abhängige
Integration-Entrypoint-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 836: CLI-Numerik an Argumentgrenzen strikt validieren

`cli.py` akzeptierte bei History-`days`, Watch-/Bridge-Intervallen und Ports
noch Integer-Subklassen. Fremde Vergleichsoperatoren konnten CLI-Validierung,
Bridge-Endpoint-Erzeugung oder History-Prune mit rohen Exceptions abbrechen.

History-Tage, Watch-Intervalle, Bridge-Ports und Endpoint-Port akzeptieren jetzt
ausschließlich Built-in-`int`; alle Pfade nutzen kontrollierte Validatoren vor
Arithmetik oder String-Erzeugung. Regression deckt direkte Helper sowie
History-/Watch-/Bridge-CLI-Aufrufer ab.

Verifikation: **118 fokussierte CLI-Tests**, **7 History-CLI-Tests**, Ruff und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 837: Health-Dauerwerte strikt kanonisieren

`health.py` akzeptierte bei `duration_ms` Integer-Subklassen. Dadurch wurden
fremde numerische Werte gespeichert; beim späteren Validieren konnten
überschriebene Vergleichsoperatoren mit rohen Exceptions abbrechen.

Aufzeichnung und Lesen akzeptieren `duration_ms` jetzt ausschließlich als
Built-in-`int`, bevor Clamp oder Bereichsvergleich laufen. Regression prüft
Aufzeichnungs- und Parserpfad mit einer absichtlich fehlerhaften Subklasse.

Verifikation: **34 Health-Tests**, **17 abhängige Scheduler-Health-Tests**,
Ruff und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 838: Attestierungsbaum auf Eigentümerwechsel prüfen

`integration_attestation._release_tree_rows()` prüfte Root- und Dateieigentümer,
akzeptierte aber fremde Eigentümer bei verschachtelten Release-Verzeichnissen.
Damit konnte ein nicht zum laufenden Benutzer gehörender Directory-FD in den
attestierten Baum gelangen.

Root, `DirEntry` und geöffnete Kinder verlangen jetzt durchgängig den aktuellen
Benutzer; fremde Verzeichnisse werden vor Hashbildung fail-closed verworfen.
Regression simuliert einen fremden Eigentümer am geöffneten Unterverzeichnis.

Verifikation: **156 Installer-/Attestierungs-Tests**, **28 Entrypoint-Tests**,
Ruff und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 839: Manifest-Hashes kanonisch validieren

`integration_attestation._valid_hash()` akzeptierte bisher Werte wie führendes
`+`, Whitespace oder Großbuchstaben, weil `int(value, 16)` weiter gefasst ist
als der erzeugte kanonische SHA-256-Text. Besonders der Source-Digest gelangte
damit ungefiltert in die Release-ID.

Die Validierung akzeptiert jetzt ausschließlich Built-in-`str` mit exakt 64
Kleinbuchstaben-Hexzeichen. Regression deckt alle drei nichtkanonischen Formen
ab; keine Manifest- oder Runtime-Reparatur erfolgt.

Verifikation: **187 Installer-/Attestierungs- und Entrypoint-Tests**, Ruff und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 840: RECORD-Digests auf kanonische Base64 begrenzen

`integration_attestation._record_digest()` ließ die Python-Base64-Decodierung
zusätzliche Padding-/Fremdzeichen ignorieren. Ein RECORD-Digest mit angehängtem
`=` oder `!!` wurde dadurch trotz nichtkanonischer Darstellung akzeptiert.

Der Parser verlangt jetzt exakt 43 URL-safe-Base64-Zeichen ohne Padding,
dekodiert mit fester Ergänzung und vergleicht zusätzlich die kanonische
Rekodierung mit dem Eingabewert. Regression deckt gültigen Digest sowie beide
malformten Varianten ab.

Verifikation: **188 Installer-/Attestierungs- und Entrypoint-Tests**, Ruff und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 841: Snapshot-Payload auf Built-in-Bytes begrenzen

`integration_snapshot.publish_schema1_cache()` akzeptierte `bytes`-Subklassen.
Eine fremde `decode()`-Implementierung konnte bereits beim JSON-Lesen rohe
Exceptions auslösen, bevor der kontrollierte Invalid-Source-Pfad griff.

Der Cache-Publisher akzeptiert jetzt ausschließlich Built-in-`bytes` vor JSON-
Parsing oder Dateiarbeit. Regression prüft die fehlerhafte Subklasse und stellt
sicher, dass kein Cache angelegt wird.

Verifikation: **55 Snapshot-Tests**, **28 Integration-Entrypoint-Tests**, Ruff
und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 842: Snapshot-Zeitstempel auf Built-in-Strings begrenzen

`integration_snapshot._canonical_timestamp()` akzeptierte `str`-Subklassen.
Ein überschriebenes `__contains__` konnte die Vorprüfung vor dem kontrollierten
ISO-Parser mit einer rohen Exception abbrechen.

Die Funktion verlangt jetzt Built-in-`str` vor `T`-Suche und Normalisierung.
Regression simuliert den fehlerhaften String-Hook; gültige ISO-Zeitwerte bleiben
unverändert.

Verifikation: **56 Snapshot-Tests**, **28 Integration-Entrypoint-Tests**, Ruff
und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 843: Snapshot-Tokens auf Built-in-Strings begrenzen

`integration_snapshot._canonical_token()` akzeptierte `str`-Subklassen und
rief deren `__len__` vor der ASCII-/Regex-Prüfung auf. Ein fremder Hook konnte
damit den direkten Contract-Helper mit einer rohen Exception abbrechen.

Die Token-Grenze verlangt jetzt Built-in-`str` vor Längen- und Zeichenprüfung.
Regression simuliert den fehlerhaften Längen-Hook; normale Account-/Pool-/Commit-
Tokens bleiben unverändert.

Verifikation: **57 Snapshot-Tests**, **28 Integration-Entrypoint-Tests**, Ruff
und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 844: Integration-Argumente vor Vergleich kanonisieren

`integration_entrypoint.execute()` verglich normalisierte `argv`-Elemente
direkt mit dem festen Kommando. Ein `str`-Subclass konnte per `__eq__` vor der
Fehlernormalisierung eine rohe Exception auslösen.

Der Entry-Point akzeptiert Argumente jetzt nur nach exakter Built-in-`str`-
Prüfung und Längenvergleich; erst danach erfolgt der Tuple-Vergleich.
Regression stellt sicher, dass der manipulative Argumentwert Exit 64 liefert
und weder Verifier noch Quelldaten anfasst.

Verifikation: **29 Integration-Entrypoint-Tests**, **57 Snapshot-Tests**, Ruff
und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 845: Fehlerhafte Integration-Argumentiteration normalisieren

`integration_entrypoint.execute()` fing beim Erzeugen des Argument-Tuples nur
`TypeError` und `ValueError`. Ein `argv`-Iterator, der bei der Iteration eine
andere Exception warf, konnte den Prozess vor der kontrollierten Antwort
abbrechen.

Die Normalisierung bildet jetzt alle normalen `Exception`-Fehler auf Exit 64
mit datenarmem Argument-Token ab. Regression prüft, dass weder Verifier noch
Quelldaten bei einem fehlerhaften Iterator erreicht werden.

Verifikation: **30 Integration-Entrypoint-Tests**, **57 Snapshot-Tests**, Ruff
und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 846: Clock-`tzinfo`-Zugriff fail-closed normalisieren

`integration_entrypoint._require_aware_utc()` las `value.tzinfo` vor dem
geschützten Validierungsblock. Ein fehlerhafter `datetime`-Subclass-Property-
Hook wurde dadurch als generischer Exit 69 statt Secure-IO-Exit 70 ausgegeben.

`tzinfo`- und `utcoffset()`-Prüfung liegen jetzt gemeinsam im kontrollierten
`ValueError`-Pfad. Regression bestätigt: keine Quelldaten, Exit 70 und
datenarme Fehlermeldung.

Verifikation: **31 Integration-Entrypoint-Tests**, **57 Snapshot-Tests**, Ruff
und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 847: JSON-Eingaben auf Built-in-Sequenzen begrenzen

`json_utils.loads_strict()` akzeptierte `str`-/`bytes`-/`bytearray`-Subklassen.
Manipulierte Iterations- oder Decode-Hooks konnten vor JSON-Fehlernormalisierung
rohe Exceptions auslösen.

Der strikte Loader akzeptiert jetzt nur Built-in-`str`, `bytes` oder `bytearray`
vor Nesting-Scan und `json.loads`. Regression deckt String- und Bytes-Hooks ab.

Verifikation: **9 JSON-Utility-Tests**, **272 State-Tests**, Ruff und Diff-Check
bestanden; keine Settings-Fenster gestartet.

## Runde 848: Private-Text-Payload auf Built-in-Strings begrenzen

`private_io.write_private_text()` akzeptierte `str`-Subklassen und rief deren
`encode()` direkt auf. Ein manipuliertes Textobjekt konnte damit rohe
Exceptions vor dem atomaren Schreibpfad auslösen.

Der Writer akzeptiert jetzt ausschließlich Built-in-`str` vor Pfad-/Encode-/I/O-
Arbeit. Regression deckt den fehlerhaften Encode-Hook ab; bestehende private
Schreib- und Rollbackpfade bleiben unverändert.

Verifikation: **52 Private-IO-Tests**, **296 abhängige Config-/History-/Snapshot-
und Entrypoint-Tests**, Ruff und Diff-Check bestanden; keine Settings-Fenster
gestartet.

## Runde 849: Private-Pfade auf kanonischen Path-Typ begrenzen

`private_io._require_path()` akzeptierte `Path`-Subklassen. Überschriebene
Pfadmethoden konnten dadurch vor Symlink-/Owner-Prüfung rohe Exceptions in
private Schreib- und Lockpfade einschleusen.

Der gemeinsame Pfad-Guard akzeptiert jetzt ausschließlich den nativen
Plattform-`Path`-Typ vor jeder Methode. Regression simuliert einen fehlerhaften
`is_symlink()`-Hook; normale `Path`-/`PosixPath`-Aufrufer bleiben unverändert.

Verifikation: **53 Private-IO-Tests**, **296 abhängige Tests**, Ruff und
Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 850: Identity-Kandidaten auf kanonische URL-Strings begrenzen

`identity._candidate_is_usable()` akzeptierte URL-`str`-Subklassen und rief
deren `strip()` vor URL-Prüfung auf. Ein manipuliertes URL-Objekt konnte damit
die Kandidatenauswahl mit einer rohen Exception abbrechen.

Kandidaten werden jetzt nur bei Built-in-`str` als URL geprüft; andere Werte
werden wie unbrauchbare Kandidaten übersprungen. Regression simuliert den
fehlerhaften URL-Hook.

Verifikation: **30 Identity-Tests**, **60 abhängige Identity-/Extractor-/Direct-
und Scheduler-Tests**, Ruff und Diff-Check bestanden; keine Settings-Fenster
gestartet.

## Runde 851: Backend-Identitäten auf Built-in-Strings begrenzen

`identity._identity_value()` akzeptierte `str`-Subklassen und rief deren
Längen-/Iterationsoperatoren vor der Identifier-Prüfung auf. Ein manipulierter
String-Hook konnte die Payload-Normalisierung mit einer rohen Exception
abbrechen.

Die Identity-Grenze verlangt jetzt Built-in-`str` vor Länge und Zeichenprüfung.
Regression simuliert den fehlerhaften Identifier-Hook; normale User-/Account-
IDs bleiben unverändert.

Verifikation: **31 Identity-Tests**, **60 abhängige Identity-/Extractor-/Direct-
und Scheduler-Tests**, Ruff und Diff-Check bestanden; keine Settings-Fenster
gestartet.

## Runde 852: Backend-Plan-Typen auf Built-in-Strings begrenzen

`identity._plan_type_value()` akzeptierte `str`-Subklassen und rief deren
Längenoperator vor der Plan-Typ-Prüfung auf. Ein manipulierter String-Hook
konnte die Payload-Normalisierung mit einer rohen Exception abbrechen.

Die Plan-Type-Grenze verlangt jetzt Built-in-`str` vor Länge und
Zeichenprüfung. Regression simuliert den fehlerhaften Plan-Type-Hook; gültige
Plan-Typen bleiben unverändert.

Verifikation: **32 Identity-Tests**, **60 abhängige Identity-/Extractor-/Direct-
und Scheduler-Tests**, Ruff und Diff-Check bestanden; keine Settings-Fenster
gestartet.

## Runde 853: Auth-Identitäten auf Built-in-Strings begrenzen

`direct._safe_auth_identity()` akzeptierte `str`-Subklassen und rief deren
Längenoperator vor der Auth-Claim-Prüfung auf. Ein manipulierter String-Hook
konnte den Authentifizierungs-Payloadpfad mit einer rohen Exception abbrechen.

Der Auth-Identitäts-Guard akzeptiert jetzt ausschließlich Built-in-`str` vor
Länge und Zeichenprüfung. Regression simuliert den fehlerhaften Claim-Hook;
gültige Auth-Identitäten bleiben unverändert.

Verifikation: **186 Direct-Tests**, **38 abhängige Browser-/Bridge-Tests**,
Ruff und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 854: Auth-Plan-Typen auf Built-in-Strings begrenzen

`direct._safe_auth_plan_type()` akzeptierte `str`-Subklassen und rief deren
Längenoperator vor der Plan-Type-Prüfung auf. Ein manipulierter String-Hook
konnte den Authentifizierungs-Payloadpfad mit einer rohen Exception abbrechen.

Der Auth-Plan-Type-Guard akzeptiert jetzt ausschließlich Built-in-`str` vor
Länge und Zeichenprüfung. Regression simuliert den fehlerhaften Plan-Type-
Hook; gültige Auth-Plan-Typen bleiben unverändert.

Verifikation: **187 Direct-Tests**, **38 abhängige Browser-/Bridge-Tests**,
Ruff und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 855: Top-Level-Auth-Account-IDs auf Built-in-Strings begrenzen

`direct._auth_account_id_from_payload()` akzeptierte `str`-Subklassen und rief
deren Längenoperator vor der Account-ID-Prüfung auf. Ein manipulierter
String-Hook konnte den Auth-Payloadpfad mit einer rohen Exception abbrechen.

Der Top-Level-Account-ID-Guard akzeptiert jetzt ausschließlich Built-in-`str`
vor Länge und Zeichenprüfung. Regression prüft den öffentlichen
`auth_identity_from_payload()`-Pfad; gültige Account-IDs bleiben unverändert.

Verifikation: **188 Direct-Tests**, **38 abhängige Browser-/Bridge-Tests**,
Ruff und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 856: Kanonische Backend-Identität auf Built-in-Strings begrenzen

`direct.canonical_backend_identity()` akzeptierte in seiner öffentlichen
Feldvalidierung `str`-Subklassen und rief deren Längen-/Iterationsoperatoren
auf. Ein manipulierter Auth- oder Backend-Identifier konnte damit vor der
kontrollierten Fehlermeldung eine rohe Exception auslösen.

Die gemeinsame Identitätsvalidierung verlangt jetzt Built-in-`str` vor Länge
und Zeichenprüfung. Regression simuliert einen fehlerhaften `auth_user_id`-
Hook; normale kanonische Identitätsaufrufe bleiben unverändert.

Verifikation: **189 Direct-Tests**, **38 abhängige Browser-/Bridge-Tests**,
Ruff und Diff-Check bestanden; keine Settings-Fenster gestartet.

## Runde 857: Response-URL-Getter fail-closed normalisieren

`direct._response_final_url()` las `response.geturl` außerhalb des
Fehlerfangs. Ein Response-Objekt mit fehlerhaftem Property-Hook konnte damit
den URL-Trustpfad mit einer rohen Exception abbrechen.

Getter- und Fallback-URL-Zugriff liegen jetzt gemeinsam in einer
Exception-Grenze; fehlerhafte Response-Objekte liefern eine leere URL und
werden anschließend als untrusted abgewiesen. Regression simuliert den
fehlerhaften `geturl`-Hook.

Verifikation: **190 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 858: Response-Content-Type fail-closed normalisieren

`direct._response_content_type()` las `response.headers` und den Legacy-
`getheader`-Pfad ohne gemeinsame Fehlergrenze. Ein fehlerhaftes Header-Property
konnte die HTTP-Response-Verarbeitung mit einer rohen Exception abbrechen.

Header- und Legacy-Zugriffe liegen jetzt in einer vollständigen
Exception-Grenze; fehlerhafte Response-Metadaten liefern den sicheren leeren
Content-Type und werden nicht als JSON akzeptiert. Regression simuliert den
fehlerhaften `headers`-Hook.

Verifikation: **191 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 859: Vertrauenswürdige Response-URLs auf Built-in-Strings begrenzen

`direct._is_trusted_wham_response_url()` akzeptierte URL-`str`-Subklassen als
vertrauenswürdige Response-Ziele. Damit konnten benutzerdefinierte String-
Objekte die Trust-Grenze passieren, obwohl sie nicht aus dem normalen JSON-
oder urllib-Pfad stammen.

Die Trustprüfung akzeptiert jetzt ausschließlich Built-in-`str` vor
`urlsplit()` und Host-/Portprüfung. Regression stellt sicher, dass eine gültige
URL-Subklasse nicht als vertrauenswürdig gilt.

Verifikation: **192 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 860: Response-Identifier auf Built-in-Strings begrenzen

`direct._normalized_response_identifier()` akzeptierte `str`-Subklassen und
iterierte sie vor der Spark-/Metered-Feature-Prüfung. Ein manipulierter
Identifier-Hook konnte die Limitklassifikation mit einer rohen Exception
abbrechen.

Der Identifier-Normalizer verlangt jetzt Built-in-`str` vor Leerzeichen-,
Steuerzeichen- und Casefold-Prüfung. Regression simuliert einen fehlerhaften
Spark-Identifier-Hook; normale Spark-/Nicht-Spark-Werte bleiben unverändert.

Verifikation: **193 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 861: ISO-Zeitwerte auf Built-in-Strings begrenzen

`direct._parse_iso_datetime()` akzeptierte `str`-Subklassen und rief deren
`strip()` vor der ISO-Prüfung auf. Ein manipulierter Zeitwert-Hook konnte die
Auth-Metadatenverarbeitung mit einer rohen Exception abbrechen.

Der Zeitparser verlangt jetzt Built-in-`str` vor Trim-, Ersetzungs- und
`datetime.fromisoformat()`-Aufrufen. Regression simuliert den fehlerhaften
`strip()`-Hook; gültige ISO-Zeitwerte bleiben unverändert.

Verifikation: **194 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 862: Auth-Pfad auf Built-in-Strings begrenzen

`direct._resolve_auth_json_path()` verglich `Account.auth_json_path` vor der
Typprüfung mit `""`. Eine `str`-Subklasse konnte dabei ihren `__eq__`-Hook
ausführen und die sichere Pfadauflösung mit einer rohen Exception abbrechen.

Der Resolver prüft jetzt zuerst den exakten Built-in-Stringtyp, behandelt erst
danach den leeren Pfad und übergibt nur validierte Werte an `Path`. Regression
simuliert den fehlerhaften Vergleichs-Hook.

Verifikation: **195 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 863: Auth-Pfade auf nativen Path-Typ begrenzen

`direct._require_auth_path()` akzeptierte `Path`-Subklassen und gab sie an
weitere Auth-/Dateipfade weiter. Überschriebene Pfadmethoden konnten damit die
spätere Sicherheitsprüfung mit fremder Semantik oder rohen Exceptions belasten.

Der Guard akzeptiert jetzt ausschließlich den nativen Plattform-`Path`-Typ.
Regression stellt sicher, dass eine `Path`-Subklasse fail-closed abgewiesen
wird; normale `Path`-/`PosixPath`-Aufrufer bleiben unverändert.

Verifikation: **196 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 864: Access-Tokens auf Built-in-Strings begrenzen

`direct._extract_auth_details()` akzeptierte `str`-Subklassen und rief deren
Bool-/Längen- und Iterationsoperatoren vor der Tokenvalidierung auf. Ein
manipulierter Access-Token-Hook konnte den Authentifizierungs-Payloadpfad mit
einer rohen Exception abbrechen.

Die Access-Token-Grenze verlangt jetzt Built-in-`str` vor Leer-, Längen- und
Zeichenprüfung. Regression simuliert den fehlerhaften Token-Hook; gültige
Tokens und JWT-Prüfung bleiben unverändert.

Verifikation: **197 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 865: JWT-Tokenprüfung auf Built-in-Strings begrenzen

`direct._validate_access_token_expiry()` war separat aufrufbar und übergab
String-Subklassen direkt an `_jwt_claims()`. Ein manipulierter Token-Hook konnte
die JWT-Prüfung mit einer rohen Exception abbrechen.

Die Expiry-Grenze verlangt jetzt Built-in-`str` vor JWT-Splitting und
Expiry-Auswertung. Regression simuliert den fehlerhaften Token-Hook; normale
Tokens und fehlende `exp`-Claims bleiben unverändert.

Verifikation: **198 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 866: Response-URLs auf Built-in-Strings begrenzen

`direct._response_final_url()` gab URLwerte aus `geturl()` oder dem
Fallback-Attribut per `isinstance(value, str)` unverändert weiter. Eine
`str`-Subklasse konnte dadurch die URL-Vertragsgrenze passieren und spätere
Verarbeitung mit eigener Semantik belasten.

Beide Rückgabepfade akzeptieren jetzt ausschließlich Built-in-`str`; andere
Werte werden leer und damit fail-closed zurückgegeben. Regression deckt
`geturl()`- und Fallback-Attribute ab; Getterfehler bleiben ebenfalls leer.

Verifikation: **199 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 867: Account-Authpfade auf Built-in-Strings begrenzen

`direct.auth_identity_for_account()` und
`direct.auth_plan_type_for_account()` prüften `auth_json_path` zunächst mit
`if not` und akzeptierten danach String-Subklassen. Ein manipulierter
Bool-Hook konnte beide Account-Helfer mit einer rohen Exception abbrechen.

Beide Helfer behandeln `None` separat, verlangen danach exakt Built-in-`str`
und prüfen erst anschließend den leeren Pfad. Regression deckt beide Helfer
mit demselben fehlerhaften Bool-Hook ab; gültige und leere Pfade bleiben
unverändert.

Verifikation: **200 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 868: Auth-Override auf nativen Path-Typ begrenzen

`direct._resolve_auth_json_path()` akzeptierte beim expliziten Override
`Path`-Subklassen per `isinstance()` und gab sie nach `expanduser()` weiter.
Damit konnte eine fremde Pfadsemantik die Auth-Datei-Grenze passieren.

Der Override-Guard verlangt jetzt exakt den nativen Plattform-`Path`-Typ,
analog zu `_require_auth_path()`. Regression weist eine `Path`-Subklasse vor
jedem Pfadaufruf ab; native Pfade und bestehende Typfehler bleiben unverändert.

Verifikation: **201 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 869: Identitätsvergleich auf Built-in-Strings begrenzen

`direct.auth_identity_changed()` verwendete Account- und User-IDs direkt in
Bool- und Vergleichsoperationen. Eine String-Subclass konnte dadurch bereits
im primären Account-ID-Test einen rohen Hook-Fehler auslösen.

Der Vergleich weist jetzt jeden nichtleeren Nicht-Built-in-String als
Identitätswechsel zurück, bevor Bool- oder Gleichheitsoperatoren laufen.
Regression deckt den Bool-Hook ab; normale gleiche und geänderte IDs bleiben
unverändert.

Verifikation: **202 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 870: Plan-Typ-Vergleich auf Built-in-Strings begrenzen

`direct._auth_plan_type_changed()` verglich einen vorhandenen Planwert vor
der Normalisierung direkt mit `None` beziehungsweise dem Gegenwert. Eine
String-Subclass konnte dadurch den Wechseltest mit einem rohen `!=`-Hook
abbrechen.

Der Helper weist jetzt jeden nichtleeren Nicht-Built-in-Planwert vor allen
Vergleichen als geändert zurück. Regression deckt den Vergleichs-Hook ab;
`None`-, Alias- und normale Gleichheitsfälle bleiben unverändert.

Verifikation: **203 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 871: Response-Identitätsvergleich typisieren

`direct._response_identity_matches_auth()` wertete Backend- und Auth-IDs
direkt per Bool- und Gleichheitsoperator aus. Eine String-Subclass konnte den
Matchpfad dadurch mit einer rohen Exception abbrechen.

Der Helper prüft jetzt alle vier Identitätswerte vor jeder Operation auf
Built-in-`str` und liefert bei einem ungültigen Typ `False`. Regression deckt
den Bool-Hook ab; gültige Identitätsfälle bleiben unverändert.

Verifikation: **204 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 872: Plan-Typ-Normalisierung typisieren

`direct._normalized_plan_type()` rief `strip()` und `casefold()` direkt auf
dem annotierten Wert auf. Eine String-Subclass konnte die Normalisierung mit
einer rohen Exception abbrechen.

Der Helper akzeptiert jetzt ausschließlich Built-in-`str`; andere Typen
werden als leerer Planwert zurückgegeben. Regression deckt den fehlerhaften
`strip()`-Hook ab; gültige Alias-Normalisierung bleibt unverändert.

Verifikation: **205 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 873: Retry-Klassifikation auf direkte Auth-Fehler begrenzen

`direct._is_retryable_direct_auth_error()` wandelte Fehlerobjekte vor der
HTTP-Codeprüfung per `str(error)` um. Ein `DirectAuthError`-Subclass konnte
damit einen rohen `__str__()`-Hook auslösen.

Die Klassifikation akzeptiert jetzt ausschließlich den direkten
`DirectAuthError`-Typ; Subklassen werden sicher als nicht retrybar behandelt.
Regression deckt den fehlerhaften String-Hook ab; normale 401-/403-Retrypfade
bleiben unverändert.

Verifikation: **206 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 874: Identity-Fehlerklassifikation typisieren

`direct._is_identity_attribution_error()` prüfte einen Fehlerwert direkt per
Set-Mitgliedschaft. Eine String-Subclass konnte dabei ihren `__hash__()`-Hook
ausführen und die Cache-Invalidierungsentscheidung mit einer rohen Exception
abbrechen.

Der Helper akzeptiert jetzt ausschließlich Built-in-`str`; andere Werte
liefern vor der Set-Prüfung `False`. Regression deckt den Hash-Hook ab;
bekannte und unbekannte normale Fehlertexte bleiben unverändert.

Verifikation: **207 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 875: Planwert in Limitfehlerdiagnose typisieren

`direct._missing_usage_limits_error()` prüfte `backend_plan_type` zuerst per
Bool-Auswertung und normalisierte ihn danach. Eine String-Subclass konnte die
Diagnose dadurch mit einem rohen Bool-Hook abbrechen.

Die Diagnose behandelt `None` und Fremdtypen jetzt ohne Operator-Hook als
`unknown`; nur Built-in-Strings werden normalisiert. Regression deckt den
Bool-Hook im verfügbaren-5h-Fensterpfad ab; bestehende Diagnoseformen bleiben
unverändert.

Verifikation: **208 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 876: Identity-Policy-Flags auf Built-in-Bool begrenzen

`direct.canonical_backend_identity()` verwendete drei Policy-Flags direkt in
Bool-Ausdrücken. Ein fremdes Objekt mit `__bool__()`-Hook konnte die
Identitätsprüfung mit einer rohen Exception abbrechen.

Die Funktion verlangt jetzt für `require_backend_identity`,
`require_backend_account_id` und `reject_ambiguous_backend_identity` exakt
Built-in-`bool`; andere Typen liefern einen klaren `ValueError`. Regression
deckt den Flag-Hook ab; alle bisherigen Identitätsentscheidungen bleiben
unverändert.

Verifikation: **209 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 877: Ungültige Signaturflags robust formatieren

`direct._signature_flag()` wandelte nichtboolesche Werte ungeschützt per
`str(value)` in eine Vergleichssignatur um. Ein fremdes Flagobjekt mit
defektem `__str__()` konnte dadurch die Stabilitätsprüfung mit einer rohen
Exception abbrechen.

Die Signaturbildung fängt Formatierungsfehler jetzt lokal ab und verwendet
`<unprintable>` als datenarmen Marker. Normale Werte behalten ihre bisherige
Darstellung; Regression deckt den defekten String-Hook ab.

Verifikation: **210 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 878: JWT-Claims auf Built-in-Strings begrenzen

`direct._jwt_claims()` akzeptierte `str`-Subklassen und rief deren `split()`
direkt auf. Ein manipulierter Tokenwert konnte den zentralen JWT-Parser damit
mit einer rohen Exception abbrechen.

Der Parser verlangt jetzt exakt Built-in-`str` vor Segmentierung und
Base64-Verarbeitung. Regression deckt den fehlerhaften `split()`-Hook ab;
normale JWT-, Expiry- und malformed-Tokenpfade bleiben unverändert.

Verifikation: **211 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 879: Token-Ablaufvergleich auf native Datetimes begrenzen

`direct._is_access_token_expired()` verglich `expiry` und `now` direkt per
`<=`. Eine manipulierbare Datetime-Subclass konnte den Login-/Refreshpfad
damit mit einer rohen Vergleichs-Exception abbrechen.

Der Helper akzeptiert nur native `datetime`-Werte; ungültige Zeittypen gelten
fail-closed als abgelaufen, `None` bleibt „nicht abgelaufen“. Regression
deckt den Vergleichs-Hook ab; normale Ablaufentscheidungen bleiben
unverändert.

Verifikation: **212 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 880: Ablauf-Fehlermeldung gegen Datetime-Hooks härten

`direct._expired_auth_error()` rief `astimezone()` und `strftime()` direkt auf
dem Ablaufwert auf. Eine Datetime-Subclass konnte dadurch die Loginmeldung
mit einer rohen Exception verhindern.

Die Funktion formatiert nur native Datetimes mit Zeitstempel; ungültige
Zeittypen fallen auf die generische Ablaufmeldung zurück. Auch fremde
Account-ID-Typen werden durch `<unknown>` ersetzt. Regression deckt den
`astimezone()`-Hook ab; normale Ablaufmeldungen bleiben unverändert.

Verifikation: **213 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 881: Usage-Window-Property-Zugriff begrenzen

`direct._has_usage_values()` dereferenzierte `has_usage_value` direkt auf
annotierten Window-Objekten. Ein fremdes Objekt oder eine Window-Subclass mit
manipulierter Property konnte die Nutzungsentscheidung mit einer rohen
Exception abbrechen.

Der Helper akzeptiert jetzt ausschließlich native `LimitWindow`-Instanzen;
andere Werte liefern vor Property-Zugriff `False`. Regression deckt den
Property-Hook ab; normale native Fensterentscheidungen bleiben unverändert.

Verifikation: **214 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 882: Usage-Window-Signaturen auf native Dicts begrenzen

`direct._usage_window_signature()` akzeptierte `dict`-Subklassen und rief
deren `.get()` direkt für alle Fensterwerte auf. Ein manipuliertes Mapping
konnte die Stabilitätssignatur dadurch mit einer rohen Exception abbrechen.

Der Helper akzeptiert jetzt ausschließlich native `dict`-Objekte und liefert
für Subklassen/Fremdtypen `None`. Regression deckt den `.get()`-Hook ab;
normale Usage-Signaturen bleiben unverändert.

Verifikation: **215 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 883: Main-Limit-Signaturen auf native Dicts begrenzen

`direct._main_limit_signature()` akzeptierte `rate_limit`-Dict-Subklassen
und rief deren `.get()` direkt für Flags auf. Ein manipuliertes Mapping konnte
die Stabilitätssignatur dadurch mit einer rohen Exception abbrechen.

Der Helper akzeptiert jetzt ausschließlich ein natives `dict`; Subklassen und
Fremdtypen liefern `("invalid-rate-limit",)`. Regression deckt den
`.get()`-Hook ab; normale Main-Limit-Signaturen bleiben unverändert.

Verifikation: **216 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 884: Spark-Limit-Signaturen auf native Listen begrenzen

`direct._spark_limit_signature()` akzeptierte `additional_rate_limits`-
Listen-Subklassen und iterierte sie direkt. Ein manipulierter Iterator konnte
die Spark-Stabilitätssignatur dadurch mit einer rohen Exception abbrechen.

Der Helper akzeptiert jetzt ausschließlich native `list`-Objekte; Subklassen
und Fremdtypen liefern `None`. Regression deckt den Iterator-Hook ab;
normale Spark-Signaturen bleiben unverändert.

Verifikation: **217 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 885: Spark-Signatur-Items auf native Dicts begrenzen

`direct._spark_limit_signature()` akzeptierte einzelne `item`-Dict-Subklassen
und rief deren `.get()` für Spark-Identifikatoren auf. Ein manipuliertes Item
konnte die Signaturbildung dadurch mit einer rohen Exception abbrechen.

Der innere Loop akzeptiert jetzt ausschließlich native `dict`-Items; fremde
Items werden wie nicht relevante Limits übersprungen. Regression deckt den
`.get()`-Hook ab; normale Spark-Items bleiben unverändert.

Verifikation: **218 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 886: Spark-Rate-Limit-Signaturen auf native Dicts begrenzen

`direct._spark_limit_signature()` akzeptierte den inneren `rate_limit`-
Dictwert per `isinstance` und rief daraus `.get()` für Flags und Fenster auf.
Ein manipuliertes Mapping konnte die Signaturbildung dadurch mit einer rohen
Exception abbrechen.

Der Helper akzeptiert jetzt ausschließlich ein natives `dict`; Subklassen und
Fremdtypen liefern `("invalid",)`. Regression deckt den `.get()`-Hook ab;
normale Spark-Rate-Limit-Signaturen bleiben unverändert.

Verifikation: **219 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 887: Main-Limit-Malformed-Guard auf native Dicts begrenzen

`direct._has_malformed_main_limit_structure()` akzeptierte `rate_limit`-
Dict-Subklassen per `isinstance`. Die nachgelagerte Signaturbildung behandelte
solche Werte dagegen bereits als ungültig und konnte bei einem manipulierten
`.get()` mit einer rohen Exception abbrechen.

Der Malformed-Guard akzeptiert jetzt ausschließlich ein natives `dict`; eine
Subklasse wird vor jeder Signaturbildung als malformed abgewiesen. Regression
deckt den `.get()`-Hook ab; normale Main-Limit-Antworten bleiben unverändert.

Verifikation: **220 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 888: Spark-Malformed-Guard auf native Listen begrenzen

`direct._has_malformed_spark_limit_structure()` akzeptierte
`additional_rate_limits`-Listen-Subklassen per `isinstance`. Die nachgelagerte
Spark-Signatur behandelte solche Werte bereits als ungültig; dadurch konnte die
Auswahl einen manipulierten Wert passieren lassen, statt ihn als malformed zu
verwerfen.

Der Malformed-Guard akzeptiert jetzt ausschließlich eine native `list`;
Subklassen werden vor der Signaturbildung abgewiesen. Regression deckt einen
manipulierten Iterator ab; normale Spark-Limit-Antworten bleiben unverändert.

Verifikation: **221 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 889: Unterstützte Fensterdauern auf native Dicts begrenzen

`direct._supported_window_durations()` akzeptierte Rate-Limit- und Fenster-
Dict-Subklassen per `isinstance` und rief deren `.get()` auf. Manipulierte
Mappings konnten die Stabilitätsauswahl dadurch mit einer rohen Exception
abbrechen.

Der Helper akzeptiert jetzt ausschließlich native `dict`-Objekte; Subklassen
werden ohne Hook-Aufruf verworfen. Regression deckt beide Mapping-Ebenen ab;
normale Fensterdauern bleiben unverändert.

Verifikation: **222 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 890: Response-Signaturen auf native Rate-Limit-Dicts begrenzen

`direct._usage_response_signature()` akzeptierte `rate_limit`-Dict-Subklassen
per `isinstance` und rief deren `.get()` für beide Fenster auf. Ein
manipuliertes Mapping konnte die Signaturbildung dadurch mit einer rohen
Exception abbrechen.

Der Helper akzeptiert jetzt ausschließlich ein natives `dict`; fremde
Mappings liefern eine Signatur ohne Fensterwerte. Regression deckt den
`.get()`-Hook ab; normale Response-Signaturen bleiben unverändert.

Verifikation: **223 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 891: Rate-Limit-Fenster auf native Dicts begrenzen

`direct._rate_limit_window()` akzeptierte Rate-Limit- und Fenster-Dict-
Subklassen per `isinstance` und rief das Rate-Limit-`.get()` direkt auf. Ein
manipuliertes Mapping konnte Resetprüfungen dadurch mit einer rohen Exception
abbrechen.

Der Helper akzeptiert jetzt ausschließlich native `dict`-Objekte auf beiden
Ebenen; Subklassen werden ohne Hook-Aufruf als nicht vorhanden behandelt.
Regression deckt beide Mapping-Hooks ab; normale Fensterauflösung bleibt
unverändert.

Verifikation: **224 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 892: Credit-Kandidaten auf native Dicts begrenzen

`direct._credit_window()` akzeptierte einen strukturierten Credit-Kandidaten
als Dict-Subclass und rief dessen `.get()` in der Zahlen- und Resetauflösung
auf. Ein manipuliertes Mapping konnte die Credit-Extraktion dadurch mit einer
rohen Exception abbrechen.

Strukturierte Kandidaten werden jetzt nur bei einem nativen `dict` verarbeitet;
Subklassen und Fremdtypen liefern `None`. Regression deckt den Credit-`.get()`-
Hook ab; skalare und normale strukturierte Creditwerte bleiben unverändert.

Verifikation: **225 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 893: Verschachtelte Credit-Quellen auf native Dicts begrenzen

`direct._credit_window()` akzeptierte verschachtelte `rateLimits`,
`rateLimitsByLimitId`- und `account`-Mappings per `isinstance`. Deren
`.get()`-/`.values()`-Hooks konnten die Credit-Extraktion mit einer rohen
Exception abbrechen.

Alle drei Quellenebenen werden jetzt nur bei nativen `dict`-Objekten gelesen;
Subklassen werden ohne Hook-Aufruf ignoriert. Regression deckt alle drei
verschachtelten Eingangsformen ab; normale Credit-Quellen bleiben unverändert.

Verifikation: **226 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 894: Fehlende-Limits-Diagnose auf native Dicts begrenzen

`direct._missing_usage_limits_error()` akzeptierte Rate-Limit- und Fenster-
Dict-Subklassen per `isinstance` und rief deren `.get()` auf. Manipulierte
Mappings konnten die eigentlich bounded Fehlerdiagnose dadurch mit einer rohen
Exception abbrechen.

Die Diagnose verarbeitet jetzt nur native `dict`-Objekte auf beiden Ebenen;
Subklassen werden ohne Hook-Aufruf ignoriert. Regression deckt beide
Mapping-Hooks ab; normale Plan-/Fensterdiagnosen bleiben unverändert.

Verifikation: **227 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 895: Auth-Account-ID nur aus nativen Token-Dicts lesen

`direct._auth_account_id_from_payload()` akzeptierte den `tokens`-Block als
Dict-Subclass per `isinstance` und rief dessen `.get()` auf. Ein manipuliertes
Auth-Mapping konnte die Account-ID-Prüfung dadurch mit einer rohen Exception
abbrechen.

Der Helper verarbeitet jetzt nur native `dict`-Tokenblöcke; Subklassen werden
ohne Hook-Aufruf als nicht vorhanden behandelt. Regression deckt den
Account-ID-`.get()`-Hook ab; normale Auth-JSON-Strukturen bleiben unverändert.

Verifikation: **228 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 896: Auth-E-Mail nur aus nativen Token-Dicts lesen

`direct.auth_email_from_payload()` akzeptierte den `tokens`-Block als
Dict-Subclass per `isinstance` und rief dessen `.get()` für beide Token auf.
Ein manipuliertes Auth-Mapping konnte die E-Mail-Auswertung dadurch mit einer
rohen Exception abbrechen.

Der Helper verarbeitet jetzt nur native `dict`-Tokenblöcke; Subklassen werden
ohne Hook-Aufruf ignoriert. Regression deckt den E-Mail-`.get()`-Hook ab;
normale Auth-JSON-Strukturen bleiben unverändert.

Verifikation: **229 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 897: Auth-Plantyp nur aus nativen Token-Dicts lesen

`direct.auth_plan_type_from_payload()` akzeptierte den `tokens`-Block als
Dict-Subclass per `isinstance` und rief dessen `.get()` für beide Token auf.
Ein manipuliertes Auth-Mapping konnte die Plantyp-Auswertung dadurch mit einer
rohen Exception abbrechen.

Der Helper verarbeitet jetzt nur native `dict`-Tokenblöcke; Subklassen werden
ohne Hook-Aufruf ignoriert. Regression deckt den Plantyp-`.get()`-Hook ab;
normale Auth-JSON-Strukturen bleiben unverändert.

Verifikation: **230 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 898: Auth-Identität nur aus nativen Token-Dicts lesen

`direct.auth_identity_from_payload()` akzeptierte den `tokens`-Block als
Dict-Subclass per `isinstance` und rief dessen `.get()` für beide Token auf.
Ein manipuliertes Auth-Mapping konnte die Identitätsauswertung dadurch mit
einer rohen Exception abbrechen.

Der Helper verarbeitet jetzt nur native `dict`-Tokenblöcke; Subklassen werden
ohne Hook-Aufruf ignoriert. Regression deckt den Identitäts-`.get()`-Hook ab;
normale Auth-JSON-Strukturen bleiben unverändert.

Verifikation: **231 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 899: Auth-Metadaten nur aus nativen Token-Dicts lesen

`direct.auth_metadata_from_payload()` akzeptierte den `tokens`-Block als
Dict-Subclass per `isinstance` und rief dessen `.get()` für Access- und
Identity-Token auf. Ein manipuliertes Auth-Mapping konnte die Metadatenbildung
dadurch mit einer rohen Exception abbrechen.

Der Helper verarbeitet jetzt nur native `dict`-Tokenblöcke; Subklassen werden
ohne Hook-Aufruf wie ein fehlender Tokenblock behandelt. Regression deckt den
Metadaten-`.get()`-Hook ab; normale Auth-JSON-Strukturen bleiben unverändert.

Verifikation: **232 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 900: Auth-Details nur aus nativen Token-Dicts lesen

`direct._extract_auth_details()` akzeptierte den `tokens`-Block als
Dict-Subclass per `isinstance` und rief dessen `.get()` für den Access-Token
auf. Ein manipuliertes Auth-Mapping konnte die Auth-Validierung dadurch mit
einer rohen Exception abbrechen.

Der Helper verlangt jetzt einen nativen `dict`-Tokenblock; Subklassen werden
als fehlendes Tokens-Objekt abgewiesen. Regression deckt den Auth-Details-
`.get()`-Hook ab; normale Auth-JSON-Strukturen bleiben unverändert.

Verifikation: **233 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 901: Auth-Metadaten nur aus nativen Payload-Dicts lesen

`direct.auth_metadata_from_payload()` akzeptierte den gesamten Payload als
Dict-Subclass per `isinstance` und rief dessen `.get()` auf. Ein manipuliertes
Auth-Mapping konnte die Metadatenbildung dadurch mit einer rohen Exception
abbrechen.

Der Helper verarbeitet jetzt nur einen nativen `dict`-Payload; Subklassen
werden ohne Hook-Aufruf wie ein nichtobjektartiger Payload behandelt.
Regression deckt den äußeren Payload-`.get()`-Hook ab; normale Auth-JSON-
Strukturen bleiben unverändert.

Verifikation: **234 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 902: Auth-Details nur aus nativen Payload-Dicts lesen

`direct._extract_auth_details()` rief `payload.get()` bisher ohne Prüfung des
äußeren Mappingtyps auf. Eine Payload-Subclass konnte die Auth-Validierung
dadurch mit einer rohen Exception abbrechen.

Der Helper verlangt jetzt einen nativen `dict`-Payload und weist fremde
Mappings als fehlendes Tokens-Objekt ab. Regression deckt den äußeren
Payload-`.get()`-Hook ab; normale Auth-JSON-Strukturen bleiben unverändert.

Verifikation: **235 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 903: Auth-Identität nur aus nativen Payload-Dicts lesen

`direct.auth_identity_from_payload()` akzeptierte den äußeren Payload als
Dict-Subclass per `isinstance` und rief dessen `.get()` auf. Ein manipuliertes
Auth-Mapping konnte die Identitätsauswertung dadurch mit einer rohen Exception
abbrechen.

Der Helper verarbeitet jetzt nur native Payload-`dict`; Subklassen werden ohne
Hook-Aufruf wie ein nicht verwertbarer Payload behandelt. Regression deckt den
äußeren Identitäts-`.get()`-Hook ab; normale Auth-JSON-Strukturen bleiben
unverändert.

Verifikation: **236 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 904: Auth-E-Mail nur aus nativen Payload-Dicts lesen

`direct.auth_email_from_payload()` akzeptierte den äußeren Payload als
Dict-Subclass per `isinstance` und rief dessen `.get()` auf. Ein manipuliertes
Auth-Mapping konnte die E-Mail-Auswertung dadurch mit einer rohen Exception
abbrechen.

Der Helper verarbeitet jetzt nur native Payload-`dict`; Subklassen werden ohne
Hook-Aufruf wie ein nicht verwertbarer Payload behandelt. Regression deckt den
äußeren E-Mail-`.get()`-Hook ab; normale Auth-JSON-Strukturen bleiben
unverändert.

Verifikation: **237 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 905: Auth-Plantyp nur aus nativen Payload-Dicts lesen

`direct.auth_plan_type_from_payload()` akzeptierte den äußeren Payload als
Dict-Subclass per `isinstance` und rief dessen `.get()` auf. Ein manipuliertes
Auth-Mapping konnte die Plantyp-Auswertung dadurch mit einer rohen Exception
abbrechen.

Der Helper verarbeitet jetzt nur native Payload-`dict`; Subklassen werden ohne
Hook-Aufruf wie ein nicht verwertbarer Payload behandelt. Regression deckt den
äußeren Plantyp-`.get()`-Hook ab; normale Auth-JSON-Strukturen bleiben
unverändert.

Verifikation: **238 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 906: Auth-Account-ID nur aus nativen Payload-Dicts lesen

`direct._auth_account_id_from_payload()` prüfte bisher nur das verschachtelte
Tokens-Objekt. Ein äußerer Payload als Dict-Subclass konnte deshalb dessen
`.get()`-Hook ausführen und die Auth-Identitätsauswertung mit einer rohen
Exception abbrechen.

Der interne Extractor verlangt jetzt ebenfalls einen nativen Payload-`dict`;
Subklassen werden ohne Hook-Aufruf wie ein nicht verwertbarer Payload behandelt.
Regression deckt den äußeren Account-ID-`.get()`-Hook ab; normale Auth-JSON-
Strukturen bleiben unverändert.

Verifikation: **239 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 907: JWT-Claims nur aus nativen Dicts übernehmen

`direct._jwt_claims()` gab ein von `loads_strict()` geliefertes Dict-Subclass
über `isinstance` weiter. Nachgelagerte Auth-Parser verwenden darauf `.get()`
und Membership-Checks; ein manipuliertes Claims-Mapping konnte diese Auswertung
mit fremden Hooks beeinflussen.

Der Parser akzeptiert jetzt ausschließlich ein natives Claims-`dict` und
verwirft Subklassen fail-closed. Regression injiziert ein Claims-Subclass in
den Parser; normale JWT-Claims bleiben unverändert.

Verifikation: **240 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 908: Aktuelle JWT-Claims nur aus nativen Dicts verwenden

`direct._current_jwt_claims()` akzeptierte einen von `_jwt_claims()` gelieferten
Dict-Subclass per `isinstance` und gab ihn direkt an Auth-Parser weiter. Ein
injizierter Claims-Wrapper konnte damit die Expiry- und Membership-Auswertung
über fremde Mapping-Hooks beeinflussen.

Der Verbraucher verlangt jetzt ebenfalls ein natives Claims-`dict`; fremde
Mappings werden fail-closed verworfen. Regression injiziert ein Claims-Subclass
am Helper-Rand; normale aktuelle JWT-Claims bleiben unverändert.

Verifikation: **241 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 909: JWT-Ablaufzeit nur aus nativen Claims-Dicts berechnen

`direct._jwt_expiry()` akzeptierte einen injizierten Claims-Subclass per
`isinstance` und las daraus den `exp`-Wert. Ein fremdes Mapping konnte dadurch
die Ablaufzeitberechnung über eigene Hooks beeinflussen.

Der Helper verlangt jetzt ein natives Claims-`dict`; nichtnative Mappings
werden fail-closed als unbekannte Ablaufzeit behandelt. Regression injiziert
ein Claims-Subclass mit gültigem `exp`; normale JWT-Ablaufzeiten bleiben
unverändert.

Verifikation: **242 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 910: Access-Token-Expiry nur aus nativen Claims-Dicts prüfen

`direct._validate_access_token_expiry()` akzeptierte einen injizierten
Claims-Subclass per `isinstance` und führte darauf den `exp`-Membership-Check
aus. Ein fremdes Mapping konnte die Tokenvalidierung dadurch mit einer rohen
Exception abbrechen.

Die Validierung verlangt jetzt ein natives Claims-`dict`; nichtnative Mappings
werden ohne Hook-Aufruf wie fehlende Ablaufclaims behandelt. Regression deckt
den Claims-`__contains__`-Hook ab; normale Ablaufvalidierung bleibt unverändert.

Verifikation: **243 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 911: Auth-Identität verwirft nichtnative aktuelle Claims

`direct.auth_identity_from_payload()` akzeptierte vom aktuellen JWT-Helper
gelieferte Claims-Subklassen per `isinstance` und rief darauf `.get()` auf. Ein
injiziertes Mapping konnte die Identitätsauswertung dadurch mit einer rohen
Exception abbrechen.

Der Identitätsparser verlangt jetzt native Claims-`dict`; fremde Mappings
werden ohne Hook-Aufruf übersprungen. Regression deckt den Claims-`.get()`-Hook
ab; normale Tokenidentitäten bleiben unverändert.

Verifikation: **244 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 912: Verschachtelte Auth-Claims nur aus nativen Dicts übernehmen

`direct.auth_identity_from_payload()` akzeptierte das verschachtelte
`https://api.openai.com/auth`-Mapping als Dict-Subclass per `isinstance` und
gab es an die Identitätsclaim-Prüfung weiter. Ein fremdes Mapping konnte dort
über `__contains__` oder weitere Hooks eine rohe Exception auslösen.

Der Parser verlangt jetzt ein natives Auth-Claims-`dict`; nichtnative
Mappings werden mit einer kontrollierten `DirectAuthError` abgewiesen.
Regression deckt den verschachtelten Membership-Hook ab; normale Auth-Claims
bleiben unverändert.

Verifikation: **245 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 913: Auth-E-Mail nur aus nativen Claims-Dicts lesen

`direct.auth_email_from_payload()` akzeptierte aktuelle Claims-Subklassen per
`isinstance` und führte darauf den E-Mail-Membership-Check aus. Ein fremdes
Mapping konnte die E-Mail-Auswertung dadurch mit einer rohen Exception
abbrechen.

Der Helper verlangt jetzt ein natives Claims-`dict`; nichtnative Mappings
werden ohne Hook-Aufruf übersprungen. Regression deckt den Claims-
`__contains__`-Hook ab; normale E-Mail-Claims bleiben unverändert.

Verifikation: **246 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 914: Auth-Plantyp nur aus nativen Claims-Dicts lesen

`direct.auth_plan_type_from_payload()` akzeptierte aktuelle Claims-Subklassen
per `isinstance` und rief darauf `.get()` für die OpenAI-Authstruktur auf. Ein
fremdes Mapping konnte die Plantyp-Auswertung dadurch mit einer rohen Exception
abbrechen.

Der Helper verlangt jetzt ein natives Claims-`dict`; nichtnative Mappings
werden ohne Hook-Aufruf übersprungen. Regression deckt den Claims-`.get()`-Hook
ab; normale Plantyp-Claims bleiben unverändert.

Verifikation: **247 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 915: Verschachtelte Auth-Plantyp-Claims nur aus nativen Dicts lesen

`direct.auth_plan_type_from_payload()` akzeptierte das verschachtelte
`https://api.openai.com/auth`-Mapping als Dict-Subclass per `isinstance` und
führte darauf den Plantyp-Membership-Check aus. Ein fremdes Mapping konnte die
Auswertung dadurch mit einer rohen Exception abbrechen.

Der Parser verlangt jetzt ein natives Auth-Claims-`dict`; nichtnative
Mappings werden mit einer kontrollierten `DirectAuthError` abgewiesen.
Regression deckt den verschachtelten Membership-Hook ab; normale Plantyp-
Claims bleiben unverändert.

Verifikation: **248 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 916: Auth-Identitätsdatei akzeptiert nur native JSON-Objekte

`direct.auth_identity_from_file()` und der gemeinsame
`_load_auth_token_and_metadata()`-Pfad akzeptierten ein von `loads_strict()`
geliefertes Dict-Subclass per `isinstance`. Dadurch wurde eine manipulierte
Parser-Rückgabe an Payload-Helper weitergereicht und als leerer Identitätswert
behandelt statt als ungültige Auth-Struktur.

Beide Datei-Grenzen verlangen jetzt ein natives JSON-`dict` und melden fremde
Mappings kontrolliert als ungültige Struktur. Regression injiziert ein
Payload-Subclass in beide Parserpfade; normale Auth-Dateien bleiben
unverändert.

Verifikation: **250 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 917: Auth-E-Mail-Datei akzeptiert nur native JSON-Objekte

`direct.auth_email_from_file()` akzeptierte ein von `loads_strict()` geliefertes
Dict-Subclass per `isinstance` und reichte es an den Payload-Helper weiter.
Dadurch konnte eine manipulierte Parser-Rückgabe als leerer E-Mail-Wert
durchlaufen.

Der Datei-Wrapper verlangt jetzt ein natives JSON-`dict` und meldet fremde
Mappings kontrolliert als ungültige Struktur. Regression injiziert ein
Payload-Subclass in den Parser; normale Auth-Dateien bleiben unverändert.

Verifikation: **251 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 918: Auth-Plantyp-Datei akzeptiert nur native JSON-Objekte

`direct.auth_plan_type_from_file()` akzeptierte ein von `loads_strict()`
geliefertes Dict-Subclass per `isinstance` und reichte es an den Payload-Helper
weiter. Eine manipulierte Parser-Rückgabe konnte dadurch als fehlender Plantyp
durchlaufen.

Der Datei-Wrapper verlangt jetzt ein natives JSON-`dict` und meldet fremde
Mappings kontrolliert als ungültige Struktur. Regression injiziert ein
Payload-Subclass in den Parser; normale Auth-Dateien bleiben unverändert.

Verifikation: **252 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 919: Direct-Response akzeptiert nur native JSON-Objekte

`direct._fetch_wham_usage()` akzeptierte eine von `loads_strict()` gelieferte
Dict-Subclass per `isinstance` und gab sie als gültige Response weiter. Eine
manipulierte Parser-Rückgabe konnte dadurch die nachgelagerte Usage-Auswertung
erreichen.

Der Fetcher verlangt jetzt ein natives JSON-`dict` und meldet fremde Mappings
als kontrollierten `DirectFetchError`. Regression injiziert ein Payload-
Subclass in die Response-Parsinggrenze; normale JSON-Responses bleiben
unverändert.

Verifikation: **253 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 920: Standard-Auth-Pfad direkt geprüft

`direct.default_auth_json_path()` hatte bisher keinen direkten Regressionstest.
Der neue Test bindet `Path.home()` an ein isoliertes Verzeichnis und prüft den
kanonischen Pfad `${HOME}/.codex/auth.json`.

Verifikation: **254 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Produktionsänderung und keine Settings-Fenster gestartet.

## Runde 921: Deadline-Berechnung direkt geprüft

`direct._direct_deadline()` hatte bisher nur indirekte Negativabdeckung.
Der neue deterministische Test fixiert `time.monotonic()` und prüft, dass ein
positives Timeout exakt als monotone Deadline zurückgegeben wird.

Verifikation: **255 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Produktionsänderung und keine Settings-Fenster gestartet.

## Runde 922: Rest-Timeout direkt geprüft

`direct._remaining_direct_timeout()` hatte bisher nur indirekte Negativabdeckung.
Der neue deterministische Test fixiert `time.monotonic()` und prüft, dass eine
noch gültige Deadline den korrekten positiven Restwert liefert.

Verifikation: **256 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Produktionsänderung und keine Settings-Fenster gestartet.

## Runde 923: Strikten Auth-Claim-Extractor direkt geprüft

`direct._strict_auth_identity_values()` hatte bisher keinen direkten
Positivtest. Der neue Test prüft angeforderte Identitätsclaims, ignoriert
unbekannte Felder und bestätigt die native String-Normalisierung über den
gemeinsamen Helpervertrag.

Verifikation: **257 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Produktionsänderung und keine Settings-Fenster gestartet.

## Runde 924: `/proc/self/fd`-Parser direkt geprüft

`direct._proc_self_fd()` hatte bisher keinen direkten Test. Die neue Matrix
prüft gültige FD-Pfade (`0`, `42`) sowie Verzeichnis- und Fremdpfade, die
korrekt als nicht vererbt verworfen werden.

Verifikation: **261 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Produktionsänderung und keine Settings-Fenster gestartet.

## Runde 925: Geerbten Auth-FD direkt geprüft

`direct._open_auth_json_fd()` hatte bisher keinen direkten Positivtest für den
`/proc/self/fd/<n>`-Pfad. Der neue Test dupliziert einen isolierten regulären
FD, liest den Inhalt aus dem Duplikat und schließt beide Deskriptoren sicher.

Verifikation: **262 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Produktionsänderung und keine Settings-Fenster gestartet.

## Runde 926: Expandierten Auth-Pfad direkt geprüft

`direct._expanded_auth_path()` hatte bisher keinen direkten Positivtest. Der
neue Test bindet `HOME` an ein isoliertes Verzeichnis und prüft die kanonische
Auflösung von `~/.codex/auth.json` ohne Produktionsänderung.

Verifikation: **263 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 927: Sichere Auth-Statistik direkt geprüft

`direct._validate_auth_json_stat()` hatte bisher keinen direkten Positivtest.
Der neue Test übergibt den nativen `stat()`-Befund einer isolierten regulären
`auth.json` mit Modus `0600` und bestätigt, dass die vollständige Sicherheits-
prüfung ohne Ausnahme durchläuft.

Verifikation: **264 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 928: Teilfenster-Konfliktprädikat direkt geprüft

`direct._has_conflicting_partial_windows()` war bisher nur über den stabilen
Antwortselektor abgedeckt. Der neue direkte Test belegt, dass ein neuester
Payload, der bei gleicher Backend-Identität ein zuvor vorhandenes Wochenfenster
verliert, als widersprüchlich erkannt wird.

Verifikation: **265 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 929: Spark-Konfliktprädikat direkt geprüft

`direct._has_conflicting_spark_limits()` war bisher nur über den stabilen
Antwortselektor abgedeckt. Der neue direkte Test prüft zwei native Spark-
Antworten mit unterschiedlichen Nutzungswerten und bestätigt die erkannte
Signaturabweichung.

Verifikation: **266 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 930: Main-Limit-Flag-Prädikat direkt geprüft

`direct._has_conflicting_main_limit_flags()` war bisher nur über den stabilen
Antwortselektor abgedeckt. Der neue direkte Test prüft zwei native Antworten
mit gegensätzlichem `allowed`-Flag und bestätigt die erkannte Signaturabweichung.

Verifikation: **267 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 931: Malformed-Main-Limit-Prädikat direkt geprüft

`direct._has_malformed_main_limit_structure()` war bisher nur über den
Antwortselektor abgedeckt. Der neue direkte Test bestätigt, dass ein
`rate_limit`-Array statt eines nativen Dicts als ungültige Struktur erkannt
wird.

Verifikation: **268 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 932: Malformed-Spark-Limit-Prädikat direkt geprüft

`direct._has_malformed_spark_limit_structure()` war bisher nur über den
Antwortselektor abgedeckt. Der neue direkte Test bestätigt, dass ein Mapping
statt einer Liste in `additional_rate_limits` als ungültige Struktur erkannt
wird.

Verifikation: **269 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 933: Response-Identifier direkt normalisiert

`direct._normalized_response_identifier()` war bisher nur über die Spark-
Erkennung abgedeckt. Der neue direkte Test prüft die Casefold-Normalisierung
eines nativen Modell-Identifier-Strings.

Verifikation: **270 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 934: Reset-Signatur-Bucketing direkt geprüft

`direct._signature_reset()` hatte bisher keinen direkten Test. Der neue
deterministische Test bestätigt die Fünf-Sekunden-Bucketbildung eines gültigen
Reset-Timestamps.

Verifikation: **271 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 935: Usage-Vollständigkeit direkt geprüft

`direct._usage_response_completeness()` hatte bisher keinen direkten Test. Der
neue Test übergibt native Primär- und Sekundärfenster mit unterstützten
Dauern/gültigen Prozentwerten und bestätigt den Vollständigkeitswert `2`.

Verifikation: **272 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 936: Usage-Progression direkt geprüft

`direct._usage_response_progresses()` hatte bisher keinen direkten Positivtest.
Der neue Test belegt zwei identische Backend-/Reset-Identitäten mit kleinem,
monotonem Nutzungsanstieg. Der erste Lauf war wegen fehlender Reset-Identität
korrekt rot; konstantes `reset_at` vervollständigt den spezifizierten Vertrag.

Verifikation: **273 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 937: Fortschritt gegenüber stabilem Quorum direkt geprüft

`direct._latest_response_progresses_beyond_group()` hatte bisher keinen
direkten Test. Der neue Test vergleicht ein stabiles Ein-Sample-Quorum mit
einem neueren Payload derselben Reset-Identität und bestätigt den Fortschritt.

Verifikation: **274 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 938: Relativen Reset-Übergang direkt geprüft

`direct._latest_response_is_relative_reset()` hatte bisher keinen direkten
Test. Der neue Test bindet gleiche Backend- und Fensteridentitäten, senkt das
Primärfenster und bewegt beide relativen Countdowns in eine frische Laufzeit.

Verifikation: **275 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 939: Absoluten Reset-Zeitstempel direkt geprüft

`direct._latest_response_is_absolute_reset()` hatte bisher keinen direkten
Test. Der neue Test hält Identität und Fensterdauer stabil, senkt die Nutzung
und verschiebt nur den primären absoluten `reset_at`-Zeitstempel nach vorn.

Verifikation: **276 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 940: Rate-Limit-Fenster direkt aufgelöst

`direct._rate_limit_window()` war bisher nur über weitere Stabilitätshelfer
abgedeckt. Der neue direkte Test prüft die Rückgabe des nativen
`primary_window`-Objekts.

Verifikation: **277 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 941: Stabile Fensteridentität direkt geprüft

`direct._progressive_window_identity_is_stable()` hatte bisher keinen direkten
Test. Der neue Test bestätigt gleiche Fensterdauer und gleiche absolute
Reset-Identität über zwei Samples.

Verifikation: **278 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 942: Reset-Regression direkt erkannt

`direct._has_reset_regression()` hatte bisher keinen direkten Test. Der neue
Test hält die absolute Reset-Identität stabil, senkt aber `used_percent`; der
Helper muss diesen Rücksprung als Regression markieren.

Verifikation: **279 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 943: Reset-Identität direkt gebucketet

`direct._signature_reset_identity()` hatte bisher keinen direkten Test. Der
neue Test bestätigt die absolute Reset-Identität und ihr Fünf-Sekunden-Bucket
für ein gültiges natives Fenster.

Verifikation: **280 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 944: Relative Reset-Phase direkt klassifiziert

`direct._signature_relative_reset_phase()` hatte bisher keinen direkten Test.
Der neue Test prüft, dass ein Countdown auf voller Fensterdauer die Phase
`fresh` erhält.

Verifikation: **281 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 945: Nicht abgelaufenes Access-Token direkt geprüft

`direct._is_access_token_expired()` hatte bisher nur einen Negativtest gegen
eine `datetime`-Subklasse. Der neue direkte Positivtest bestätigt, dass ein
zukünftiger nativer, UTC-bezogener Ablaufzeitpunkt nicht als abgelaufen gilt.

Verifikation: **282 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 946: Ablauf-Fehlertext direkt formatiert

`direct._expired_auth_error()` hatte bisher nur den fail-closed Subklassenfall
direkt geprüft. Der neue Test bindet die lokale Zeitzone deterministisch an UTC
und bestätigt die formatierte Ausgabe für ein natives Ablaufdatum.

Verifikation: **283 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 947: Gültige Usage-Fenster direkt erkannt

`direct._has_usage_values()` hatte bisher nur den Reject-/Hook-Fall direkt
geprüft. Der neue Test erzeugt zwei native `LimitWindow`-Objekte mit gültigen
Restwerten und bestätigt die positive Erkennung.

Verifikation: **284 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 948: Default-Auth-Pfadauflösung vollständig verzweigt geprüft

`direct._resolve_auth_json_path()` hatte für `auth_json_path=None` und den
leeren String nur indirekte Abdeckung. Der neue Test bindet den kanonischen
Defaultpfad und bestätigt beide Fallbackzweige ohne Dateisystemzugriff.

Verifikation: **285 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **89 %**, Missing-Zeilen 104; keine Settings-Fenster
gestartet.

## Runde 949: HTTP-401 im Direct-Fetch direkt abgebildet

`direct._fetch_wham_usage()` hatte den `HTTPError`-Authentifizierungszweig nur
indirekt über den stabilen Fetchpfad. Der neue Test injiziert einen 401-Fehler
und bestätigt die kontrollierte `DirectAuthError`-Meldung ohne Secret-Leak.

Verifikation: **286 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **89 %**, Missing-Zeilen 102; keine Settings-Fenster
gestartet.

## Runde 950: HTTP-500 im Direct-Fetch direkt abgebildet

`direct._fetch_wham_usage()` hatte den allgemeinen `HTTPError`-Zweig nur
indirekt. Der neue Test injiziert HTTP 500 und bestätigt die getrennte
`DirectFetchError`-Meldung statt einer Authentifizierungs-Reaktion.

Verifikation: **287 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **90 %**, Missing-Zeilen 101; keine Settings-Fenster
gestartet.

## Runde 951: Netzwerkfehler im Direct-Fetch direkt abgebildet

`direct._fetch_wham_usage()` hatte den `URLError`-Zweig bisher nicht direkt
belegt. Der neue Test injiziert einen Offline-Fehler und bestätigt die
redigierte Meldung `direct fetch failed: network error`.

Verifikation: **288 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **90 %**, Missing-Zeilen 100; keine Settings-Fenster
gestartet.

## Runde 952: I/O-Fehler im Direct-Fetch direkt abgebildet

`direct._fetch_wham_usage()` hatte den lokalen `OSError`-Zweig bisher nicht
direkt belegt. Der neue Test injiziert einen I/O-Fehler und bestätigt die
redigierte `direct fetch failed: I/O error`-Meldung.

Verifikation: **289 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **90 %**, Missing-Zeilen 100; keine Settings-Fenster
gestartet.

## Runde 953: Ungültigen JSON-Körper direkt abgewiesen

`direct._fetch_wham_usage()` hatte die Parsefehler nach gültigem HTTP-Transport
nicht direkt belegt. Der neue Test liefert `not-json` als JSON-Response und
bestätigt die redigierte `direct response is not valid JSON`-Ablehnung.

Verifikation: **290 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **90 %**, Missing-Zeilen 99; keine Settings-Fenster
gestartet.

## Runde 954: Oversize-Direct-Response direkt abgewiesen

`direct._fetch_wham_usage()` hatte den Körpergrößen-Guard bisher nicht direkt
belegt. Der neue Test liefert exakt ein Byte über `MAX_RESPONSE_BYTES` und
bestätigt die Ablehnung vor JSON-Parsing.

Verifikation: **291 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **90 %**, Missing-Zeilen 98; keine Settings-Fenster
gestartet.

## Runde 955: Ungültigen Wham-URL-Port direkt abgewiesen

`direct._is_trusted_wham_response_url()` hatte den `urlsplit`-/Port-
Fehlerzweig bisher nur indirekt. Der neue Test liefert einen nichtnumerischen
Port und bestätigt fail-closed `False`.

Verifikation: **292 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **90 %**, Missing-Zeilen 96; keine Settings-Fenster
gestartet.

## Runde 956: ISO-UTC-Zeitstempel direkt geparst

`direct._parse_iso_datetime()` hatte bisher nur einen Subklassen-Reject-Test.
Der neue Positivtest bestätigt die native ISO-8601-`Z`-Auflösung in einen
UTC-bezogenen `datetime`-Wert.

Verifikation: **293 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **90 %**, Missing-Zeilen 93; keine Settings-Fenster
gestartet.

## Runde 957: Explizite Credit-Skalare direkt extrahiert

`direct._credit_window()` hatte bisher nur verschachtelte Balance- und
Reject-Fälle direkt belegt. Der neue Test akzeptiert einen expliziten nativen
String-Skalar, wandelt ihn sicher in `remaining=123.5` um und kennzeichnet das
Fenster als `credits`.

Verifikation: **294 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **90 %**, Missing-Zeilen 92; keine Settings-Fenster
gestartet.

## Runde 958: Credit-Restmenge direkt abgeleitet

`direct._credit_window()` hatte die `used`-/`limit`-Ableitung bisher nur
indirekt. Der neue Test bestätigt `remaining=limit-used` und die daraus
berechnete Prozentangabe für eine explizite Credit-Struktur.

Verifikation: **295 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **90 %**, Missing-Zeilen 91; keine Settings-Fenster
gestartet.

## Runde 959: Credit-Prozent aus Restmenge direkt projiziert

`direct._credit_window()` hatte den `remaining`-/`limit`-Pfad bisher nur
indirekt. Der neue Test bestätigt eine Restmenge von 50 bei Limit 100 ohne
`used`-Feld und die daraus abgeleitete Prozentangabe.

Verifikation: **296 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 960: Credit-Prozent-only direkt akzeptiert

`direct._credit_window()` hatte den alleinigen Prozentwert bisher nur
indirekt. Der neue Test bestätigt eine gültige `percent=75`-Balance ohne
Restmenge.

Verifikation: **297 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 961: Nicht retrybaren Direct-Auth-Status direkt abgewiesen

`direct._is_retryable_direct_auth_error()` hatte bisher nur den Subklassen-
Reject direkt belegt. Der neue Test bestätigt, dass ein passender, aber nicht
retrybarer HTTP-500-Status `False` liefert.

Verifikation: **298 Direct-Tests**, Ruff und Diff-Check bestanden; keine
Settings-Fenster gestartet.

## Runde 962: Nicht unterstütztes Backend-Fenster direkt gemeldet

`direct._missing_usage_limits_error()` hatte den Fall eines formal gültigen,
aber nicht unterstützten `limit_window_seconds`-Werts bisher nicht direkt
belegt. Der neue Test bestätigt die sortierte Backend-Fensterangabe samt
normalisiertem Plan im Fehlertext.

Verifikation: **299 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **92 %**, Missing-Zeilen 88; keine Settings-Fenster
gestartet.

## Runde 963: Nicht retrybaren Auth-Fehler direkt beendet

`fetch_account_usage_direct()` hatte den Nicht-Retry-Zweig nach einem direkten
Auth-Fehler noch nicht über den öffentlichen Rückgabepfad belegt. Der neue
Test bestätigt, dass HTTP 500 keinen zweiten Auth-Read oder Usage-Versuch
auslöst und als `LOGIN_REQUIRED` zurückkommt.

Verifikation: **300 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **92 %**, Missing-Zeilen 87; keine Settings-Fenster
gestartet.

## Runde 964: Ursprünglichen Auth-Fehler bei fehlgeschlagenem Refresh erhalten

`fetch_account_usage_direct()` hatte den Ausnahmezweig beim zweiten
`auth.json`-Laden nach HTTP 401/403 noch nicht direkt belegt. Der neue Test
bestätigt, dass ein Refresh-Lesefehler den ursprünglichen Direct-Auth-Fehler
weitergibt und kein inkonsistenter Ersatztext entsteht.

Verifikation: **301 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **92 %**, Missing-Zeilen 85; keine Settings-Fenster
gestartet.

## Runde 965: Backend-only-Identität direkt abgewiesen

`fetch_account_usage_direct()` hatte den Guard nach erfolgreicher
Backend-Kanonisierung, aber fehlender Auth-Identität noch nicht direkt
belegt. Der neue Test bestätigt fail-closed `backend response identity cannot
be verified`, auch wenn Backend User- und Account-ID liefert.

Verifikation: **302 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **92 %**, Missing-Zeilen 84; keine Settings-Fenster
gestartet.

## Runde 966: Nicht-finite Direct-Deadline direkt abgewiesen

`direct._direct_deadline()` hatte den Schutz gegen eine nicht-finite
`time.monotonic()`-Deadline noch nicht direkt belegt. Der neue Test bestätigt
die sichere `DirectFetchError`-Ablehnung bei `inf`.

Verifikation: **303 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **92 %**, Missing-Zeilen 83; keine Settings-Fenster
gestartet.

## Runde 967: Auth-Fehler ohne HTTP-Code direkt abgewiesen

`direct._is_retryable_direct_auth_error()` hatte den Regex-No-Match-Zweig noch
nicht direkt belegt. Der neue Test bestätigt `False` für einen Auth-Fehlertext
ohne HTTP-Status und hält Retry auf bekannte 401/403-Signaturen begrenzt.

Verifikation: **304 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **93 %**, Missing-Zeilen 82; keine Settings-Fenster
gestartet.

## Runde 968: Ungültiges Auth-JSON direkt abgewiesen

`direct._load_auth_token_and_metadata()` hatte den Parsefehler-Zweig nach
`read_auth_json_file()` noch nicht direkt belegt. Der neue Test bestätigt den
Pfad-gebundenen `invalid auth.json`-Fehlertext bei ungültigem JSON.

Verifikation: **305 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **93 %**, Missing-Zeilen 80; keine Settings-Fenster
gestartet.

## Runde 969: Ungültiges Auth-JSON im Identitätsleser direkt abgewiesen

`direct.auth_identity_from_file()` hatte den Parsefehler nach dem Datei-Read
noch nicht direkt belegt. Der neue Test bestätigt den kontextgebundenen
`invalid auth.json`-Fehler statt einer unkontrollierten JSON-Ausnahme.

Verifikation: **306 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **93 %**, Missing-Zeilen 78; keine Settings-Fenster
gestartet.

## Runde 970: Leere Auth-Identität sauber zurückgegeben

`direct.auth_identity_from_file()` hatte den erfolgreichen Rückgabepfad nach
gültigem JSON noch nicht direkt belegt. Der neue Test bestätigt `(None, None)`
für eine tokenlose, syntaktisch gültige Datei.

Verifikation: **307 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **93 %**, Missing-Zeilen 77; keine Settings-Fenster
gestartet.

## Runde 971: Account-Identitätshelper deckt Pfadvarianten ab

`direct.auth_identity_for_account()` hatte fehlenden, leeren und gesetzten
`auth_json_path` bisher nicht direkt zusammen belegt. Die neuen Tests
bestätigen `(None, None)` für fehlende Konfiguration und die korrekte
Delegation eines gesetzten Pfads.

Verifikation: **310 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **93 %**, Missing-Zeilen 73; keine Settings-Fenster
gestartet.

## Runde 972: Auth-E-Mail-Parser vollständigere Fehlerpfade

`direct.auth_email_from_payload()` und `auth_email_from_file()` hatten
ungültige bzw. widersprüchliche Claim-Werte sowie Parse- und Positivpfade noch
nicht direkt belegt. Vier Tests bestätigen präzise Ablehnung und leere
Rückgabe.

Verifikation: **314 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **94 %**, Missing-Zeilen 65; keine Settings-Fenster
gestartet.

## Runde 973: Auth-Plan-Typ-Parserpfade direkt belegt

`direct.auth_plan_type_from_payload()`, `auth_plan_type_from_file()` und
`auth_plan_type_for_account()` hatten Namespace-Fallback, Konflikt, Datei-
Parse/Positivpfad sowie fehlende Account-Pfade noch offen. Sieben Tests
bestätigen diese Fälle.

Verifikation: **321 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **95 %**, Missing-Zeilen 56; keine Settings-Fenster
gestartet.

## Runde 974: Ungültige Auth-Identitäts-/Plan-Strings direkt abgewiesen

`direct._safe_auth_identity()` und `_safe_auth_plan_type()` hatten die
inhaltlich ungültigen, aber typmäßig korrekten Strings noch nicht direkt
belegt. Zwei Tests bestätigen fail-closed `None` für Leer-/Whitespace-Werte.

Verifikation: **323 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **95 %**, Missing-Zeilen 54; keine Settings-Fenster
gestartet.

## Runde 975: Fremde Backend-User-ID direkt abgewiesen

`direct._response_identity_matches_auth()` hatte den Mismatch-Zweig ohne
Account-IDs noch nicht direkt belegt. Der neue Test bestätigt fail-closed
`False` bei abweichender Backend- und Auth-User-ID.

Verifikation: **324 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **95 %**, Missing-Zeilen 53; keine Settings-Fenster
gestartet.

## Runde 976: Übergroßes Access-Token direkt abgewiesen

`direct._extract_auth_details()` hatte die harte
`MAX_ACCESS_TOKEN_CHARS`-Grenze noch nicht direkt belegt. Der neue Test
bestätigt die frühe Ablehnung vor JWT-Parsing.

Verifikation: **325 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **95 %**, Missing-Zeilen 52; keine Settings-Fenster
gestartet.

## Runde 977: Nicht-finite JWT-Ablaufzeit direkt abgewiesen

`direct._validate_access_token_expiry()` hatte den `math.isfinite`-/Exception-
Pfad bisher nur über typfremde Werte gestreift. Der neue Test injiziert eine
native `inf`-Expiry und bestätigt die sichere `DirectAuthError`-Ablehnung.

Verifikation: **326 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **96 %**, Missing-Zeilen 49; keine Settings-Fenster
gestartet.

## Runde 978: Ungültige Proc-FD-Nummer direkt abgewiesen

`direct._proc_self_fd()` hatte den defensiven `int()`-Fehlerzweig noch nicht
direkt belegt. Der neue Test simuliert einen nichtnumerischen Regex-Treffer
und bestätigt `None` statt einer Ausnahme.

Verifikation: **327 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **96 %**, Missing-Zeilen 47; keine Settings-Fenster
gestartet.

## Runde 979: Inherited-Auth-FD-Fehler sicher bereinigt

`direct._open_auth_json_fd()` hatte die Fehlerfälle eines geschlossenen
Inherited-FDs und eines Fehlschlags nach erfolgreichem `dup()` noch nicht
direkt belegt. Zwei Tests bestätigen Fehlerweitergabe und Schließen des
Duplikats.

Verifikation: **329 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **96 %**, Missing-Zeilen 43; keine Settings-Fenster
gestartet.

## Runde 980: Directory-Errno beim Auth-Open korrekt gemappt

`direct._open_auth_json_fd()` hatte das `EISDIR`-/Nicht-reguläre-Datei-
Mapping von `os.open` noch nicht direkt belegt. Der neue Test bestätigt den
fachlich passenden Fehlertext statt eines generischen Leseproblems.

Verifikation: **330 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **96 %**, Missing-Zeilen 42; keine Settings-Fenster
gestartet.

## Runde 981: Auth-Dateileser-I/O-Fehler direkt gemappt

`direct.read_auth_json_file()` hatte den `OSError`-Handler bei `fstat`/Read
noch nicht direkt belegt. Der neue Test bestätigt den sicheren Fehlertext und
das abschließende Schließen des Dateideskriptors.

Verifikation: **331 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **96 %**, Missing-Zeilen 41; keine Settings-Fenster
gestartet.

## Runde 982: Post-Read-Auth-Größenlimit direkt belegt

`direct.read_auth_json_file()` hatte den zweiten Oversize-Schutz nach dem
tatsächlichen Read noch nicht direkt belegt; der Stat-Guard greift normalerweise
früher. Der neue Test bestätigt die erneute Begrenzung mit
`MAX_AUTH_JSON_BYTES`.

Verifikation: **332 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **96 %**, Missing-Zeilen 40; keine Settings-Fenster
gestartet.

## Runde 983: Ungültiges UTF-8 in Auth-Datei direkt abgewiesen

`direct.read_auth_json_file()` hatte den Decode-Fehlerpfad nach erfolgreichem
Read noch nicht direkt belegt. Der neue Test bestätigt `invalid auth.json` mit
konkretem Pfad statt einer rohen `UnicodeDecodeError`-Weitergabe.

Verifikation: **333 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **97 %**, Missing-Zeilen 38; keine Settings-Fenster
gestartet.

## Runde 984: Validate-Fstat-Fehler direkt gemappt

`direct.validate_auth_json_file()` hatte den `os.fstat`-Fehlerpfad noch nicht
direkt belegt. Der neue Test bestätigt kontrollierte Fehlerweitergabe und
verlässliches Schließen des Dateideskriptors.

Verifikation: **334 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **97 %**, Missing-Zeilen 36; keine Settings-Fenster
gestartet.

## Runde 985: Stable-Selector-ValueError direkt gemappt

`direct._fetch_stable_wham_usage()` hatte den unerwarteten `ValueError` des
Stabilitätsselectors noch nicht direkt belegt. Der neue Test bestätigt die
Umwandlung in `DirectFetchError` ohne Retry- oder Iterator-Leak.

Verifikation: **335 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **97 %**, Missing-Zeilen 35; keine Settings-Fenster
gestartet.

## Runde 986: Unvollständigen Stable-Sample-Iterator kontrolliert behandelt

`direct._fetch_stable_wham_usage()` hatte den defensiven
`StopIteration`-Pfad für Test-/Stub-Iteratoren noch nicht direkt belegt. Der
neue Test bestätigt die explizite Weitergabe bei unvollständigem Batch.

Verifikation: **336 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **97 %**, Missing-Zeilen 34; keine Settings-Fenster
gestartet.

## Runde 987: Latest-Sample bei gleichbleibender Nutzung und Reset direkt gewählt

`direct._select_stable_wham_usage()` hatte den Reset-Rückgabepfad ohne
Nutzungsabfall noch nicht direkt belegt. Der neue Test bestätigt, dass ein
vorgerückter absoluter Reset mit gleicher Nutzung als aktuelles Sample
zurückgegeben wird.

Verifikation: **337 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **97 %**, Missing-Zeilen 33; keine Settings-Fenster
gestartet.

## Runde 988: Fremde Partial-Window-Gruppe direkt übersprungen

`direct._has_conflicting_partial_windows()` hatte den Backend-Identity-
`continue`-Zweig noch nicht direkt belegt. Der neue Test bestätigt, dass
Fenster eines fremden Accounts nicht in die Konfliktaggregation einfließen.

Verifikation: **338 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **97 %**, Missing-Zeilen 32; keine Settings-Fenster
gestartet.

## Runde 989: Letzten Stable-Fetch-Fehler nach Iteratorende erhalten

`direct._fetch_stable_wham_usage()` hatte den defensiven Schleifen-Tail nach
vorzeitigem Attempt-Iteratorende noch nicht direkt belegt. Der neue Test
bestätigt, dass der letzte `DirectFetchError` unverändert weitergegeben wird.

Verifikation: **339 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **97 %**, Missing-Zeilen 30; keine Settings-Fenster
gestartet.

## Runde 990: Reset-Helper-Guardpfade direkt belegt

`direct._latest_response_is_relative_reset()` und
`_latest_response_is_absolute_reset()` hatten Grenzfälle für kurze/alte
Gruppen, fremde Identitäten, fehlende Gegenfenster/Nutzungswerte und fehlende
Reset-Identitäten noch offen. Acht fokussierte Assertions bestätigen alle
fail-closed Rückgaben.

Verifikation: **345 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **98 %**, Missing-Zeilen 18; keine Settings-Fenster
gestartet.

## Runde 991: Progressive-Window-Mismatchpfade direkt abgewiesen

`direct._latest_response_progresses_beyond_group()` hatte die Fälle
fehlender Gegenfenster, fehlender Nutzungswerte und rückläufiger Nutzung noch
offen. Drei parametrisierte Tests bestätigen jeweils fail-closed `False`.

Verifikation: **348 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **99 %**, Missing-Zeilen 15; keine Settings-Fenster
gestartet.

## Runde 992: Header-Getter-Fehler im Content-Type-Parser abgefangen

`direct._response_content_type()` hatte die geschützte Ausnahme beim
Header-`.get()` noch nicht direkt belegt. Der neue Test bestätigt Fallback auf
`getheader()` bei einem fehlerhaften Header-Mapping.

Verifikation: **349 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **99 %**, Missing-Zeilen 13; keine Settings-Fenster
gestartet.

## Runde 993: Hostlose URL direkt verworfen

`direct._redact_url()` hatte den Hostname-Guard noch nicht direkt belegt. Der
neue Test bestätigt leere Ausgabe für `https:///path` und verhindert damit
eine scheinbar vertrauenswürdige Zieladresse.

Verifikation: **350 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **99 %**, Missing-Zeilen 12; keine Settings-Fenster
gestartet.

## Runde 994: Ungültigen ISO-Zeitstempel direkt abgewiesen

`direct._parse_iso_datetime()` hatte den echten `datetime.fromisoformat`-
Fehlerpfad noch nicht direkt belegt. Der neue Test bestätigt `None` für
ungültigen Text.

Verifikation: **351 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **99 %**, Missing-Zeilen 10; keine Settings-Fenster
gestartet.

## Runde 995: Verschachtelte Credit-Quellen direkt extrahiert

`direct._credit_window()` hatte die nativen `rateLimits`- und
`rateLimitsByLimitId`-Quellen bisher nur über Reject-Hooks berührt. Der neue
Test bestätigt echte Restmengen aus beiden verschachtelten Strukturen.

Verifikation: **352 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **99 %**, Missing-Zeilen 6; keine Settings-Fenster
gestartet.

## Runde 996: Credit-Prozent über 100 direkt verworfen

`direct._credit_window()` hatte den Schutz gegen überhöhte Prozentwerte noch
nicht direkt belegt. Der neue Test bestätigt, dass `percent=101` nicht als
Credit-Fenster ausgegeben wird.

Verifikation: **353 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **99 %**, Missing-Zeilen 5; keine Settings-Fenster
gestartet.

## Runde 997: Nicht-finite JWT-Expiry direkt verworfen

`direct._jwt_expiry()` hatte den `datetime.fromtimestamp`-Exceptionpfad für
native, nicht-finite Werte noch nicht direkt belegt. Der neue Test bestätigt
sichere `None`-Rückgabe bei `exp=inf`.

Verifikation: **354 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **99 %**, Missing-Zeilen 3; keine Settings-Fenster
gestartet.

## Runde 998: Current-JWT-Expiry vollständig fail-closed

`direct._current_jwt_claims()` hatte die letzten beiden offenen
Expiry-Zweige: native `inf`-Werte und `float()`-Overflow bei riesigen Integern.
Zwei Tests bestätigen jeweils `None`.

Verifikation: **356 Direct-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `direct.py`: **100 %**, keine Missing-Zeilen; keine Settings-Fenster
gestartet.

## Runde 999: Gleichlautende Usage-Reset-Quelle direkt akzeptiert

`usage_resets._parse_usage_resets_mapping()` hatte den konfliktfreien
Duplicate-`continue`-Zweig für vollständige Canonical-/Legacy-Daten noch nicht
direkt belegt. Der neue Test bestätigt die konsistente `UsageResetState`-
Rückgabe.

Verifikation: **23 Usage-Reset-Tests**, Ruff und Diff-Check bestanden; Coverage-
Auszug für `usage_resets.py`: **100 %**, keine Missing-Zeilen; keine
Settings-Fenster gestartet.

## Runde 1000: Ungültige WHAM-Spark-Einträge fail-closed behandelt

`usage_limits.parse_wham_usage_pools()` hatte die beiden Pfade für ungültige
Spark-Einträge noch nicht direkt belegt: Ein ungültiger Duplikateintrag muss
einen vorhandenen gültigen Spark-Pool deaktivieren; bei ausschließlich
ungültigen Spark-Daten muss trotzdem ein nicht verfügbarer Spark-Pool
materialisiert werden. Zwei Regressionstests bestätigen beide Verträge.

Verifikation: **129 Usage-Limits-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `usage_limits.py`: **96 %**, verbleibende Missing-Zeilen
liegen ausschließlich in weiteren Parser-/Guardpfaden (163, 187, 212, 278,
366, 436, 465, 512, 526, 538–539, 571–572); keine Settings-Fenster gestartet.

## Runde 1001: App-Server-Main- und Spark-Invalidpfade direkt belegt

`usage_limits.parse_app_server_usage_pools()` hatte noch drei defensive
Zweige ohne direkte Regression: Ein malformed verschachteltes Main-Window
kann ohne gültige Restdaten nur einen nicht verfügbaren Main-Pool erzeugen;
ein ungültiger Spark-Dictionary-Bucket wird als invalid markiert; bei nur
solchen Spark-Daten wird ein nicht verfügbarer Spark-Pool materialisiert. Zwei
Tests bestätigen diese fail-closed Semantik.

Verifikation: **131 Usage-Limits-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `usage_limits.py`: **97 %**, verbleibende Missing-Zeilen
liegen ausschließlich in weiteren Fallback-/Identitäts-/Konversionspfaden
(278, 366, 436, 465, 512, 526, 538–539, 571–572); keine Settings-Fenster
gestartet.

## Runde 1002: Legacy-Fenster ohne Main-Pool direkt abgesichert

`usage_limits.legacy_windows()` hatte den `main is None`-Guard noch nicht
direkt getestet. Ein Regressionstest bestätigt, dass beide Legacy-Slots bei
fehlendem Main-Pool sicher als `None` zurückgegeben werden.

Verifikation: **132 Usage-Limits-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `usage_limits.py`: **97 %**, verbleibende Missing-Zeilen
liegen ausschließlich in Fallback-/Identitäts-/Konversionspfaden (366, 436,
465, 512, 526, 538–539, 571–572); keine Settings-Fenster gestartet.

## Runde 1003: Ungültige App-Server-Fallback-Dauer fail-closed behandelt

`usage_limits._app_server_pool()` hatte den Guard für ein fehlendes Main-
Window mit nichtnumerischer Gegenfenster-Dauer noch offen. Der neue Test
bestätigt, dass daraus kein erfundenes Fenster entsteht und der Main-Pool
`None` bleibt.

Verifikation: **133 Usage-Limits-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `usage_limits.py`: **97 %**, verbleibende Missing-Zeilen
liegen ausschließlich in Fenster-/Identitäts-/Zeit- und Prozentkonversionen
(436, 465, 512, 526, 538–539, 571–572); keine Settings-Fenster gestartet.

## Runde 1004: Leere WHAM-/App-Server-Fenster fail-closed verworfen

`_wham_window()` und `_app_server_window()` hatten den Fall einer formal
gültigen, aber vollständig leeren Mapping-Eingabe noch nicht direkt belegt.
Ein gemeinsamer Regressionstest bestätigt, dass ohne Dauer, Nutzung oder
Resetdatum kein erfundenes Fenster entsteht.

Verifikation: **134 Usage-Limits-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `usage_limits.py`: **98 %**, verbleibende Missing-Zeilen
liegen ausschließlich in Fensteridentitäts-, Zeit- und Prozentkonversionen
(512, 526, 538–539, 571–572); keine Settings-Fenster gestartet.

## Runde 1005: Unbekannte Fensteridentitäten fail-closed abgesichert

`_window_identities_are_unique()` hatte die defensiven Pfade für nichtstringige
und nicht abbildbare Namen hinter einem bereits als vertrauenswürdig markierten
Fensterobjekt noch offen. Parametrisierte Regressionen bestätigen, dass beide
Fälle abgewiesen werden; normale Modellobjekte bleiben unverändert.

Verifikation: **136 Usage-Limits-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `usage_limits.py`: **99 %**, verbleibende Missing-Zeilen
liegen ausschließlich in absoluter Reset-Zeit- und Prozentkonversion
(538–539, 571–572); keine Settings-Fenster gestartet.

## Runde 1006: Unrepräsentierbare absolute Reset-Zeit fail-closed verworfen

`usage_limits._reset_at()` hatte die Ausnahmebehandlung bei einer formal
ganzzahligen, aber nicht darstellbaren Epoch-Zeit noch nicht direkt belegt.
Ein Regressionstest bestätigt `None` statt eines Parserabbruchs.

Verifikation: **137 Usage-Limits-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `usage_limits.py`: **99 %**, verbleibende Missing-Zeilen
liegen ausschließlich in der abgesicherten Float-Konversion von
Prozentwerten (571–572); keine Settings-Fenster gestartet.

## Runde 1007: Usage-Limits-Modul vollständig abgedeckt

`usage_limits._percent()` hatte zuletzt noch die Ausnahmebehandlung bei der
Konversion eines extrem großen Built-in-Integerwerts offen. Der neue Test
bestätigt sichere `None`-Rückgabe statt eines `OverflowError`.

Verifikation: **138 Usage-Limits-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `usage_limits.py`: **100 %**, keine Missing-Zeilen; keine
Settings-Fenster gestartet.

## Runde 1008: Dynamische Fensteridentitäten im Modell direkt belegt

`models.LimitWindow.has_known_identity()` hatte die kanonischen
Fallback-Namen für dynamische Tages-, Stunden- und Sekundenfenster noch nicht
direkt getestet. Drei Parametervarianten bestätigen `1d`, `1h` und `61s`.

Verifikation: **38 Model-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `models.py`: **81 %**, verbleibende Missing-Zeilen liegen
in weiteren Identity-, Usage-, Pool- und Serialisierungszweigen; keine
Settings-Fenster gestartet.

## Runde 1009: Model-Pool-Identitätslookup defensiv abgesichert

`UsagePool.window_for_duration()` hatte erfolgreiche Auflösung über explizite
Dauer und kanonischen Fensternamen noch nicht direkt belegt. Zusätzlich prüft
ein Regressionstest den nicht-hashbaren Identitätswert; der Pool bleibt dabei
fail-closed statt mit einem `TypeError` abzubrechen.

Verifikation: **40 Model-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `models.py`: **87 %**, verbleibende Missing-Zeilen liegen
in weiteren Usage-, Pool- und Serialisierungszweigen; keine Settings-Fenster
gestartet.

## Runde 1010: Remaining-Percent-Quellen im Modell direkt geprüft

`LimitWindow.remaining_percent` hatte gültige absolute Nutzung, Restmenge,
passenden/abweichenden expliziten Prozentwert und Prozent-only-Fallback noch
nicht vollständig direkt belegt. Zusätzlich prüfen die Tests `limit=0` und
Restmengen ohne Limit als fail-closed Invalidwerte.

Verifikation: **47 Model-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `models.py`: **92 %**, verbleibende Missing-Zeilen liegen
in defensiven Exception-/Pool-/Serialisierungszweigen; keine Settings-Fenster
gestartet.

## Runde 1011: Defensive Remaining-Percent-Guards direkt ausgeführt

Die internen `LimitWindow.remaining_percent`-Guards für nichtfinite Werte,
Null-Limit, ausgeschöpftes Limit, ungültige Restmenge und vollständig leere
Werte waren nur durch den vorgelagerten Validierungsvertrag geschützt. Ein
kontrolliertes Testdouble bestätigt diese fail-closed Rückgaben direkt, ohne
Produktionslogik zu ändern.

Verifikation: **52 Model-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `models.py`: **94 %**, verbleibende Missing-Zeilen liegen
in Identity-, Pool- und Serialisierungs-Exceptionpfaden; keine Settings-Fenster
gestartet.

## Runde 1012: Strikte Fensteridentität mit expliziter Dauer geprüft

`LimitWindow.has_known_identity()` hatte die Abzweige für bekannte Aliasnamen
mit passender/falscher expliziter Dauer sowie nicht-stringige Namen noch offen.
Parametrisierte Tests bestätigen Annahme nur bei konsistenter Identität und
fail-closed Ablehnung sonstiger Werte.

Verifikation: **55 Model-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `models.py`: **94 %**, verbleibende Missing-Zeilen liegen
in Pool-/Serialisierungs- und defensiven Exceptionpfaden; keine Settings-Fenster
gestartet.

## Runde 1013: UsagePool-Validity und Exhaustion fail-closed geprüft

`UsagePool.has_valid_usage` und `.exhausted` hatten noch ungetestete Grenzen
für Property-Fehler, nicht-tuple Fenster, leere Usage-Quellen und ungültige
Control-Flags. Sieben Regressionen bestätigen sichere False/True-Fallbacks
ohne Ausnahmeleck.

Verifikation: **62 Model-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `models.py`: **98 %**, verbleibende Missing-Zeilen liegen
in AccountUsage-/Serialisierungs- und einem inkonsistenten Reset-Guard;
keine Settings-Fenster gestartet.

## Runde 1014: Expliziten AccountUsage-Main-Pool erhalten

`AccountUsage.__post_init__()` hatte den Preserve-Return bei bereits gesetztem
Main-Pool noch nicht direkt belegt. Der neue Test bestätigt, dass vorhandener
Pool nicht durch Legacy-Fenster rekonstruiert oder überschrieben wird.

Verifikation: **63 Model-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `models.py`: **98 %**, verbleibende Missing-Zeilen liegen
in einem inkonsistenten Reset-Guard und Serialisierungs-Exceptionpfaden; keine
Settings-Fenster gestartet.

## Runde 1015: Wiederholte ambige Modellschlüssel nicht serialisiert

`AccountUsage.as_dict()` hatte den `ambiguous_model_keys`-Skip bei einem
dritten Eintrag desselben normalisierten Schlüssels noch nicht direkt
belegt. Der Regressionstest bestätigt, dass ambige Pools vollständig aus der
serialisierten Modellkarte fernbleiben.

Verifikation: **63 Model-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `models.py`: **98 %**, verbleibende Missing-Zeilen liegen
in Reset-/Pool- und Float-Exceptionpfaden; keine Settings-Fenster gestartet.

## Runde 1016: Pool-Serialisierung bei Exhaustion-Fehler fail-closed

`models._pool_to_dict()` hatte den Ausnahme-Fallback beim Zugriff auf
`pool.exhausted` noch nicht direkt belegt. Ein Testdouble bestätigt, dass der
serialisierte Pool stattdessen sicher `exhausted: true` erhält.

Verifikation: **64 Model-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `models.py`: **99 %**, verbleibende Missing-Zeilen liegen
im inkonsistenten Reset-Guard und Float-Overflow-Fallback; keine
Settings-Fenster gestartet.

## Runde 1017: Float-Overflow im Modell-Helper fail-closed

`models._finite_number()` hatte den `OverflowError` bei einem extrem großen
Built-in-Integer noch nicht direkt getestet. Ein Regressionstest bestätigt
sichere `None`-Rückgabe; der Helper lässt keine nicht darstellbare Zahl in
Usage-/JSON-Pfade gelangen.

Verifikation: **65 Model-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `models.py`: **99 %**, einzige Missing-Zeile bleibt der
redundante inkonsistente-Reset-Guard (186); keine Settings-Fenster gestartet.

## Runde 1018: Model-Modul vollständig abgedeckt

Der letzte `LimitWindow.has_invalid_usage_value`-Guard für inkonsistente
Konversion (`remaining` zunächst `None`, anschließend wieder numerisch) war
semantisch redundant, aber defensiv vorhanden. Ein kontrollierter
Converter-Test bestätigt ihn direkt.

Verifikation: **66 Model-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `models.py`: **100 %**, keine Missing-Zeilen; keine
Settings-Fenster gestartet.

## Runde 1019: JSON-Helper vollständig abgedeckt

`json_utils` hatte noch Bytes-/Bytearray-Scanning, die Abbildung eines
Parser-`RecursionError` und das Ablehnen nichtstandardmäßiger Konstanten offen.
Drei Regressionen bestätigen jeweils sichere Validierungsfehler.

Verifikation: **13 JSON-Utility-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `json_utils.py`: **100 %**, keine Missing-Zeilen; keine
Settings-Fenster gestartet.

## Runde 1020: `load_config()`-Grenzen direkt belegt

`config.load_config()` hatte die Pfade für einen gebrochenen Symlink, einen
nicht-listigen `accounts`-Wert und ein Intervall unter 60 Sekunden noch offen.
Drei Regressionen bestätigen jeweils explizite `ValueError`-Ablehnung.

Verifikation: **123 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **85 %**, verbleibende Missing-Zeilen liegen
in weiteren Account-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1021: Config-Account-Cap direkt abgesichert

`load_config()` hatte die Begrenzung auf `MAX_CONFIG_ACCOUNTS` noch nicht
direkt getestet. Ein Regressionstest mit 101 TOML-Accounts bestätigt
fail-closed Ablehnung vor Account-Materialisierung.

Verifikation: **124 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **86 %**, verbleibende Missing-Zeilen liegen
in weiteren Account-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1022: Config-Save-Größenlimit direkt geprüft

`config._save_config_unlocked()` hatte den Guard für übergroßen bereits
serialisierten TOML-Text noch nicht direkt belegt. Ein Testdouble bestätigt
Ablehnung vor dem Dateischreiben.

Verifikation: **125 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **86 %**, verbleibende Missing-Zeilen liegen
in weiteren Account-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1023: Nicht auflösbares Config-Verzeichnis fail-closed

`config._prepare_config_directory()` hatte den Fehler beim Auflösen des
Verzeichnisses noch nicht direkt getestet. Ein kontrollierter Resolve-Fehler
wird als verständlicher `ValueError` gemeldet, bevor Dateisystemänderungen
erfolgen.

Verifikation: **126 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **86 %**, verbleibende Missing-Zeilen liegen
in weiteren Account-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1024: Symlink-Guard am Config-Verzeichnis direkt geprüft

`_prepare_config_directory()` hatte den expliziten `config_dir.is_symlink()`-
Guard hinter dem vorgelagerten Ancestor-Check noch nicht direkt erreicht. Ein
isolierter Ancestor-Stub bestätigt zusätzlich diese zweite Schranke; ein
symlinkisches Ziel wird vor jeder Sicherung abgewiesen.

Verifikation: **127 Config-Tests**, fokussierter Test, Ruff und Diff-Check
bestanden; Coverage-Auszug für `config.py`: **86 %**, verbleibende Missing-
Zeilen liegen in weiteren Account-/Rollback-/Pfad- und Serialisierungspfaden;
keine Settings-Fenster gestartet.

## Runde 1025: Optionale Account-Flags strikt validiert

`add_or_update_account()` hatte Guards für Tag-/Series-/Test-Home-Typen,
inkompatible Auth-Optionen und den internen Lock-Parameter noch nicht direkt
abgedeckt. Sechs parametrische Regressionen bestätigen klare `ValueError`-
Ablehnung vor jedem Config-I/O.

Verifikation: **133 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **87 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1026: Account-Datenparser bei Container-/Leerwert-Grenzen

`_account_from_data()` hatte die explizite Ablehnung eines Nicht-TOML-Table-
Containers und die Normalisierung eines leeren `auth_json_path` noch nicht
direkt getestet. Zwei Regressionen bestätigen strukturierte Ablehnung bzw.
kanonisches `None`.

Verifikation: **135 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **87 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1027: Optionale Account-Feldtypen im Parser abgesichert

`_account_from_data()` hatte die Nicht-String-Pfade für `tag`,
`reactivation_browser` und `series` noch offen. Drei parametrisierte Tests
bestätigen jeweils klare Ablehnung vor Pfad-/Config-Auflösung.

Verifikation: **138 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **88 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1028: Gültige optionale Account-Felder normalisiert

`_account_from_data()` hatte den positiven Pfad für Tag,
`reactivation_browser` und Serie noch nicht direkt belegt. Ein Test bestätigt
gültige Werte sowie die beabsichtigte Großschreibung der Serie.

Verifikation: **139 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **88 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1029: Test-Home-Auth-Integration an Dateigrenzen geprüft

`_integrate_test_home_auth()` hatte den Identitäts-No-op bei gleichem Quell-
und Zielpfad, die Ablehnung einer Nicht-Datei sowie das bereits belegte Ziel
noch nicht direkt abgedeckt. Drei Tests bestätigen jeweils unveränderte Quelle
oder fail-closed `ValueError`.

Verifikation: **142 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **89 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1030: Test-Codex-Home-Grenzen direkt abgesichert

`_prepare_test_codex_home()` hatte noch ungetestete Pfade für eine nicht
reguläre `config.toml`, das Ergänzen des File-Credential-Settings und Fehler
beim `codex --help`-Probeprozess. Vier Tests bestätigen sichere Ablehnung,
idempotente Ergänzung und konsistente Fehlerabbildung für OSError/Timeout.

Verifikation: **146 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **89 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1031: Fremder Auth-Dateibesitzer fail-closed abgewiesen

`_integrate_test_home_auth()` hatte die Bedingung für einen nicht zum Prozess
gehörenden Dateibesitzer noch nicht direkt ausgeführt. Ein kontrollierter
UID-Test bestätigt Ablehnung vor dem Verschieben der Quelle.

Verifikation: **147 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **89 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1032: Auth-Restore-Guards direkt belegt

`_restore_moved_test_home_auth()` hatte die Fälle einer bereits vorhandenen
Rollback-Quelle und eines verschwundenen Ziels noch nicht direkt getestet.
Zwei Regressionen bestätigen, dass beide Zustände ohne Verschieben abgelehnt
werden.

Verifikation: **149 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **90 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1033: Created-Home-Cleanup bei Missing/Symlink abgesichert

`_cleanup_created_test_home()` hatte die Fälle verschwundener Dateien/
Verzeichnisse und gebrochener Symlinks noch nicht direkt belegt. Vier
parametrisierte Tests bestätigen toleriertes Verschwinden und fail-closed
Abbruch bei Identitätsverfälschung.

Verifikation: **153 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **91 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1034: Absolute Account-Pfade strikt begrenzt

`_absolute_account_path()` hatte noch keinen direkten Test für relative Pfade
und File-URIs mit Query oder Fragment. Drei Regressionen bestätigen, dass nur
absolute lokale Pfade ohne URI-Zusatz akzeptiert werden.

Verifikation: **156 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **91 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1035: Unbekannte Account-Referenz erklärt verfügbare Accounts

`resolve_account()` hatte den Fehlerzweig mit vorhandener Account-Liste noch
nicht direkt ausgeführt. Eine Regression bestätigt, dass unbekannte IDs oder
Labels die verfügbaren IDs und Labels im Fehler nennen.

Verifikation: **157 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **91 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1036: Profilverzeichnis-Cleanup gegen falsche Typen gehärtet

`_remove_created_profile_dir()` hatte die Ablehnung von Dateien/Symlinks als
Profilpfad und von Symlink-/Verzeichnis-Markern noch nicht direkt belegt. Vier
Regressionen bestätigen, dass Rollback nur echte Verzeichnisse mit regulärer
Markerdatei entfernt.

Verifikation: **161 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **92 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1037: Profil-Rollback bei verschwundenen Ancestors abgesichert

`_cleanup_created_profile_directories()` hatte die Behandlung eines
verschwundenen Ancestors und eines an seine Stelle gesetzten Symlinks noch
nicht direkt belegt. Zwei Regressionen bestätigen toleriertes Verschwinden
und fail-closed Abbruch bei Symlink-Ersatz.

Verifikation: **163 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **92 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.

## Runde 1038: Cleanup-Identität gegen Inode-Wechsel geprüft

`_assert_created_directory_identity()` und
`_assert_created_file_identity()` hatten den Inode-Wechsel noch nicht direkt
ausgeführt. Zwei Regressionen bestätigen fail-closed Ablehnung veränderter
Cleanup-Ziele.

Verifikation: **165 Config-Tests**, Ruff und Diff-Check bestanden;
Coverage-Auszug für `config.py`: **92 %**, verbleibende Missing-Zeilen liegen
in weiteren Test-Home-/Rollback-/Pfad- und Serialisierungspfaden; keine
Settings-Fenster gestartet.
