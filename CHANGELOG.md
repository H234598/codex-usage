# Changelog

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
