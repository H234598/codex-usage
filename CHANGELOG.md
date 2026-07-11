# Changelog

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
