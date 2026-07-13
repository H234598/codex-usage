# Changelog

## 0.6.363 - 2026-07-13

### Fixed

- Inferierte inaktive 5h-Werte bleiben als 100% verbleibend erhalten, bis ein
  frischer Wert sie ersetzt; weder Python-State noch Cinnamon-Applet lassen
  sie nach der normalen 5h-Cachefrist verschwinden.

## 0.6.362 - 2026-07-13

### Fixed

- Eine alte 5h-Resetzeit wird nicht mehr in ein neu inferiertes inaktives
  Fenster zurückgemischt.

## 0.6.361 - 2026-07-13

### Fixed

- Ein inaktives 5h-Fenster bezahlter Accounts wird als 100% verbleibend ohne
  erfundene Resetzeit dargestellt; Free-/30-Tage-Fenster bleiben unverändert.

## 0.6.360 - 2026-07-13

### Fixed

- Gültige App-Server-Nutzwerte bleiben erhalten, wenn `resetsAt` nicht
  darstellbar ist; nur die Resetzeit wird als unbekannt markiert.

## 0.6.359 - 2026-07-13

### Fixed

- Ein ungültiges `usedPercent`-Feld verwirft im App-Server nicht mehr das
  jeweils andere gültige Limitfenster.

## 0.6.358 - 2026-07-13

### Fixed

- Das Verwerfen mehrdeutiger Teilidentitäten verschleiert bei fehlendem
  Auth-Kontext keine weiteren Backend-Konten mehr.

## 0.6.357 - 2026-07-13

### Fixed

- Ein unvollständiger Ziel-Account wird neben einem fremden, nur per
  `account_id` bekannten Backend-Datensatz nicht mehr akzeptiert.

## 0.6.356 - 2026-07-13

### Fixed

- Unvollständige Benutzer-Identitäten werden auch dann verworfen, wenn ein
  weiteres Konto nur durch seine `account_id` bekannt ist.

## 0.6.355 - 2026-07-13

### Fixed

- Unvollständige Backend-Identitäten werden bei mehreren Konten mit derselben
  Benutzerkennung nicht mehr einem beliebigen Account zugeschlagen.

## 0.6.354 - 2026-07-13

### Fixed

- Cache-Merges restaurieren keine 5h- oder Wochenwerte mehr, deren
  Resetzeit ausserhalb der eigenen Fensterdauer liegt.

## 0.6.353 - 2026-07-13

### Fixed

- Browser-DOM-Werte werden nicht mehr mit bereits identifizierter JSON-Nutzung
  vermischt, wenn ein Profil falsche oder veraltete Cookies verwendet.

## 0.6.352 - 2026-07-13

### Fixed

- Browser- und Bridge-Antworten mischen keine 5h- und Wochenfenster mehr aus
  unterschiedlichen Backend-Accounts.

## 0.6.351 - 2026-07-13

### Fixed

- Reaktivierungspruefungen verwenden jetzt dieselbe DST-faehige lokale
  Zeitzone wie die Auth-Metadaten.

## 0.6.350 - 2026-07-13

### Fixed

- Relative `profile_dir`- und `auth_json_path`-Werte werden abgewiesen, damit
  CLI und systemd nie unterschiedliche Accounts aus demselben Config-Eintrag
  aufloesen.

## 0.6.349 - 2026-07-13

### Fixed

- Reaktivierung akzeptiert keine erneuerte `auth.json` mit abweichender
  User-Identitaet mehr, auch wenn die Account-ID gleich geblieben ist.

## 0.6.348 - 2026-07-13

### Fixed

- Browser-Capture und Diagnose verwenden jetzt dieselbe DST-faehige lokale
  Zeitzone wie Direct-, Scheduler- und Render-Ausgaben.

## 0.6.347 - 2026-07-13

### Fixed

- Tabellenstand und Auth-Ablaufpruefung verwenden jetzt dieselbe DST-faehige
  lokale Zeitzone wie die gespeicherten Nutzungswerte.

## 0.6.346 - 2026-07-13

### Fixed

- Das Applet verwendet gecachte Prozentwerte mit unbekanntem Aufnahmezeitpunkt
  nicht mehr nur wegen einer gueltigen zukuenftigen Resetzeit.

## 0.6.345 - 2026-07-13

### Fixed

- Scheduler-Watchdog-Zeitpunkte verwenden jetzt ebenfalls die DST-faehige
  lokale Zeitzone fuer Block-, Snapshot- und Fehlerentscheidungen.

## 0.6.344 - 2026-07-13

### Fixed

- Bridge-Captures werden jetzt mit einer DST-faehigen lokalen Zeitzone
  interpretiert, auch bei naiven oder historischen ISO-Zeitstempeln.

## 0.6.343 - 2026-07-13

### Fixed

- Naive Legacy-Zeitstempel im Cache werden jetzt mit der DST-faehigen lokalen
  Zeitzone interpretiert und nicht mehr mit dem aktuellen festen Offset.

## 0.6.342 - 2026-07-13

### Fixed

- Direct-JWT-Ablaufzeiten und Direct-Snapshots verwenden jetzt dieselbe
  DST-faehige lokale Zeitzone wie der App-Server.

## 0.6.341 - 2026-07-13

### Fixed

- App-Server-Resetzeitpunkte werden mit der DST-faehigen lokalen Zeitzone
  formatiert. Winter-Resets erhalten dadurch nicht mehr versehentlich den
  aktuellen Sommeroffset.

## 0.6.340 - 2026-07-13

### Fixed

- Resetlose Snapshot-Fenster werden fuer die Ablaufpruefung jetzt als
  verstrichene Sekunden in UTC berechnet. Ein 5h- oder Wochenfenster wird
  dadurch beim Sommer-/Winterzeitwechsel nicht eine Stunde zu frueh oder zu
  spaet verworfen.

## 0.6.339 - 2026-07-13

### Fixed

- Reset-Zeitpunkte werden bei lokalen festen CEST/CET-Offsets jetzt in einer
  DST-faehigen Zeitzone berechnet. Absolute Epoch-Zeitstempel, relative
  Resetdauern und Zeit-only-Angaben bleiben dadurch auch beim Sommer- oder
  Winterzeitwechsel korrekt.

## 0.6.338 - 2026-07-13

### Fixed

- Sichtbare, eindeutig markierte HTML-Fortschrittsbalken überstimmen stale
  Prozenttexte aus `bodyText`, absolute Nutzungswerte bleiben vorrangig.

## 0.6.337 - 2026-07-13

### Fixed

- Bestätigte Browser-Identitäten dürfen sichtbare Graphwerte als Fallback
  verwenden, wenn ein JSON-Endpunkt nur die Identität, aber keine Limits liefert.

## 0.6.336 - 2026-07-13

### Fixed

- Teilidentitaeten werden beim Wechsel von Browser- zu authentifizierten
  Cachewerten kompatibel zusammengefuehrt, wenn eine gemeinsame Backend-ID
  bestaetigt ist.

## 0.6.335 - 2026-07-13

### Fixed

- Ein identitaetsgleicher Direct-/App-Server-Cache kann nun einen alten
  Browser-Snapshot abloesen, wenn er zum konfigurierten Backend passt.

## 0.6.334 - 2026-07-13

### Fixed

- `service status` normalisiert numerische systemd-`ExecMainCode`-Werte, damit
  ein erfolgreicher Dienst als `exited` statt irrefuehrend als Code `1` erscheint.

## 0.6.333 - 2026-07-12

### Fixed

- Mehrkonto-Cache-Leser und -Schreiber verwenden einen gemeinsamen Lock, damit
  `latest` keine gemischte Generation aus alten und neuen Accountwerten anzeigt.

## 0.6.332 - 2026-07-12

### Fixed

- Leere Backend-Platzhalter dürfen beim Cache-Laden weiterhin mit einem
  ersten Snapshot gefüllt werden; nur bestehende Fenster werden bei abweichender
  Abrufweg-Provenienz geschützt.

## 0.6.331 - 2026-07-12

### Fixed

- Neuere Cachewerte mit abweichender Abrufweg-Provenienz ersetzen keine
  bestehenden In-Memory-Werte mehr und werden stattdessen als veraltet markiert.

## 0.6.330 - 2026-07-12

### Fixed

- Resetzeiten außerhalb der plausiblen Fensterdauer verhindern jetzt die
  Wiederverwendung alter Applet-Cachewerte.

## 0.6.329 - 2026-07-12

### Fixed

- Ungültige Reset-Zeitstempel verhindern jetzt die Wiederverwendung alter
  Cachewerte im Cinnamon-Applet.

## 0.6.328 - 2026-07-12

### Fixed

- Das Applet akzeptiert keine deutlich in der Zukunft liegenden Capture-Zeitpunkte
  mehr als frische Werte und verdrängt damit keine gültigen aktuellen Snapshots.

## 0.6.327 - 2026-07-12

### Fixed

- Das Cinnamon-Applet bewertet resetlose Cachefenster jetzt anhand ihres
  tatsächlichen `values_captured_at`-Zeitpunkts und zeigt dadurch keine bereits
  abgelaufenen Gegenwerte aus Mischsnapshots mehr an.

## 0.6.326 - 2026-07-12

### Fixed

- Cache-Merges verwenden für resetlose Fenster den tatsächlichen
  `values_captured_at`-Zeitpunkt statt eines späteren Wrapper-Captures.

## 0.6.325 - 2026-07-12

### Fixed

- Booleanwerte in numerischen Snapshot-Feldern werden nicht mehr als `0` oder
  `1` in Nutzungswerte umgewandelt.

## 0.6.324 - 2026-07-12

### Fixed

- Ein Partial-Snapshot mit ausschließlich einer neuen Reset-Zeit übernimmt
  keinen älteren Prozentwert mehr. Cache-Stabilisierung greift nur, wenn der
  aktuelle Snapshot auch einen belastbaren Nutzungswert enthält.

## 0.6.323 - 2026-07-12

### Fixed

- Ein relativer Reset akzeptiert keinen unvollständigen oder widersprüchlichen
  neuesten Snapshot mehr. Fehlende Gegenfenster, instabile Fensteridentitäten
  und große Sprünge im nicht zurückgesetzten Fenster lösen stattdessen eine
  erneute Abfrage aus.

## 0.6.322 - 2026-07-12

### Fixed

- Browser-Merges ergänzen kein älteres Gegenfenster mehr, wenn ein frisches
  resetloses Nutzungsfenster vorhanden ist. Dadurch wird dessen Ablauf nicht
  mit einem älteren gemeinsamen Capture-Zeitpunkt vorgezogen.

## 0.6.321 - 2026-07-12

### Fixed

- Der Parser verwendet bei WHAM-Payloads nie mehr einen Zusatzbucket aus
  `additional_rate_limits` als Hauptlimit, auch wenn die Quelle keine
  bekannte Standard-URL trägt.
- Blockierte Cachezustände mit naiven lokalen Zeitstempeln werden nach dem
  Ablauf korrekt freigegeben.

## 0.6.287 - 2026-07-12

### Fixed

- Der Browser-Parser ignoriert versteckte HTML-Progressbar-Klone inklusive
  ihrer Reset-Zeiten. Sichtbare aktuelle Bars werden dadurch nicht mehr von
  alten React-DOM-Ständen überschrieben.

## 0.6.286 - 2026-07-12

### Security

- Das Cinnamon-Applet führt Browserwerte nicht mehr mit unmarkierten
  Legacy-Caches zusammen. Fehlende Fenster werden dadurch nicht aus einer
  unbekannten Quelle ergänzt.

## 0.6.285 - 2026-07-12

### Security

- Browser-Partialwerte werden nicht mehr mit unbekannten Legacy-Ständen
  zusammengeführt. Dadurch können unmarkierte alte Werte kein fehlendes
  Browser-Fenster auffüllen.

## 0.6.284 - 2026-07-12

### Fixed

- Die HTML-Fallbackauswertung bevorzugt eine sichtbare Progressbar auch dann,
  wenn ein später versteckter Textklon im selben HTML-Fragment andere Werte
  enthält.

## 0.6.283 - 2026-07-12

### Fixed

- Ein unbeschrifteter sichtbarer Prozentwert kann nicht mehr von einem
  späteren versteckten DOM-Klon mit höherer Parser-Qualität überstimmt werden.
  Nur eine erkannte HTML-Progressbar darf generischen sichtbaren Gebrauchstext
  ersetzen.

## 0.6.282 - 2026-07-12

### Fixed

- Die Bridge wertet sichtbaren Seitentext getrennt von DOM-Klonen aus. Alte
  versteckte Werte können dadurch keine aktuellen 5h-/Wochenwerte mehr
  überschreiben; HTML-Progressbars bleiben als spezialisierte Fallbackquelle
  nutzbar.

## 0.6.281 - 2026-07-12

### Fixed

- Der Watchdog ignoriert einen alten Block-Snapshot, sobald ein neuerer
  nicht blockierter Current-Stand vorhanden ist.

## 0.6.280 - 2026-07-12

### Fixed

- Das Applet verwirft beim Abrufwegwechsel sofort Werte aus dem vorherigen
  Backend und zeigt bis zum neuen Poll einen gekennzeichneten Leerstand.

## 0.6.279 - 2026-07-12

### Security

- Bei identischem Capture-Zeitstempel gewinnt der konfigurierte authentifizierte
  Backend-Stand gegen Browser- und unklare Fallback-Werte.

## 0.6.278 - 2026-07-12

### Security

- Ein aktueller authentifizierter Current-Stand kann nicht mehr von einem
  zeitnahen Browser-Bridge-Payload überschrieben werden.

## 0.6.277 - 2026-07-12

### Security

- Das Applet vermischt beim Cache-Merge keine Browser-, Direct- und App-Server-Werte mehr.

## 0.6.276 - 2026-07-12

### Security

- Browser-Bridge-Werte tragen jetzt ihre Browser-Provenienz. Sie werden nicht
  mehr still mit Direct- oder App-Server-Caches zusammengeführt.

## 0.6.275 - 2026-07-12

### Fixed

- Bridge-Ingest verwirft verspätete Payloads jetzt vor dem Speichern, wenn
  bereits ein neuerer Current- oder Snapshot-Zustand bekannt ist.

## 0.6.274 - 2026-07-12

### Security

- Ein aktueller Direct-Abruf übernimmt keine Reset-Stabilisierung mehr aus
  einem unbewiesenen älteren App-Server-Snapshot. Der bewusste App-Server-
  gegen-Direct-Fallback bleibt erhalten.

## 0.6.273 - 2026-07-12

### Security

- Der State-Merge prüft jetzt ebenfalls die Backend-Provenienz. Ein direkt
  geladener Status kann dadurch keine unbewiesenen App-Server-Cachewerte
  übernehmen.

## 0.6.272 - 2026-07-12

### Fixed

- Die Direct-Stabilisierung bewertet nicht unterstützte Limitdauern nicht
  länger als vollständige 5h-/Wochenwerte. Gemischte Backend-Antworten können
  dadurch kein falsches Limitmodell als Mehrheitswert auswählen.

## 0.6.271 - 2026-07-12

### Fixed

- Der Browser-Page-Hook verwirft Hauptantworten aus einer alten Refresh-Runde
  und ignoriert verspätete Antworten, damit ein neuer Abruf keine veralteten
  Nutzungswerte wieder einspeist.

## 0.6.270 - 2026-07-12

### Security

- Der Watchdog verwendet alte Block-Snapshots ohne Account-ID nicht mehr für
  ein anderes Konto mit derselben User-ID.

## 0.6.269 - 2026-07-12

### Security

- `latest` verwirft Cachewerte auch dann, wenn sich die `auth.json`-Identität
  während des Ladevorgangs ändert.

## 0.6.268 - 2026-07-12

### Fixed

- Nach einem `auth.json`-Identitätswechsel verwirft das Applet alte gecachte
  Limitwerte statt sie nur als veraltet weiterzuzeigen.
- `make check` führt den vorhandenen Applet-Runtime-Testlauf jetzt ebenfalls
  aus.

## 0.6.267 - 2026-07-12

### Security

- `latest` zeigt gecachte Limitwerte nach einem Wechsel der `auth.json` nicht
  mehr unter der neuen Account-Identität an.

## 0.6.266 - 2026-07-12

### Security

- Backendübergreifende State-Merges akzeptieren nicht mehr jeden beliebigen
  `fallback_reason` als Identitätsnachweis.
  Erlaubt bleiben bekannte Reset-Fallbackmarker und explizite Direct-Fallbacks
  eines als App-Server konfigurierten Accounts.

## 0.6.265 - 2026-07-12

### Fixed

- Ein bereits einmal verwendeter App-Server-Fallback blockiert bestätigte
  Werte des nächsten App-Server-Polls nicht mehr dauerhaft.
  Die Direct-Wiederholungsabsicherung bleibt unverändert bestehen.

## 0.6.264 - 2026-07-12

### Fixed

- Direct-Sample-Quoren zählen Reset-only- und ungültige Fenster nicht mehr als
  vollständige Nutzungswerte.
  Reine Reset-Metadaten können dadurch keine echten Verbrauchswerte verdrängen.

## 0.6.263 - 2026-07-12

### Fixed

- Wertlose Direct-Sample-Gruppen können keine vollständige Nutzungsgruppe
  mehr überstimmen.
  Bei widersprüchlichen Teilantworten wird fail-closed erneut versucht,
  statt alle aktuellen Limits zu leeren.

## 0.6.262 - 2026-07-12

### Fixed

- Unvollständige verschachtelte App-Server-`codex`-Buckets überschreiben keine
  vollständigen Top-Level-Werte mehr.
  Fehlt `usedPercent`, bleibt die Teilantwort unklassifiziert statt einen
  gesamten Accountabruf als Protokollfehler zu behandeln.

## 0.6.261 - 2026-07-12

### Fixed

- Die App-Server-Inferenz klassifiziert explizit ungültige
  `windowDurationMins`-Felder nicht mehr als fehlende Dauern.
  Unklassifizierbare Teilantworten bleiben dadurch leer statt einen falschen
  5h- oder Wochenwert zu erzeugen.

## 0.6.260 - 2026-07-12

### Fixed

- Ein explizit nicht unterstütztes Top-Level-Fenster blockiert jetzt die
  5h-/Wochen-Inferenz aus einem verschachtelten `codex`-Bucket ohne Dauer.
  Dadurch werden unklassifizierbare App-Server-Teilantworten nicht als echte
  Nutzungswerte angezeigt.

## 0.6.259 - 2026-07-12

### Fixed

- Ein partieller App-Server-Codex-Bucket ohne Dauerangabe überschreibt keinen
  vollständigen unterstützten Top-Level-Bucket mehr.
- Dadurch bleiben belegte 5h-/Wochenfenster erhalten, statt durch eine
  unvollständig klassifizierbare Teilantwort falsch zu werden.

## 0.6.258 - 2026-07-12

### Security

- Die Direct-Mehrdeutigkeitsprüfung normalisiert jetzt Plan-Aliase wie
  `pro` und `plus` konsistent mit der Backend-Identitätsprüfung.
- Konten mit gemeinsamem User und unterschiedlichen Account-IDs werden
  dadurch auch bei diesen Aliasen nicht ungeschützt zusammengeführt.

## 0.6.257 - 2026-07-12

### Fixed

- Ein neuerer authentifizierter `partial`-Snapshot stellt fehlende 5h- oder
  Wochenfenster nicht mehr aus einem älteren Snapshot wieder her.
- Browser-Partials behalten weiterhin die bestehende vorsichtige Ergänzung
  älterer Werte, wenn keine Backend-Identität die Daten autoritativ macht.

## 0.6.256 - 2026-07-12

### Fixed

- Direct-Samples erkennen einen Reset jetzt auch dann, wenn der
  `used_percent` unverändert bleibt und nur der relative Countdown auf ein
  frisches Fenster springt.
- Dadurch kann ein alter Resetzeitpunkt nach einem `0% -> 0%`-Fensterwechsel
  nicht mehr als aktueller Wert ausgewählt werden.

## 0.6.255 - 2026-07-12

### Fixed

- Ein neuerer partieller Current-/Snapshot-Datensatz verwirft keine älteren,
  noch gültigen Fenster mehr.
- Nur vollständige `ok`-Werte ersetzen einen älteren Datensatz vollständig;
  per Fenster bleibt die Übernahme als stale gekennzeichnet.

## 0.6.254 - 2026-07-12

### Security

- Bridge-Aufnahmen kombinieren identitätsbehaftete JSON-Antworten nicht mehr
  mit ungebundenen DOM-Werten aus einer möglicherweise fremden Browserseite.
- Solche Aufnahmen bleiben partiell, statt Zahlen eines anderen Accounts als
  gültige Nutzungswerte auszugeben.

## 0.6.253 - 2026-07-12

### Fixed

- Reset-only-JSON- und DOM-Metadaten verwenden jetzt den neuesten passenden
  Fund statt einer veralteten zuerst gefundenen Resetzeit.
- Mehrfach gerenderte Browser-/Bridge-Blöcke können dadurch keine alten
  Resetzeiten mehr in die Accountanzeige übernehmen.

## 0.6.252 - 2026-07-12

### Fixed

- Bridge-Debug-Dumps werden bei Account-Resets jetzt ebenfalls durch
  Account-Lock und State-Generation geschützt.
- Ein verspäteter Fehler-Callback kann dadurch keinen alten, potenziell
  sensiblen Debug-State nach einer Account-Löschung wiederherstellen.

## 0.6.251 - 2026-07-12

### Fixed

- Eine bestätigte monotone Direct-Antwort mit größerem Nutzungssprung wird
  nicht mehr zugunsten eines älteren 1%-Mehrheitswerts verworfen.
- Unbestätigte große Sprünge ohne Quorum bleiben weiterhin blockiert.

## 0.6.250 - 2026-07-12

### Fixed

- Direct-Abrufe melden jetzt ausdrücklich, wenn ein Account nur ein
  nicht unterstütztes Fenster wie 30 Tage liefert und deshalb keine 5h-/
  Wochenwerte vorhanden sind.

## 0.6.249 - 2026-07-12

### Fixed

- In-flight Abrufe schreiben nach einer Account-Neukonfiguration keinen alten
  `current`- oder Snapshot-State mehr zurück.
- State-Dateien tragen dafür eine interne Account-Generation; alte
  Generationen werden nach dem Zurücksetzen verworfen, neue Abrufe bleiben
  speicherbar.

## 0.6.248 - 2026-07-12

### Fixed

- Der Watchdog verwendet aktive Block-Snapshots nicht mehr, wenn Account-
  Backend, `--backend`-Override oder `--direct` vom Snapshot abweichen.
- Ein alter Direct-Block kann dadurch keinen App-Server- oder neuen Direct-
  Abruf mehr unterdrücken.

## 0.6.247 - 2026-07-12

### Fixed

- Die Browser-Bridge kombiniert JSON-Fenster nicht mehr aus verschiedenen
  Backend-Identitäten.
- Bei mehreren erkannten Identitäten wird die zur Account-`auth.json` passende
  Antwort gewählt; ohne eindeutige Zuordnung wird der Import abgelehnt.

## 0.6.246 - 2026-07-12

### Fixed

- Der Scheduler übernimmt einen alten Fensterwert nicht mehr, wenn das
  aktuelle bekannte Fenster eine andere Art (`5h` oder `weekly`) hat.
- Dadurch kann ein Resetwechsel nicht mehr einen 5h-Wert als Wochenwert oder
  umgekehrt anzeigen.

## 0.6.245 - 2026-07-12

### Fixed

- State-Merges akzeptieren ein bekanntes Fenster nicht mehr gegen ein
  aktuelles Fenster ohne erkennbare Fensterart.
- Dadurch kann ein alter 5h-Wert nicht mehr in ein unklassifiziertes oder
  möglicherweise wöchentliches Fenster geraten.

## 0.6.244 - 2026-07-12

### Fixed

- Ein generischer `used`-Text im selben DOM-Fenster kann den gerenderten
  Fortschrittsbalken nicht mehr mit einem falschen Restwert ueberschreiben.
- Absolute `used/limit`-Angaben bleiben weiterhin vorrangig.
- Regression fuer `100% used` vor den echten `97%`-/`55%`-Balken ergaenzt.

## 0.6.243 - 2026-07-12

### Fixed

- Der DOM-Parser ignoriert Layout-Breiten vor dem eigentlichen
  Nutzungsbalken und bevorzugt gerenderte Fortschrittsbalken anhand ihrer
  Elementklassen.
- Regression für vollständiges HTML mit konkurrierenden `width`-Attributen
  ergänzt.

## 0.6.242 - 2026-07-12

### Fixed

- Explizite Backend-Overrides koennen den persistenten Cache eines Accounts
  nicht mehr mit Werten eines anderen authentifizierten Abrufwegs vergiften.
- `latest` verwirft solche fremden Cache-Eintraege fail-closed und faellt auf
  einen passenden Snapshot zurueck.
- Ein dokumentierter Direct-Fallback des App-Servers bleibt weiterhin gueltig.

## 0.6.241 - 2026-07-12

### Fixed

- Der State-Merge führt ein bekanntes `5h`- oder Wochenfenster nicht mehr mit
  einem anderen bekannten Limitmodell zusammen, wenn die aktuelle Quelle keine
  Rohdauer liefert.
- Ein Browser-/Login-Fallback kann dadurch keinen alten 30-Tage-Wert mit einem
  neuen 5h-Resetzeitpunkt anzeigen.
- Regression für Browser-`5h` ohne Rohdauer gegenüber altem Direct-
  `2592000`-Sekunden-Fenster ergänzt.

## 0.6.240 - 2026-07-12

### Fixed

- Die Mehrkonto-Ambiguitätsprüfung vergleicht jetzt die echte Backend-
  `account_id` statt versehentlich die lokale Konfigurations-ID.
- Zwei lokale Aliase desselben Backend-Accounts werden dadurch nicht mehr
  fälschlich als verschiedene Accounts behandelt.
- Regression für lokale Aliase mit identischer Backend-Identität ergänzt.

## 0.6.239 - 2026-07-12

### Fixed

- `make install-local` lädt eine bereits laufende Cinnamon-Applet-Instanz nach
  der atomaren Installation automatisch neu.
- Der optionale Installer-Schalter `--reload-running` verhindert, dass trotz
  aktualisierter Dateien weiterhin ein alter Applet-Code Werte darstellt.
- Wenn Cinnamon oder Looking Glass nicht laufen, bleibt die Installation
  erfolgreich und meldet den Reload als nicht verfügbar.

## 0.6.238 - 2026-07-12

### Fixed

- Der DOM-Textparser bevorzugt bei mehrfach gerenderten 5h-Labels jetzt die
  spätere Nutzungszahl.
- Eine ältere Resetzeit kann dadurch keinen veralteten Verbrauchswert mehr
  vor einen frischeren Wert setzen.
- Regression für DOM-Text `3 / 100` alt mit Resetzeit gegenüber `20 / 100`
  neu ohne Reset ergänzt.

## 0.6.237 - 2026-07-12

### Fixed

- Auch Generic-JSON-Kandidaten gleicher Priorität werden jetzt nach ihrer
  Reihenfolge als Frischemerkmal bewertet.
- Eine ältere Generic-Antwort mit Resetzeit kann dadurch keinen neueren
  Nutzungswert ohne Resetzeit mehr verdrängen.
- Regression für Generic-JSON `3 %` alt mit Resetzeit gegenüber `20 %` neu
  ohne Resetzeit ergänzt.

## 0.6.236 - 2026-07-12

### Fixed

- Der gemeinsame WHAM-Parser bevorzugt jetzt den neuesten Nutzungswert vor
  einer älteren Antwort, die nur durch eine vorhandene Resetzeit vollständiger
  wirkt.
- Ein fehlender aktueller Reset wird nicht mehr durch einen möglicherweise
  veralteten Reset samt altem Verbrauchswert kaschiert.
- Regression für `3 %` alt mit Resetzeit gegenüber `20 %` neu ohne Resetzeit
  ergänzt.

## 0.6.235 - 2026-07-12

### Fixed

- Bridge-Antworten derselben WHAM-URL werden jetzt auch über verschiedene
  Capture-Quellen hinweg nach `requestSequence` geordnet.
- Widersprüchliche erfolgreiche Antworten können dadurch nicht mehr abhängig
  von der Reihenfolge im Browser-Payload falsche Nutzungswerte liefern.
- Quellen ohne Sequenz bleiben als deterministischer Fallback erhalten; eine
  aktuelle Fehlerantwort einer einzelnen Quelle verdrängt weiterhin nicht die
  erfolgreiche Probe einer anderen Quelle.
- Regression für die quellenübergreifende Antwortauswahl ergänzt.

## 0.6.234 - 2026-07-12

### Fixed

- Die Direct-Stabilisierung erkennt einen echten Reset im letzten WHAM-Sample
  jetzt an sinkendem Verbrauch und einem `reset_after_seconds`-Sprung auf die
  bekannte Fensterdauer.
- Ein alter Mehrheitsstand kann dadurch einen frischen Resetwert nicht mehr
  als inkonsistent verdrängen; feste Reset-Rückgänge ohne Countdown bleiben
  fail-closed.
- Regression für den Sampleübergang `5% -> 0%` ergänzt.

## 0.6.233 - 2026-07-12

### Fixed

- WHAM-Reset-Countdowns werden auch bei zusätzlich gelieferten dynamischen
  `reset_at`-Zeitstempeln als relative Zeitquelle erkannt.
- Ein echter Reset mit höherem frischem Restwert kann dadurch nicht mehr durch
  den alten Snapshotwert als `stale` ersetzt werden.
- Regression für den Reset von `95%` auf `100%` bei beiden Resetfeldern ergänzt.

## 0.6.232 - 2026-07-12

### Fixed

- Relative Resetzeiten werden auch bei bereits laufenden Countdowns erkannt,
  solange kein absolutes `reset_at` vorhanden ist.
- Normale Countdown-Fortschreibung kann dadurch keinen alten Snapshotwert als
  Reset-Transition reaktivieren.
- Regression für ein laufendes relatives 5h-Fenster ergänzt.

## 0.6.231 - 2026-07-12

### Fixed

- Der Scheduler übernimmt bei einer Reset-Transition keinen alten Wert mehr,
  wenn sich die bekannte `limit_window_seconds`-Identität des Fensters ändert.
- Ein Wechsel von einem 30-Tage- zu einem 5h-Fenster kann dadurch keinen
  höheren alten Restwert mehr als aktuelle Daten ausgeben.
- Regression für die Fensterdauerprüfung im Scheduler ergänzt.

## 0.6.230 - 2026-07-12

### Fixed

- Fenster mit ausschließlich `limit` und Resetzeit gelten nicht mehr als
  vorhandene Nutzungswerte.
- Direct-, Bridge-, Browser- und App-Server-Status können dadurch keine
  scheinbar vollständigen Accounts ohne Verbrauchs-/Restwert melden.
- Regressionen für limit-only JSON-Fenster und Bridge-Payloads ergänzt.

## 0.6.229 - 2026-07-12

### Fixed

- Der Browserabruf reicht den festen Erfassungszeitpunkt jetzt an den
  gemeinsamen Parser weiter.
- Relative Resetzeiten werden dadurch nicht mehr mit einem spaeteren
  Parserzeitpunkt berechnet.
- Regression fuer die Zeitstempelweitergabe im Browserpfad ergaenzt.

## 0.6.228 - 2026-07-12

### Fixed

- Der Snapshot-Merge uebernimmt eine fehlende Resetzeit nicht mehr aus einem
  vorherigen Limitfenster mit anderer `limit_window_seconds`-Identitaet.
- Dadurch koennen Tarif- oder Fensterwechsel keine alte 30-Tage-Resetzeit in
  ein aktuelles 5h-Fenster uebertragen.
- Regression fuer den Wechsel von 30-Tage- zu 5h-Fenster ergaenzt.

## 0.6.227 - 2026-07-12

### Fixed

- Scheduler, Stabilisierung und Watchdog begrenzen Restprozente jetzt wie
  Renderer und Cinnamon-Applet auf `0..100`.
- Nichtfinite Werte wie `NaN` oder `Infinity` werden in diesem Pfad verworfen,
  statt Vergleichs- und Blockierungsentscheidungen zu verfalschen.
- Regressionen fuer ueberlaufende, negative und nichtfinite Limitwerte ergänzt.

## 0.6.226 - 2026-07-12

### Security

- Das Cinnamon-Applet behandelt widerspruchsfreie, aber unvollständige
  Backend-Identitäten nicht mehr als Accountwechsel.
- Der vollständige bekannte Accountstand bleibt dadurch erhalten, bis eine
  vollständige neue Identität vorliegt; echte abweichende Identitäten werden
  weiterhin als Wechsel verarbeitet.
- Regression für fehlende Account-ID bei passender User-ID ergänzt.

## 0.6.225 - 2026-07-12

### Security

- Das Cinnamon-Applet übernimmt identitätslose Fresh-/Cache-Payloads nicht
  mehr, wenn bereits ein identifizierter Accountstand vorhanden ist.
- Der letzte bekannte Wert bleibt stattdessen als `stale`/`partial` sichtbar;
  ein nicht zuordenbarer Datensatz kann dadurch keine fremden Limits anzeigen.
- Regression für beide Applet-Mergepfade ergänzt.

## 0.6.224 - 2026-07-12

### Security

- Der App-Server prüft jetzt neben User- und Account-ID auch den Tariftyp
  vor und nach dem Rate-Limit-RPC.
- Ein Wechsel zwischen Tariftypen bei gleicher technischer Identität wird
  dadurch als `login_required` abgewiesen, statt fremde Limitfenster anzuzeigen.
- Regression für einen `free`-zu-`enterprise`-Wechsel ergänzt.

## 0.6.223 - 2026-07-12

### Security

- Der App-Server verweigert jetzt `auth.json` ohne User- oder Account-ID,
  bevor ein Rate-Limit-RPC gestartet wird.
- Dadurch können identitätslose App-Server-Antworten keine Werte einem
  konfigurierten Account zugeordnet werden.
- Regression für ein gültiges Token ohne Accountidentität ergänzt.

## 0.6.222 - 2026-07-12

### Security

- `auth_identity_changed` behandelt jetzt auch das Auftauchen oder
  Verschwinden einer User-ID bei gleichbleibender Account-ID als
  Identitätswechsel.
- Direct-, App-Server- und Browser-Abrufe können dadurch keinen Tokenwechsel
  mit unvollständiger Identität als denselben Account weiterverwenden.
- Regressionen für beide Richtungen des fehlenden User-ID-Feldes ergänzt.

## 0.6.221 - 2026-07-12

### Security

- Der Direct-Mehrkontenabruf verwirft jetzt nicht account-spezifische
  Backend-Identitäten, wenn mehrere konfigurierte Accounts dieselbe `user_id`
  und dieselbe oder unbekannte Plansemantik teilen.
- Accounts mit gemeinsamem User, aber nachweislich unterschiedlichen
  Plan-Typen bleiben kompatibel; die bestehende Planprüfung trennt sie.
- Regressionen für die Canonical-Identität, Scheduler-Durchreichung und die
  Unterscheidung gleicher beziehungsweise verschiedener Pläne ergänzt.

## 0.6.220 - 2026-07-12

### Fixed

- Abgelaufene `BLOCKED`-Cachezustände werden in `latest` nicht mehr als aktive
  Sperre angezeigt. Wenn alle zugehörigen Resetfenster und die Freigabezeit
  abgelaufen sind, wird der Account als veraltet/`partial` markiert und beim
  nächsten Poll erneut geprüft.
- Die alte `blocked_until`-Zeit und der alte Sperrgrund werden dabei entfernt,
  damit keine abgelaufene Sperre weiter in CLI oder Cinnamon erscheint.
- Regression für einen vollständig abgelaufenen blockierten Snapshot ergänzt.

## 0.6.219 - 2026-07-12

### Fixed

- Cachewerte werden nach dem Ablauf ihres Resetfensters nicht mehr als aktuell
  ausgegeben. Bis zum nächsten erfolgreichen Poll markiert `latest` das
  betroffene Fenster als veraltet und fordert einen Refresh an.
- Dadurch bleiben Werte nach einem Reset nicht bis zu fünf Minuten falsch im
  CLI, in der Bridge und im Cinnamon-Applet sichtbar.
- Regressionen für ein abgelaufenes einzelnes Fenster und die Beibehaltung des
  noch gültigen Wochenfensters ergänzt.

## 0.6.218 - 2026-07-12

### Fixed

- Der Watchdog wertet absolute `used/limit`-Fenster bei der Erschöpfungsprüfung
  jetzt vor einem widersprüchlichen `remaining`- oder `percent`-Feld aus.
- Ein veralteter Restwert kann dadurch keinen erschöpften Account mehr als
  verfügbar durchlassen; nicht erschöpfte absolute Nutzung bleibt vorrangig.
- Regression für `used=100, limit=100, remaining=100` ergänzt.

## 0.6.217 - 2026-07-12

### Fixed

- `LimitWindow.percent` ist bei generischen und DOM-Fenstern mit absolutem
  `used/limit` jetzt konsistent der Restprozentsatz, wie bei WHAM und App-
  Server. Absolute Nutzung bleibt gegenüber widersprüchlichen Prozentfeldern
  vorrangig.
- Dadurch können JSON-State, Scheduler-Fallbacks und direkte Parserausgaben
  nicht mehr Nutzung (`42`) und Restwert (`58`) für dasselbe Fenster mischen.
- Regressionen für absolute Nutzung, DOM-Werte und widersprüchliches
  `remaining_percent` ergänzt.

## 0.6.216 - 2026-07-12

### Fixed

- Die DOM-/generische Label-Erkennung akzeptiert `5h` und `5-hour` nur noch
  an alphanumerisch getrennten Grenzen. Limittexte wie `15h`, `25h` oder
  `15-hour` können dadurch nicht mehr fälschlich als 5h-Fenster gelesen werden.
- Regressionen für DOM- und JSON-Labels mit längeren Stundenangaben ergänzt.

## 0.6.215 - 2026-07-12

### Security

- Die Direct-Stabilisierung gruppiert Sample-Antworten jetzt zusätzlich nach
  Backend-`user_id` und `account_id`. Antworten mehrerer Konten können dadurch
  kein gemeinsames Nutzungsquorum bilden.
- Progressive Nutzungswerte werden nur noch bei stabiler Backend-Identität als
  neueste Antwort akzeptiert; eine fremde Sample-Mehrheit kann keine Werte in
  ein anderes Konto einbringen.
- Regression für gemischte Sample-Identitäten ergänzt.

## 0.6.214 - 2026-07-12

### Security

- Direct- und Bridge-Antworten akzeptieren bei fehlender Auth-`account_id` keine
  beliebige Backend-`account_id` mehr. Die bekannte `user_id` darf weiterhin
  als vom WHAM-Backend wiederholte Kontoidentität verwendet werden; fremde
  Kontoidentitäten werden fail-closed verworfen.
- Regression für gemeinsam genutzte `user_id`s ohne konfigurierte
  Auth-`account_id` ergänzt.

## 0.6.213 - 2026-07-12

### Fixed

- Die Direct-Stabilisierung erkennt relative `reset_after_seconds`-Count-downs
  ohne absolutes `reset_at` jetzt über die feste Fensterdauer. Fortschreitende
  Nutzungswerte werden dadurch nicht mehr fälschlich als inkonsistent verworfen.
- Verbrauchsrücksprünge bleiben weiterhin durch den Reset-Regression-Guard
  geschützt.
- Regression für progressive 5h-/Wochenantworten ohne `reset_at` ergänzt.

## 0.6.212 - 2026-07-12

### Fixed

- WHAM-JSON-Fenster leiten eine fehlende absolute Resetzeit jetzt aus
  `reset_after_seconds` und dem Erfassungszeitpunkt ab. Damit bleiben Datum,
  Uhrzeit und Restlaufzeit auch bei relativen Backend-Antworten verfügbar;
  eine vorhandene absolute `reset_at`-Zeit bleibt vorrangig.
- Regression für 5h- und Wochenfenster ohne `reset_at` ergänzt.

## 0.6.211 - 2026-07-12

### Fixed

- Der Cinnamon-Cache aktiviert den Sync-Cooldown nur noch bei einer
  erfolgreich empfangenen und verarbeiteten `latest`-Payload. Fehlgeschlagene
  Cache-Kommandos lösen dadurch beim nächsten Zyklus erneut einen Abruf aus,
  statt alte Werte unnötig länger zu halten.
- Regression für fehlgeschlagene Cache-Kommandos ergänzt.

## 0.6.210 - 2026-07-12

### Fixed

- Der Cinnamon-Cache aktiviert den Sync-Cooldown erst nach erfolgreicher
  Verarbeitung einer `latest`-Payload. Ein Fehler in `_applyPayload` kann
  dadurch keinen weiteren Reload für 60 Sekunden unterdrücken.
- Regression für fehlgeschlagene Cache-Payload-Verarbeitung ergänzt.

## 0.6.209 - 2026-07-12

### Fixed

- Der Cinnamon-Cache markiert einen unveränderten Snapshot nach einem
  erfolgreichen `latest`-Reload nicht mehr sofort erneut als unsynchronisiert.
  Dadurch entfallen unnötige Reload-Schleifen im Minuten-Takt und die Anzeige
  bleibt bis zum nächsten systemd-Poll stabil.
- Regressionen für den Cache-Sync-Cooldown und das Erfassen des Sync-Zeitpunkts
  ergänzt.

## 0.6.208 - 2026-07-12

### Fixed

- Der laufende Bridge-Server lädt die Config vor jedem Ingest-Request neu.
  Neu hinzugefügte Accounts werden dadurch ohne Serverneustart erkannt,
  sofern ihre Extension den zugehörigen Bridge-Token angelegt hat.
- Gelöschte oder unbekannte Accounts bleiben auch nach einem Config-Reload
  abgewiesen; ein ungültiger Config-Stand führt fail-closed zu `503`.
- Regression für das Hinzufügen eines Accounts nach Serverstart ergänzt.

## 0.6.207 - 2026-07-12

### Fixed

- Beim Aktualisieren eines bestehenden Accounts mit geänderter Konfiguration
  werden `current`, Snapshot und Bridge-Debug-Dump des alten Kontostands
  entfernt. Dadurch können `auth_json_path`, Backend, Browser, Profil oder
  Label nicht mehr vor dem nächsten Abruf alte Nutzungswerte anzeigen.
- Ein unverändertes `account add` bewahrt den gültigen State weiterhin.
- Regressionen decken Re-Konfiguration und unveränderte Updates ab.

## 0.6.206 - 2026-07-12

### Fixed

- `account delete` entfernt jetzt den accountbezogenen Snapshot, Current-Wert
  und letzten Bridge-Debug-Dump. Das Browserprofil bleibt weiterhin erhalten,
  sofern `--delete-profile` nicht gesetzt ist.
- Beim erneuten Anlegen derselben Account-ID können dadurch keine alten Werte
  vor dem ersten erfolgreichen Abruf wieder im Applet erscheinen.
- Regressionen decken die State-Bereinigung und den vollständigen Delete-/Re-Add-
  Ablauf ab.

## 0.6.205 - 2026-07-12

### Security

- `account delete` widerruft den zugehörigen Bridge-Token. Beim erneuten
  Anlegen derselben Account-ID wird dadurch ein neuer Token erzeugt; alte
  Browser-Extensions können nicht wieder autorisiert werden.
- Der laufende Bridge-Server liest den aktuellen Token-Dateistand pro Request.
  Widerruf und Tokenrotation wirken deshalb sofort ohne Serverneustart.
- Regressionen decken Löschung, erneutes Anlegen sowie `401` für alte und
  `200` für neue Tokens im laufenden Server ab.

## 0.6.204 - 2026-07-12

### Security

- Persistierte Bridge-Tokens werden beim Lesen zusätzlich auf eine private
  Datei mit Modus `0600` und genau einem Hardlink geprüft. Manipulierte
  Berechtigungen oder Hardlinks werden fail-closed abgewiesen.
- Regressionen decken beide Dateimanipulationen ab.

### Fixed

- Authentifizierte `partial`-Abrufe werden ebenfalls als aktueller Snapshot
  persistiert. Dadurch können ein fehlendes oder nicht unterstütztes Limitfenster
  und ein zwischenzeitlicher Cache-/Dienst-Ausfall keine alten Prozentwerte
  weiter anzeigen.
- Browser-Partialdaten ohne authentifizierte Backend-Identität werden weiterhin
  nicht als dauerhafter Snapshot gespeichert.

## 0.6.203 - 2026-07-12

### Security

- Der HTTP-Bridge-Server verlangt jetzt ein zufällig erzeugtes, pro Account
  gespeichertes Bearer-Token. Unautorisierte lokale Programme und fremde
  Browser-Extensions können dadurch keine plausiblen Nutzungswerte mehr
  einspeisen.
- `bridge-snippet` und `bridge-extension` verwenden automatisch dasselbe
  private Token; CORS-Preflight erlaubt den neuen `Authorization`-Header.
- Token-Dateien werden atomar und mit Modus `0600` unter
  `~/.local/share/codex-usage/bridge-tokens/` verwaltet.

## 0.6.202 - 2026-07-12

### Fixed

- Der Cinnamon-Cache-Merge verwirft verspätete ältere `latest`-Payloads und
  schützt dadurch aktuellere Werte vor Rücksprung.
- Während eines accountweisen systemd-Saves ausgelassene Accounts bleiben
  sichtbar und werden als veraltet markiert; konfigurierte Accounts ohne
  Snapshot erhalten einen sicheren Platzhalter statt aus dem Panel zu
  verschwinden.
- Der Applet-Regressionstest deckt beide Zwischenzustände ab.

## 0.6.201 - 2026-07-12

### Fixed

- Die HTTP-Bridge verwirft bei mehreren Accounts mit derselben
  `chatgpt_user_id` einen Browser-Payload, der nur `account_id == user_id`
  oder gar keine kontospezifische Workspace-ID liefert. Dadurch können
  persönliche/generische Backend-Antworten keine Werte mehr zwischen
  unterschiedlichen Workspace-Accounts vermischen.
- Eindeutige Accounts und der direkte `auth.json`-Abruf bleiben unverändert;
  ein Regressionstest deckt den gemeinsamen Benutzer mit zwei Account-IDs ab.

## 0.6.200 - 2026-07-12

### Fixed

- Die HTTP-Bridge validiert bei Accounts mit `auth.json` die Backend-Identität
  unmittelbar vor dem Speichern erneut. Ein Tokenwechsel während des Parsens
  kann dadurch keinen alten Payload mehr persistieren.
- Eine Regression deckt den Wechsel zwischen Parse- und Save-Phase ab und
  stellt sicher, dass dabei kein Snapshot geschrieben wird.

## 0.6.199 - 2026-07-12

### Fixed

- Ein Browser-Account ohne `auth.json` darf keinen unbekannten ersten
  Backend-Payload als Identitätsanker speichern. Die Bridge verlangt dafür
  jetzt einen bereits initialisierten, passenden Account-Zustand.
- Bei der Identitätsprüfung wird der zeitlich neueste Zustand aus Snapshot und
  Current verwendet; ein alter Snapshot kann dadurch keinen aktuellen
  Identitätswechsel mehr verdecken oder blockieren.
- Regressionen decken Erstkontakt, passenden initialisierten Browser-Account
  und einen neueren Current-Zustand gegenüber einem alten Snapshot ab.

## 0.6.198 - 2026-07-12

### Fixed

- Der Bridge-Server speichert Browser-Payloads ohne erkannte Backend-Identität
  nicht mehr. Reine DOM-Werte können dadurch nicht als vermeintlich gültige
  Account-Werte persistiert werden, wenn ein Browser versehentlich mit den
  Cookies eines anderen Accounts geöffnet ist.
- Eine Regression stellt sicher, dass ein solcher Payload vor dem ersten
  Snapshot verworfen wird.

## 0.6.197 - 2026-07-12

### Fixed

- Die Browser-Bridge serialisiert überlappende Content-Script-Sends jetzt.
  Ein langsamer Refresh kann dadurch keinen zweiten Zyklus starten, dessen
  Timeout einen inzwischen frischeren Page-Hook-Response löscht.
- Eine ausstehende Folgeabfrage wird nach Abschluss des laufenden Zyklus genau
  einmal nachgeholt; ältere Page-Hook-Responses lösen markierte Wartebereiche
  weiterhin kontrolliert auf.
- Ein Node-Regressionstest deckt zwei gleichzeitig angestoßene Refresh-Zyklen
  und die Begrenzung auf genau einen aktiven Seitenabruf ab.

## 0.6.196 - 2026-07-12

### Fixed

- Die Browser-Bridge fragt den authentifizierten `/backend-api/wham/usage`-
  Endpoint vor jedem 5-Minuten-Zyklus im Page-Hook erneut ab, statt einen
  erfolgreichen Response vom Seitenaufbau weiterzuverwenden.
- Der Content-Script wartet auf den frischen Page-Hook-Response und verwirft
  bei Timeout alte Hauptendpoint-Werte, bevor der Fallback-Probeweg startet.
- Node-Regressionen decken sowohl den Content-Refresh als auch die tatsächliche
  Page-Hook-Antwort mit Browser-Cookies ab.

## 0.6.195 - 2026-07-12

### Fixed

- Das Cinnamon-Applet markiert gespeicherte (`stale`) Werte jetzt auch in der
  Statusleiste als Warnung. Alte Werte aus einem partiellen Browser-/Bridge-
  Abruf können dadurch nicht mehr optisch wie frische Werte wirken.
- Ein Node-Regressionstest deckt den stale-Panelzustand ab; echte Abruffehler
  bleiben weiterhin als Fehler markiert.

## 0.6.194 - 2026-07-12

### Fixed

- Direkte Wham-Abrufe gruppieren ein unbenutztes Fenster jetzt anhand seiner
  festen Fensterdauer statt anhand des sekündlich sinkenden `reset_after`-Werts.
- Vorübergehend widersprüchliche 3er-Stichproben werden bis zu zweimal erneut
  gelesen, bevor der Abruf als Fehler markiert und ein alter Snapshot genutzt
  wird.
- Regressionstests decken dynamische Reset-Countdowns und eine kurzlebige
  Reset-Regression mit anschließend stabilem aktuellem Wert ab.

## 0.6.193 - 2026-07-12

### Fixed

- Der Watchdog verwendet einen `blocked`-Snapshot bei gleicher Account-ID
  jetzt nur noch, wenn auch die bekannten User-IDs übereinstimmen.
- Ein Wechsel von `user-old/shared-account` zu `user-new/shared-account`
  erzwingt dadurch einen neuen Abruf statt den alten Account-Zustand zu
  konservieren.
- Eine Regression deckt den Shared-Account-Userwechsel ab; unterschiedliche
  Account-IDs bleiben weiterhin geschützt.

## 0.6.192 - 2026-07-12

### Fixed

- Auth-Refresh-Prüfungen behandeln jetzt auch zwei bekannte, unterschiedliche
  User-IDs bei gleicher Account-ID als Identitätswechsel.
- Die bisher erlaubte Backend-Antwort mit abweichender `user_id` bei gleicher
  Account-ID bleibt unverändert gültig; die neue Sperre betrifft nur die
  während eines Requests erneut gelesene `auth.json`-Identität.
- Eine Regression sichert die Unterscheidung zwischen Userwechsel und stabiler
  gemeinsamer Account-ID ab.

## 0.6.191 - 2026-07-12

### Fixed

- Bei einem `401`/`403` liest der Direct-Abruf `auth.json` jetzt einmal neu,
  wenn Codex während des Requests einen anderen Token gespeichert hat.
- Der Retry ist auf denselben Account, denselben Plan, einen nicht abgelaufenen
  Token und genau einen Versuch begrenzt; unveränderte, fremde oder abgelaufene
  Tokens werden nicht erneut verwendet.
- Regressionen decken erfolgreiche Tokenrotation, unveränderte Credentials und
  abgelaufene rotierte Credentials ab.

## 0.6.190 - 2026-07-12

### Fixed

- Der Direct-Sampler verwirft jetzt eine alte Mehrheitsantwort, wenn der
  letzte Response innerhalb derselben Fensterdauer einen deutlich niedrigeren
  Verbrauch nach einem Reset meldet, auch wenn `reset_at` unverändert bleibt.
- Regressionen decken sowohl den sicheren Abbruch ohne Quorum als auch die
  Annahme des neuen Werts nach einem Quorum ab.

## 0.6.189 - 2026-07-12

### Fixed

- Die Browser-Bridge wertet einen gecachten Hauptendpoint jetzt nur dann als
  vorhanden, wenn die Antwort erfolgreich, nicht abgeschnitten und tatsächlich
  mit Rate-Limit-Daten gefüllt ist.
- Nach `401`/`403` oder einer nutzlosen JSON-Antwort wird der authentifizierte
  Ersatzabruf nicht mehr fälschlich übersprungen.

## 0.6.188 - 2026-07-12

### Fixed

- Das Applet gleicht bei aktivem systemd-Polling den lokalen `latest`-Cache
  jetzt im Anzeige-Takt ab. Dadurch kann ein unabhängig gestarteter systemd-
  Timer den Panelwert nicht mehr mehrere Minuten hinter dem gespeicherten
  Stand zurücklassen.
- Dieser Abgleich liest nur lokale Snapshots und startet keinen neuen
  Backend-Abruf.

## 0.6.187 - 2026-07-12

### Fixed

- Beschädigte Snapshot-Fensterfelder werden beim Laden jetzt als fehlendes
  Fenster behandelt, statt den gesamten Abrufzyklus mit `AttributeError`
  abzubrechen.
- Regressionen für Listen-, String- und Zahlenwerte an dieser JSON-Grenze ergänzt.

## 0.6.186 - 2026-07-12

### Fixed

- Die Direct-Stabilisierung erkennt `reset_after_seconds` nahe der Fensterlänge
  jetzt als relativen Resetzeitpunkt. Dessen Verschiebung zwischen zwei Polls
  darf keinen falschen Bucket-Wechsel und damit keinen veralteten Prozentwert
  auslösen.
- Regression für frische 5h-Werte mit dynamischem `reset_at` ergänzt.

## 0.6.185 - 2026-07-12

### Fixed

- Der Direct-Sampler verwirft jetzt einen Mehrheitswert, wenn der letzte
  Sample einen Resetzeit-Bucket rückwärts auf einen älteren Stand verschiebt.
  Dadurch kann ein zweimal gelieferter, inkonsistenter Backend-Bucket keinen
  falschen aktuellen Wert legitimieren.
- Ein einzelner früher Ausreißer wird weiterhin von zwei späteren stabilen
  Samples überstimmt.
- Regression für den beobachteten Nufker-Rücksprung ergänzt.

## 0.6.184 - 2026-07-12

### Fixed

- Generische Prozentfelder werden bei einem absoluten `limit` jetzt auf die
  tatsächliche Limitgröße skaliert. `used_percent=3` mit `limit=1000` ergibt
  dadurch `970` verbleibend statt fälschlich `97` Einheiten.
- Die Regression deckt zusätzlich normalisierte Nutzungs-Ratios und die
  konsistente Restprozentberechnung ab.

## 0.6.183 - 2026-07-12

### Fixed

- Das Cinnamon-Applet restauriert bei authentifizierten partiellen
  Fresh-Payloads fehlende Fenster nicht mehr aus seinem lokalen Cache.
- Browser- und Bridge-Partials behalten den bisherigen Fallback.
- Regressionen für Direct und App Server ergänzt.

## 0.6.182 - 2026-07-12

### Fixed

- Der authentifizierte Reset-Fallback wird jetzt pro Limitfenster angewendet.
  Ein verdächtiger 5h-Reset-Sprung kann dadurch kein gleichzeitig gültiges
  Wochenfenster mehr durch einen alten Snapshot ersetzen.
- Regression für gemischte aktuelle und zurückgehaltene Fenster ergänzt.

## 0.6.181 - 2026-07-12

### Fixed

- Der DOM-Extractor priorisiert absolute `used`-/`limit`-Werte jetzt auch
  gegenüber einer widersprüchlichen Fortschrittsbalkenbreite.
- Regression für `42 / 100 genutzt` zusammen mit `width: 97%` ergänzt.

## 0.6.180 - 2026-07-12

### Fixed

- Authentifizierte partielle Abrufe restaurieren fehlende 5h-/Wochenfenster
  nicht mehr aus einem alten Snapshot. So kann ein Wechsel des
  Limitmodells keinen veralteten Wert als aktuell anzeigen.
- Browser- und Bridge-Partials behalten ihren bisherigen Cache-Fallback.
- Regressionen für Direct und App Server ergänzt.

## 0.6.179 - 2026-07-12

### Fixed

- Der Scheduler berechnet Restprozente bei Fenstern mit absoluten
  `used`-/`limit`-Werten jetzt vor einem eventuell widersprüchlichen
  `percent`-Feld.
- Regression für die gemischte Darstellung von Verbrauchs- und Restwerten
  ergänzt.

## 0.6.178 - 2026-07-12

### Fixed

- Unsupported or invalid `rateLimitsByLimitId.codex` buckets no longer
  overwrite valid top-level App-Server windows.
- Regressionen decken sowohl das Verstecken eines gültigen Fensters als auch
  ungültige Dauerwerte ohne Top-Level-Fallback ab.

## 0.6.177 - 2026-07-12

### Fixed

- Bridge-Antworten werden nach Quelle und URL dedupliziert. Eine fehlgeschlagene
  Page-Hook-Antwort kann dadurch keine gültige Content-Probe derselben URL
  mehr verdrängen.
- Regression für `page-fetch` 401 gegenüber `content-probe` 200 ergänzt.

## 0.6.176 - 2026-07-12

### Fixed

- Die Bridge führt `apiResponses` und `api_responses` jetzt gemeinsam aus,
  statt das zweite Feld bei einem nichtleeren ersten Feld zu ignorieren.
- Dadurch kann eine gültige Usage-Antwort nicht mehr durch eine unabhängige
  `settings/user`-Antwort im anderen Feld verdeckt werden.
- Regression für beide Feldnamen ergänzt.

## 0.6.175 - 2026-07-12

### Fixed

- Als `truncated` markierte Bridge-JSON-Antworten werden nicht mehr als
  vollständige Parserquelle verwendet, auch wenn der übertragene Präfix
  zufällig noch valides JSON bildet.
- Regression für abgeschnittene WHAM-Antworten ergänzt.

## 0.6.174 - 2026-07-12

### Fixed

- Ein Limitfenster mit gemeinsamer `user_id`, aber ohne prüfbaren `plan_type`,
  wird bei vorhandener Account-ID nicht mehr als aktueller Account akzeptiert.
- Damit bleibt die mehrdeutige Browser-/Backend-Antwort fail-closed, statt
  Werte eines anderen Accounts anzuzeigen.
- Regression für fehlenden Tariftyp ergänzt.

## 0.6.173 - 2026-07-12

### Fixed

- Gemeinsame `user_id`-Werte mehrerer Accounts können keine fremden
  Tarif-Limitfenster mehr legitimieren.
- Direct-, Browser- und Bridge-Abrufe prüfen zusätzlich den Tariftyp aus
  `auth.json` und der WHAM-Antwort; `plus`/`pro` werden als bekannter Alias
  behandelt.
- Regressionen für den BW-Privat/BW-Work-Fall ergänzt.

## 0.6.172 - 2026-07-12

### Fixed

- Der Browser-Bridge-Probe wird nicht mehr unterdrückt, wenn nur eine andere
  `/backend-api/wham/*`-Antwort wie `settings/user` erfasst wurde.
- Der eigentliche `/backend-api/wham/usage`-Endpoint wird dadurch auch bei
  unvollständigen Page-Hook-Daten zuverlässig nachgeladen.
- Regression im Content-Script-Laufzeittest ergänzt.

## 0.6.171 - 2026-07-12

### Fixed

- Ein neuer Browser-Response darf nach einer Reaktivierung einen alten
  Snapshot mit fremder Backend-ID ersetzen, wenn die aktuelle `auth.json`
  genau diese neue Identität bestätigt.
- Unbestätigte Accountwechsel bleiben weiterhin fail-closed und werden vom
  Bridge-Ingest zurückgewiesen.
- Regression für den authentifizierten Snapshotwechsel ergänzt.

## 0.6.170 - 2026-07-12

### Fixed

- Direkte Abruffehler behalten die zuvor sicher aus `auth.json` gelesene
  Backend-Identität.
- Bei einem transienten Netzwerkfehler kann der letzte gültige Wert dadurch
  als veraltet weiter angezeigt werden, statt auf `-` zu springen.
- Nach einem während des Abrufs erkannten Tokenwechsel wird weiterhin keine
  alte Identität übernommen.

## 0.6.169 - 2026-07-12

### Fixed

- Partielle `rateLimitsByLimitId.codex`-Antworten werden mit dem Top-Level-
  Snapshot ergänzt, statt das jeweils nicht gelieferte Fenster zu verlieren.
- Sparse App-Server-Updates können dadurch keine bereits gültigen 5h- oder
  Wochenwerte mehr aus der Übersicht entfernen.
- Regression für einen nur teilweise gelieferten Codex-Bucket ergänzt.

## 0.6.168 - 2026-07-12

### Fixed

- `additional_rate_limits` werden nicht mehr als 5h-/Wochenwerte verwendet,
  wenn das Hauptkonto-Fenster fehlt oder eine nicht unterstützte Dauer besitzt.
- Modellbezogene Zusatzlimits können dadurch kein Hauptkonto-Limit mehr
  vortäuschen.
- Regression für ein 30-Tage-Hauptfenster mit GPT-5.3-Codex-Spark-Zusatzlimits
  ergänzt.

## 0.6.167 - 2026-07-12

### Fixed

- Ein frischer Nutzungswert ohne Resetzeit übernimmt keine bereits abgelaufene
  Resetzeit aus dem letzten erfolgreichen Snapshot.
- Dadurch werden neue Werte nicht mehr mit veralteten Resetdaten kombiniert.
- Regression für den abgelaufenen Reset-Fallback ergänzt.

## 0.6.166 - 2026-07-12

### Fixed

- Der App-Server ordnet unbekannte Fensterdauern nicht mehr positional als
  5h oder Woche ein, wenn bereits eine nicht unterstützte Dauer erkannt wurde.
- Doppelte bekannte Fensterdauern werden fail-closed verworfen, statt durch ein
  Dict-Overwrite einen falschen Limitbucket zu erzeugen.
- Regressionen für unsupported und doppelte `windowDurationMins` ergänzt.

## 0.6.165 - 2026-07-12

### Fixed

- Der Watchdog refetched Accounts, deren gespeicherte Blockierung während des
  Abrufs anderer Accounts abgelaufen ist.
- Abgelaufene `BLOCKED`-Snapshots bleiben dadurch nicht noch einen Zyklus als
  aktive Sperre sichtbar.
- Regression für den Ablauf eines blockierten Accounts während eines
  Mehrkonto-Fetchs ergänzt.

## 0.6.164 - 2026-07-12

### Fixed

- Monoton fortschreitende Direct-Samples innerhalb desselben Reset-Fensters
  überschreiben keinen neueren Prozentpunkt mehr durch den älteren Mehrheitswert.
- Rückläufige oder sprunghafte Samples bleiben weiterhin gegen transient falsche
  Limitwerte geschützt.
- Regression für den real beobachteten Übergang von `22%` auf `23%` Nutzung ergänzt.

## 0.6.163 - 2026-07-12

### Fixed

- `watchdog` bewertet frisch abgerufene Limits erst mit einer Uhrzeit nach
  dem Fetch.
- Läuft ein Reset während eines langen Mehrkonto-Abrufs ab, wird der Account
  nicht mehr für einen zusätzlichen Zyklus fälschlich als `BLOCKED` persistiert.
- Regression für einen während des Fetchs abgelaufenen Reset ergänzt.

## 0.6.162 - 2026-07-12

### Fixed

- `watch` zieht die Dauer einer erfolgreichen Abfrage vom nächsten Intervall
  ab, statt Abrufzeit und Intervall zu addieren.
- Mehrkonto-Polls bleiben dadurch näher am konfigurierten 5-Minuten-Takt.
- Regression für die verbleibende Wartezeit nach einer erfolgreichen,
  messbar langen Abfrage ergänzt.

## 0.6.161 - 2026-07-12

### Fixed

- Stale Snapshot-Fallbacks aus `0.6.155` bleiben nach dem Upgrade auf die
  authentifizierte Direct-/App-Server-Kontinuität als sichere Referenz gültig.
- Beliebige stale Snapshots ohne bekannten Fallback-Marker werden weiterhin
  nicht wiederverwendet.
- Regression für die Migration des alten Direct-Fallback-Markers ergänzt.

## 0.6.160 - 2026-07-12

### Fixed

- Die Snapshot-Kontinuitätsprüfung gilt jetzt für Direct und App-Server.
- Ein einzelner inkonsistenter App-Server-Stand kann dadurch keine zuvor
  bestätigten Werte eines kontengleichen Direct-/App-Server-Snapshots mehr
  überschreiben.
- Regression für den Querbackend-Fall mit wechselnden zukünftigen
  Resetfenstern ergänzt.

## 0.6.159 - 2026-07-12

### Fixed

- App-Server-Antworten ohne Dauerfelder ordnen einzelne Fenster jetzt anhand
  der expliziten `primary`-/`secondary`-Schlüssel zu.
- Ein einzelnes `secondary`-Fenster wird dadurch nicht mehr fälschlich als
  5h-Wert angezeigt.
- Regression für den unvollständigen Single-`secondary`-Payload ergänzt.

## 0.6.158 - 2026-07-12

### Fixed

- Der App-Server ordnet ein `primary`- oder `secondary`-Fenster ohne
  `windowDurationMins` wieder anhand seiner expliziten Position zu, wenn das
  jeweils andere Fenster eine bekannte Dauer liefert.
- Explizit unterstützte fremde Fensterdauern werden weiterhin nicht als 5h-
  oder Wochenlimit umetikettiert.
- Regressionen für beide Richtungen des unvollständigen Duration-Payloads
  ergänzt.

## 0.6.157 - 2026-07-12

### Fixed

- Direct-Samples mit dynamisch fortgeschriebenem `reset_at` werden jetzt über
  ein stabiles `reset_after_seconds`-Fenster gruppiert.
- Ein 5-Sekunden-Bucketwechsel kann dadurch keinen konsistenten Mehrheitsstand
  mehr in drei künstlich verschiedene Antworten aufspalten.
- Regression für BW-Work-artige Antworten mit einem transienten Wochenstand
  und gleichzeitigem Resetzeit-Bucketwechsel ergänzt.

## 0.6.156 - 2026-07-12

### Fixed

- Snapshot- und Current-Merges akzeptieren dieselbe Backend-Account-ID nicht
  mehr, wenn gleichzeitig widersprüchliche Backend-User-IDs vorliegen.
- Ein Kontenwechsel kann dadurch keine alten Limitfenster unter einer
  wiederverwendeten Account-ID weitertragen.
- Regression für widersprüchliche User-Identitäten bei gleicher Account-ID
  ergänzt.

## 0.6.155 - 2026-07-12

### Fixed

- Ein als stale markierter Direct-Fallback bleibt bei nachfolgenden Polls als
  Kontinuitätsreferenz erhalten, statt den nächsten inkonsistenten Backendwert
  wieder zu übernehmen.
- Gleichbleibende Restwerte mit dynamisch fortgeschriebenen Resetzeiten werden
  nicht mehr unnötig als stale markiert.
- Ein früheres Resetfenster mit gleichzeitig höherem Restwert wird weiterhin
  fail-closed verworfen.
- Regressionen für mehrfache Fallback-Polls und widersprüchliche Resetwechsel
  ergänzt.

## 0.6.154 - 2026-07-12

### Fixed

- Kleine fortlaufende Prozentänderungen im selben Limitfenster werden jetzt
  als zeitliche Entwicklung erkannt und verwenden den letzten Samplewert.
- Große Sprünge oder wechselnde Fensteridentitäten bleiben fail-closed.
- Regressionen für fortlaufende und sprunghafte Samples ergänzt.

## 0.6.153 - 2026-07-12

### Fixed

- Der Direct-Abruf verwirft drei nicht übereinstimmende Backend-Samples
  jetzt fail-closed, statt bei fehlendem Quorum die erste Antwort anzuzeigen.
- Regression für drei unterschiedliche Limitstände ergänzt.

## 0.6.152 - 2026-07-11

### Fixed

- Der Direct-Abruf gleicht drei identische Backend-Samples ab und verwendet
  bei flackernden Antworten den am häufigsten gelieferten Limitstand.
- Cache-Vermeidungsheader verhindern, dass ein alter HTTP-Zwischenstand als
  aktueller Accountwert gespeichert wird.
- Regression für einen transienten falschen Backend-Stand ergänzt.

## 0.6.151 - 2026-07-11

### Fixed

- Der DOM-Parser bevorzugt bei mehreren vollständigen Blöcken desselben
  Limits den späteren Block mit Resetzeit.
- Alte/versteckte Limitwerte können dadurch aktuelle Seitenwerte nicht mehr
  überschreiben.
- Regression für doppelte vollständige DOM-Limitblöcke ergänzt.

## 0.6.150 - 2026-07-11

### Fixed

- `id_token`, `access_token` und `tokens.account_id` werden jetzt gemeinsam
  auf widerspruchsfreie Auth-Identität geprüft.
- Ein veralteter `id_token` kann dadurch keinen neueren Accountwechsel im
  `access_token` mehr verdecken.
- Regression für widersprüchliche Token-Claims ergänzt.

## 0.6.149 - 2026-07-11

### Fixed

- Stale Fensterwerte werden beim Snapshot- und Applet-Merge nicht mehr
  übernommen, wenn ihre gespeicherte Zurücksetzungszeit vor dem neuen Capture
  liegt.
- Der Applet-Status wird nur dann als stale markiert, wenn tatsächlich ein
  gültiges Cachefenster übernommen wurde.
- Regressionen für abgelaufene 5h-/Wochenfenster ergänzt.

## 0.6.148 - 2026-07-11

### Fixed

- Auth-gebundene Direct-, Browser- und Bridge-Abrufe akzeptieren keine
  Nutzungswerte mehr, wenn der Backend-Response keine Account-Identität
  liefert.
- DOM-Werte aus einem falschen Browserprofil können dadurch nicht mehr unter
  die Identität der lokalen `auth.json` umetikettiert werden.
- Regressionen für Browser- und Bridge-Payloads ohne Backend-Identität ergänzt.

## 0.6.147 - 2026-07-11

### Fixed

- Der Browser-Abruf prüft die `auth.json`-Identität vor und nach dem
  Playwright-Abruf.
- Ein Accountwechsel während des Browserabrufs wird fail-closed als
  `LOGIN_REQUIRED` behandelt, statt Seitenwerte dem falschen Account
  zuzuordnen.
- Regression für den Browser-Auth-Race ergänzt.

## 0.6.146 - 2026-07-11

### Fixed

- Der Direct-Abruf prüft die Auth-Identität nach dem Netzwerkabruf erneut.
- Ein Wechsel von `auth.json` während der Anfrage wird fail-closed als
  `LOGIN_REQUIRED` behandelt, statt alte Limits unter einem neuen Account
  zu speichern.
- Regression für den Direct-Auth-Race ergänzt.

## 0.6.145 - 2026-07-11

### Fixed

- Der App-Server verwirft jetzt einen Abruf, wenn sich die konfigurierte
  Auth-Identität während der Rate-Limit-Anfrage ändert.
- Dadurch werden Limits einer alten Sitzung nicht mehr unter einer neuen
  `account_id` gespeichert oder angezeigt.
- Regression für den Auth-Identitätswechsel während des Abrufs ergänzt.

## 0.6.144 - 2026-07-11

### Fixed

- Eine neue partielle Usage-Identität wird jetzt nicht mehr durch ein altes
  vollständiges Identitätspaar vervollständigt.
- Veraltete `account_id`-Werte können dadurch keine aktuellen Nutzungsdaten
  mehr falsch binden.
- Regression für neue `user_id` mit alter `account_id` ergänzt.

## 0.6.143 - 2026-07-11

### Fixed

- Die Identitätsauflösung bevorzugt bei gleich priorisierten Usage-Antworten
  jetzt ebenfalls den neuesten Kandidaten.
- Dadurch bleiben aktuelle Nutzungsfenster und `account_id` konsistent.
- Regression für alte und neue Identität desselben Usage-Endpunkts ergänzt.

## 0.6.142 - 2026-07-11

### Fixed

- Der Browser-Extractor bevorzugt bei gleich priorisierten WHAM-Endpunkt-
  und Pfad-Treffern jetzt die zuletzt eingetroffene Antwort.
- Dadurch bleiben auch Browser-Fallbacks gegen alte Werte aus derselben
  Seitennavigation geschützt.
- Regression für doppelte gleich priorisierte WHAM-Antworten ergänzt.

## 0.6.141 - 2026-07-11

### Fixed

- Eine aktuelle fehlerhafte WHAM-Antwort (`401`/`403`) verdrängt jetzt ältere
  erfolgreiche Antworten desselben Endpunkts.
- Dadurch werden alte Nutzungswerte bei abgelaufener oder blockierter
  Authentifizierung nicht mehr als aktuell angezeigt.
- Regression für `200` gefolgt von `403` ergänzt.

## 0.6.140 - 2026-07-11

### Fixed

- Browser- und Page-Hook nummerieren WHAM-Anfragen jetzt beim Start.
- Eine ältere Anfrage, die erst nach einer jüngeren Antwort fertig wird,
  kann dadurch keine aktuelleren Nutzungswerte mehr überschreiben.
- Regression für Antworten in umgekehrter Abschlussreihenfolge ergänzt.

## 0.6.139 - 2026-07-11

### Fixed

- Die Browser-Bridge ersetzt alte Antworten desselben WHAM-Endpunkts durch
  die jüngste Antwort, statt eine kurze Historie als aktuelle Daten zu senden.
- Der Ingest-Parser dedupliziert zusätzlich bereits gespeicherte Payloads pro
  Endpunkt und HTTP-Status und verwendet den letzten Eintrag.
- Regression gegen veraltete 5h-/Wochenwerte in einer Antwortliste ergänzt.

## 0.6.138 - 2026-07-11

### Fixed

- Das Applet synchronisiert Account-IDs, Labels und Abrufwege jetzt über
  `account overview --config-only`, ohne dabei einen zweiten Live-Poll aller
  Accounts auszulösen.
- Die normale `account overview`-Ausgabe bleibt live und zeigt weiterhin die
  aktuellen Werte.
- Regressionen prüfen die CLI-Option und den Applet-Aufruf.

## 0.6.137 - 2026-07-11

### Fixed

- Die Applet-Stale-Pruefung bewertet jetzt jeden konfigurierten Account
  einzeln.
- Ein frischer Account kann dadurch keinen veralteten Account mehr verdecken;
  der systemd-Reparaturpfad erkennt den alten Stand wieder.
- Regression fuer gemischte frische und veraltete Account-Caches ergaenzt.

## 0.6.136 - 2026-07-11

### Fixed

- Konfigurationen mit einem Account-Label, das exakt die ID eines anderen
  Accounts verwendet, werden jetzt abgewiesen.
- Dadurch kann eine CLI-Referenz nicht mehr stillschweigend wegen der
  ID-vor-Label-Priorität auf das falsche Konto zeigen.
- Regression für die Label-/ID-Kollision ergänzt.

## 0.6.135 - 2026-07-11

### Fixed

- `bridge-snippet` normalisiert eine eingegebene Account-Label-Referenz jetzt
  auf die stabile Account-ID, wie es `bridge-extension` bereits tut.
- Nachträgliche Labeländerungen oder gleichnamige Labeltexte können dadurch
  keinen bestehenden Snippet mehr auf eine falsche Zuordnung lenken.
- Regression für `BW_Privat` als Label und `privat` als Account-ID ergänzt.

## 0.6.134 - 2026-07-11

### Fixed

- Der Watchdog prüft beim expliziten `--auth-json`-Override ebenfalls die
  tatsächlich verwendete Auth-Identität, bevor ein aktiver Block-Snapshot
  wiederverwendet wird.
- Regression für einen alten Blockstatus ohne kontoindividuelle Auth-Datei
  und einen neuen Override-Account ergänzt.

## 0.6.133 - 2026-07-11

### Fixed

- Der Watchdog überspringt einen aktiven `blocked`-Snapshot nur noch, wenn
  dessen Backend-Identität zur konfigurierten `auth.json` passt.
- Nach einer Reaktivierung oder einem Kontowechsel kann ein alter Blockstatus
  dadurch keinen frischen Abruf bis zum fremden Resetzeitpunkt unterdrücken.
- Regression für den Wechsel von `account-old` zu `account-new` ergänzt.

## 0.6.132 - 2026-07-11

### Fixed

- `codex-usage latest` kennzeichnet gespeicherte `current`-/Snapshotwerte
  jetzt anhand des konfigurierten Pollintervalls plus 60 Sekunden als veraltet.
- Die Tabellenansicht zeigt diesen Zustand zusätzlich mit `(gespeichert)` an,
  damit alte Werte nicht wie aktuelle Livewerte wirken.
- Regressionen für sieben Minuten alten `current`-Stand und die Statusanzeige
  ergänzt.

## 0.6.131 - 2026-07-11

### Fixed

- Absolute Restmengen mit eigenem Limit, etwa `remaining=690` und
  `limit=1000`, werden jetzt zuverlässig in `69%` Restwert umgerechnet.
- Parser, CLI und Applet verwenden diese Berechnung einheitlich, statt die
  Restmenge im Applet auf `100%` zu klemmen oder in der CLI nur das Limit zu
  zeigen.
- Regressionen für Parser, CLI-Darstellung und Applet-Restwert ergänzt.

## 0.6.130 - 2026-07-11

### Fixed

- Das Applet markiert einen ausgebliebenen 5-Minuten-Poll nach einer
  Pollperiode plus 60 Sekunden als veraltet, statt alte Werte bis zu zwei
  Pollperioden als aktuell erscheinen zu lassen.
- Die Stale-Grenze wird für Cacheprüfung und Payload-Anwendung gemeinsam
  berechnet; eine Regression deckt die 5-Minuten-Grenze ab.

## 0.6.129 - 2026-07-11

### Fixed

- Die Reaktivierung bindet eine vorhandene `auth.json` vor dem Login an ihre
  bisherige Kontoidentität.
- Ein erfolgreicher Login für ein anderes Konto wird abgewiesen und die alte
  Datei wird wiederhergestellt.
- Auch bei Loginfehlern oder unvollständigen neuen Dateien wird die vorherige
  `auth.json` atomar zurückgesetzt.
- Regressionen für Kontowechsel und fehlgeschlagenen Login ergänzt.

## 0.6.128 - 2026-07-11

### Fixed

- Ein gültiger Direct-/App-Server-Abruf ohne angeforderte 5h-/Wochenfenster
  restauriert keine alten Nutzwerte mehr. Das verhindert, dass ein Account mit
  einem anderen Limitmodell als vermeintlich aktuelle Werte erscheint.
- Browser-/Bridge-Partials behalten ihre bisherigen Fallbackwerte weiterhin,
  wenn sie nicht als autoritativ leer gekennzeichnet sind.
- Regressionen für State- und Applet-Merges ergänzt.

## 0.6.127 - 2026-07-11

### Fixed

- Bei vorhandener gleicher Account-UUID wird eine abweichende User-ID-
  Darstellung jetzt akzeptiert und auf die Auth-Identität normalisiert.
  Unterschiedliche Account-UUIDs bleiben strikt verboten.
- Regression für gleiche Account-UUID mit abweichender User-ID ergänzt.

## 0.6.126 - 2026-07-11

### Fixed

- Widersprüchliche Abrufoptionen werden jetzt abgewiesen: `--headed` kann
  nicht zusammen mit `--direct`, `--auth-json` oder `--backend` verwendet
  werden. Dadurch wird ein expliziter Direct-/App-Server-Wunsch nicht mehr
  stillschweigend in einen Browserabruf umgebogen.
- Regressionen für alle drei Konfliktkombinationen ergänzt.

## 0.6.125 - 2026-07-11

### Fixed

- Mehrkonten-Polls werden jetzt auch bei sichtbaren Browsern (`--headed`)
  vollständig serialisiert und pro Account gesperrt. Gleichzeitige Browser-
  Cookies-/WHAM-Anfragen können dadurch keine fremden Limit-Buckets mehr
  übernehmen.
- Regression für sichtbare Mehrkonten-Polls ergänzt.

## 0.6.124 - 2026-07-11

### Fixed

- Der allgemeine `rate_limit`-Knoten gewinnt jetzt deterministisch gegen
  zusätzliche modellbezogene WHAM-Limits. Die Auswahl hängt nicht mehr von
  der Reihenfolge der JSON-Schlüssel ab.
- Regression mit vorangestelltem `GPT-5.3-Codex-Spark`-Limit ergänzt.

## 0.6.123 - 2026-07-11

### Fixed

- Die Browser-/Bridge-Identität wird nicht mehr aus User-ID und Account-ID
  verschiedener JSON-Antworten zusammengesetzt. Bevorzugt wird jetzt ein
  vollständiges Identitätspaar aus derselben Antwort; unvollständige Antworten
  bleiben unvollständig und werden nicht künstlich kombiniert.
- Regression für widersprüchliche Kandidaten-Identitäten ergänzt.

## 0.6.122 - 2026-07-11

### Fixed

- Die Konfiguration lehnt jetzt geteilte Browser-Profilordner und geteilte
  `auth.json`-Pfade zwischen Accounts ab; dadurch können Cookies oder Tokens
  nicht mehr versehentlich den Limitwert eines anderen Accounts liefern.
- Auch Pfadvarianten mit `~`, `..` oder Symlink-Auflösung werden als derselbe
  Ressourcenpfad erkannt.
- Regressionen für beide Konten-Isolationsregeln ergänzt.

## 0.6.121 - 2026-07-11

### Fixed

- Browser- und Bridge-Abrufe kanonisieren persönliche Account-Identitäten jetzt
  ebenfalls über die konfigurierte `auth.json`, damit Direct-, App-Server-,
  Browser- und Bridge-Snapshots nicht als verschiedene Konten behandelt werden.
- Ein ungültiges oder fehlendes konfiguriertes `auth.json` führt beim Browser-
  und Bridge-Abruf nicht mehr zu einem unkontrollierten Fehler oder einer
  möglichen Fremddatenübernahme.
- Regressionen für die Browser-/Bridge-Identitätsnormalisierung und die
  Ablehnung fremder Bridge-Payloads ergänzt.

## 0.6.120 - 2026-07-11

### Fixed

- Direct-Abfragen validieren die Backend-User- und Account-ID jetzt gegen die
  Identität aus `auth.json`, bevor Limitwerte gespeichert werden.
- Bekannte persönliche `account_id=user_id`-Antworten bleiben zulässig; fremde
  Limit-Buckets werden kontrolliert als Fehler verworfen.
- Regression für die Ablehnung eines fremden WHAM-Accounts ergänzt.

## 0.6.119 - 2026-07-11

### Fixed

- Direct-Abfragen verwenden die stabile Account-UUID aus `auth.json` als
  kanonische Backend-Identität, auch wenn die WHAM-Antwort eine andere
  Darstellung der User-ID liefert.
- Identitätsvergleiche priorisieren jetzt eine gleiche Account-ID gegenüber
  abweichenden User-ID-Formaten; verschiedene Account-IDs bleiben strikt
  getrennt.
- Regression für den Wechsel zwischen Direct- und App-Server-Abruf ergänzt.

## 0.6.118 - 2026-07-11

### Fixed

- Der App-Server übernimmt jetzt die `tokens.account_id` als Backend-Identität
  in `AccountUsage`, damit Cache- und Applet-Merges auch bei diesem Abrufweg
  keine Werte verschiedener Konten vermischen.
- Regression für die Kontenidentität des App-Server-Abrufs ergänzt.

## 0.6.117 - 2026-07-11

### Fixed

- Direkte WHAM-Abfragen senden jetzt die `tokens.account_id` aus `auth.json`
  als `ChatGPT-Account-Id`, damit mehrere Konten derselben Benutzer-ID nicht
  denselben oder den falschen Limit-Bucket anzeigen.
- Ungültige Account-IDs aus `auth.json` werden vor jedem Netzwerkabruf
  abgewiesen.

## 0.6.116 - 2026-07-11

### Fixed

- Reaktivierung verwendet jetzt dieselbe Inhalts-, Größen- und Zeichenprüfung
  für `access_token` wie der Direct-Abruf.
- Auch ein nichtleerer, aber syntaktisch ungültiger Token wird nicht mehr als
  erfolgreicher Login gemeldet.

## 0.6.115 - 2026-07-11

### Fixed

- Reaktivierung meldet ein `auth.json` mit leerem `access_token` nicht mehr
  fälschlich als erfolgreich.
- Regression für die Inhaltsprüfung des frisch geschriebenen Tokens ergänzt.

## 0.6.114 - 2026-07-11

### Fixed

- Der App-Server refresht abgelaufene oder ungültige Tokens nun auch dann,
  wenn bereits `account/read` selbst mit einem Auth-Fehler antwortet.
- Regression für den Auth-Fehler beim initialen `account/read` ergänzt.

## 0.6.113 - 2026-07-11

### Fixed

- Die CLI zeigt bei absoluten `used/limit`-Fenstern jetzt zusätzlich den
  konsistenten Restprozentsatz an, statt den Nutzungsprozentsatz als aktuellen
  Restwert erscheinen zu lassen.
- Regression für `42 / 100` und `310 / 1000` mit `58%` bzw. `69%` Restwert
  ergänzt.

## 0.6.112 - 2026-07-11

### Fixed

- Das Applet verwirft einen Fresh-Eintrag mit anderer Backend-Identität nicht
  mehr wegen eines älteren Capture-Zeitstempels und zeigt dadurch keine alten
  Kontowerte weiter an.
- Regression für Identitätswechsel bei älterem Capture ergänzt.

## 0.6.111 - 2026-07-11

### Fixed

- Bridge-Payloads können mit weit in der Zukunft liegenden `capturedAt`-Werten
  keine späteren echten Messungen mehr aus dem Snapshot verdrängen.
- Zukunftszeitstempel außerhalb einer fünfminütigen Toleranz werden auf die
  lokale Empfangszeit begrenzt; eine Regression deckt diesen Fall ab.

## 0.6.110 - 2026-07-11

### Fixed

- Browser- und Bridge-Identitäten priorisieren jetzt die Backend-Usage-Antwort
  gegenüber früher geladenen Settings-Antworten.
- Regression für die reale Antwortreihenfolge `wham/settings/user` vor
  `wham/usage` ergänzt.

## 0.6.109 - 2026-07-11

### Fixed

- Der Watchdog wertet einen vorhandenen positiven Restwert jetzt vorrangig
  aus und interpretiert einen widersprüchlichen Nutzungs-Prozentwert nicht
  mehr als Erschöpfung.
- Regression für `used=0`, `limit=100`, `remaining=100`, `percent=0` ergänzt.

## 0.6.108 - 2026-07-11

### Fixed

- Direct-, Browser- und Bridge-Abrufe führen die vom Backend gemeldete User-
  und Account-ID mit.
- Alte oder kontenfremde Snapshotwerte werden bei partiellen Abrufen nicht
  mehr als aktuelle 5h-/Wochenwerte angezeigt.
- Bridge- und Applet-Fallbacks lehnen eine abweichende Backend-Account-ID ab;
  damit können Browser-Cookies eines anderen Kontos keine Werte übernehmen.
- Authentifizierte Pollzyklen laufen exklusiv und seriell; auch einzelne
  manuelle Abrufe respektieren denselben Gesamt-Lock, damit gemeinsam
  gecachte Backend-Antworten nicht zwischen Accounts vertauscht werden.
- Regressionen für identitätslose Legacy-Snapshots und geteilte Benutzer mit
  unterschiedlichen Accounts ergänzt.

## 0.6.107 - 2026-07-11

### Fixed

- Nicht unterstützte App-Server-Fenster, etwa 30-Tage-Limits, werden nicht
  mehr fälschlich als 5h- oder Wochenlimit etikettiert.
- Die App-Server-Zuordnung akzeptiert nur exakte Fensterdauern von 300 bzw.
  10080 Minuten.
- Regressionen für einzelne und gemischte unbekannte Fenster ergänzt.

## 0.6.106 - 2026-07-11

### Fixed

- Aggregierte JSON-Roots können keine Resetzeit eines anderen Fensters mehr
  in ein spezifisches 5h- oder Wochenfenster übernehmen.
- Kandidaten mit fensterspezifischem JSON-Pfad werden vor Root-Aggregaten
  ausgewertet.
- Regression für getrennte 5h-/Wochenwerte mit partieller Resetmetadatenlage
  ergänzt.

## 0.6.105 - 2026-07-11

### Fixed

- Vollständige spätere DOM-Fenster gewinnen gegen frühere Nutzwert-Treffer
  ohne Resetzeit.
- Mehrere DOM-Kandidaten werden wie JSON-Kandidaten nach vorhandener
  Resetzeit ausgewertet.
- Regression für partielle und vollständige DOM-Abschnitte ergänzt.

## 0.6.104 - 2026-07-11

### Fixed

- Ein strukturierter JSON-Reset bleibt auch bei vollständigen, aber
  widersprüchlichen DOM-Werten maßgeblich.
- Reset-only-JSON-Fenster ergänzen weiterhin DOM-Nutzwerte, ohne ihre
  Resetzeit zu verlieren.
- Regression für konkurrierende Resetzeiten ergänzt.

## 0.6.103 - 2026-07-11

### Fixed

- Partielle JSON-Fenster blockieren nicht mehr die vollständigen DOM-Werte.
- Fehlende Resetzeiten werden aus dem DOM ergänzt, während strukturierte
  JSON-Nutzwerte und vollständige JSON-Fenster Vorrang behalten.
- Regressionen für beide Richtungen der JSON-/DOM-Zusammenführung ergänzt.

## 0.6.102 - 2026-07-11

### Fixed

- Vollständige spätere JSON-Fenster gewinnen gegen frühere Nutzwert-Treffer
  ohne Resetzeit.
- Spezifische Fensterknoten gewinnen bei gleichem Zielrang gegen den
  Sammel-Root, ohne die 5h-/Wochenzuordnung zu vertauschen.
- Regressionen für generische und WHAM-Kandidaten ergänzt.

## 0.6.101 - 2026-07-11

### Fixed

- Relative Resetfelder wie `resetAfterSeconds` werden auch in camelCase nicht
  mehr als absolute Unix-Zeitstempel interpretiert.
- Eine vorhandene absolute Resetzeit gewinnt dadurch zuverlässig.
- Regression für die Reihenfolge `resetAfterSeconds` vor `resetsAt` ergänzt.

## 0.6.100 - 2026-07-11

### Fixed

- Absolute `used`-/`limit`-Werte dominieren widersprüchliche `used_percent`
  Angaben in DOM- und JSON-Payloads.
- CLI, Applet und Parser verwenden dadurch wieder konsistente Prozentwerte.
- Regressionen für widersprüchliche Text- und JSON-Felder ergänzt.

## 0.6.99 - 2026-07-11

### Fixed

- Ältere oder ungültig datierte Fresh-Payloads können im Applet keine neueren
  Cachewerte mehr zurücksetzen.
- Regressionen für verspätete und zeitlose Fresh-Abrufe ergänzt.

## 0.6.98 - 2026-07-11

### Fixed

- Das Applet übernimmt fehlende Resetzeiten auch bei erfolgreichen frischen
  Nutzdaten aus dem letzten Cache.
- Datum, Uhrzeit und Restlaufzeit bleiben dadurch bei `status: ok` ohne
  `reset_at` verfügbar; die frischen Prozentwerte bleiben führend.
- Runtime-Regression für diesen erfolgreichen Teilabruf ergänzt.

## 0.6.97 - 2026-07-11

### Fixed

- Frische Nutzwerte ohne `reset_at` bewahren den letzten bekannten
  Resetzeitpunkt für Datum, Uhrzeit und Restlaufzeit.
- Regression für partielle Nutzdaten ohne Resetzeit ergänzt.

## 0.6.96 - 2026-07-11

### Fixed

- Partielle Bridge-Snapshots überschreiben keine zuletzt gültigen Nutzwerte
  mehr, bevor der `current`-/`last_success`-Merge greifen kann.
- Regression für einen partiellen Snapshot mit neuer Resetzeit ergänzt.

## 0.6.95 - 2026-07-11

### Fixed

- Die Snapshot-Merge behandelt Reset-only-Fenster wie fehlende Nutzwerte und
  bewahrt den letzten gültigen Nutzwert mit neuer Resetzeit.
- Regression für den vorgelagerten `current`-/`last_success`-Merge ergänzt.

## 0.6.94 - 2026-07-11

### Fixed

- Reset-only-Fenster aus partiellen Fresh-Abrufen überschreiben keine letzten
  gültigen Nutzwerte mehr; ein neuer Resetzeitpunkt bleibt erhalten.
- Regression für den Fresh-Merge mit Reset-only-Daten ergänzt.

## 0.6.93 - 2026-07-11

### Fixed

- Der Command-Fehlerhandler bleibt auch bei defektem Menü-, Panel- oder
  Benachrichtigungs-UI nicht-werfend.
- Die Service-Recovery verliert ihre Fortsetzung nicht mehr, wenn die
  Fehleranzeige selbst fehlschlägt.
- Regressionen für Fehleranzeige und Service-Fortsetzung ergänzt.

## 0.6.92 - 2026-07-11

### Fixed

- Ein Fehler beim Aufbau der Reaktivierungs-Ladeanzeige hinterlässt keinen
  phantomhaft laufenden Account mehr in der Reaktivierungsverwaltung.
- Die Reaktivierungs-Regression prüft den frühen Setup-Abbruch und den
  kontrollierten Fehlerstatus.

## 0.6.91 - 2026-07-11

### Fixed

- Neu geplante Refresh-, Anzeige- und Stale-Check-Timer werden durch alte
  bereits zugestellte Callbacks nicht mehr doppelt ausgeführt oder entwertet.
- Safe Mode invalidiert bereits zugestellte periodische Timer-Callbacks und
  die Stale-Recovery plant nach einem synchronen Safe-Mode-Eintritt keinen
  neuen Check mehr.
- Regressionen für Neuplanung, Safe Mode und konkurrierende Stale-Checks
  ergänzt.

## 0.6.90 - 2026-07-11

### Fixed

- Alte, bereits abgebrochene Prozess-Timeouts können die Timer einer jüngeren
  Primär-, Hilfs- oder Health-Anfrage nicht mehr löschen.
- Regression für den generationenübergreifenden Timeout-Race ergänzt.

## 0.6.89 - 2026-07-11

### Fixed

- Synchronisations-Guards werden tokenisiert freigegeben, sodass ein alter
  Idle-Callback keine jüngere Synchronisierung vorzeitig entsperren kann.
- Regression für überlappende Backend-, Account- und Style-Synchronisierungen
  ergänzt.

## 0.6.88 - 2026-07-11

### Fixed

- Cache- und Reaktivierungs-Follow-ups gehen bei Payload- oder Menüfehlern
  nicht mehr verloren.
- UI-Fehler beim Start eines Fresh-Abrufs lassen `_refreshing` nicht dauerhaft
  gesetzt; Applet-Entfernung bereinigt den Zustand ebenfalls.
- Regressionen für Queue-Drain und Refresh-Setup ergänzt.

## 0.6.87 - 2026-07-11

### Fixed

- Der Safe-Modus beendet jetzt auch laufende Health-Prozesse und lässt keine
  Diagnose-Subprozesse außerhalb des Cleanup-Lebenszyklus zurück.
- Regression für Health-Prozess-Cleanup beim Safe-Mode-Eintritt ergänzt.

## 0.6.86 - 2026-07-11

### Fixed

- Safe-Mode-Recovery startet nach fehlgeschlagener Timer-Neuplanung keine
  Auxiliary-, Backend- oder Folgeprozesse mehr.
- Status-, Service-, Backend- und Einstellungs-Callbacks prüfen den Safe-Modus
  vor jeder Folgearbeit.
- Regressionen für abgebrochene Safe-Mode-Recovery ergänzt.

## 0.6.85 - 2026-07-11

### Fixed

- Alle Cinnamon-Timeoutpfade behandeln Exceptions und leere Source-IDs
  fail-closed.
- Fehlende Prozess-Timeouts beenden gestartete Kinder kontrolliert; fehlende
  Refresh-, Anzeige- und Stale-Check-Timer führen in den Safe-Modus.
- Regressionen für leere Timeout-Quellen ergänzt.

## 0.6.84 - 2026-07-11

### Fixed

- Backend-, Account- und Formatierungs-Synchronisations-Guards werden auch
  bei fehlgeschlagenem oder leerem Idle-Scheduling freigegeben.
- Ungültige Idle-Source-IDs werden nicht mehr im Cleanup-Zustand gespeichert.
- Regressionen für alle drei Guards und die leere Idle-Source ergänzt.

## 0.6.83 - 2026-07-11

### Fixed

- Der Backend-Synchronisations-Guard fällt auch dann synchron zurück, wenn
  keine Idle-Source erzeugt werden kann.
- Regressionen für fehlgeschlagene und leere Idle-Scheduling-Ergebnisse ergänzt.

## 0.6.82 - 2026-07-11

### Fixed

- Der Backend-Synchronisations-Guard wird auch bei Ausnahmen in der
  Settings-Synchronisierung zuverlässig freigegeben.
- Regression für blockierte Folgeänderungen ergänzt.

## 0.6.81 - 2026-07-11

### Fixed

- Fresh-Nutzungsdaten für nicht mehr konfigurierte Accounts werden nach der
  Backend-Synchronisierung nicht wieder in den Applet-Zustand aufgenommen.
- Regression für gelöschte Accounts im Fresh-Merge ergänzt.

## 0.6.80 - 2026-07-11

### Fixed

- Backend-Accountübersichten werden bei ungültigen oder zu vielen Zeilen
  vollständig verworfen, statt den bestehenden Zustand teilweise zu ersetzen.
- Regression für den atomaren Zustandsschutz ergänzt.

## 0.6.79 - 2026-07-11

### Fixed

- Gültige Epoch-Zeitstempel werden nicht mehr als fehlende Resetzeit behandelt.
- Abgelaufene Resetzeiten zeigen eine Restlaufzeit von null statt `–`.
- Datums- und Capture-Prüfungen unterscheiden nun sauber zwischen `0` und ungültig.

## 0.6.78 - 2026-07-11

### Fixed

- Die Applet-Prozentberechnung bevorzugt bei absoluten `used`-/`limit`-Werten
  das tatsächliche Verhältnis statt eines absoluten `remaining`-Zählers.
- Ungültige oder fehlende Fensterzahlen erzeugen keinen `NaN`-Prozentwert.
- Runtime-Regression für absolute Quoten ergänzt.

## 0.6.77 - 2026-07-11

### Fixed

- Generische `*_percent`-Felder mit Wert 1 werden als 1 % verarbeitet.
- Nur Ratio-Felder werden von Bruchteilen auf Prozentwerte normalisiert.
- Regressionstests für Grenzwerte und eigenständige `ratio`-Felder ergänzt.

## 0.6.76 - 2026-07-11

### Fixed

- WHAM-Ratio-Felder werden auch im direkten Nutzungs-Payload verarbeitet.
- Echte Prozentfelder behalten ihre Semantik, sodass `used_percent: 1`
  korrekt als 1 % Nutzung und 99 % Restwert erscheint.

## 0.6.75 - 2026-07-11

### Fixed

- Nutzungs-Ratio-Felder wie `used_ratio` und `usage_ratio` werden vor der
  Restwertberechnung auf Prozentwerte normalisiert.

## 0.6.74 - 2026-07-11

### Fixed

- Restprozent-Verhältnisse wie `remaining_ratio` und `available_ratio` werden
  vor der Darstellung auf Prozentwerte normalisiert.

## 0.6.73 - 2026-07-11

### Fixed

- Prozentfeld-Aliase mit `percentage` werden wie `percent` verarbeitet und
  nicht als absolute Nutzwerte fehlinterpretiert.

## 0.6.72 - 2026-07-11

### Fixed

- Generische Zeitfelder wie `limit_window_seconds` werden nicht mehr als
  absolute Quota-Limits interpretiert.

## 0.6.71 - 2026-07-11

### Fixed

- Generische `used_percent`-Felder werden als Prozentwerte behandelt und zu
  Restprozenten umgerechnet, statt als absolute Nutzwerte zu erscheinen.

## 0.6.70 - 2026-07-11

### Fixed

- Die generische JSON-Feldwahl respektiert nun auch die fachliche Reihenfolge
  von `used` vor `usage` und `limit` vor `total`.

## 0.6.69 - 2026-07-11

### Fixed

- Die generische JSON-Extraktion bevorzugt exakte Nutzwertfelder vor
  Teilstring-Treffern wie `used_percent`.

## 0.6.68 - 2026-07-11

### Fixed

- DOM-Abschnitte mit ausschließlich einer Resetzeit blockieren spätere
  Abschnitte mit echten Nutzwerten nicht mehr.

## 0.6.67 - 2026-07-11

### Fixed

- DOM-Text mit Nutzungsprozenten wird in konsistente Restprozente normalisiert,
  damit CLI und Applet denselben aktuellen Wert anzeigen.

## 0.6.66 - 2026-07-11

### Fixed

- Kompakte ISO-Datumswerte wie `YYYYMMDD` werden nicht mehr als Unix-
  Timestamps fehlinterpretiert.

## 0.6.65 - 2026-07-11

### Fixed

- Numerische Reset-Timestamps als JSON-Strings werden wie numerische Werte
  verarbeitet; übergroße Timestamp-Werte werden kontrolliert verworfen.

## 0.6.64 - 2026-07-11

### Fixed

- JSON-Extraktion bevorzugt Nutzwerte vor einem früheren Treffer, der nur eine
  Resetzeit enthält; Reset-only-Daten bleiben als Fallback erhalten.
- Zeitparser lehnen Bool-Timestamps ab und behandeln einen nicht darstellbaren
  Folgetag kontrolliert.

## 0.6.63 - 2026-07-11

### Fixed

- Extractor-Resetzeitpunkte, die bei der Zeitzonenumrechnung nicht darstellbar
  sind, werden kontrolliert verworfen statt den Abruf abstürzen zu lassen.

## 0.6.62 - 2026-07-11

### Fixed

- Abrufe werten ein Limitfenster nur noch dann als erfolgreich, wenn es neben
  der Resetzeit mindestens einen echten Nutzwert enthält.

## 0.6.61 - 2026-07-11

### Fixed

- Snapshot-Schreibvorgänge normalisieren naive `captured_at`-Zeitpunkte vor
  dem Monotoniecheck, damit ältere Daten keine neueren Werte überschreiben.

## 0.6.60 - 2026-07-11

### Fixed

- Safe-Modus stoppt seine Refresh-, Anzeige- und Stale-Check-Timer sofort,
  damit nach einem internen Fehler keine leeren Dauer-Callbacks weiterlaufen.

## 0.6.59 - 2026-07-11

### Fixed

- Browser login, fetch, probe and diagnose now close persistent Playwright
  contexts even when navigation, DOM access or user interaction fails.

## 0.6.58 - 2026-07-11

### Fixed

- `service status` now reports enabled/active state only for both locally
  owned, managed units and no longer exposes foreign systemd state.

## 0.6.57 - 2026-07-11

### Fixed

- The browser-extension `sendMessage` callback now catches context invalidation
  while reading `chrome.runtime.lastError` after an extension reload.

## 0.6.56 - 2026-07-11

### Fixed

- Private Config-, State-, Health-, Lock-, Bridge- and Browser-Ausgaben lehnen
  symlinked Ancestors vor dem Anlegen von Verzeichnissen und Dateien ab.

## 0.6.55 - 2026-07-11

### Fixed

- Service unit operations now reject symlinked ancestors of the user unit
  directory, preventing writes through a redirected `XDG_CONFIG_HOME`.

## 0.6.54 - 2026-07-11

### Fixed

- A newer successful snapshot now supersedes an older current snapshot during
  bridge/latest merging, preventing stale complete values from winning.

## 0.6.53 - 2026-07-11

### Fixed

- Partial current snapshots now fill each missing limit window from the last
  successful snapshot and remain explicitly marked as stale.

## 0.6.52 - 2026-07-11

### Fixed

- `account overview --format json` now performs the same live fetch as the
  table output and includes current limit values, reset times, status, and
  capture time without exposing raw payload fields.

## 0.6.51 - 2026-07-11

### Fixed

- JWT payloads in `auth.json` that are valid JSON but not objects are rejected
  as malformed metadata instead of crashing expiry parsing.

## 0.6.50 - 2026-07-11

### Fixed

- Legacy snapshots without timezone information are normalized to local time,
  so watchdog comparisons cannot crash on naive datetimes.

## 0.6.49 - 2026-07-11

### Fixed

- An empty `rateLimitsByLimitId.codex` container no longer hides valid legacy
  rate-limit data from the App Server response.

## 0.6.48 - 2026-07-11

### Fixed

- App Server partial responses with only a weekly bucket no longer mislabel
  that bucket as the five-hour limit.

## 0.6.47 - 2026-07-11

### Fixed

- App Server cleanup also signals the isolated process group after the parent
  process has already exited, preventing orphaned child processes.

## 0.6.46 - 2026-07-11

### Fixed

- App Server shutdown now signals its isolated process group, so child
  processes do not survive timeout or cleanup paths.

## 0.6.45 - 2026-07-11

### Fixed

- Closed App Server pipes now produce a bounded reader error instead of
  silently terminating the reader thread and waiting for a timeout.

## 0.6.44 - 2026-07-11

### Fixed

- App Server reader error sentinels are retained even when the bounded queue
  is full, avoiding silent 30-second timeouts on malformed output.

## 0.6.43 - 2026-07-11

### Fixed

- App Server output readers no longer block forever when their bounded message
  queue is full; excess protocol traffic becomes a controlled error.

## 0.6.42 - 2026-07-11

### Fixed

- App Server process cleanup now tolerates exit races, so a completed usage
  response is not replaced by a shutdown exception.

## 0.6.41 - 2026-07-11

### Fixed

- Applet systemd ownership detection now accepts only strict boolean status
  fields, so malformed helper output cannot disable the applet poller.

## 0.6.40 - 2026-07-11

### Fixed

- `service uninstall` now keeps managed unit files when `systemctl disable
  --now` fails, preventing an active timer from losing its control files.

## 0.6.39 - 2026-07-11

### Fixed

- Existing systemd user unit directories are restricted to mode `0700` before
  managed units are inspected or written.

## 0.6.38 - 2026-07-11

### Fixed

- `service disable` now validates managed units before calling
  `systemctl disable --now`, and becomes a no-op when no managed units exist,
  so foreign timers cannot be stopped accidentally.

## 0.6.37 - 2026-07-11

### Fixed

- `service uninstall` now validates managed units before any `systemctl`
  operation and becomes a no-op when no managed units exist, so foreign timers
  cannot be stopped accidentally.

## 0.6.36 - 2026-07-11

### Fixed

- The applet now treats a systemd timer as an active poll owner only when the
  status reports that the units are managed by codex-usage.

## 0.6.35 - 2026-07-11

### Fixed

- Service installation now rejects existing unmanaged systemd units before
  writing either managed unit, preserving both files unchanged.

## 0.6.34 - 2026-07-11

### Fixed

- Safe Mode retry now reinstates the refresh and display timers before
  starting recovery requests.

## 0.6.33 - 2026-07-11

### Fixed

- Backend account removal now cancels only the matching reactivation process,
  preventing a deleted account from completing a browser login in the
  background.

## 0.6.32 - 2026-07-11

### Fixed

- Direct account panel and alert changes now upsert their row, preserving
  early user changes made before backend settings synchronization completes.

## 0.6.31 - 2026-07-11

### Fixed

- Account-level warning and error toggles now rebuild an open menu immediately,
  keeping status and severity synchronized with the changed setting.

## 0.6.30 - 2026-07-11

### Fixed

- Service-enable detection now matches the actual `service enable` argument
  pair, even when a config argument contains the token `service`.

## 0.6.29 - 2026-07-11

### Fixed

- Child processes are now force-stopped when primary, auxiliary, health or
  reactivation setup fails after `spawnv()`.

## 0.6.28 - 2026-07-11

### Fixed

- Stale systemd timer repair now completes before the follow-up account
  overview request, so the overview cannot cancel the repair process.

## 0.6.27 - 2026-07-11

### Fixed

- A valid inactive systemd status now resets the automatic activation guard,
  allowing recovery after the timer was stopped externally.
- Safe Mode clears the same guard when queued activation requests are dropped.

## 0.6.26 - 2026-07-11

### Fixed

- Synchronous service-enable command-resolution failures now also release the
  automatic activation attempt for future retries.

## 0.6.25 - 2026-07-11

### Fixed

- Cancelled or failed service-enable auxiliary processes now release the
  automatic activation attempt, allowing a later status check to retry.

## 0.6.24 - 2026-07-11

### Fixed

- Service ownership now remains fail-closed when either command resolution or
  the status subprocess fails after a systemd timer was known to be active.

## 0.6.23 - 2026-07-11

### Fixed

- Failed systemd status checks now preserve the last known active poll owner,
  preventing a transient diagnostic error from starting a second applet poller.

## 0.6.22 - 2026-07-11

### Fixed

- Enabling automatic refresh now rechecks the systemd poll owner, so an
  applet started with refresh disabled no longer silently keeps polling itself.

## 0.6.21 - 2026-07-11

### Fixed

- Backend settings queues now clear the active change and continue with the
  next account even when result handling raises an exception.

## 0.6.20 - 2026-07-11

### Fixed

- Primary request queue draining now runs in a guaranteed `finally` path, so
  an exception during cache or fresh payload handling cannot strand a queued
  follow-up request.

## 0.6.19 - 2026-07-11

### Fixed

- Primary cache and fresh usage requests are now serialized and coalesced, so
  a concurrent request cannot cancel a refresh and leave the applet stuck in
  its loading state.

## 0.6.18 - 2026-07-11

### Fixed

- Safe Mode now cancels active account reactivation processes and pending
  reactivation refreshes.

## 0.6.17 - 2026-07-11

### Fixed

- Backend synchronization now keeps configured accounts visible when no cache
  snapshot exists yet and removes rows for deleted accounts.

## 0.6.16 - 2026-07-11

### Fixed

- Fresh payloads now retain configured accounts omitted by a partial response as
  stale/partial instead of dropping them from the panel.

## 0.6.15 - 2026-07-11

### Fixed

- Deferred auxiliary requests are now coalesced and bounded, with controlled
  errors when the queue is full.

## 0.6.14 - 2026-07-11

### Fixed

- Backend setting changes now survive competing service or health auxiliary
  requests without leaving the queue permanently blocked.

## 0.6.13 - 2026-07-10

### Fixed

- Account backend changes are now applied serially for all changed rows, while
  in-flight settings edits reconcile to the latest desired state.

## 0.6.12 - 2026-07-10

### Fixed

- Backend account overviews now reject duplicate account IDs without replacing
  the last valid state.

## 0.6.11 - 2026-07-10

### Fixed

- Account backend setting changes now reject duplicate rows before applying a
  backend update.

## 0.6.10 - 2026-07-10

### Fixed

- Account-keyed applet maps now use null prototypes, so IDs such as
  `__proto__`, `constructor`, and `toString` cannot corrupt or shadow state.

## 0.6.9 - 2026-07-10

### Fixed

- Payload validation now rejects duplicate account identities without treating
  prototype property names such as `constructor` as already seen.

## 0.6.8 - 2026-07-10

### Fixed

- Partial fresh payloads now preserve missing five-hour or weekly windows
  independently from the stale cache and retain the cached-value timestamp.

## 0.6.7 - 2026-07-10

### Fixed

- A successful account reactivation now queues a fresh usage request when
  another request is already running, preventing the old login-required state
  from surviving until the next timer cycle.

## 0.6.6 - 2026-07-10

### Fixed

- Health subprocess cleanup now completes even when `force_exit()` reports an
  already-terminated process, so later health events are not blocked.

## 0.6.5 - 2026-07-10

### Fixed

- `refresh-on-open` no longer starts a refresh when the applet menu is closed.

## 0.6.4 - 2026-07-10

### Fixed

- Per-account alert severity now compares each limit with its own threshold.
- Alert setting changes immediately refresh the panel and menu.
- Account, style, and target synchronization immediately reapplies settings to
  already cached usage values.

## 0.6.3 - 2026-07-10

### Added

- Independent font colors for percentage, date, time, and restlaufzeit
  formatting profiles.
- Four formatting modes per account and value: always, below threshold only,
  always with an alternate below-threshold profile, or off.
- Separate below-threshold font, size, emphasis, color, and background settings
  with migration from the former `conditional` setting.

## 0.6.2 - 2026-07-10

### Added

- Live per-account restlaufzeit until each five-hour or weekly reset, with
  compact, clock-like, long-text, and total-hours formats.
- Restlaufzeit styling and visibility targets for the status bar, hover
  tooltip, and click menu, plus a minute display refresh independent of fetches.

## 0.6.1 - 2026-07-10

### Fixed

- Existing managed systemd units are no longer rebound to an unrelated
  temporary or custom `--config` path by account-management commands.
- `service enable` restarts the managed timer after unit regeneration so a
  pending timer run cannot use stale unit state.

## 0.6.0 - 2026-07-10

### Added

- Two independently configurable panel slots per account: off, five-hour,
  weekly, or mean value.
- Per-account tags, ordering, mute state, separators, alert thresholds, and
  warning/error switches.
- `codex-usage health` with bounded, redacted event storage and JSON/table
  output.
- Node applet runtime harness and CI execution for JavaScript lifecycle tests.

### Changed

- Existing panel settings migrate into slot 1; slot 2 starts disabled and
  duplicate sources are normalized away.
- Configuration, snapshots, current values, health data, and managed systemd
  units use atomic private writes with locks where concurrent writers can
  overlap.
- `watch` handles signals, unexpected cycle failures, and bounded exponential
  backoff.
- The Cinnamon applet uses bounded incremental process reads, stale-generation
  guards, idempotent cleanup, a refresh circuit breaker, safe mode, and
  managed systemd timer repair.
- Managed services now expose execution status and enforce runtime, memory,
  task, stop, and OOM limits.

### Security

- Oversized or unreadable applet process output terminates the affected child
  process instead of leaving an unbounded or orphaned reader behind.
- Health events contain no tokens, raw responses, or complete stderr output.
- Re-activation callbacks cannot remove or overwrite a newer account login
  process.
