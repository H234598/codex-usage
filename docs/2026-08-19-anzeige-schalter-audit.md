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
