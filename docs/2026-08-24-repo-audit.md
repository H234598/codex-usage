# Repo-Audit 2026-08-24

## Runde 1: Device-Login-Umgebungsgrenzen

Fokus: `src/codex_usage/profile_login.py`. Produktionssuite vor Änderung:
`tests/test_profile_login.py` 75/75.

`_safe_environment()` kopiert nur die freigegebenen Prozessvariablen und setzt
`CODEX_HOME` ausschließlich auf das private Staging-Verzeichnis. Ein vererbtes
`CODEX_HOME` wird ohne expliziten Pfad entfernt. `OPENAI_API_KEY` und andere
nicht freigegebene Variablen gelangen nicht in den Codex-Prozess.

`_bounded_output()` akzeptiert nur Text und begrenzt die kombinierte Ausgabe auf
`DEVICE_OUTPUT_MAX_BYTES` (64 KiB). Direkte Regressionen decken erlaubte
Variablen, Entfernung eines geerbten `CODEX_HOME`, Nicht-Text-Eingaben und die
harte Obergrenze ab.

Verifikation: `pytest -q tests/test_profile_login.py` 77/77, `py_compile` und
`git diff --check` grün. Keine Produktionsänderung nötig.

## Runde 2: Teilweise kanonische Resetdaten

Fokus: `src/codex_usage/usage_resets.py`. Ein Mapping wie
`{"usage_resets": {"available": 2, "known": false}}` wurde bisher als
Legacy-Zähler `2` akzeptiert. Ebenso konnten unvollständige kanonische Felder
auf Top-Level oder in einem Duplikat einen widersprüchlichen Zustand
überstimmen.

Parser verwirft solche Teilmetadaten jetzt fail-closed. Alte Legacy-Form
`{"usage_resets": {"available": 2}}` bleibt kompatibel. Regression deckt
Top-Level-, verschachtelte und widersprüchliche Varianten ab.

Verifikation: `tests/test_usage_resets.py` 29/29, `tests/test_state.py` plus
Resettests 360/360, App-Server-Resetfilter 4/4, `py_compile` und
`git diff --check` grün. Ein kompletter App-Server-Lauf stößt weiterhin an die
Sandbox-Socket-Sperre (`EPERM`), unabhängig von dieser Änderung.

## Runde 3: Kaputte Katalog-Quellen

Fokus: `src/codex_usage/usage_limits.py`. `merge_model_catalog()` behandelte
einen fehlerhaften `availability_sources`-Iterator fail-closed, ließ aber
unhashbare Einträge bis `dict.fromkeys()` durch. Eine manipulierte
`UsagePool`-Struktur konnte dadurch mit `TypeError` statt leerem Ergebnis
abbrechen.

Die Quellen-Deduplizierung läuft jetzt innerhalb derselben Schutzgrenze.
Regression deckt Liste, verschachtelte Liste und Set als unhashbare Einträge
ab. Verifikation: `tests/test_usage_limits.py` 146/146, Katalog-/Spark-Subset
37/37, `py_compile` und `git diff --check` grün.

## Runde 4: Falsy Bridge-Tokens

Fokus: `src/codex_usage/bridge.py`. `render_bridge_snippet()` und
`write_bridge_extension()` behandelten jedes falsy `token` wie `None` und
generierten stillschweigend ein neues Token. Damit wurden `""`, `False`, `0`,
Listen und Maps nicht validiert.

Nur `None` bedeutet jetzt „Token aus sicherem Store beziehen“. Jeder explizite
Wert läuft durch `_validate_bridge_token()`. Regression deckt beide APIs und
alle falsy Fehlertypen ab.

Verifikation: Bridge-Fokussuite ohne sandboxgesperrte Netzwerk-Tests 257/257,
Token-Regression 5/5, `py_compile` und `git diff --check` grün. Die 39
ausgeschlossenen Tests benötigen lokale Socket-Erzeugung; die Sandbox liefert
dafür `EPERM`.

## Runde 5: Ungültige Animationswerte in Codex-Homes

Fokus: `src/codex_usage/config.py`. Der Home-Normalizer ersetzte bisher nur
`true`/`false`. Gültiges TOML wie `animations = "yes"`, ein Array oder ein
mehrzeiliger String blieb stehen; danach wurde zusätzlich `animations = false`
angehängt. Ergebnis: doppelter Schlüssel und unlesbare `config.toml`.

Der Normalizer erkennt jetzt jede einfache Animationszuweisung, entfernt
mehrzeilige Werte vollständig und setzt genau einen booleschen Wert. Hashes in
Strings bleiben von Inline-Kommentaren unterscheidbar. Regression deckt
String, Array, Hash-in-String, Inline-Kommentar und mehrzeilige Werte ab.

Verifikation: `tests/test_config.py` plus `tests/test_profile_layout.py`
274/274, Animations-/Kommentar-Subset 13/13, Profil-/CLI-Subset 10/10.

## Runde 6: Konfigurations-Normalizer und Transaktions-Rollback

Fokus blieb `src/codex_usage/config.py`. Vor dem Fix wurden zehn belastbare
Randfälle gesammelt:

1. Credential-Store-Werte außerhalb einfacher Strings erzeugten doppelte
   TOML-Schlüssel.
2. Multiline-Credential-Store-Werte wurden ebenfalls doppelt geschrieben.
3. Gültige Tabellenamen mit `[` oder `]` wurden nicht als Header erkannt;
   `animations`/Credential-Store im falschen Abschnitt wurden verändert.
4. TOML-DEL (`U+007F`) wurde von `_quote()` ungeescaped geschrieben.
5. Lone-Surrogates passierten Textvalidierung und scheiterten erst beim UTF-8-
   Schreiben.
6. `test_home` behandelte lokale `file://`-Auth-URIs als Dateinamen relativ zum
   Arbeitsverzeichnis.
7. Ein relativer `test_home`-Auth-Pfad konnte dadurch bei passendem CWD bewegt
   werden, obwohl Konfigurationspfade absolut sein müssen.
8. Eine fehlgeschlagene Account-Transaktion ließ normalisierte bestehende
   Codex-Home-Konfiguration zurück.
9. Bei bestehendem Profil blieb ein neu angelegter Profilmarker nach
   Transaktionsfehler liegen.
10. Ein nicht-null Codex-Help-Probe-Status wurde ignoriert; außerdem wurden
    großgeschriebene `LOCALHOST`-File-URIs fälschlich abgewiesen.

Batch-Fix: generische TOML-Zuweisungen/Headererkennung, URI-Normalisierung,
Unicode-Grenzen, Probe-Statusprüfung sowie identitätsgesicherte Rollbacks für
Codex-Home-Konfiguration und Profilmarker. Neue Regressionen decken alle zehn
Fälle ab.

Verifikation: `tests/test_config.py` plus `tests/test_profile_layout.py`
291/291, Profil-/CLI-Subset 31/31, Ruff, `py_compile` und `git diff --check`
grün. Vollsuite nicht ausgeführt.

## Runde 7: Profil-Layout-Fehlerpfade

Fokus: `src/codex_usage/profile_layout.py`. Fünf Fehlerbilder wurden
reproduziert und gemeinsam behoben:

- Ungültiges kanonisches `auth.json` wurde erst nach Anlage von Jobs-,
  Migration- und Codex-Verzeichnissen erkannt.
- Profilpfade mit `..` konnten beim privaten Directory-Setup ein übergeordnetes
  Verzeichnis treffen und dessen Modus ändern.
- NUL- und überlange Profilpfade wurden von `layout_for_account()` erst tief in
  Dateisystemoperationen abgewiesen.
- `_record_created_file()` verschluckte echte Metadaten-/`lstat`-Fehler.
- Eine bestehende `config.toml`, die auf `animations = false` normalisiert
  wurde, blieb bei anschließendem Metadatenfehler verändert.

Fix: frühe Auth-/Pfadvalidierung, identitätsgesicherter Config-Rollback und
fail-closed Datei-Metadaten. Regressionen decken jeden Fehlerpfad ab.

Verifikation: `tests/test_profile_layout.py` 65/65, Config plus Profile
296/296, Ruff, `py_compile` und `git diff --check` grün. Nächste Runde wechselt
Datei/Schwerpunkt.

## Runde 8: Private-I/O-Grenzen

Fokus: `src/codex_usage/private_io.py`. Vier Fehlerbilder wurden behoben:

- `ensure_private_directory()` akzeptierte Dot-Segmente und konnte dadurch ein
  übergeordnetes Verzeichnis auf `0700` setzen.
- `private_path_lock()` legte Lockdateien in gruppen-/weltzugänglichen Parents
  an; Lock-Lifecycle war damit von fremden Unlink-/Rename-Rechten abhängig.
- `write_private_text()` verwendete solche Parents ebenfalls für temporäre
  Dateien.
- Ein temporäres Schreiben brach bei `EINTR` statt den Write fortzusetzen ab.

Ein gemeinsamer Parent-Check, Dot-Segment-Sperre und EINTR-Retry härten die
Primitive. Regressionen decken Mutation/Fehlerpfade direkt ab.

Verifikation: `tests/test_private_io.py` 95/95, Private-I/O plus Account-Lock
plus Profile-Layout 182/182, Ruff, `py_compile` und `git diff --check` grün.

## Runde 9: Account-Lock-No-Finding

Fokus: `src/codex_usage/account_lock.py`. Lock-ID-Validierung, interner
`__all_accounts__`-Koordinationslock, Timeout-/Contention-Pfade, Ownership,
Symlink-Schutz, optionale Open-Flags sowie Unlock-Fehler wurden geprüft.
`__all_accounts__` bleibt als absichtlich reservierter interner Lockname
zulässig; normale Account-Konfiguration weist ihn bereits zurück.

Keine belastbare Produktionsänderung nötig. Verifikation:
`tests/test_account_lock.py` 22/22 und Branch-/Statement-Coverage 100 %.

## Runde 10: State-Persistenz und Generationslöschung

Fokus: `src/codex_usage/state.py`. Snapshot-/Current-Schreiben, private
Lesegrenzen, Generationszähler, Account-Löschtransaktion, Rollback-
Reihenfolge, Reset-Ablauf und Merge-Pfade wurden geprüft. Ein isolierter Lauf
mit `XDG_DATA_HOME=/tmp/...` bestand mit 331/331 Tests und 100 %
Statement-/Branch-Coverage.

Keine belastbare Produktionsänderung nötig. Der erste Lauf scheiterte nur an
der schreibgeschützten Host-Umgebung (`~/.local/share`); kein Produktfehler.

## Runde 11: Systemd-Service-Grenzen

Fokus: `src/codex_usage/service.py`. Zwei Fehlerbilder wurden reproduziert
und behoben:

- `ReadWritePaths` für State und Playwright-Cache akzeptierte symlinked
  Pfade. Die erzeugte Systemd-Sandbox konnte dadurch Schreibzugriff auf ein
  unerwartetes Ziel erhalten.
- `select()`/`read()` im bounded-`systemctl`-Runner brachen bei `EINTR` ab,
  obwohl der Prozess weiter gültige Ausgabe liefern konnte.

Fix: Symlink-Ancestor-Prüfung für beide festen Schreibziele sowie Retry bei
unterbrochenem Select-/Read-Systemcall. Regressionen decken beide Ziele und
beide Retry-Stellen ab.

Verifikation: `tests/test_service.py` 103/103, Statement-/Branch-Coverage
100 %, Ruff, `py_compile` und `git diff --check` grün. Ein längerer
abhängiger Installer-/Systemd-Lauf wurde von der eingeschränkten Ausführung
vor dem Summary beendet; kein Fehlerauszug vorhanden.

## Runde 12: Integration-Entrypoint

Fokus: `src/codex_usage/integration_entrypoint.py`. Argument-/XDG-
Validierung, Lock-/Attestation-Reihenfolge, Fehlercode-Normalisierung,
UTC-/Lookback-Grenzen, History-Limits und Publish-Driftprüfung wurden geprüft.
Keine belastbare Produktionsänderung nötig.

Verifikation: `tests/test_integration_entrypoint.py` 38/38 und
Statement-/Branch-Coverage 100 %. Ein `runpy`-Warnhinweis betrifft nur das
bereits importierte Testmodul, nicht Produktionsverhalten.

## Runde 13: Integration-Snapshot-Projektion und Publish

Fokus: `src/codex_usage/integration_snapshot.py`. Current-Verzeichnis-
Identitäten, Datei-/Hardlink-/Symlink-Grenzen, Schema-Projektion,
Secret-Scan, Timestamp-/Zahlenkanonisierung und atomarer Cache-Publish wurden
geprüft. Keine belastbare Produktionsänderung nötig.

Verifikation: `tests/test_integration_snapshot.py` 79/79, 99 % Coverage;
vier ausschließlich defensive Property-Exception-Zweige in `_pool_windows`
bleiben ungetriggert. Ruff, `py_compile` und `git diff --check` grün.

## Runde 14: History-Transaktionen

Fokus: `src/codex_usage/history.py`. SQLite-Pfad-/Sidecar-Schutz,
Migrationsgrenzen, Sample-/Zeitvalidierung, bounded Queries und
Batch-Rollbacks wurden geprüft. Ein Fehler wurde behoben:

- `HistoryStore.prune()` ließ bei fehlgeschlagenem `DELETE` oder `commit()`
  die Transaktion offen; derselbe Store konnte danach uncommittete Löschungen
  weitertragen.

Fix: Rollback bei jedem Fehler vor erfolgreichem Commit, analog zu
`record_many()`. Regressionstest prüft Rollback und Erhalt des Samples.

Verifikation: `tests/test_history.py` 125/125, Consumption plus
Integration-Entrypoint 94/94, Ruff, `py_compile` und `git diff --check` grün.
Zwei defensive Rollback-Fehler-Zeilen bleiben ungetriggert; zehn
ResourceWarnings stammen aus vorhandenen künstlich defekten Connection-Tests.

## Runde 15: Consumption-/Forecast-Rechnung

Fokus: `src/codex_usage/consumption.py`. Lookback-/Baseline-Grenzen,
Sortierung, Pool-Isolation, Reset-Generationen, Gap-/Stale-Coverage,
EMA-Smoothing und Forecast-Obergrenzen wurden geprüft. Keine belastbare
Produktionsänderung nötig.

Verifikation: `tests/test_consumption.py` 56/56 und
Statement-/Branch-Coverage 100 %.

## Runde 16: Terminal-Rendering

Fokus: `src/codex_usage/render.py`. Tabellen-/JSON-Grenzen, Status-/Reset-
Darstellung, Backend-Provenienz und numerische Fail-Closed-Pfade wurden
geprüft. Ein Fehler wurde behoben:

- `_shorten()` normalisierte Whitespace, entfernte aber keine ANSI-/C0-/C1-
  Steuerzeichen. Fehlertexte und Blockierungsgründe konnten Terminal-Ausgabe
  manipulieren oder Darstellung beschädigen.

Fix: Steuerzeichen `U+0000–U+001F`, `U+007F–U+009F` vor Whitespace-Normalisierung
durch Leerzeichen ersetzen. Regressionstest deckt ANSI-/C1-Eingabe ab.

Verifikation: `tests/test_render.py` 85/85, 100 % Statement-/Branch-Coverage;
Terminal-/CLI-Subset 33/33, Ruff, `py_compile` und `git diff --check` grün.

## Runde 17: Backend-Identitätsattribution

Fokus: `src/codex_usage/identity.py`. Candidate-Bounds, URL-Usability,
Provenienz-/Plan-Priorität, Teilidentitäten, Alias-Fälle und Auth-Mismatch-
Fehlerpfade wurden geprüft. Vertrauenswürdige Browser-Hosts werden bereits
vor diesem Modul gefiltert; keine belastbare Produktionsänderung nötig.

Verifikation: `tests/test_identity.py` 46/46 und
Statement-/Branch-Coverage 100 %.

## Runde 18: Health-Event-Speicher

Fokus: `src/codex_usage/health.py`. Event-/Tokenvalidierung,
Retention-/Count-Grenzen, private JSON-I/O, Lock-Reihenfolge und korrupte
Health-Dateien wurden geprüft. Keine belastbare Produktionsänderung nötig.

Verifikation: `tests/test_health.py` 49/49 und
Statement-/Branch-Coverage 100 %.

## Runde 19: Routing-Entscheidungen

Fokus: `src/codex_usage/routing.py`. Policy-Schema/Overrides,
Credit-Limit-Auflösung, Backend-Provenienz, Reset-/Future-Grenzen,
Spark-Health und Main-/Credit-Entscheidungen wurden geprüft. Keine
belastbare Produktionsänderung nötig.

Verifikation: `tests/test_routing.py` 168/168 und
Statement-/Branch-Coverage 100 %.

## Runde 20: Spark-Health-Status

Fokus: `src/codex_usage/spark_health.py`. Backend-ID-Grenzen,
Healthy-/Failed-/Stale-TTL, Hash-Schlüssel, JSON-Rekord-Limits und private
Persistenz wurden geprüft. Keine belastbare Produktionsänderung nötig.

Verifikation: `tests/test_spark_health.py` 36/36 und
Statement-/Branch-Coverage 100 %.

## Runde 21: Direkter WHAM-Abruf und auth.json

Fokus: `src/codex_usage/direct.py`. Auth-Datei- und Tokenvalidierung,
Symlink-/Hardlink-Schutz, Redirect-/Header-Provenienz, Größen- und Timeout-
Grenzen, Backend-Identitätsbindung, Credit-Parsing sowie stabile Mehrfach-
Antworten wurden geprüft. Keine belastbare Produktionsänderung nötig.

Verifikation: `tests/test_direct.py` 362/362 und Statement-/Branch-Coverage
100 %; Ruff und `py_compile` grün.

## Runde 22: Usage-Limit-Normalisierung

Fokus: `src/codex_usage/usage_limits.py`. WHAM-/App-Server-Fenster,
Fallback-Dauern, dynamische 30-Tage-Fenster, Spark-Duplikate,
Model-Katalog-Grenzen, Control-Flags und Reset-Zeitstempel wurden geprüft.
Keine belastbare Produktionsänderung nötig.

Verifikation: `tests/test_usage_limits.py` 146/146 und
Statement-/Branch-Coverage 100 %; Ruff und `py_compile` grün.

## Runde 23: OAuth-Browser und Reactivation-Rollback

Fokus: `src/codex_usage/oauth_browser.py` und der angrenzende
`reactivate.py`-Restore-Pfad. URL-Allowlist, isolierte Browser-Profile,
Marker-/Parent-Symlinks, Umgebungsfilter und Prozessstart waren unauffällig.
Ein Regressionstest zeigte jedoch: `reactivate._validate_auth_target()` ließ
einen `0755`-Parent zu, während der gehärtete Restore-Schreiber nur private
`0700`-Parents akzeptiert. Nach fehlgeschlagenem Login wurde dadurch der alte
`auth.json`-Inhalt nicht wiederhergestellt.

Fix: Auth-Target weist nicht-private Parents vor Login zurück; betroffene
Fixtures verwenden explizit private Parents. So bleibt Restore fail-closed und
startet keinen Login mit später unbrauchbarem Rollback.

Verifikation: `tests/test_reactivate.py` 112/112, Reactivation- und
OAuth-Browser-Coverage jeweils 100 %; `tests/test_private_io.py` plus
Account-Lock 117/117, Ruff, `py_compile` und `git diff --check` grün.

## Runde 24: Bridge-Fehlertexte und Terminal-Steuerzeichen

Fokus: Steuerzeichenpfad in `src/codex_usage/bridge.py`. `_ingest_error()`
übernahm Seitentext und Titel in Diagnoseausgaben; ANSI-/C0-/C1-Zeichen
konnten damit beim Bridge-Server-Logging oder späterer Debug-Anzeige die
Terminaldarstellung manipulieren.

Fix: zentrale Entfernung von C0/C1/DEL in Bridge-Excerpt-, Kontext- und
Debug-Text-Sanitizern. JSON-/HTML-/Token-Redaction bleibt unverändert.
Regressionstest prüft ANSI- und C1-Eingabe über den vollständigen
`_ingest_error()`-Pfad.

Verifikation: Sanitizer-/Debug-Subset 29/29, Ruff, `py_compile` und
`git diff --check` grün. Der erste vollständige Lauf war in der damaligen
Read-only-Sandbox auf 286/297 begrenzt; Wiederholung in schreib-/socket-
fähiger Umgebung: 297/297 und Statement-/Branch-Coverage 100 %.

## Runde 25: Scheduler-Watch-Fehlerausgaben

Fokus: `src/codex_usage/scheduler.py`, Watch-/Fehlerpfad. Die reguläre
Fehlerausgabe schrieb Ausnahme- und Backendtexte direkt nach stderr; C0/C1-
und ANSI-Steuerzeichen konnten die Terminaldarstellung verändern.

Fix: zentrale Sanitization für Watch-Fehlertexte; `_watch_failure_usages()` und
direkte `Fehler:`-Ausgabe entfernen C0/C1/DEL vor Whitespace-Normalisierung und
Begrenzung.

Verifikation: `tests/test_scheduler.py` 273/273, Coverage 99 % (eine rein
defensive Vergleichszeile ungetriggert), Ruff, `py_compile` und
`git diff --check` grün.

## Runde 26: App-Server-Fehlertexte

Fokus: Fehlerpfade in `src/codex_usage/app_server.py`. `_raise_rpc_error()`,
`_bounded_error()` und `_StderrReader.text()` normalisierten zuvor nur
Whitespace. ANSI-/C0-/C1-/DEL-Zeichen aus RPC-Fehlern, Prozessfehlern oder
stderr konnten dadurch in Exceptions und Diagnoseausgaben gelangen und die
Terminaldarstellung manipulieren.

Fix: gemeinsames C0/C1/DEL-Muster; alle drei Textpfade entfernen
Steuerzeichen vor Whitespace-Normalisierung und Längenbegrenzung.

Verifikation: `tests/test_app_server.py` 163/163,
Statement-/Branch-Coverage 100 % (536/536 Statements, 202/202 Branches),
Ruff, `py_compile` und `git diff --check` grün.

## Runde 27: Strikter JSON-Parser

Fokus: `src/codex_usage/json_utils.py`. Typgrenzen, Byte-/Bytearray-Eingaben,
Duplicate-Key-Erkennung, `NaN`/`Infinity`, Zeichenketten mit Strukturzeichen
und maximale JSON-Nesting-Tiefe wurden geprüft. Kein belastbarer Fehler.

Verifikation: `tests/test_json_utils.py` 13/13 und
Statement-/Branch-Coverage 100 %; Ruff und `py_compile` grün.

## Runde 28: Auth-Migrations-Rollback-Identität

Fokus: Rollback in `src/codex_usage/profile_migration.py`. Nach sicherem Lesen
und Digest-Prüfung löschte `rollback_auth_migration()` das Ziel zuvor nur über
den Pfad. Bei einem Austausch zwischen Lesen und `unlink()` konnte dadurch
eine fremde Datei mit gleichem Inhalt gelöscht werden.

Fix: Vor dem Löschen wird `lstat()` erneut geprüft; regulärer Dateityp,
Einfachverlinkung sowie Device-/Inode-Identität müssen weiterhin dem geöffneten
privaten Ziel entsprechen. Austausch oder fehlgeschlagene Prüfung bricht
Rollback fail-closed ab. Alte Rollback-Fixtures erhielten private
Manifest-Eltern passend zur bestehenden Lock-Härtung.

Verifikation: `tests/test_profile_migration.py` 95/95,
Statement-/Branch-Coverage 100 % (319/319 Statements, 134/134 Branches),
Ruff, `py_compile` und `git diff --check` grün.

## Runde 29: Browser-Lock und Diagnoseausgaben

Fokus: `src/codex_usage/browser.py`. Profil-Lockdateien prüften Typ und
Hardlink-Zahl, aber nicht den Besitzer; eine fremde, zu weit freigegebene
Lockdatei konnte dadurch bis zum `fchmod()`-Fehler genutzt oder als DoS-Hebel
verwendet werden. Zusätzlich gelangten ANSI-/C0-/C1-/DEL-Zeichen aus
Playwright-/Identity-Fehlern, Page-Excerpt, URLs und Content-Type in
Diagnoseausgaben.

Fix: Lockdatei muss dem aktuellen Benutzer gehören. Gemeinsame
Steuerzeichen-Sanitization für Fehler/Excerpt/Diagnose-Header sowie Entfernung
von Steuerzeichen aus redigierten URL-Pfaden; mehrere direkte `str(exc)`-
Ausgaben laufen nun durch `_clean_error()`.

Verifikation: `tests/test_browser_profile.py` 178/178,
Statement-/Branch-Coverage 87 % (unveränderte Diagnose-/Playwright-Zweige
bleiben ungetriggert), Ruff, `py_compile` und `git diff --check` grün.

## Runde 30: Usage-Extractor

Fokus: `src/codex_usage/extractor.py`. JSON-/HTML-Walk-Grenzen,
Duplicate-/Konfliktauflösung, WHAM-Strukturpfade, Prozentkomplement,
absolute Zähler, Reset-Zeitstempel, Zeitzonen und versteckte Progressbars
wurden geprüft. Keine belastbare Produktionsänderung nötig.

Verifikation: `tests/test_extractor.py` 213/213 und
Statement-/Branch-Coverage 100 % (1046/1046 Statements, 522/522 Branches),
Ruff/`py_compile` grün; zusätzlich 2.000 zufällige JSON-/HTML-Eingaben ohne
unerwartete Exception.

## Runde 31: Device-Login-Prozess und Auth-Cleanup

Fokus: `src/codex_usage/profile_login.py`. Der bounded Reader behandelte ein
unterbrechbares `os.read()` nicht transient; ein Signal konnte dadurch einen
gesunden Device-Login als Prozessfehler abbrechen. Außerdem entfernten zwei
Rollbackpfade `auth.json` nur per Pfad, sodass ein zwischenzeitlich ersetztes
File gelöscht werden konnte.

Fix: `InterruptedError` beim Read wird erneut versucht. Nach Auth-Publikation
wird Device-/Inode-Identität gebunden; Cleanup löscht nur noch reguläre,
einfach verlinkte Datei mit identischer Identität. Ersetzte Dateien bleiben
erhalten.

Verifikation: `tests/test_profile_login.py` 79/79,
Statement-/Branch-Coverage 98 % (unveränderte defensive Cleanup-Zweige bleiben
ungetriggert), Ruff, `py_compile` und `git diff --check` grün.

## Runde 32: Profile-Job-Worker-Erkennung

Fokus: `/proc/<pid>/cmdline`-Prüfung in `src/codex_usage/profile_jobs.py`.
Ein transient unterbrochenes `os.read()` wurde als Worker-Verlust behandelt;
`profile_job_status()` konnte laufende Jobs dadurch fälschlich auf failed oder
cancelled setzen.

Fix: `InterruptedError` beim Cmdline-Read wird wiederholt; erst echte
Lese-/Prozessfehler gelten als Worker nicht gefunden.

Verifikation: `tests/test_profile_jobs.py` 129/129,
Statement-/Branch-Coverage 100 % (516/516 Statements, 222/222 Branches),
Ruff, `py_compile` und `git diff --check` grün.

## Runde 33: Account-Terminal-Auth-Pfad

Fokus: `src/codex_usage/terminal.py`. `_validate_auth_json()` prüfte zuvor
nur Auth-Datei selbst, nicht den `codex-home`-Parent. Ein öffentliches oder
fremd besessenes Parent konnte damit trotz privater Datei als Terminal-
Startziel akzeptiert werden.

Fix: Parent wird auf reales, user-owned und nicht gruppen-/weltzugreifbares
Directory geprüft; fehlender Parent lässt weiterhin „auth.json missing“ zu,
damit Fehlerreihenfolge stabil bleibt.

Verifikation: `tests/test_terminal.py` 38/38,
Statement-/Branch-Coverage 100 % (101/101 Statements, 40/40 Branches),
Ruff, `py_compile` und `git diff --check` grün.

## Runde 34: Integration-Attestation

Fokus: `src/codex_usage/integration_attestation.py`. Manifest-, RECORD- und
Release-Tree-Prüfungen verwenden bereits private, user-owned Pfade,
`O_NOFOLLOW`, Descriptor-Identitäten, Hardlink-/Größenlimits und Hash-Checks.
Symlink-, Hardlink-, Race-, Ownership-, Modus- und Driftpfade sind durch die
Tests abgedeckt. Keine belastbare Produktionsänderung nötig.

Verifikation: Attestation-Fokus in `tests/test_integration_installer.py`
61/61 Tests grün; Statement-/Branch-Coverage 99 % (390 Statements,
128 Branches; drei reine Manifest-/Drift-Fehlerzeilen ungetriggert),
Ruff, `py_compile` und `git diff --check` grün.

## Runde 35: Integration-Builder-Preflight

Fokus: bounded Offline-Builder-Preflight in
`src/codex_usage/integration_installer.py`. Der Selector-Read konnte bei
`InterruptedError` einen laufenden Preflight als Installationsfehler abbrechen
und den Prozess beenden, obwohl das Signal transient war.

Fix: `InterruptedError` beim `os.read()` wird innerhalb des laufenden
Selector-Zyklus wiederholt. Echte Read-/Timeout-Fehler behalten bisheriges
Cleanup und Fehlerverhalten.

Verifikation: Neue Regression `test_builder_preflight_retries_interrupted_read`
plus komplette `tests/test_integration_installer.py`: 221/221 Tests,
Statement-/Branch-Coverage 100 % (1465/1465 Statements, 574/574 Branches),
Ruff, `py_compile` und `git diff --check` grün.

## Runde 36: CLI-Fehlerausgaben

Fokus: Exception-Ausgaben in `src/codex_usage/cli.py`. `main()` sowie
mehrere textuelle Profil-/Account-Fehlerpfade gaben Exception-Text ungefiltert
aus; Steuerzeichen aus Pfaden, Browser- oder Backendfehlern konnten damit
Terminalausgabe manipulieren.

Fix: Gemeinsame `_clean_cli_error()`-Sanitization ersetzt C0/C1/DEL durch
Leerzeichen, normalisiert Whitespace und begrenzt Meldungen auf 500 Zeichen.
Ungefilterte CLI-Fehlerausgaben und betroffene Table-/JSON-Fehlerfelder laufen
nun darüber; JSON bleibt maschinenlesbar.

Verifikation: Neue Regression plus `tests/test_cli.py` 157/157 Tests,
Statement-/Branch-Coverage 99 % (1348 Statements, 392 Branches),
Ruff für Produktionsdatei, `py_compile` und `git diff --check` grün.

## Runde 37: Usage-Modelle

Fokus: `src/codex_usage/models.py`. Fensteridentität, Prozent-/Restwert-
Normalisierung, Pool-Erschöpfung, Legacy-Feldprojektion und JSON-Projektion
wurden auf Typ-, NaN/Inf-, Duplikat- und Statusgrenzen geprüft. Keine
belastbare Produktionsänderung nötig.

Verifikation: `tests/test_models.py` 75/75,
Statement-/Branch-Coverage 100 % (301/301 Statements, 102/102 Branches),
Ruff, `py_compile` und `git diff --check` grün.

## Runde 38: Config-Parser und Account-Rollback

Fokus: `src/codex_usage/config.py`. TOML-Normalisierung, XDG-/Pfadgrenzen,
private Datei-I/O, Test-Home-Auth-Move/Rollback, Profil-Cleanup,
Account-Ressourcen-Duplikate sowie strikte Feld-/URL-Validierung geprüft.
Keine zusätzliche belastbare Produktionsänderung nötig.

Verifikation: `tests/test_config.py` 231/231,
Statement-/Branch-Coverage 97 % (959 Statements, 466 Branches; nur defensive
Rollback-/Parserfehlerpfade ungetriggert), Ruff, `py_compile` und
`git diff --check` grün.

## Runde 39: Profil-Löschquarantäne

Fokus: Commit-Pfad von `_ProfileDeleteTransaction` in `src/codex_usage/cli.py`.
Nach dem atomaren Umbenennen wurde die Quarantäne bisher nicht an ihre
Device-/Inode-Identität gebunden; eine ersetzte Directory konnte dadurch beim
Commit per `shutil.rmtree()` gelöscht werden.

Fix: Quarantäne-Identität wird nach dem Rename gespeichert und vor Commit auf
reguläres Directory sowie unveränderte Device-/Inode-Werte geprüft. Bei Drift
wird Rollback deaktiviert; fremder Ersatzpfad bleibt unangetastet.

Verifikation: Neue Regression plus `tests/test_cli.py` 158/158 Tests,
Statement-/Branch-Coverage 98 % (1362 Statements, 398 Branches), Ruff,
`py_compile` und `git diff --check` grün.

## Runde 40: Direkter Usage-/Auth-Pfad

Fokus: `src/codex_usage/direct.py`. Auth-Datei-Descriptor- und
Identitätsbindung, JWT-/Claim-Validierung, Redirect-/Content-Type-Grenzen,
WHAM-Response-Konsistenz, Reset-/Fenstersignaturen und JSON-Projektion geprüft.
Keine zusätzliche belastbare Produktionsänderung nötig.

Verifikation: `tests/test_direct.py` 362/362,
Statement-/Branch-Coverage 100 % (1107/1107 Statements, 558/558 Branches),
Ruff, `py_compile` und `git diff --check` grün.

## Runde 41: Panel-Wertemigration und Editor-Layout

Fokus: `files/codex-usage@H234598/panel_settings_list.py`. Editorpositionen
legen Wertfelder spaltenweise von oben nach unten; Slot-Anzahl und Dialoggröße
sind begrenzt. Legacy-Quellen werden entsprechend der gewünschten
Zusammenführung kanonisiert (z. B. alte Woche/5h-IDs auf `Limit … %`). Acht
Test-Erwartungen verlangten noch Legacy-IDs und wurden auf den kanonischen
Persistenzwert korrigiert; Produktionscode unverändert.

Verifikation: `tests/test_panel_settings_list.py` 179/179,
Statement-/Branch-Coverage 87 % (944 Statements, 304 Branches), Ruff,
`py_compile` und `git diff --check` grün.

## Runde 42: Gesammelte Hilfe

Fokus: `files/codex-usage@H234598/help_page.py`. Schema-Ableitung,
Formatierungs-Kopien, Feld-/Optionsdetails, GTK-Markup-Escaping und
scrollbare/expandierbare Darstellung geprüft. Keine zusätzliche belastbare
Produktionsänderung nötig.

Verifikation: `tests/test_help_page.py` 15/15,
Statement-/Branch-Coverage 94 % (331 Statements, 146 Branches; nur defensive
Schema-/GTK-Fehlerpfade ungetriggert), Ruff, `py_compile` und
`git diff --check` grün.

## Runde 43: Prognosen-Selector

Fokus: `files/codex-usage@H234598/forecast_table_selector.py`. Sibling-Modul-
Laden, Tabelle-/Setting-Key-Validierung, GTK-Markup-Escaping, lazy Aufbau und
Listener-/Widget-Cleanup geprüft. Keine zusätzliche belastbare Produktionsänderung
nötig.

Verifikation: `tests/test_forecast_table_selector.py` 27/27,
Statement-/Branch-Coverage 85 % (216 Statements, 54 Branches; defensive
GTK-/Importfehlerpfade ungetriggert), Ruff, `py_compile` und
`git diff --check` grün.

## Runde 44: Cinnamon-Heap und Guard-Idle-Quellen

Fokus: Guard-Synchronisation in `files/codex-usage@H234598/applet.js`.
`_deferGuardRelease()` erzeugte bei wiederholten Settings-/Backend-Syncs je
Aufruf eine eigene Idle-Quelle. Veraltete Quellen änderten zwar keinen Guard
mehr, hielten aber bis zur Abarbeitung Callback und Applet-Referenz im
Cinnamon-Mainloop. Schnelle oder gebündelte Einstellungsänderungen konnten so
unnötig viele Quellen aufstauen.

Fix: Pro Guard gibt es höchstens eine ausstehende Idle-Quelle. Wiederholte
Aufrufe aktualisieren nur deren Token; beim Entfernen des Applets werden die
Pending-Zustände zusammen mit den Idle-Quellen verworfen. Fehler- und
„source unavailable“-Pfade setzen den Guard weiterhin sofort zurück.

Verifikation: Neue Regression plus komplette `tests/applet_runtime.test.js`:
607/607 Tests grün. `node --check`, `git diff --check` grün.

## Runde 45: Entfernte Accounts und transienter Applet-State

Fokus: per-Account-Fehler- und Device-Login-Maps in
`files/codex-usage@H234598/applet.js`. Manage-/Terminal-/Reaktivierungsfehler
sowie Login-Events konnten nach Account-Löschung unter ihrer alten ID im
Applet bleiben. Wiederholtes Account-Anlegen/-Löschen ließ damit State und
gebundene Text-/Eventdaten anwachsen; aktive Profiljobs dürfen währenddessen
nicht verloren gehen.

Fix: Nach erfolgreichem Backend-Overview werden entfernte IDs aus transienten
Fehler-, Event-, Live-Text- und Löschwarte-Maps entfernt. Aktive Jobs,
Reaktivierungen, Pending-Profile und laufende Account-Änderungen bleiben als
bekannte IDs erhalten.

Verifikation: Regression plus komplette `tests/applet_runtime.test.js`:
608/608 Tests grün. `node --check`, `git diff --check` grün.

## Runde 46: GTK-Tabellenwachstum

Fokus: `format_table_selector.py` und `panel_settings_list.py`. Persistierte
JSON-Listen wurden ohne Zeilenlimit in GTK-Modelle geladen. Beschädigte oder
alte Settings mit tausenden Account-Zeilen konnten dadurch Cinnamon-Heap und
Layout stark belasten, obwohl Backend-Account-State auf 100 Accounts begrenzt
ist.

Fix: Beide Tabellen brechen den GTK-Aufbau nach 100 Zeilen ab. Datenquelle
bleibt unverändert; nur UI-Objektzahl wird begrenzt. Prognosetabellen nutzen
denselben gebundenen Format-Listentyp und sind damit ebenfalls geschützt.

Verifikation: Format-/Prognose-/Panel-Suiten 320/320 Tests grün, Ruff,
`py_compile` und `git diff --check` grün.

## Runde 47: Legacy-Serienliste

Fokus: `files/codex-usage@H234598/dynamic_series_list.py`. Die alte
Serien-Kompatibilitätsseite lud persistierte Account-Zeilen unbegrenzt in ein
GTK-`ListStore`. Auch bei künftig pool-only Betrieb können alte Settings diese
Seite noch öffnen und Cinnamon-Heap verbrauchen.

Fix: Serienliste rendert höchstens 100 Zeilen, passend zum Account-Limit. Die
Serienabfrage selbst blieb unverändert und weiterhin begrenzt.

Verifikation: `tests/test_dynamic_series_list.py` 27/27, Ruff,
`py_compile` und `git diff --check` grün.

## Runde 48: Cinnamon-Installer und Reload

Fokus: `scripts/install_cinnamon_applet.py`, insbesondere der Pfad, der bei
„Merge/Reload“ früher durch die alte Read-only-Sandbox blockiert war.
Quell-/Zielpfade, Symlink-Ketten, atomarer Austausch, Staging-Bereinigung,
Settings-Migration sowie GDBus-Reload/Versionsprüfung sind begrenzt und
fehlerrobust. Keine zusätzliche belastbare Produktionsänderung nötig.

Verifikation: `tests/test_install_cinnamon_applet.py` 9/9, Ruff,
`py_compile` und `git diff --check` grün. Aktueller Reload lokal erfolgreich.

## Runde 49: Leiste-/Formatierungs-Schema

Fokus: Konsistenz zwischen `_SOURCE_OPTIONS` in `panel_settings_list.py` und
den `format-table-selector.tables` in `settings-schema.json`. Alle 44
kanonischen Leiste-Quellen (Legacy-Aliase ausgenommen) besitzen eine eigene
Formatierungstabelle; Tabellen-Keys sind eindeutig, alle Section-Referenzen
zeigen auf vorhandene Settings. Keine Produktionsänderung nötig.

Verifikation: automatischer Schema-/Quellenvergleich ohne fehlende Zuordnung;
Help-/Panel-Suiten 195/195 Tests grün.

## Runde 50: Bounded-Prozess-Reader

Fokus: `_readBoundedProcessOutput()` in
`files/codex-usage@H234598/applet.js`, da Codex-Terminal-/PSI-Aufrufe bei
hoher Ausgabe Cinnamon-Heap und Mainloop belasten können. stdout wird in
8-KiB-Schritten bis 64 KiB, stderr bis 8 KiB gelesen. Überschreitung leert
Puffer, beendet den Child-Prozess und liefert einen kurzen Fehler. UTF-8-
Live-Chunks halten höchstens unvollständige Schlussbytes; Generationen und
Abbruchpfade verhindern alte Callbacks an neue Requests.

Die EOF-Kopie verdoppelt kurzzeitig höchstens den bereits gebundenen
Ausgabepuffer (unter 150 KiB je Prozess), nicht unbounded. Timeout- und
Cancel-Pfade erzwingen Child-Ende; nachfolgende Reader-Callbacks sind
idempotent bzw. generation-geschützt. Keine belastbare Produktionsänderung
nötig.

Verifikation: gezielte Reader-/Timeout-Suite 23/23, `node --check` und
`git diff --check` grün.

## Runde 51: Auxiliary-Request-Queue

Fokus: `_spawnAuxJson()` und `_backendAuxQueue` in
`files/codex-usage@H234598/applet.js`. Während Backend-/Account-Änderungen
werden Hilfsanfragen dedupliziert und auf acht Einträge begrenzt; bei
Überlauf wird der Callback mit Fehler beendet. Der Drain startet genau einen
Prozess und wartet auf dessen Ende. Cancel-/Generation-Pfade beenden laufende
Prozesse und ignorieren alte Ergebnisse; Device-Login-Cancel entfernt nur die
zugehörige Queue-Anfrage.

Keine unbounded Queue oder wiederholte Prozesskette gefunden. Argumente sind
bereits durch Config-/Account-Limits begrenzt; die Queue selbst bleibt bei
acht Einträgen.

Verifikation: gezielte Queue-/Service-Suite 6/6, `node --check` und
`git diff --check` grün.

## Runde 52: Timer- und Mainloop-Quellen

Fokus: Refresh-, Anzeige-, Stale-, Device-Login- und Settings-Timer in
`files/codex-usage@H234598/applet.js`. Jede Quelle besitzt einen eindeutigen
Property-Slot, wird vor Neuanlage entfernt und über Generationen gegen alte
Callbacks geschützt. Safe-Mode und Applet-Entfernung löschen Quellen,
Prozesse und Idle-Quellen; ungültige/fehlende Source-IDs führen nicht zu
phantomhaften Referenzen.

Settings-Maximierung begrenzt Lookup/Placement auf feste Versuche; laufende
wmctrl-Prozesse werden bei Neustart/Entfernung beendet. Keine unbounded
Timer- oder Idle-Quelle gefunden.

Verifikation: gezielte Timer-/Source-Suite 14/14, `node --check` und
`git diff --check` grün.

## Runde 53: Menü-, Panel- und Tooltip-Rendering

Fokus: `_buildUsageMenu()`, `_updatePanel()` und `_tooltipContent()` in
`files/codex-usage@H234598/applet.js`. Account-Zahl ist durch
`MAX_ACCOUNTS=100` begrenzt; Panel-Slots durch `PANEL_VALUE_MAX_COUNT=64` und
duplizierte Quellen werden verworfen. Menü-Neuaufbau pausiert während des
Öffnens und markiert nur einen Dirty-Zustand; beim Schließen erfolgt ein
einmaliger Neuaufbau. Einzelne Menütexte und Fehlertexte sind auf 240 Zeichen
begrenzt, Live-Login-Text auf 4096 Zeichen je Stream.

Tooltip-/Panel-Zusammenbau verarbeitet nur validierte, begrenzte Usage-Pools
und Fenster. Keine unbounded Actor-, String- oder Signal-Retention gefunden;
keine Produktionsänderung nötig.

Verifikation: gezielte Menü-/Panel-/Tooltip-Suite 13/13, `node --check` und
`git diff --check` grün.

## Runde 54: Consumption-/Forecast-Rechnung

Fokus: `src/codex_usage/consumption.py`. Lookback-/Baseline-Werte,
Smoothing, Reset-Erkennung und Forecast sind validiert. Iteratoren werden
vor Verarbeitung auf `MAX_CONSUMPTION_SAMPLES` begrenzt; Sortierung,
Beobachtungen und EMA arbeiten nur auf diesem gebundenen Satz. Ungültige
Zeiten, Samples, Gaps und nicht endliche Werte brechen kontrolliert ab.

Der hohe gemeinsame History-Cap ist endlich und durch die History-Abfrage
begrenzt; kein neuer spezifischer Reader-/Heap-Bug in diesem Modul gefunden.

Verifikation: `tests/test_consumption.py` plus
`tests/test_integration_entrypoint.py` 94/94, Ruff, `py_compile` und
`git diff --check` grün.

## Runde 55: History-DB-Retention und I/O

Fokus: `src/codex_usage/history.py` und Scheduler-Aufruf. SQLite nutzt einen
indizierten Lookup über Account/Pool/Fenster/Zeit; `samples()` und
`samples_for_consumption()` begrenzen Materialisierung auf
`MAX_HISTORY_SAMPLES`. `record_many()` dedupliziert über den UNIQUE-Schlüssel
und rollt bei Fehlern zurück. WAL plus `synchronous=FULL` verursacht bewusst
dauerhafte Flushes pro Snapshot-Batch, nicht pro Sample.

Befund: Die DB-Retention ist nicht automatisch begrenzt. Ohne explizites
`codex-usage history prune --apply` wächst die Historie über Monate weiter;
damit steigen Speicherplatz und Index-I/O. Automatisches Löschen wäre eine
unautorisierte Datenaufbewahrungsentscheidung und wurde nicht eingebaut.
Prune bleibt explizit, sicher und getestet.

Verifikation: `tests/test_history.py`, `tests/test_history_cli.py` und
`tests/test_scheduler.py` 405/405, Ruff, `py_compile` und `git diff --check`
grün.

## Runde 56: Scheduler-Polling und Parallelität

Fokus: `src/codex_usage/scheduler.py`. Account-Input ist auf 100 Einträge
begrenzt; Browser-Fetches nutzen höchstens vier Worker. Authentifizierte oder
gemeinsam verwendete Quellen werden bewusst seriell und global gelockt, um
Provider-Bucket-/Identity-Vermischung zu verhindern. Watch-Zyklen überlappen
nicht: die Wartezeit wird erst nach Zyklusende berechnet; Fehler nutzen
exponentielles Backoff bis zum Intervall-Limit.

Snapshot-/History-Schreiben erfolgt als ein gebundener Batch unter globalem
Lock. Watchdog-Mehrfachfetches bleiben auf die konfigurierte Accountzahl
begrenzt. Keine unbounded Thread-, Future- oder Poll-Quelle gefunden.

Verifikation: Scheduler-/History-CLI-Suite 280/280, Ruff, `py_compile` und
`git diff --check` grün.

## Runde 57: Account-Terminal-Spawn

Fokus: `src/codex_usage/terminal.py`. Terminal- und Codex-Binaries werden
über `shutil.which()` aufgelöst; Account-ID, Profilpfad und kanonisches
`auth.json` werden vor dem Spawn geprüft. API-Keys werden aus der Umgebung
entfernt, `CODEX_HOME` und CWD zeigen auf den Account, stdin/stdout/stderr
sind entkoppelt und `start_new_session=True` verhindert Bindung an den
Applet-Prozess.

Der Spawn ist absichtlich ein expliziter „neues Terminal“-Aufruf; das Modul
führt keine Timer-/Retry-Schleife und hält keinen Prozesszustand. Wiederholte
Fenster entstehen nur durch wiederholte Benutzeraktion; keine ungewollte
Spawnquelle gefunden.

Verifikation: Terminal-/CLI-Subset 38/38, Ruff, `py_compile` und
`git diff --check` grün.

## Runde 58: Browser-Fetch und Playwright-Lebensdauer

Fokus: `src/codex_usage/browser.py`. Headless ist Standard bei Usage-Fetches;
headed wird nur über explizite Login-/Diagnosepfade aktiviert. Jeder
persistent context liegt hinter einem Profil-Lock und wird in `finally`
geschlossen. Response-Kandidaten sind auf 50 Stück/4 MiB gesamt und 2 MiB je
JSON begrenzt; DOM-/HTML-Walker begrenzen Text und Knoten, Diagnoseantworten
und Screenshots sind ebenfalls begrenzt bzw. privat geschrieben.

Playwrights `response.text()` muss bei fehlendem/fehlerhaftem
Content-Length-Header zunächst vollständig materialisieren; danach greift der
2-MiB-Cap. API bietet hier keinen Streaming-Read. Das bleibt bekannte,
providerabhängige Restlast, aber kein unbounded Applet-/Cinnamon-State.

Verifikation: Browser-Profil-/Diagnose-Boundsuite 93/93, Ruff, `py_compile`
und `git diff --check` grün.

## Runde 59: Animationen finden auch benutzerdefinierte Datenhomes

Fokus: `src/codex_usage/config.py`, `src/codex_usage/profile_layout.py` und
die CLI-Discovery für `profile normalize-animations`. Neue und migrierte
Homes schreiben weiterhin `[tui].animations = false`; bekannte globale,
Agent-, Account- und verwaltete Profil-Homes werden fail-closed normalisiert.

Befund: Die Discovery verwendete für verwaltete Profile trotz
`default_state_dir()` hartkodiert `~/.local/share/codex-usage/profiles`. Bei
gesetztem absolutem `XDG_DATA_HOME` wurden diese Homes nicht gefunden und
konnten Animationen behalten. Die Profil-Discovery nutzt jetzt denselben
XDG-respektierenden `default_state_dir()`-Pfad wie die Profilerzeugung.

Verifikation: Regression zunächst rot, danach **11 fokussierte CLI-Tests**
grün (Animations-Discovery und Normalisierung), `py_compile`, Ruff für
`cli.py` und `git diff --check` grün.

## Runde 60: OAuth-Browser-Helper

Fokus: `src/codex_usage/oauth_browser.py`. Login-URL ist auf HTTPS und
OpenAI-/ChatGPT-Hosts begrenzt; Ports, Credentials, überlange URLs und
ungültige Argumente werden abgewiesen. Browserprofil, Markerdateien,
Eigentümer, Hardlinks, Modi und Symlink-Ancestors werden vor dem Start
geprüft. Der Browser startet mit getrennten Standard-I/O-Kanälen und
`start_new_session=True`; `CODEX_HOME` und `BROWSER` werden nicht in die
Helper-Umgebung übernommen.

Keine unbounded Datenhaltung, Retry-Schleife oder ungewollte Fensterquelle
gefunden. Der Helper startet nur auf explizitem Reaktivierungsaufruf.

Verifikation: **23 fokussierte OAuth-Browser-Tests** grün (eine erwartete
`runpy`-Warnung), `py_compile`, Ruff und `git diff --check` grün.

## Runde 61: Private Datei-I/O und Locks

Fokus: `src/codex_usage/private_io.py`. Private Reads prüfen Eigentümer,
Regularität, Symlink-Ancestors und Dateigröße vor dem bounded Read; UTF-8-
Fehler werden kontrolliert gemeldet. Writes nutzen zufällige create-only
Temporärdateien, private Modi, partielle-Write-Wiederholung, `fsync`,
atomisches Replace bzw. create-only Hardlink und Directory-`fsync`.
Lock-Dateien sind private Einzeldateien und ihre nicht-blockierende
Exklusivsperre endet am festen Deadline-Limit.

Keine unbounded Read-/Lock-Schleife, Heap-Retention oder neue Race-Lücke
gefunden. Bestehende absichtliche `EINTR`-/Cleanup-Pfade bleiben erhalten.

Verifikation: **95 `tests/test_private_io.py`-Tests** grün, `py_compile`,
Ruff und `git diff --check` grün.

## Runde 62: CLI-Rendering

Fokus: `src/codex_usage/render.py`. Account- und Usage-Iterables werden vor
Materialisierung auf die Konfigurationsgrenze begrenzt; Werte, Status,
Fehler, Pfade und Rohtexte erhalten feste Zell-/Textgrenzen. Modellfenster
und Pools stammen aus bereits gebundenen State-/Extractor-Strukturen; ungültige
Provenienz oder Status wird fail-closed als nicht anzeigbar projiziert.

Kein unbounded String-/Listenaufbau in den Renderingpfaden und keine neue
Heap-/I/O-Quelle gefunden. Dateistatusprüfungen bleiben auf höchstens 100
Accounts begrenzt.

Verifikation: **85 `tests/test_render.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 63: State-/Snapshot-Speicher

Fokus: `src/codex_usage/state.py`. Snapshot- und Generationsdateien haben
harte Byte-, Text-, URL-, Pool- und Fenstergrenzen. Reads laufen über
`read_private_text`; JSON-/Provenienz-/Reset-/Zeitwerte werden fail-closed
validiert. State-Updates schreiben atomisch unter Account-Lock, und
Account-Löschung kapselt Current-, Snapshot- und Generation-Dateien in einer
begrenzten Transaktion.

Keine neue unbounded Materialisierung, Retention-Schleife oder Heap-Leak
gefunden. Historien-Retention bleibt separat und bewusst explizit (Runde 55).

Verifikation: **331 `tests/test_state.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 64: Usage-Extractor erneut gegen Heap-/Parserlast geprüft

Fokus: `src/codex_usage/extractor.py`. JSON-Kandidaten sind auf 50 begrenzt;
Walk-Tiefe und Kinder, Flatten-Felder, Window-Matches, Textlabel-Offsets und
HTML-Progressparser-Einträge haben feste Caps. Textfenster werden pro Label
auf 1500 Zeichen begrenzt, Roh-/Previewwerte auf 500 Zeichen. Browser und
Bridge liefern zusätzlich maximal zwei bzw. neun bereits begrenzte
Textquellen; direkte JSON-Kandidaten werden vor dem Walk begrenzt.

Keine neue reproduzierbare Fehlfunktion oder unbounded Applet-/Cinnamon-Last
gefunden. Eine zusätzliche globale Textquellenkürzung wäre nur redundante
Verhaltensänderung gegenüber den bereits begrenzten Aufrufern.

Verifikation: **213 `tests/test_extractor.py`-Tests** grün, `py_compile`,
Ruff und `git diff --check` grün.

## Runde 65: Backend-Identitätsauswahl

Fokus: `src/codex_usage/identity.py`. Kandidaten übernehmen den Extractor-
Cap von 50; URLs werden vor Priorisierung geparst. Identitäts- und Planwerte
haben feste Längen-/Zeichenregeln. Account-/User-Gruppen und partielle
Identitäten werden nur bei eindeutiger Auth-Zuordnung zusammengeführt;
mehrdeutige oder fremde Gruppen bleiben fail-closed verworfen.

Keine unbounded Kandidaten-/Gruppenstruktur, URL- oder String-Retention und
keine neue Account-Vermischung gefunden.

Verifikation: **46 `tests/test_identity.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 66: Usage-Limit-Pools

Fokus: `src/codex_usage/usage_limits.py`. WHAM- und App-Server-Parser
verarbeiten nur die bekannten Primär-/Sekundärfenster; Dauer, Zähler,
Prozentwerte, Resetzeiten und Control-Flags werden streng validiert. Spark-
Duplikate und Modellkataloge bleiben auf einen bounded Pool-/ID-Satz begrenzt;
der vorgelagerte Response-/Ingest-Pfad begrenzt Payload-Bytes.

Keine neue unbounded Retention, ungültige Window-Inferenz oder Pool-
Vermischung gefunden. Zusätzliche Einträge liefern höchstens den bereits
validierten Spark-Pool und werden nicht kumulativ gespeichert.

Verifikation: **146 `tests/test_usage_limits.py`-Tests** grün, `py_compile`,
Ruff und `git diff --check` grün.

## Runde 67: Reset-Status

Fokus: `src/codex_usage/usage_resets.py`. Kanonische, verschachtelte,
Legacy- und App-Server-Resetfelder werden nur bei vollständigen, konsistenten
Quellen übernommen. Zähler sind strikt integer und auf 0–10.000 begrenzt;
unbekannte/konfligierende Werte bleiben unbekannt. Formatierung blendet Null
optional aus, Redemption bleibt ohne Capability und positive bekannte Anzahl
gesperrt.

Keine unbounded Struktur, falsche Null-/Unbekannt-Projektion oder neue
Resetvermischung gefunden.

Verifikation: **29 `tests/test_usage_resets.py`-Tests** grün, `py_compile`,
Ruff und `git diff --check` grün.

## Runde 68: Health-Event-Store

Fokus: `src/codex_usage/health.py`. Health-Datei ist auf 256 KiB begrenzt;
Events werden auf 128 gültige Einträge, Tokenfelder und Dauerwerte begrenzt.
Read-Recovery verwirft malformed/private-unsaubere Dateien fail-closed,
trimmt abgelaufene Events und stoppt nach einem gültigen Tail. Writes
entfernen bei Bedarf älteste Einträge bis Bytebudget passt und nutzen private,
atomische Datei-I/O.

Keine unbounded Retention, Recovery-Schleife oder Heap-Quelle gefunden.

Verifikation: **49 `tests/test_health.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 69: Account-/Usage-Modelle

Fokus: `src/codex_usage/models.py`. Fensteridentität, numerische Grenzen,
Pool-Erschöpfung, Legacy-Main-Pool, case-ambige Modellschlüssel und
malformed Container bleiben fail-closed. Serialisierung ignoriert fremde
Typen und fängt fehlerhafte Property-/Zeitkonversionen ab. Die eigentlichen
Eingangspfade begrenzen Modellpools, Fenster, Quellen und JSON-Kandidaten;
im Model selbst wurde keine neue unbounded Retention oder falsche
Account-/Fenstervermischung gefunden.

Keine neue reproduzierbare Fehlfunktion gefunden.

Verifikation: **75 `tests/test_models.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 70: Integrations-Installer

Fokus: `src/codex_usage/integration_installer.py`, insbesondere die
Read-only-/Reload-relevanten Installationspfade. State-, Data- und
Temporary-Verzeichnisse werden vor jeder Mutation als private Eigentümer-
Verzeichnisse revalidiert. Staging wird exklusiv erstellt und per
`renameat2(RENAME_NOREPLACE)` aktiviert; Manifest, Wheel, RECORD, Launcher
und Release-Tree werden vor dem Umschalten erneut geprüft. Builder-Subprozesse
sind offline, timeoutbegrenzt und als eigene Prozessgruppe terminierbar.
Wheel-/Datei-/Tree-Größen und Eintragszahlen sind begrenzt; Cleanup entfernt
nur identitätsgleiche eigene Artefakte.

Keine reproduzierbare Read-only-Sperre, Race-Lücke, unbounded Materialisierung
oder fehlerhafte Reload-Aktivierung gefunden. Der frühere Read-only-Eindruck
war Sandbox-Verhalten, nicht Installer-Logik.

Verifikation: **221 `tests/test_integration_installer.py`-Tests** grün
(2:02 min), `py_compile`, Ruff und `git diff --check` grün.

## Runde 71: Integration-Attestation

Fokus: `src/codex_usage/integration_attestation.py`. Manifestpfade sind
absolut, zustands-/datengebunden und innerhalb des Release-Baums. Private
Datei- und Verzeichnis-Identitäten werden vor und nach dem Öffnen geprüft;
Symlinks, Fremdeigentümer, Hardlinks, Modusänderungen und Descriptor-Swaps
fallen fail-closed aus. Release-Tree-Hash liest per FD, begrenzt 4-MiB-
Dateien, 128-MiB-Gesamtpayload und 4096 Einträge. RECORD-CSV, Digest,
Launchervertrag und Metadaten werden strikt geprüft.

Keine neue Race-Lücke, Read-only-Fehlfunktion oder unbounded Materialisierung
gefunden.

Verifikation: **78 Attestation-/Manifest-/RECORD-Tests** grün,
143 Tests deselected, `py_compile`, Ruff und `git diff --check` grün.

## Runde 72: Cinnamon-Settings-/Reload-Handler

Fokus: `files/codex-usage@H234598/applet.js`, `_openSettings()` und
`_scheduleSettingsMaximize()`. Settings-Prozess nutzt den Applet-Instance-
Parameter und `NO_AT_BRIDGE=1`; ein laufender Prozess wird wiederverwendet.
PID-/Window-Lookup, Monitor-Placement, Maximierung und Fokus haben
Generationsguards, Retry-/Timeout-Grenzen und Cleanup bei Entfernung.
Fehlendes `wmctrl` darf Settings-Öffnung nicht als Spawn-Fehler maskieren.

Keine reproduzierbare Read-only-Blockade, unbounded Prozess-/Timerquelle oder
verwaiste Settings-Callback-Kette gefunden.

Verifikation: **43 Settings-/Reload-Tests** grün, `node --check` und
`git diff --check` grün.

## Runde 73: Applet-Payload-/Consumption-Grenzen

Fokus: `files/codex-usage@H234598/applet.js`, DTO-Sanitizer und
`_refreshConsumption()`. Eingehende Accounts, Modellpools, Fenster,
Consumption-Fenster, Textfelder und Prozessausgaben haben feste Grenzen;
Pool-/Fensteridentitäten und widersprüchliche Metadaten werden nicht
normalisiert, sondern verworfen. Die Consumption-Queue wächst nur aus dem
bereits auf 100 Accounts begrenzten Usage-Satz und maximal wenigen
konfigurierten Pool-Abfragen je Account; Generationen verwerfen alte
Antworten.

Keine neue unbounded Cinnamon-Heap-Quelle, Queue-Leak oder falsche
Pool-/Fensterprojektion gefunden.

Verifikation: **7 fokussierte Runtime-Tests** grün, `node --check` und
`git diff --check` grün.

## Runde 74: Producer-Integration-Entrypoint

Fokus: `src/codex_usage/integration_entrypoint.py`. Der externe Producer-
Aufruf akzeptiert nur das exakte argv-Schema, verlangt absolute XDG-Wurzeln,
verifiziert aktiven Release vor und nach Snapshot-Erzeugung und publiziert
erst danach. Fehler werden auf feste Exit-Codes/Tokens ohne Pfad- oder
Exception-Details abgebildet. Kostenfenster sind aus History-Dauern dedupliziert
und auf `MAX_CONSUMPTION_WINDOWS` begrenzt; Credit-Fenster bleiben separat.

Keine neue unbounded History-Abfrage, Lock-/Publish-Race oder falsche
Fehler-/Exit-Projektion gefunden.

Verifikation: **38 `tests/test_integration_entrypoint.py`-Tests** grün,
`py_compile`, Ruff und `git diff --check` grün. Ein bestehender `runpy`-
RuntimeWarning ist testbedingte Import-Reihenfolge, kein Produktionsfehler.

## Runde 75: Consumption-Berechnung

Fokus: `src/codex_usage/consumption.py`. Lookback-, Baseline-, Stale- und
Gap-Grenzen werden vor Zeit-Arithmetik geprüft. Samples sind auf
`MAX_CONSUMPTION_SAMPLES` begrenzt, müssen echte valide `UsageSample`-Objekte
eines Accounts/Pools/Fensters sein und werden bei Bedarf stabil sortiert.
Positive Deltas, bestätigte Resets und EMA verwenden dieselben Gap-Regeln;
Forecasts bleiben endlich und auf ein Jahr begrenzt.

Keine neue NaN/Infinity-Arithmetik, Overflow-Kante, Resetvermischung oder
unbounded Sample-Materialisierung gefunden.

Verifikation: **56 `tests/test_consumption.py`-Tests** grün, `py_compile`,
Ruff und `git diff --check` grün.

## Runde 76: Account-Lock — drei Findings für Sammelfix

Fokus: `src/codex_usage/account_lock.py`. Basistests bleiben grün, aber drei
reproduzierbare Kanten werden gesammelt und noch nicht einzeln repariert:

1. Bei Contention schläft der Retrypfad immer 50 ms. Ein Lock mit Timeout
   10 ms kann dadurch nach Ablauf seiner Deadline noch erfolgreich erworben
   werden, wenn er während des Sleeps frei wird.
2. Nach `_prepare_lock_directory()` werden `codex-usage/locks` und alle
   Ancestor-Pfade erneut per String geöffnet. Ein gleichzeitiger Austausch
   des validierten `locks`-Verzeichnisses gegen einen Symlink wird nicht durch
   `O_NOFOLLOW` am finalen Locknamen verhindert; der Lock kann in ein fremdes
   user-eigenes Ziel geschrieben werden und Synchronisierung umgehen.
3. `isinstance(account_id, str)` akzeptiert einen `str`-Subclass mit eigener
   `__format__`. Der f-String für den Locknamen kann dadurch `../` injizieren;
   Inline-Reproduktion erzeugte `codex-usage/escaped.lock` außerhalb von
   `locks`.

Verifikation: **22 `tests/test_account_lock.py`-Tests** grün, plus drei
isolierte Inline-Reproduktionen der Kanten; `py_compile`, Ruff und
`git diff --check` grün. Findings bleiben bis zum Sammelfix offen.

## Runde 77: Service-Read-only-Sonderpfad

Fokus: `src/codex_usage/service.py`, insbesondere `_unit_directory(create=False)`.
Der EROFS-Fallback wird nur für bereits existierende, eigene 0700-Verzeichnisse
bei reiner Inspektion zugelassen; mutierende Installationspfade verwenden
weiterhin `create=True` und melden Read-only korrekt. Systemctl-Ausgabe,
Timeouts, Rollback und Symlink-Prüfungen bleiben begrenzt.

Keine neue Fehlermeldung, die Read-only fälschlich als erfolgreiches Schreiben
maskiert, und keine zusätzliche Prozess-/Output-Leakquelle gefunden.

Verifikation: **103 `tests/test_service.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün. Die drei Account-Lock-Findings aus Runde 76
bleiben für den Sammelfix offen.

## Runde 78: State-Snapshot-/Generation-Transaktionen

Fokus: `src/codex_usage/state.py`. Snapshot- und Current-Dateien sind
byte-/feldbegrenzt, Generationen werden unter Account-Lock geprüft und
Deletion-Transaktionen verschieben höchstens acht bekannte Artefakte vor
Generation-Invaliderung. Rollback stellt Generation und Dateien in
umgekehrter Reihenfolge wieder her; malformed Fenster, Pools, Backends,
Zeitwerte und Resetdaten fallen fail-closed.

Keine neue Snapshot-Vermischung, unbounded JSON-Struktur, Reset-/Generation-
Fehlprojektion oder reproduzierbare Rollback-Lücke gefunden. Pfad-TOCTOU-
Risiken bleiben bereits als allgemeines privates-I/O-Problem in Runde 76
gesammelt; keine zusätzliche unabhängige State-Funktion ergänzt.

Verifikation: **331 `tests/test_state.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün. Drei Account-Lock-Findings bleiben offen.

## Runde 79: Account-Lock-Sammelfix

Die drei Findings aus Runde 76 sind behoben. Account-IDs müssen jetzt
Built-in-`str` sein; der Lockname kann keine formatierte `../`-Komponente mehr
einschleusen. Der Lock wird relativ zu einem validierten, per
`O_DIRECTORY|O_NOFOLLOW` geöffneten `locks`-Descriptor angelegt; ein
Directory-Symlink-Swap erreicht kein Redirect-Ziel. Retry-Sleeps werden auf
die verbleibende Deadline gekürzt und nach Ablauf nicht erneut versucht.

Regressionen decken alle drei Fälle ab. Verifikation: **25
`tests/test_account_lock.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 80: Private-I/O-Deadline und Directory-Fsync

`private_path_lock()` hatte denselben 50-ms-Deadline-Überlauf wie der
Account-Lock; der Retrypfad kürzt jetzt den Sleep und prüft vor jedem weiteren
Versuch die Deadline. `_fsync_directory()` öffnete Verzeichnisse bisher ohne
`O_NOFOLLOW`; ein Symlink konnte dadurch den falschen Directory-FD für die
Durability-Synchronisierung liefern. Der optionale No-follow-Flag ist jetzt
gesetzt.

Verifikation: **97 `tests/test_private_io.py`-Tests** grün, inklusive beider
Regressionen, `py_compile`, Ruff und `git diff --check` grün.

## Runde 81: Browser-/Playwright-Lebensdauer

Fokus: `src/codex_usage/browser.py`. Persistent contexts werden in allen
Fetch-/Probe-/Diagnosepfaden per `finally` geschlossen; Navigation und
`networkidle`-Timeouts hinterlassen keinen offenen Context. Response-Capture
begrenzt Kandidatenzahl und aggregierte Payloadgröße, filtert vertrauenswürdige
HTTPS-Hosts und verwirft bekannte übergroße Content-Lengths. DOM-/HTML-Ausgabe
ist durch Zeichen- und Node-Grenzen beschränkt; Diagnoseantworten und private
Probe-Ausgaben bleiben ebenfalls endlich.

Keine neue reproduzierbare Playwright-Context-Leak-, Response-Queue- oder
unbounded DOM-Ausgabe gefunden. Responses ohne Content-Length müssen weiter
materialisiert werden, weil Playwright synchron keine begrenzte `text()`-Lesung
anbietet; nachgelagerte Größen-/Kandidatenlimits bleiben aktiv.

Verifikation: **178 `tests/test_browser_profile.py`-Tests** grün,
`py_compile`, Ruff und `git diff --check` grün.

## Runde 82: Reactivation-Browser-Profil-Symlink

Fokus: `src/codex_usage/reactivate.py`, `_manage_browser_profile()`. Der
Kompatibilitätsweg für `open_account_in_reactivation_browser()` prüfte bisher
nur `is_dir()` und einen Marker. Ein vorhandenes
`profile_dir/firefox`-/`chromium`-Symlink wurde dadurch als gültiges Profil
zurückgegeben; der isolierte Browser-Helfer hätte ein fremdes Zielverzeichnis
geöffnet. Inline-Reproduktion bestätigt Redirect außerhalb des Account-
Profilbaums.

Regressionstest reproduziert die Kante. `_manage_browser_profile()` prüft den
kompletten Pfad jetzt vor Wiederverwendung auf Symlink-Ancestors und bricht
fail-closed ab.

Verifikation: **113 `tests/test_reactivate.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün. Der bestehende `oauth_browser`-`runpy`-Hinweis ist
testbedingte Import-Reihenfolge.

## Runde 83: Profile-Job-Worker

Fokus: `src/codex_usage/profile_jobs.py`. Manifest-, Event-, Verzeichnis- und
Worker-Argumente bleiben hart begrenzt; Worker-Ausgaben gehen nach `DEVNULL`.
Start-/Tracking-/Reap-Fehler räumen Manifest und Prozessgruppe best effort auf.
Status- und Cancel-Races verwenden erwartete Zustände; `/proc`-Cmdline wird
bounded und mit `O_NOFOLLOW` gelesen. Terminal-Jobs löschen Events erst nach
dem Statusübergang.

Keine neue reproduzierbare Prozessgruppen-, Queue-/Manifest-Leak- oder
unbounded Output-Lücke gefunden.

Verifikation: **129 `tests/test_profile_jobs.py`-Tests** grün, `py_compile`,
Ruff und `git diff --check` grün. Der bestehende `profile_jobs`-`runpy`-Hinweis
ist testbedingte Import-Reihenfolge.

## Runde 84: App-Server-RPC

Fokus: `src/codex_usage/app_server.py`. RPC-Zeilen sind auf 2 MB, ausstehende
Nachrichten auf 101 und Model-IDs auf 100 begrenzt. Nonblocking-Stdin schreibt
mit Deadline; Reader-Threads sind daemonisiert, stderr bleibt auf 4 KiB und
Prozessgruppen werden bei Timeout beendet. Auth-/Plan-/E-Mail-Identität wird
vor und nach dem Request verglichen; Fehlertexte verlieren Steuerzeichen und
bleiben kurz.

Keine neue reproduzierbare Queue-/Reader-Leak-, Prozessgruppen- oder
unbounded JSON-/Fehlerausgabe gefunden.

Verifikation: **163 `tests/test_app_server.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 85: Bridge-URL-Sanitization

Fokus: `src/codex_usage/bridge.py`. `_redact_url()` entfernte Query,
Fragment und Userinfo, ließ aber eingebettete C0-/C1-Steuerzeichen im Pfad
durch. Ein Browser-Payload konnte damit terminalartige Zeichen in
`AccountUsage.source_urls` und Debug-Metadaten persistieren.

Inline-Reproduktion bestätigte den durchgereichten Escape-Text. `_redact_url()`
entfernt Pfad-Steuerzeichen jetzt vor Persistenz und Ausgabe.

Verifikation: **298 `tests/test_bridge.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 86: Direct-Fetch-Fehlerausgabe

Fokus: `src/codex_usage/direct.py`. `fetch_account_usage_direct()` gab
`DirectAuthError`-/`DirectFetchError`-Texte unverändert zurück. Fehler aus
`auth.json`-Lese-/Validierungspfaden enthalten den konfigurierten Pfad; ein
Pfad mit C0/C1-Steuerzeichen gelangte damit direkt in `AccountUsage.error` und
konnte CLI-/Panel-Ausgabe verändern.

Inline-Reproduktion bestätigte den Escape-Text. Direct-Fetch-Fehler werden am
`AccountUsage`-Ausgabepunkt jetzt whitespace-normalisiert und von C0/C1-
Steuerzeichen bereinigt, ohne normale Fehlermeldungen zu verändern.

Verifikation: **363 `tests/test_direct.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 87: OAuth-Browser-CLI-Fehlerausgabe

Fokus: `src/codex_usage/oauth_browser.py`. `main()` schrieb rohe
`OSError`-/`ValueError`-Texte auf stderr. Ein kontrollierter Browserpfad oder
Subprozessfehler konnte dadurch C0/C1-Steuerzeichen in Terminalausgabe
bringen. Die URL-/Profilvalidierung selbst war bereits begrenzt und
symlink-sicher.

Regressionstest reproduziert die Kante. `oauth_browser.main()` normalisiert
Fehlertexte jetzt und begrenzt sie vor stderr-Ausgabe; normale Meldungen
bleiben unverändert.

Verifikation: **114 `tests/test_reactivate.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün. Der bestehende `oauth_browser`-`runpy`-Hinweis ist
testbedingte Import-Reihenfolge.

## Runde 88: Health-Event-Store

Fokus: `src/codex_usage/health.py`. Ereignisse akzeptieren nur begrenzte
Tokens, Accounts und Dauerwerte; Retention hält 30 Tage/128 Einträge,
JSON-Dateien 256 KiB. Private I/O/Lock-Prüfungen schützen Datei und Directory;
malformierte Daten werden verworfen, statt Fehlerzustand zu vergrößern.

Keine neue reproduzierbare Event-/Datei-Queue-, Zeitwert- oder
Steuerzeichen-Projektion gefunden.

Verifikation: **49 `tests/test_health.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 89: Modell-Serialisierung

Fokus: `src/codex_usage/models.py`, `AccountUsage.as_dict()`. Optionales
`error`, `blocked_reason`, `raw`, Label-/Backend-Felder und `source_urls`
wurden nur auf `str` geprüft. Ein älterer oder manipulierter Snapshot konnte
dadurch C0/C1- oder Zeilensteuerzeichen bis in UI-/Terminal-Ausgabe tragen.

Regressionstest reproduziert die Kante. `_safe_text()` normalisiert optionale
Serialisierungsfelder jetzt zentral; `source_urls` verwendet denselben Pfad.

Verifikation: **76 `tests/test_models.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 90: Usage-Reset-State

Fokus: `src/codex_usage/usage_resets.py` und die Reset-Projektion im Applet.
Canonical-/Legacy-/App-Server-Resetquellen werden nur bei vollständiger,
gleicher und bounded Information zusammengeführt; `0` bleibt bekannt und
wird nur durch explizites `hide-when-zero` verborgen. Unbekannte Werte bleiben
`—`; Reset-Anzahl und Window-Resetdaten sind getrennte Felder.

Keine neue reproduzierbare Parser-/Zuordnungs- oder Resetwert-Auslassung
gefunden. Nufker-Sichtbarkeit hängt damit an der aktivierten Formatierungs-
Ziel-/Nullausblendung, nicht am Resetparser.

Verifikation: **29 `tests/test_usage_resets.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 91: Routing-Projektion

Fokus: `src/codex_usage/routing.py`, Pool-/Fensteridentität, Resetgrenzen,
Spark-Health, Credit-Policy und JSON-/Dateipfade. Unbekannte oder abgelaufene
Fenster blockieren fail-closed; Spark benötigt echte Usage-Evidenz und frische
Health-Daten. Policy-Datei und Vorfahren bleiben symlinkfrei und exakt privat.

Keine neue reproduzierbare Routing-/Pool-/Serializer-Ausgabe gefunden.

Verifikation: **168 `tests/test_routing.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 92: Auth-Migrationsplan-Zielbindung

Fokus: `src/codex_usage/profile_migration.py`. Ein manipuliertes
`AuthMigrationPlan` konnte bei `apply_auth_migration()` ein Ziel wie
`.../codex-home/secret` vorgeben. Der Layout-Aufbau akzeptierte das und legte
Auth-Inhalt unter einem nicht-kanonischen Dateinamen ab; bei anderer
Pfadstruktur konnte er außerdem fremde Profilpfade berühren.

Regressionstest reproduziert die Annahme. Planvalidierung verlangt jetzt
kanonisch `target.parent.name == "codex-home"` und
`target.name == "auth.json"`; Dot-Segmente im Profilpfad werden verworfen.

Verifikation: **96 `tests/test_profile_migration.py`-Tests** grün,
`py_compile`, Ruff und `git diff --check` grün.

## Runde 93: Profil-Layout

Fokus: `src/codex_usage/profile_layout.py`, Profil-/Codex-Home-Pfade,
Animationsnormalisierung, private Metadaten und Rollback. Pfade werden vor
Erzeugung auf absolute, symlinkfreie und private Verzeichnisse begrenzt;
Konfigurationsänderungen erhalten inode-/Modusprüfung und werden bei
Metadatenfehlern zurückgerollt.

Keine neue reproduzierbare Layout-/Animations-/Rollback-Lücke gefunden.

Verifikation: **65 `tests/test_profile_layout.py`-Tests** grün, `py_compile`,
Ruff und `git diff --check` grün.

## Runde 94: Device-Login

Fokus: `src/codex_usage/profile_login.py`. Staging-CODEX_HOME, Prozessgruppe,
Output-/Event-Caps, Auth-Validierung, create-only-Publish und Finalize-Rollback
wurden verfolgt. Produktionspfad liest Subprozessausgabe bounded; Auth wird
vor Publish zweimal validiert und bei Finalize-Fehler über inode-/Linkprüfung
bereinigt.

Keine neue reproduzierbare Device-Login-/Staging-/Cleanup-Lücke gefunden.

Verifikation: **79 `tests/test_profile_login.py`-Tests** grün, `py_compile`,
Ruff und `git diff --check` grün.

## Runde 95: Config-Pfad-Leerfall

Fokus: `src/codex_usage/config.py`. `load_config()` prüfte Vorfahren erst
beim Lesen einer vorhandenen Datei. Eine fehlende Datei unter symlinked parent
lieferte dagegen still `AppConfig(accounts=())`; derselbe Pfad wurde beim
Speichern abgelehnt. Das kann Accounts scheinbar verschwinden lassen und
verdeckte Pfadumleitung erlauben.

Regressionstest reproduziert den Leerfall. `load_config()` prüft jetzt den
Config-Pfadvorfahren vor `exists()`; symlinked parents werden fail-closed
abgelehnt.

Verifikation: **232 `tests/test_config.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 96: CLI-Textausgabe

Fokus: `src/codex_usage/cli.py`, menschliche Account-/Profil-Ausgaben.
Konfigurationslabels dürfen für TOML-Roundtrip weiterhin Newline enthalten;
direkte `print()`-Interpolation konnte dadurch aber ESC-/C0-Sequenzen ins
Terminal schreiben. JSON-Ausgaben bleiben unverändert strukturiert.

Textausgaben für Account, Label, Backend und Profilpfad verwenden jetzt die
vorhandene zentrale Steuerzeichen-/Längenbereinigung `_clean_cli_error()`.

Verifikation: **160 `tests/test_cli.py`-Tests** grün (eine erwartete
`runpy`-Warnung), `py_compile`, Ruff für `cli.py` und `git diff --check` grün.
Ruff meldet im historisch gewachsenen `tests/test_cli.py` 60 bestehende
`E501`-Zeilen; diese reine Formatierungsbaustelle wurde nicht vermischt.

## Runde 97: CLI-Historienpfad

Fokus bleibt `cli.py`: `history status` druckte den frei wählbaren SQLite-Pfad
noch direkt und konnte damit denselben Terminal-Injection-Pfad öffnen.
Tabellenausgabe nutzt nun ebenfalls `_clean_cli_error()`; JSON bleibt
unverändert.

Verifikation: gezielter Regressionstest und bestehende History-CLI-Tests grün,
`py_compile`, Ruff für `cli.py` und `git diff --check` grün.

## Runde 98: CLI-Datei-/Historienwerte

Fokus bleibt `cli.py`: frei gespeicherte `reset_generation`-Werte sowie
Ingest-, Extension- und Pfadausgaben wurden noch direkt in Textausgaben
interpoliert. Diese Werte sind nicht alle durch Account-Validierung geschützt.

Die betroffenen menschlichen Ausgaben verwenden jetzt dieselbe zentrale
Steuerzeichenbereinigung; strukturierte JSON-Ausgaben bleiben unverändert.

Verifikation: **162 `tests/test_cli.py`-Tests** grün (eine erwartete
`runpy`-Warnung), `py_compile`, Ruff für `cli.py` und `git diff --check` grün.

## Runde 99: State-Transaktionen und Eingabegrenzen

Fokus: `src/codex_usage/state.py`, Snapshot-/Generation-Lesen,
Account-State-Löschung und Rollback.

Drei reproduzierbare Fehler gefunden und behoben:

1. `_remove_state_transaction_dir()` folgte einem ersetzten
   Transaktionsverzeichnis-Symlink. Dateien im Symlink-Ziel konnten gelöscht
   werden, bevor `rmdir()` den Fehler meldete. Cleanup prüft jetzt
   symlinkfreie, echte Verzeichnisse; Regressionstest schützt Sentinel-Datei.
2. State-Einstiegspunkte akzeptierten `Path`-Subklassen und führten deren
   überschreibbare Operatoren aus. Verzeichnisse werden jetzt nur als nativer
   `Path`-Typ akzeptiert.
3. Snapshot-Account-IDs akzeptierten `str`-Subklassen; ein überschriebenes
   `__hash__` konnte ungefangenen Code auslösen. IDs werden jetzt auf exakten
   Builtin-`str` begrenzt.

Verifikation: **334 `tests/test_state.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 100: Scheduler-Eingaben und Watchdog-Fail-Closed

Fokus: `src/codex_usage/scheduler.py`, Fetch-Orchestrierung, Account-Listen,
Auth-Pfad und Watchdog-Fehlerpfade.

Fünf reproduzierbare Fehler gefunden und behoben:

1. Scheduler-Account-IDs akzeptierten `str`-Subklassen; Hash-/Set-Hooks
   konnten ungefangen auslösen. IDs werden jetzt auf exakten Builtin-`str`
   begrenzt.
2. Doppelte Account-IDs wurden in `fetch_all()` parallel verarbeitet und beim
   Speichern über ein Dictionary zusammengefaltet. Doppelte IDs werden jetzt
   abgelehnt.
3. `_validated_auth_json_path()` akzeptierte `Path`-Subklassen, deren
   `expanduser()` einen Nicht-Pfad zurückgab. Das führte später zu
   `AttributeError`; Eingabe und Rückgabewert werden jetzt exakt geprüft.
4. `_window_is_exhausted()` ließ fehlerhafte Fenster-Properties bis in den
   Watchdog durch. Property-Fehler gelten jetzt als erschöpft; Blockierung
   bleibt fail-closed mit unbekannter Resetzeit.
5. `_sanitize_failure_text()` konnte bei Exceptions mit kaputtem `__str__`
   selbst abstürzen. Fallback nutzt jetzt Exception-Typnamen.

Verifikation: **278 `tests/test_scheduler.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 101: History-Pfade und Sample-Extraktion

Fokus: `src/codex_usage/history.py`, SQLite-HistoryStore, Timestamp- und
Usage-Sample-Grenzen.

Fünf reproduzierbare Fehler gefunden und behoben; zusammen mit dem
unmittelbaren Round-131-Relayout-Fix sechs:

1. History-Pfade akzeptierten `Path`-Subklassen mit überschreibbaren Methoden.
   Nur native `Path`-Objekte werden jetzt zugelassen.
2. `UsageSample` akzeptierte String-Subklassen für Account, Pool,
   Reset-Generation und Quelle; überschreibbare Längen-/ASCII-Hooks konnten
   abstürzen. Alle Felder verlangen jetzt Builtin-`str`.
3. `_to_millis()` ließ fehlerhafte Datetime-Subklassen ungefangen durch. Die
   Konvertierung liefert jetzt einen kontrollierten Timestamp-Fehler.
4. Limit-Fenster mit fehlerhaften Properties konnten Sample-Extraktion
   abbrechen. Fenster werden bei Property-Fehler übersprungen.
5. Fehlerhafte Duration-/Reset-Properties in Limit-Fenstern wurden ebenfalls
   nicht abgefangen. Zugriff läuft jetzt durch sichere lokale Werte.
6. Credits hatten denselben Duration-/Reset-Fehlerpfad; Credit-Samples
   überspringen fehlerhafte Fenster jetzt fail-closed.

Verifikation: **131 `tests/test_history.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 102: Private I/O

Fokus: `src/codex_usage/private_io.py`, gemeinsame Lock-, Lese-, Schreib-
und Verzeichnis-Primitiven.

Kein neuer reproduzierbarer Fehler gefunden. Native-`Path`-Grenzen,
Symlink-Prüfungen, private Eigentümer-/Modusprüfungen, atomisches Schreiben,
Rollback, Dateideskriptor-Cleanup und Lock-Timeouts sind durch bestehende
Regressionstests abgedeckt. Die verbleibenden theoretischen
Path-basierte-TOCTOU-Fenster erfordern eine größere `openat`-Umstellung und
wurden ohne reproduzierbaren Schaden nicht spekulativ umgebaut.

Verifikation: **97 `tests/test_private_io.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 103: Account-Terminal

Fokus: `src/codex_usage/terminal.py`, Account-Terminal-Start und Resolver.

Fünf reproduzierbare Grenzfehler gefunden und behoben:

1. Ungültige Layout-Felder entkamen als rohes `ValueError`; der Terminal-
   Einstieg liefert jetzt konsistent `TerminalError`.
2. Account-Felder und explizite Resolver-Kommandos akzeptierten
   `str`-Subklassen mit überschreibbaren Hooks. Nur Builtin-Strings passieren
   diese Grenze; ungültige Account-IDs bleiben als solche klassifiziert.
3. Ein öffentlich lesbares Profilverzeichnis wurde trotz privater
   `auth.json` gestartet. Start prüft jetzt Eigentümer, Verzeichnis-Typ und
   Gruppen-/Andere-Rechte.
4. `_validate_auth_json()` akzeptierte `Path`-Subklassen; native `Path`-Typen
   sind jetzt verbindlich.
5. Symlink-/Resolve-Fehler eines gefundenen Terminal-Kandidaten konnten
   ungefangen abbrechen. Der Kandidat wird jetzt verworfen und Suche läuft
   fail-closed weiter.

Verifikation: **46 `tests/test_terminal.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 104: Applet-Settings-Prozesslebenszyklus

Fokus: `files/codex-usage@H234598/applet.js`, Settings-/`wmctrl`-Start,
Window-Lookup, Timeout- und Remove-Cleanup.

Kein neuer reproduzierbarer Fehler gefunden. Settings-Prozesse werden bei
erneutem Öffnen wiederverwendet, Window-Lookup und Positionierung sind durch
Generationen gegen verspätete Callbacks geschützt, Ausgaben bleiben begrenzt,
und Applet-Entfernung räumt Timer, Lookup-/Placement-Kinder und übrige
Subprozesse auf. Der Settings-Prozess selbst bleibt absichtlich bestehen,
damit ein geöffnetes Settings-Fenster nicht beim Applet-Reload verschwindet.

Verifikation: **608 `tests/applet_runtime.test.js`-Tests** und
`node --check files/codex-usage@H234598/applet.js` grün.

## Runde 105: Cinnamon-Installer-Settingsmigration

Fokus: `scripts/install_cinnamon_applet.py`, `_migrate_cached_settings()`.

Drei reproduzierbare Fehler gefunden und behoben:

1. Der Cache-Pfad akzeptierte `Path`-Subklassen mit überschreibbaren Hooks;
   nur native `Path`-Objekte werden verarbeitet.
2. Symlinked Eltern konnten die Migration in ein fremdes Verzeichnis lenken.
   Die komplette Verzeichniskette wird jetzt vor jeder Mutation geprüft.
3. Cache-JSON wurde ohne Größenlimit eingelesen und vollständig traversiert.
   Lesen ist auf **8 MiB** begrenzt; größere Dateien bleiben unverändert.

Verifikation: **12 `tests/test_install_cinnamon_applet.py`-Tests** grün,
`py_compile`, Ruff und `git diff --check` grün.

## Runde 106: Profil-Migrationsgrenzen

Fokus: `src/codex_usage/profile_migration.py`, Plan-/Pfadvalidierung vor
Auth-Migration und Rollback.

Vier reproduzierbare Eingabefehler gefunden und behoben:

1. Absolute Pfade akzeptierten `Path`-Subklassen mit überschreibbaren
   Methoden; nur native `Path`-Objekte passieren `_require_absolute()` und
   Migrationspläne.
2. Account-IDs und explizite Auth-Quellen akzeptierten `str`-Subklassen;
   Hook-Ausführung ist jetzt ausgeschlossen.
3. Migrations-ID und Status-/Reason-Felder konnten überschreibbare
   String-Hooks enthalten; Planvalidierung verlangt Builtin-Strings.
4. `datetime`-Subklassen konnten Planvalidierung übersteuern; nur native
   `datetime`-Werte werden akzeptiert.

Verifikation: **100 `tests/test_profile_migration.py`-Tests** grün,
`py_compile`, Ruff und `git diff --check` grün.

## Runde 107: Account-Lock

Fokus: `src/codex_usage/account_lock.py`, per-Account-Lock und
Verzeichnis-FD-Grenzen.

Fünf reproduzierbare Fehler gefunden und behoben:

1. Reservierte globale ID `__all_accounts__` konnte einen echten Account-Lock
   anlegen; sie wird jetzt wie andere ungültige IDs abgewiesen.
2. Überlange IDs liefen erst durch Regex-Prüfung; Längenlimit greift vor
   Regex und hält CPU-/Heap-Aufwand begrenzt.
3. Das Lock-Verzeichnis wurde nach dem Öffnen nicht nochmals auf private
   Modusbits geprüft; FD-Stat verlangt jetzt Eigentümer und 0700-Semantik.
4. `EINTR` bei `flock()`/`sleep()` wurde nicht retrybar behandelt; beide
   Pfade laufen jetzt bis Deadline weiter.
5. Ein Fehler bei `close(fd)` konnte Body-/Lock-Fehler maskieren; Cleanup
   bleibt best-effort und erhält den Primärfehler.

Verifikation: **30 `tests/test_account_lock.py`-Tests** grün, `py_compile`,
Ruff und `git diff --check` grün.

## Runde 108: Integration-Entrypoint

Fokus: `src/codex_usage/integration_entrypoint.py`, strikt validierte
Snapshot-Argumente, Runtime-Pfade, Lock-/Attestation-Reihenfolge und
begrenzte History-Kosten.

Kein neuer reproduzierbarer Fehler gefunden. Ungültige Argumente und
Zeitwerte bleiben fail-closed, Attestation läuft vor und nach Datenaufnahme,
Producer-Lock schützt Veröffentlichung, History-Fenster sind begrenzt und
Fehler werden in stabile Exit-Codes übersetzt.

Verifikation: **38 `tests/test_integration_entrypoint.py`-Tests** grün (eine
erwartete `runpy`-Warnung), `py_compile`, Ruff und `git diff --check` grün.

## Runde 109: Integration-Snapshot-Grenzen und Konsistenz

Fokus: `src/codex_usage/integration_snapshot.py`, Current-Reader,
Schema-Kanonisierung und Cache-Pfadgrenzen.

Sechs reproduzierbare Fehler gefunden und behoben:

1. `.`/`..` und der interne Sentinel `__all_accounts__` wurden bei direkter
   Dokumentprojektion und kanonischer Eingabe als Account-ID akzeptiert. Diese
   IDs werden jetzt an allen Snapshot-Grenzen abgewiesen.
2. `Path`-Subklassen konnten vor der Validierung `is_absolute()` ausführen und
   ungefangene Hooks auslösen. Snapshot-Einstiege verlangen jetzt native
   `Path`-Objekte.
3. Directory-Identitäten enthielten nur Device, Inode und Modus. Neue oder
   entfernte Current-Dateien blieben dadurch trotz laufender Aufnahme
   unbemerkt. Änderungszeiten sind jetzt Teil der Identität.
4. Gleich große In-Place-Änderungen einer Current-Datei blieben unbemerkt.
   Datei-Änderungszeiten werden vor/nach dem Laden mitgeprüft.
5. Account-IDs als `str`-Subklasse konnten beim Set-Hash ungefangene Hooks
   auslösen. Projektion verlangt jetzt Builtin-`str`.
6. Kanonische Snapshots konnten Limitwerte mit `partial`, `error`,
   `login_required` oder `unknown` kombinieren. Solche widersprüchlichen
   Dokumente werden jetzt abgewiesen.

Verifikation: **89 `tests/test_integration_snapshot.py`-Tests** und **372
gemeinsame Tests** für Integration-Entrypoint/State grün (eine erwartete
`runpy`-Warnung), Mypy für `integration_snapshot.py`, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 110: State-Account-ID-Namespace

Fokus: `src/codex_usage/state.py`, Snapshot-/Current-State-Einstiege und
globale Lock-Sentinel-ID.

Ein reproduzierbarer Fehler gefunden und behoben: `_validate_snapshot_account_id()`
ließ `__all_accounts__` zu, obwohl diese ID ausschließlich für globale
Koordination reserviert ist. Dadurch konnten State-Speicherpfade den Sentinel
als scheinbaren Account anlegen. Alle State-Einstiege weisen die ID jetzt
konsistent ab; der separate globale Lock darf den Sentinel weiterhin nutzen.

Verifikation: **335 `tests/test_state.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 111: Config-Pfadgrenzen und globaler Lock-Sentinel

Fokus: `src/codex_usage/config.py`, Konfigurationspfad-/Account-IDs und
Aufrufer des globalen Account-Locks.

Drei reproduzierbare Fehler bzw. Regressionen gefunden und behoben:

1. `load_config()`/`save_config()` akzeptierten `Path`-Subklassen und konnten
   deren überschreibbare `parent`-/Pfadmethoden vor der Validierung ausführen.
   Nur native `Path`-Objekte passieren jetzt `_select_config_path()`.
2. Config-Account-IDs akzeptierten `str`-Subklassen; ein eigener Hash-Hook
   konnte die ID-Prüfung ungefangen abbrechen. Die Prüfung verlangt jetzt
   Builtin-`str`.
3. `__all_accounts__` ist interner globaler Lock-Sentinel und wird von Config,
   Scheduler, CLI, Bridge und Profile-Login absichtlich verwendet. Eine
   pauschale Ablehnung in `account_lock()` blockierte diese Aufrufer und 52
   Config-Tests. Sentinel bleibt als Lockziel erlaubt; echte Account-IDs
   werden weiterhin durch Config-/State-Validatoren ausgeschlossen.

Verifikation: **1081 kombinierte Config/Lock/Scheduler/Profile-Login/CLI/
Bridge-Tests** grün (eine erwartete `runpy`-Warnung), `py_compile`, Ruff und
`git diff --check` grün.

## Runde 112: Usage-Limit-Zeitbasis

Fokus: `src/codex_usage/usage_limits.py`, WHAM-/App-Server-Payloads und
relative Resetzeitberechnung.

Ein reproduzierbarer Fehler gefunden und behoben: Die Pool-Parser akzeptierten
naive `captured_at`-Zeitstempel. Relative Resetzeiten wurden dadurch mit der
lokalen Zeitzone statt mit einer expliziten Zeitbasis berechnet. Parser
verwerfen naive Capture-Zeit jetzt; defekte Provider-Tzinfo bleibt wie zuvor
kontrolliert auf fehlende Resetzeit begrenzt.

Verifikation: **147 `tests/test_usage_limits.py`-Tests** grün, `py_compile`,
Ruff und `git diff --check` grün.

## Runde 113: Health-Pfad- und Account-Grenzen

Fokus: `src/codex_usage/health.py`, Health-Event-Redaction und private
Persistenzpfade.

Zwei reproduzierbare Fehler gefunden und behoben:

1. `record_health_event()`/`load_health()`/`clear_health()` akzeptierten
   `Path`-Subklassen. Überschreibbare Pfad-Properties konnten dadurch vor
   der privaten I/O-Validierung ausgeführt werden. Nur native `Path`-Objekte
   passieren jetzt `_health_path()`.
2. Account-IDs als `str`-Subklasse konnten bei der Truthiness-Prüfung einen
   ungefangenen Hook auslösen. Nur Builtin-`str` wird jetzt als persistierbare
   Account-ID akzeptiert; Subklassen werden redigiert.

Verifikation: **51 `tests/test_health.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 114: Spark-Health-Eingabegrenzen

Fokus: `src/codex_usage/spark_health.py`, Spark-Zustand, Zeitstempel und
private Persistenzpfade.

Drei reproduzierbare Fehler gefunden und behoben:

1. `spark_health_status()`/`set_spark_health()` akzeptierten `Path`-Subklassen;
   Pfad-Hooks liefen vor privater I/O-Validierung. Nur native `Path`-Objekte
   passieren jetzt `_spark_health_path()`.
2. Reason-Strings als `str`-Subklasse konnten beim Begrenzen auf 120 Zeichen
   ungefangen in `__getitem__` laufen. Nur Builtin-`str` wird als Reason
   verarbeitet.
3. Eine `datetime`-Subklasse mit fehlerhafter `tzinfo`-Property konnte die
   Health-Abfrage vor dem kontrollierten Invalid-Clock-Ergebnis abbrechen.
   `tzinfo`-Zugriff ist jetzt vollständig geschützt.

Verifikation: **39 `tests/test_spark_health.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 115: History-Iterator und Zeitbereichsgrenzen

Fokus: `src/codex_usage/history.py`, SQLite-History-Einstiege sowie
Materialisierung von Usage-Samples.

Sieben reproduzierbare Fehler gefunden und behoben:

1. `datetime.tzinfo`-Properties von Subklassen konnten `_is_aware()` ungefangen
   abbrechen. Zugriff läuft jetzt im Schutzblock.
2. Fenstername-Normalisierung konnte bei fehlerhaftem `str`-Subclass außerhalb
   des Iterator-Schutzes werfen. Namen werden kontrolliert normalisiert.
3. `AccountUsage.account_id` als `str`-Subclass konnte Truthiness-Hooks
   auslösen. Nur Builtin-`str` wird weiterverarbeitet.
4. `UsagePool.windows` als Tuple-Subclass konnte beim Iterieren ungefangen
   werfen. Nur native Tupel werden iteriert; Propertyzugriff ist geschützt.
5. Fehlerhafte `AccountUsage.credits`-Properties konnten die Sample-Erzeugung
   abbrechen. Der Credit-Zweig fällt jetzt kontrolliert aus.
6. `record_many()` und `record_usage_samples_batch()` akzeptierten Tuple-
   Subklassen und iterierten deren überschreibbare Methoden. Batch-Einstiege
   verlangen native Tupel.
7. Verbrauchs-Zeitbereiche verglichen direkt `datetime`-Objekte. Vergleiche
   laufen jetzt über normalisierte Millisekunden und sind gegen Vergleichs-
   Subklassen geschützt.

Verifikation: **139 `tests/test_history.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 116: Routing-Fail-Closed-Grenzen

Fokus: `src/codex_usage/routing.py`, Policy-Persistenz, Routing-Zeitbasis und
Pool-/Fensterzustände.

Zwölf reproduzierbare Fehler gefunden und behoben:

1. Policy-Einstiege akzeptierten `Path`-Subklassen; Pfad-Hooks liefen vor
   Validierung. Policy-Pfade verlangen native `Path`-Objekte.
2. `datetime.tzinfo`-Subclass-Properties konnten `_aware_datetime()` abbrechen;
   der Zugriff ist geschützt.
3. Policy-Identifier als `str`-Subclass konnten bis zu Hash-/Dict-Operationen
   gelangen. Nur Builtin-`str` wird akzeptiert.
4. Fenstername-Subclasses konnten Identity-Normalisierung abbrechen. Identity
   prüft native Strings und fällt bei Hooks auf unbekannt.
5. Fehlerhafte `reset_at`-Properties werden im Reset-Guard jetzt als ungültig
   behandelt.
6. Pool-Flag-Properties werden geschützt gelesen; Fehler ergeben ungültigen
   Poolzustand.
7. `availability_sources`-Tuple-Subclasses konnten Evidenzprüfungen beim
   Iterieren abbrechen. Nur native Tupel werden geprüft.
8. Backend-Identities und `backend_used` als String-Subclasses konnten
   Truthiness-/String-Hooks auslösen; solche Werte werden abgewiesen.
9. Policy-Scope-Subclasses konnten `.strip()` ungefangen ausführen; Scope-
   Eingaben verlangen Builtin-`str`.
10. Routing-`role` und `policy_source` hatten dieselbe Lücke; beide Grenzen
    sind jetzt strikt.
11. Fenster-Tuple-Subclasses konnten `_main_state()` und `_pool_usage_state()`
    beim Iterieren abbrechen; beide verlangen native Tupel.

Verifikation: **178 `tests/test_routing.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 117: Verbrauchs- und Delta-Berechnung

Fokus: `src/codex_usage/consumption.py`, Zeitbasis, Eingabevalidierung und
Reset-/Sample-Berechnung.

Sieben reproduzierbare Fehler gefunden und behoben:

1. `datetime.tzinfo`-Subclass-Properties konnten `_is_aware()` abbrechen;
   Zugriff ist geschützt.
2. Unit-Strings als `str`-Subclass konnten beim Dictionary-Lookup Hash-Hooks
   auslösen. Nur Builtin-`str` wird akzeptiert.
3. Smoothing-Strings als Subclass konnten `.startswith()` ungefangen ausführen;
   Smoothing-Eingaben sind jetzt strikt.
4. Sample-Zeitstempel konnten beim Sortiervergleich Fremd-Hooks auslösen;
   Ordnung und Sortierung liefern kontrolliert `samples are invalid`.
5. Weitere Zeitvergleiche bei Baseline-/Observation-Auswahl sind geschützt und
   führen bei fehlerhaften Samples ebenfalls kontrolliert ab.
6. Reset-Erkennung konnte durch fehlerhafte datetime-Vergleiche abbrechen;
   unlesbare Resetdaten gelten jetzt als kein bestätigter Reset.
7. Berechnung verarbeitet nur native, validierte Eingabeformen an den neuen
   Unit-/Smoothing-Grenzen.

Verifikation: **61 `tests/test_consumption.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 118: Backend-Identity-Payloads

Fokus: `src/codex_usage/identity.py`, Candidate-/Payload-Auswahl und
Backend-Identitätskonsistenz.

Vier reproduzierbare Fehler gefunden und behoben:

1. Payload-`dict`-Subklassen konnten `.get()` in Identity-/Plan-Type-Helfern
   ungefangen ausführen. Nur native `dict`-Payloads werden verarbeitet.
2. Payload-`dict`-Subklassen konnten in Candidate-Priorisierung über
   `__contains__` ausbrechen. Priorisierung ignoriert solche Payloads.
3. `JsonCandidate`-Subklassen konnten URL-Properties vor Usability-Validierung
   ausführen. Nur native `JsonCandidate`-Objekte passieren den Filter.
4. Vorherige strikte Feldvalidierung bleibt an allen Identitäts-/Plan-Type-
   Grenzen wirksam; keine Subclass-Werte gelangen in Gruppen-/Set-Operationen.

Verifikation: **49 `tests/test_identity.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 119: Usage-Reset-Konsistenz

Fokus: `src/codex_usage/usage_resets.py`, kanonische und Legacy-Resetquellen.

Ein reproduzierbarer Konsistenzfehler gefunden und behoben: Ein partielles
Top-Level-`available` wurde neben vollständigem `usage_resets`-Nested-State
ignoriert. Abweichende Werte werden jetzt als unbekannter/conflicting State
behandelt; gleiche Werte bleiben kompatibel.

Verifikation: **30 `tests/test_usage_resets.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 120: Usage-Modell-Invarianten

Fokus: `src/codex_usage/models.py`, Fenster-/Pool-Identität und JSON-
Serialisierung.

Sechs reproduzierbare Fehler gefunden und behoben:

1. Fenstername-Subclasses konnten `has_known_identity` über `.strip()`
   abbrechen; nur Builtin-`str` gilt als Identity-Name.
2. `_safe_text()` akzeptierte String-Subclasses und konnte deren Hooks in der
   Serialisierung ausführen. Subclasses werden redigiert.
3. `_window_to_dict()` konnte fehlerhafte Fenster-Properties ungefangen in
   `AccountUsage.as_dict()` durchreichen. Fehlerhafte Fenster werden ausgelassen.
4. `_window_identity_key()` behandelte Identity-Property-Fehler nicht direkt
   fail-closed; jetzt liefert er `None`.
5. `LimitWindow.has_invalid_usage_value` konnte fehlerhafte Wert-Properties
   abbrechen; Wertfehler ergeben jetzt sicher `True`/keinen Restwert.
6. `AccountUsage.model_pool()` akzeptierte Modellnamen-Subclasses; diese werden
   vor `.strip()` abgewiesen.

Verifikation: **82 `tests/test_models.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 121: Render-Fail-Closed-Anzeige

Fokus: `src/codex_usage/render.py`, Window-Werte, Resettexte und Prozentanzeige.

Drei reproduzierbare Fehler gefunden und behoben: Defekte
`has_invalid_usage_value`-Properties konnten `_usage_value()`,
`_remaining_percent()` und `_is_remaining_percent_window()` abbrechen.
Fehlerhafte `reset_at`-Properties konnten `_reset_value()` ebenfalls abbrechen.
Alle vier Anzeigewege liefern jetzt kontrolliert `-`/`None` statt die GUI-
Renderkette zu unterbrechen.

Verifikation: **88 `tests/test_render.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 122: Browser-Profil- und Diagnosepfade

Fokus: `src/codex_usage/browser.py`, Browser-Profil, Auth-Override sowie
Diagnose-/Probe-Ausgabepfade.

Drei reproduzierbare Fehler gefunden und behoben:

1. `_prepare_profile()` akzeptierte String-Subclasses für Profilpfad und
   Browser; Truthiness-/Hash-Hooks konnten vor Validierung abbrechen.
2. `diagnose_account()` akzeptierte `Path`-Subklassen als Auth-Override und
   validierte sie erst nach Profilvorbereitung. Nur native `Path`-Objekte
   passieren jetzt die Grenze.
3. Probe- und Screenshot-Ausgabe akzeptierten Path-Subklassen bis in private
   I/O. Die Ausgabehelfer weisen sie vor Verzeichnis-/Browseraktionen ab.

Verifikation: **181 `tests/test_browser_profile.py`-Tests** grün, `py_compile`,
Ruff und `git diff --check` grün.

## Runde 123: App-Server-RPC und Prozessgrenzen

Fokus: `src/codex_usage/app_server.py`, Auth-/Command-Pfade, JSON-RPC-
Validierung, Fenster-Mapping sowie Prozess-/Reader-Cleanup.

Mehrere reproduzierbare Randfehler gefunden und behoben:

1. Auth- und Codex-Pfade akzeptierten `str`-Subclasses; Equality-/Strip-
   Hooks konnten den Abruf ungefangen abbrechen. Nur native Strings passieren
   diese Grenzen.
2. CODEX_HOME akzeptierte `Path`-Subclasses und fehlerhafte Path-Properties;
   Pfade werden jetzt strikt geprüft und Fehler als Auth-Fehler gemeldet.
3. RPC-Ergebnisse, Model-Listen, Fenster-Snapshots und Fehlermappings
   akzeptierten Mapping-/Listen-/String-Subclasses; alle Parsergrenzen sind
   jetzt native Typen und fail-closed.
4. Nicht serialisierbare Requests, nicht-bytes Queue-Einträge und fehlerhafte
   RPC-Fehlertexte konnten ungefangene Exceptions erzeugen; sie werden als
   kontrollierte Protocol-Fehler behandelt.
5. Naive/ungültige Refresh-Zeitstempel werden nicht mehr direkt subtrahiert.
6. Stream-, PID-, Process- und Reader-Hooks konnten Cleanup/Threads abbrechen;
   Cleanup bleibt jetzt best-effort, Reader melden kontrollierte Fehler.
7. Fehlerhafte Exception-String-Konvertierung kann Fehlermeldungen nicht mehr
   selbst zum Absturz bringen.

Verifikation: **175 `tests/test_app_server.py`-Tests** und **884 abhängige
Tests** grün, `py_compile`, Ruff und `git diff --check` grün.

## Runde 124: Extractor-Parser-Grenzen

Fokus: `src/codex_usage/extractor.py`, JSON-/Text-Parser, Zeitstempel,
Mapping-Walk und HTML-Fortschrittswerte.

Mehrere reproduzierbare Randfehler gefunden und behoben:

1. URL-, Text- und Label-Strings als Subclasses konnten `strip()`, `replace()`
   oder `casefold()` ungefangen ausführen. Parser akzeptieren an diesen
   Grenzen nur native Strings.
2. JSON-/Mapping-Subclasses konnten `.get()`/`.items()` in WHAM-, Generic-
   Window- und Walk-/Flatten-Pfaden abbrechen. Fremde Mapping-Typen werden
   jetzt verworfen.
3. `datetime`-Subclasses konnten `tzinfo`-Zugriffe und Datumsparser
   unterbrechen. Ungültige Zeitwerte liefern kontrolliert keinen Reset.
4. Nicht serialisierbare Objekte konnten `_json_preview()` über ihre
   String-Konvertierung abbrechen; Vorschau fällt auf den Typnamen zurück.
5. Fehlerhafte Candidate-/Iterable-/Listenformen sowie Parser-Hilfs-Mappings
   werden vor Iteration/Traversal fail-closed geprüft.

Verifikation: **216 `tests/test_extractor.py`-Tests** und **868 abhängige
Tests** grün, `py_compile`, Ruff und `git diff --check` grün.

## Runde 125: Private-I/O-Grenzen

Fokus: `src/codex_usage/private_io.py`, Pfadvalidierung, private
Verzeichnisse/Dateien und Lock-Erwerb.

Vier reproduzierbare Fehler gefunden und behoben:

1. Interne Directory-/Parent-/Chmod-/Fsync-Helfer akzeptierten
   `Path`-Subclasses bis in `lstat()`/I/O; sie weisen fremde Pfade jetzt vor
   Methodenaufruf ab.
2. `ensure_private_directory()` akzeptierte `list`-Subclasses für
   `created_paths`; Append-Hooks konnten Verzeichnisanlage abbrechen.
3. `write_private_text()` wertete beliebige `replace_existing`-Objekte aus;
   nur echte Booleans werden akzeptiert.
4. Ein unterbrochener `fcntl.flock()`-Aufruf (`EINTR`) wurde nicht wiederholt;
   Lock-Erwerb setzt jetzt kontrolliert fort.

Verifikation: **101 `tests/test_private_io.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 126: Scheduler-Zustandsgrenzen

Fokus: `src/codex_usage/scheduler.py`, Account-/Backend-Auflösung,
Window-/Pool-Properties und Watchdog-Blockzustand.

Zehn reproduzierbare Randfehler gefunden und behoben:

1. Account-Subclasses konnten `_bounded_account_list()` über `id`-Hooks
   abbrechen; nur native Account-Datensätze werden akzeptiert.
2. Auth-Pfad-Subclasses konnten in Shared-Auth-Erkennung Truthiness-/Path-
   Hooks auslösen; fremde Strings werden als ungültige Quelle behandelt.
3. `_serial_fetch_required()` entscheidet bei fehlerhaften Backend-Properties
   konservativ für serielle Ausführung.
4. Window-Namen, Window-Werte und `reset_at`-Properties sind fail-closed.
5. Pool-Usage-/Fenster-Properties werden in Core-/Watchdog-Helfern geschützt.
6. Watchdog-Blockzustand meldet fehlerhafte Resetdaten als unbekannten Reset
   statt die Scheduler-Schleife abzubrechen.
7. Backend-Ermittlung liefert bei fehlerhaften Account-Properties `None`.
8. Main-Pool-/Usage-Properties und Name-Properties werden vor Zugriffen
   kontrolliert gelesen.

Verifikation: **287 `tests/test_scheduler.py`-Tests** grün, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 127: Bridge-Validatoren und Sanitizing

Fokus: `src/codex_usage/bridge.py`, Response-Metadaten, Endpoint-/Token-
Validierung und Debug-Sanitizing.

Mehrere reproduzierbare Randfehler gefunden und behoben:

1. Response-Quellen und Source-Prioritäten akzeptierten String-Subclasses;
   Hash-/Strip-/Casefold-Hooks konnten die Bridge abbrechen.
2. Content-Type-, Account-Ref-, Endpoint- und Bridge-Token-Validatoren
   verlangen jetzt native Strings.
3. Mapping-Subclasses konnten API-Response-, Length- und Flag-Sanitizing über
   `get`/`contains`/Index-Hooks abbrechen; solche Mappings werden verworfen.
4. `_safe_context_value()` fängt fehlerhafte String-Konvertierung und liefert
   den Typnamen als sichere Kurzdiagnose.
5. Debug-Text-/Terminal-Control- und Zahlenpfade bleiben bei Fremdtypen
   fail-closed.

Verifikation: **301 `tests/test_bridge.py`-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 128: Systemd-Service-Eingangsgrenzen

Fokus: `src/codex_usage/service.py`, Pfad-/String-Helfer und Prozess-Cleanup.
Vier reproduzierbare Fehler gefunden und behoben:

1. Config-Pfad- und Symlink-Prüfungen konnten `Path`-Subclasses mit eigenen
   Methoden ausführen; nur native Pfade passieren diese Grenzen.
2. Home-Pfadauflösung konnte `RuntimeError`/Typfehler aus ungeeigneten Pfaden
   ungefangen durchreichen; die Validierung meldet jetzt kontrolliert einen
   unbrauchbaren Auth-Pfad.
3. Systemd-Unit-Quoting und Exit-Code-Normalisierung akzeptierten String-
   Subclasses; fehlerhafte Werte werden verworfen, ohne deren Hooks aufzurufen.
4. PID-, `kill()`- und `wait()`-Hooks konnten Prozess-Cleanup abbrechen;
   Cleanup bleibt best-effort und fängt fremde Prozessfehler.

Verifikation: **107 `tests/test_service.py`-Tests** und **318 abhängige
CLI-/Entrypoint-/Profil-/History-Tests** grün, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 129: Integrations-Installer-Prozessgrenzen

Fokus: `src/codex_usage/integration_installer.py`, native Pfad-/String-
Grenzen, Builder-Selector und Subprozess-Cleanup.

Sechs reproduzierbare Randfehler gefunden und behoben:

1. Absolute Pfade, Symlink-Prüfungen und Shell-Quoting akzeptierten fremde
   `Path`-/`str`-Subclasses mit auslösbaren Methoden; Eingaben sind jetzt
   native Typen und Fehler werden kontrolliert abgewiesen.
2. Builder-Preflight brach bei `InterruptedError` aus `selector.select()` ab;
   transiente Signale werden wie beim Read-Pfad wiederholt.
3. Preflight- und Wheel-Builder-Cleanup konnte bei fehlerhaftem `poll()` den
   Primärfehler verdecken; der Prozess wird jetzt konservativ weiter bereinigt.
4. PID-/Kill-/Wait-Hooks in Preflight-Cleanup bleiben best-effort.
5. Prozessgruppen-Cleanup behandelt auch nicht-lookupbedingte OS-Fehler als
   bereits nicht mehr sicher terminierbar.

Verifikation: **227 `tests/test_integration_installer.py`-Tests** grün
(112 s), `py_compile`, Ruff und `git diff --check` grün.

## Runde 130: Integrations-Attestation-Eingangsgrenzen

Fokus: `src/codex_usage/integration_attestation.py`, Manifest-/Release-
Prüfung und private Datei-Leser.

Acht reproduzierbare Randfehler gefunden und behoben:

1. Absolute Manifestpfade akzeptierten `str`-Subclasses mit überschreibbaren
   String-Operatoren.
2. Private Pfad-Leser und Containment-Prüfung akzeptierten `Path`-Subclasses
   bis in `lstat()`/`relative_to()`.
3. Manifest-Mappings akzeptierten `dict`-Subclasses mit fehlerhaften
   `.get()`-Hooks.
4. Der erwartete Entrypoint akzeptierte `Path`-Subclasses vor dem
   Identitätsvergleich.

Alle diese Grenzen weisen fremde Typen jetzt kontrolliert zurück; native
Manifest-/Dateiwerte bleiben unverändert.

Verifikation: **228 `tests/test_integration_installer.py`-Tests** grün
(120 s), davon acht gezielte Regressionen, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 131: Cinnamon-Leisten-Relayout nach Reload

Fokus: `files/codex-usage@H234598/applet.js`, `_setPanelMarkup()`.

Nach Cinnamon-Reload wurde Panel-Markup direkt über
`clutter_text.set_markup()` gesetzt, aber kein Relayout angefordert. Dadurch
blieben Farbe/Markup bis zum nächsten Hover-Paint unsichtbar. Cinnamon nutzt
für dynamisches Markup denselben expliziten `queue_relayout()`-Schritt.

Fix: Nach erfolgreichem Panel-Markup-Setzen wird `queue_relayout()` verwendet,
falls Cinnamon diese API anbietet. Fehlende API bleibt kompatibel.

Verifikation: RED-Test reproduzierte fehlenden Relayout, danach **608
`tests/applet_runtime.test.js`-Tests** grün, `node --check`, `git diff --check`,
Installation und Cinnamon-Reload (`reload=ok`) grün.

## Runde 132: Leisten-/Klickmenü-Lifecycle und Consumption-Queue

Fokus: `files/codex-usage@H234598/applet.js`, Panel-/Menü-Markup und
asynchrone Hilfsanfragen.

Sechs reproduzierbare Fehler gefunden und behoben:

1. `_setItemMarkup()` setzte Menü-Markup ohne `queue_relayout()`. Farben und
   Formatierungen konnten dadurch ebenfalls erst nach Hover sichtbar werden.
2. `_cancelAuxProcess()` löste eine laufende Consumption-Anfrage nicht aus
   `_consumptionCurrent`. Nach einer konkurrierenden Hilfsanfrage blieb die
   Queue blockiert; die Anfrage wird jetzt generationstreu zurückgestellt.
3. Credits und Creditverbrauch wurden im Account-Klickmenü als Plaintext
   eingefügt. Ihre jeweiligen Formatierungszeilen werden jetzt angewendet.
4. Der technische Status wurde im Klickmenü als Plaintext eingefügt. Status-
   Formatierung wird jetzt auch dort angewendet.
5. Ein fehlender CLI-/Config-Pfad verlor die bereits aus der Consumption-Queue
   gezogene Anfrage. Sie wird jetzt vorne wieder eingereiht, statt still zu
   verschwinden.
Die direkten RED-Tests decken Panel, Menü, Status und Queue ab; vorhandene
Lifecycle-Guards bleiben unverändert.

Verifikation: **610 `tests/applet_runtime.test.js`-Tests** grün, gezielte RED-
Tests vor jedem Fix reproduziert, `node --check`, `git diff --check`, Installer-
Abgleich und Cinnamon-Reload (`reload=ok`) grün.

## Runde 133: Aux-Lifecycle-Serialisierung

Fokus: `files/codex-usage@H234598/applet.js`, konkurrierende Hintergrund-
Hilfsprozesse und Generation-/Queue-Verlust.

Vier reproduzierbare Fehler gefunden und behoben:

1. Ein laufender Device-/Health-/Consumption-Aux-Prozess konnte durch
   `account overview` ersetzt werden. Der Overview-Callback wurde dadurch
   generationstreu verworfen; Account-/Profilstatus blieb veraltet.
2. `service status` konnte denselben laufenden Aux-Prozess abbrechen. Status-
   Callback und anschließender Overview-Resume gingen verloren.
3. Account-Overview startete Routing-Status und Profiljob-Erkennung direkt
   nacheinander. Der zweite Spawn beendete den ersten Routing-Request; Routing-
   Einstellungen wurden nicht aktualisiert.
4. Routing-Status und Profiljob-Erkennung konnten auch durch nachfolgende
   User-Aux-Kommandos ohne Retry verworfen werden. Beide Requests werden jetzt
   als Pending markiert und nach Aux-Abschluss seriell erneut gestartet.

Fix: Pending-Flags serialisieren Service, Overview, Routing und Profiljobs;
`_drainDeferredAuxRequests()` startet sie in fester Reihenfolge. `_auxCommand`
klassifiziert Routing-Status; `_cancelAuxProcess()` stellt abgebrochene
Hintergrundanfragen wieder an. Safe-Mode- und Removal-Cleanup löschen Pending-
Zustand.

Verifikation: RED-Tests vor jedem Fix reproduziert, **619
`tests/applet_runtime.test.js`-Tests** grün, `node --check` grün.

## Runde 134: Routing-Policy-Write-Lifecycle

Fokus: `files/codex-usage@H234598/applet.js`, Routing-Policy-Schreibkette,
Auxiliary-Serialisierung und konkurrierende Settings-Signale.

Vier reproduzierbare Verluste gefunden und behoben:

1. Ein laufender `policy set`-/`policy set-limits`-Prozess wurde durch einen
   fremden Aux-Aufruf (Health, Service, Profil oder Status) abgebrochen. Der
   Routing-Write hatte keine eigene Klassifikation; sein Callback verfiel
   generationstreu, während die Policy-Änderung unvollständig blieb.
2. Eine Settings-Änderung während einer mehrteiligen Routing-Policy-Anwendung
   wurde wegen `_routingPolicyApplying` einfach verworfen.
3. Eine Routing-Änderung während Account-/Backend-Schreibvorgängen konnte den
   seriellen Write-Zyklus umgehen oder dessen Reihenfolge stören.
4. Nach erfolgreichem oder fehlgeschlagenem Teil-Write wurde ein gemerkter
   neuer Wunsch nicht zuverlässig nach dem autoritativen Status-Reload erneut
   angewendet.

Fix: Routing-Writes werden als `routing-write` klassifiziert. Fremde Aux-
Anfragen, Account-Writes und Backend-Writes warten in den vorhandenen Queues;
der Routing-Write selbst bleibt ununterbrochen. `_routingSettingsPendingDesired`
merkt den zuletzt normalisierten Settings-Wunsch. Nach dem Status-Reload wird
dieser Wunsch erneut durch die normale Policy-/Limit-Kette geführt. Abbruch,
Safe-Mode und Applet-Entfernung löschen oder übergeben den Pending-Zustand
kontrolliert; kein Callback darf eine fremde Generation zurückschreiben.

Verifikation: sechs neue RED-Regressionstests, **626
`tests/applet_runtime.test.js`-Tests** grün, `node --check` und
`git diff --check` grün.

## Runde 135: Schutz kritischer Auxiliary-Prozesse

Fokus: `files/codex-usage@H234598/applet.js`, `_spawnAuxJson()` und die
Lebenszyklen von Service, Device-Login und Profiljobs.

Sechs reproduzierbare Abbruchpfade gefunden und behoben:

1. `service enable` verlor seine Continuation, wenn Health/Manage/Terminal
   einen zweiten Aux-Befehl startete.
2. Ein laufender Device-Login wurde abgebrochen; `_cancelAuxProcess()` löschte
   Live-Code, URL und aktiven Login ohne Retry.
3. `service status` war nicht als laufender Command klassifiziert und konnte
   Status-/Polling-Übergänge verlieren.
4. `profile cancel` wurde abgebrochen und danach nur ein Statuspoll requeued;
   Account-Löschung konnte dadurch dauerhaft auf den Profiljob warten.
5. `profile create` lief nach dem Account-Write ohne Account-Queue-Guard; ein
   Abbruch ließ `_profilePendingAccounts` hängen und verlor den Jobstart.
6. Routing-Settings konnten während dieser kritischen Prozesse direkt einen
   Policy-Write beginnen und den laufenden Prozess verdrängen.

Fix: `_spawnAuxJson()` klassifiziert `service-status` und `profile-create`.
Kritische Commands (`service-enable`, `service-status`, `device-login`,
`profile-create`, `profile-job-cancel`, `routing-write`) blockieren fremde
Aux-Aufrufe in der vorhandenen, begrenzten Queue. Routing-Settings werden in
dieser Phase als letzter normalisierter Wunsch vorgemerkt und nach Abschluss
seriell gestartet. Bestehende Poll-/Cancel- und Safe-Mode-Generationen bleiben
intakt.

Zusätzlich direkte Tests für `_startProfileCreation()` ergänzt (Erfolg und
Fehlerbereinigung).

Verifikation: **122 fokussierte Lifecycle-Tests** grün, inklusive RED-Tests
vor jedem Schutzpfad; anschließend **635 `tests/applet_runtime.test.js`-Tests**
im Volltest, `node --check` grün.

## Runde 136: Leistenwerte eindeutig benannt

Die sichtbaren Leistenwerte heißen jetzt durchgängig `Restzeit …` und
`Resetdatum …`. Geändert wurden Quelllabels im Applet, die Auswahltexte des
Panel-Editors, alle vier Schema-Dropdowns, die Formatierungszielnamen sowie
Tooltip-, Klickmenü- und Restzeitpräfixe. Numerische Quell-IDs und interne
Parser-/Persistenzschlüssel bleiben unverändert; bestehende Konfigurationen
bleiben dadurch kompatibel.

Verifikation: **636 `tests/applet_runtime.test.js`-Tests** und **209 gezielte
Python-Tests** grün, `node --check`, JSON-Parse und `git diff --check` grün;
Quellen installiert und Cinnamon-Applet erfolgreich neu geladen.

## Runde 137: Tokendelta-Namen und zentrale Formatprofile

Fokus: alle Formatierungstabellen unter `Formatierungen`, inklusive der vielen
Leisten-Kopien, Help-Materialisierung und Credit-Schwellen.

Änderungen:

1. `Delta 5h`, `Delta Woche`, `Delta 30 Tage`, `Delta Spark` und `Delta
   sonstiges` heißen jetzt durchgängig `Tokendelta ...`. Interne Quell-IDs
   bleiben unverändert. `Tokendelta` ohne Fenster bleibt der konfigurierbare
   Account-Verbrauchsrückblick; `Tokendelta Woche` ist das feste Wochenfenster.
2. Formatdefinitionen werden in `format_table_selector.py` zentral als Profile
   materialisiert. Dadurch erhalten Label, Status-/Identitätswerte, Resetdatum,
   Resetzeit, Restzeit und Tokenende nur Account, Hover/Klick, Nullausblendung
   und passende Formatfelder. `Formatierungsmodus`, `Schwelle`, alle
   `Über der Schwelle ...`- und `Unter der Schwelle ...`-Felder verschwinden in
   diesen Tabellen. Tokenende erbt jetzt korrekt das Restzeit-Format inklusive
   Format-Auswahl.
3. Zahlenprofile behalten ihre Schwellensteuerung. Credits vergleichen jetzt
   verbleibende absolute Credits; die GUI-Einheit lautet `c` (Default 20 c).
   Creditverbrauch vergleicht verbrauchte Credit-Prozentpunkte; die Einheit
   lautet `Credit-%`. Tokendelta nutzt `Tokendelta %`, Resets `Resets` mit
   Default 0. Dynamisch bleibt nur für Token- und Credit-Deltas.
4. Die Help-Seite verwendet dieselbe Materialisierung wie die GUI und erklärt
   Einheiten sowie schwellenlose Formate ohne widersprüchliche
   `oberhalb/unterhalb`-Texte. Kopieren/Einfügen bleibt pro Tabelle möglich.

RED-Tests wurden vor der Implementierung für Tokendelta-Labels, schwellenlose
Profile und Credit-Einheiten ergänzt; veraltete Erwartungen anschließend auf die
neue Semantik aktualisiert.

Verifikation: **638 `tests/applet_runtime.test.js`-Tests** und **159 gezielte
Python-Tests** grün; `node --check`, JSON-Parse und `git diff --check` grün.
Applet-Installation und Cinnamon-Reload erfolgreich (`reload=ok`); installierte
Applet-Dateien entsprechen den Quellen.

## Runde 138: Formatlisten gegen beschädigte Eingaben und Schreibfehler

Fokus: `files/codex-usage@H234598/format_table_selector.py`, Laden,
Bearbeiten, Kopieren/Einfügen sowie Entfernen und Verschieben von
Formatzeilen.

Zehn reproduzierbare Fehler gefunden und behoben:

1. Eine nicht-hashbare Spalten-ID (z. B. Liste) ließ Profilmaterialisierung
   mit `TypeError` abbrechen.
2. Persistierte Werte wurden nach Typ, Bereich und Optionen nicht vollständig
   geprüft; ungültige Integer-, Float- und Boolean-Werte konnten in das GTK-
   Modell gelangen.
3. `NaN` und unendliche Floatwerte wurden akzeptiert.
4. NUL-Zeichen in persistierten Texten wurden nicht verworfen.
5. Doppelte Accountzeilen blieben in der Editorliste sichtbar.
6. Eine nicht-hashbare Combo-Auswahl konnte den Tabellenwechsel abbrechen.
7. Entfernen schrieb bei Persistenzfehler weiter einen veränderten UI-Zustand.
8. Verschieben nach oben und nach unten hatte denselben Fehler.
9. Einfügen konnte bei Persistenzfehlern eine Formatänderung im Modell lassen.
10. Ein Laufzeitfehler beim Einfügen konnte die ursprüngliche Zelle nicht
    zuverlässig wiederherstellen.

Fix: `_valid_column_value()` validiert primitive Typen, Optionen, Grenzen,
endliche Zahlen und Text ohne NUL. Accountzeilen werden dedupliziert.
Profil- und Combo-IDs werden vor Set-/Dict-Zugriffen typgesichert. Alle
mutierenden Listenaktionen nehmen Snapshots; bei fehlgeschlagenem Settings-
Write wird das Modell atomar auf den vorherigen Zustand zurückgesetzt.

Verifikation: zehn neue RED-Regressionstests vor Fix reproduziert; danach
**125 `tests/test_format_table_selector.py`-Tests** und **349 fokussierte
Python-Tests** (`format_table_selector`, `help_page`, `applet`,
`panel_settings_list`) grün.

## Runde 139: Leisten-Editor vor Modell- und Persistenzfehlern schützen

Fokus: `files/codex-usage@H234598/panel_settings_list.py`, Laden der
Leistenzeilen und alle mutierenden Aktionen des Editors.

Zwölf reproduzierbare Fehler gefunden und behoben:

1. Integerwerte außerhalb ihrer Spaltengrenzen wurden geladen.
2. Falsche Integer-Typen wurden in das Modell übernommen.
3. `NaN` und unendliche Floatwerte wurden geladen.
4. Falsche Boolean-Typen wurden geladen.
5. NUL-haltige Strings konnten GTK erreichen.
6. Persistierte Werte wurden in der Baum-Neuerzeugung anders validiert als
   beim normalen Reload.
7. Add ließ nach einem fehlgeschlagenen Write eine neue Zeile sichtbar.
8. Edit ließ nach einem fehlgeschlagenen Write geänderte Zellen sichtbar.
9. Remove ließ eine entfernte Zeile nach Write-Fehler verschwinden.
10. Move-up und Move-down ließen Reihenfolgeänderungen nach Write-Fehlern
    sichtbar.
11. Paste ließ kopierte Werte nach Write-Fehler sichtbar.
12. Ein Settings-Lesefehler während einer Mutation ließ den UI-Zustand
    ebenfalls verändert.

Fix: `_panel_column_value_valid()` prüft primitive Typen, endliche Zahlen,
Grenzen, Optionen und GTK-sichere Texte. Reload und Tree-Rebuild verwenden
denselben Prüfer. Modell-Snapshots werden vor Add/Edit/Remove/Move/Paste und
Checkbox-Toggle genommen; bei Read-, Serialisierungs- oder Write-Fehlern wird
der vorherige Zustand wiederhergestellt. Modelle ohne echten Iterator werden
vor Snapshot-Versuchen sicher abgewiesen, damit fehlerhafte `__getitem__`-
Sequenzen nicht endlos laufen.

Verifikation: **14 neue fokussierte RED-Tests** (inklusive parametrisierter
Typ- und Richtungsfälle) vor Fix reproduziert; danach **194
`tests/test_panel_settings_list.py`-Tests** grün.

## Runde 140: Pool-/Serien-Editor fehlertolerant halten

Fokus: `files/codex-usage@H234598/dynamic_series_list.py`, Reload,
Masterjet-Abfrage und Serienauswahl. Serien bleiben fachlich unverändert;
dieser Fix betrifft nur Fehlerisolierung.

Zehn reproduzierbare Abbruchpfade gefunden und behoben:

1. Ein Fehler beim Leeren des GTK-Modells brach Reload ab.
2. Ein beliebiger Settings-Lesefehler (z. B. `RuntimeError`) brach Reload ab.
3. Fehler beim Anhängen einzelner Zeilen wurden nicht vollständig isoliert.
4. Ein Fehler bei `columns_autosize()` brach Reload ab.
5. Fehler beim Iterieren des Modells brachen Persistenz-Callbacks ab.
6. Derselbe Fehlerpfad brach die Ermittlung aktiver Serienbesitzer ab.
7. Ein Fehler des Masterjet-Providers brach das Add/Edit-Menü statt leerer
   Auswahl ab.
8. Ein Stream ohne nutzbares `fileno()` wurde nicht als Providerfehler
   behandelt.
9. Nicht-dict-Spalten führten bei der Pflichtspaltensuche zu `AttributeError`.
10. Fehler beim Lesen einer bestehenden Serienzuordnung brachen das Menü ab.

Fix: Reload, Modellserialisierung, Besitzer-/Optionssuche und Providerzugriff
fangen jetzt unerwartete Laufzeitfehler lokal ab. Provider- und Modellfehler
fallen geschlossen auf leere Daten/Auswahl zurück. Pflichtspaltensuche ignoriert
beschädigte Spalteneinträge; Dialog-Schema wird bei nicht kopierbaren Metadaten
nicht mutiert.

Verifikation: **10 neue RED-Regressionstests** vor Fix reproduziert; danach
**37 `tests/test_dynamic_series_list.py`-Tests** grün.

## Runde 141: Formatprofil-Matrix vollständig und materialisiererfest

Fokus: alle Tabellenziele unter `Formatierungen`, einschließlich Label,
Tokenende, OpenAI-Resetwerten, Rest-/Resetfenstern, Tokendelta-Fenstern,
Credits und Creditverbrauch.

Die bestehende zentrale Materialisierung bleibt maßgeblich: schwellenlose
Text-, Datums- und Restzeitprofile enthalten nur Account, Hover/Klick,
Nullausblendung und ihre gültigen Formatfelder. Prozent-, Credit-, Reset- und
Tokendelta-Profile behalten nur dort Schwelle/Alternativformat, wo der
Laufzeitwert diese Semantik besitzt. Credits vergleichen absolute verbleibende
Credits (`c`); Creditverbrauch vergleicht verbrauchte Prozentpunkte des
Credit-Limits (`Credit-%`). Alle Fenster-Tokendeltas heißen in der Oberfläche
`Tokendelta …`.

Zusätzliche Fehlerbehebung: `_materialize_format_definition()` weist nicht-
stringige Tabellen-IDs jetzt geschlossen ab. Materialisierte Spalten werden
dedupliziert; eine beschädigte geerbte Definition kann keine zweite
`Dynamisch`-Spalte in GUI oder Hilfe erzeugen.

Verifikation: die Matrix deckt sämtliche 29 schwellenlosen Tabellenziele ab;
**130 `tests/test_format_table_selector.py`-Tests** und **174 Tests** im
kombinierten Format-/Help-/Applet-Lauf, JSON-/JavaScript-Syntaxprüfung,
`git diff --check` und Cinnamon-Reload grün.

## Runde 142: Codex-Home-Normalisierung und Auth-Rollback

Fokus: `src/codex_usage/config.py`, TOML-Normalisierung für Test-Codex-Homes
und die dabei benutzten lokalen Auth-Dateipfade.

Elf reproduzierbare Fehler gefunden und behoben:

1. Punktierte `tui`-Schlüssel erzeugten beim nachträglichen `[tui]`-Block
   ungültiges TOML.
2. Eine vorhandene Root-Zuweisung `tui = ...` wurde ebenfalls mit `[tui]`
   kollidierend ergänzt; der Normalizer weist diese nicht erweiterbare Form
   jetzt explizit ab.
3. Unicode-escapte `tui`-Tabellen wurden nicht als vorhandener Block erkannt.
4. Unicode-escapte `[[tui]]`-Arraytabellen umgingen die Schutzprüfung.
5. Unicode-escaptes `animations` im `[tui]`-Block erzeugte einen doppelten
   Schlüssel.
6. Unicode-escapte punktierte Animationsschlüssel und
   `tui`-Nebenschlüssel erzeugten doppelte Tabellen.
7. Dasselbe Problem trat beim Unicode-escapten
   `cli_auth_credentials_store` auf.
8. Test-Home-Auth konnte über einen Symlink im Ziel-Elternpfad in einen
   fremden Ordner verschoben werden.
9. Auth-Rollback sicherte einen bereits vorhandenen Quell-Elternpfad nicht
   wieder auf Modus `0700`.
10. Ungültige IPv6-/Host-Syntax ließ `_validate_analytics_url()` eine rohe
    Parserfehlermeldung statt des Config-Fehlers nach außen geben.
11. Eine verschachtelte Arrayzeile wie `["x"]` wurde als Tabellenkopf
    fehlinterpretiert und konnte die Root-Credential-Zuweisung mitten in ein
    Array schreiben.

Fix: TOML-Schlüssel- und Tabellenpfade werden semantisch über `tomllib`
erkannt, einschließlich quoted keys und Unicode-Escapes. Ein Struktur-Scanner
unterscheidet Tabellenköpfe von verschachtelten Multiline-Arrays. Punktierte
Root-Schlüssel bleiben Root-Schlüssel; nicht erweiterbare Root-`tui`-Inlinewerte
werden abgewiesen. Auth-Zielpfade dürfen keine Symlink-Ancestoren enthalten;
Rollback-Eltern werden wieder privat gesetzt. URL-Parserfehler werden in den
stabilen Validierungsfehler übersetzt.

Verifikation: **12 RED-Regressionstests** vor Fix reproduziert; danach **247
`tests/test_config.py`-Tests**, **357 fokussierte Tests** mit
`test_profile_layout.py` und den passenden CLI-Fällen,
TOML-Semantik-Fuzzlauf ohne unerwartete Ausgaben,
`py_compile` und `git diff --check` grün.

## Runde 143: Browser-Diagnose robust und privat

Fokus: `src/codex_usage/browser.py`, Response-Erfassung, Seitenstatus und
private Diagnose-/Probeausgaben.

Dreizehn reproduzierbare Fehler gefunden und behoben:

1. `None` als JSON-Response-Header ließ `.get()` abstürzen.
2. Ein nicht-stringiger `content-type` ließ `.lower()` abstürzen.
3. Ein nicht-stringiger Response-Body ließ `.encode()` abstürzen.
4. Diagnostik-Responses mit fehlendem Header-Mapping brachen ebenfalls ab.
5. Ein ungültiger Diagnostik-Response entfernte vorzeitig den ältesten gültigen
   Eintrag aus dem begrenzten Response-Fenster.
6. Naive Auth-Zeitstempel wurden implizit in Systemzeitzone statt in
   `LOCAL_TZ` interpretiert.
7. Eine fremde URL mit eingebettetem Analytics-Pfad wurde als Cloudflare
   klassifiziert.
8. Ein fremder `/cdn-cgi/challenge-platform/`-Response wurde als echte
   Cloudflare-Challenge klassifiziert.
9. Vorhandene öffentliche Profilmarker wurden akzeptiert.
10. `_validate_private_output_path()` akzeptierte Dateien fremder Besitzer.
11. Dieselbe Prüfung akzeptierte Dateien mit Gruppen-/Weltzugriff.
12. Die Probe-Speichertransaktion begrenzte Kandidaten nur im Capturepfad;
    direkte Transaktionsaufrufe konnten mehr als `JSON_CANDIDATE_MAX_COUNT`
    Dateien erzeugen.
13. Der begrenzte Textwriter meldete Nicht-Strings als rohe `AttributeError`
    statt als validierten Eingabefehler.

Fix: Header und Body werden vor Verarbeitung typgeprüft und fehlerhafte
Browserobjekte geschlossen ignoriert. Diagnose-URLs müssen vertrauenswürdige
HTTPS-Hosts sein; Analytics-403 wird zusätzlich auf exakten ChatGPT-Pfad
begrenzt. Naive Zeiten erhalten explizit `LOCAL_TZ`. Bestehende Ausgabedateien
werden auf aktuellen Besitzer und Modus ohne Gruppen-/Weltbits geprüft;
Screenshots werden vor der zweiten Prüfung auf `0600` gehärtet. Probe-
Transaktionen erzwingen das Kandidatenlimit und Texttypen.

Verifikation: **13 RED-Regressionstests** vor Fix reproduziert; danach **253
Browser-Diagnose-/Profiltests**, **377 angrenzende Profiltests**, 99%-Branch-
Coverage für `browser.py`, `py_compile` und `git diff --check` grün.

## Runde 144: Private-I/O-Grenzen und unterbrochene Systemaufrufe

Fokus: `src/codex_usage/private_io.py`, interne Verzeichnis-/Fsync-Helfer und
Lock-/Atomicschreibpfade.

Zehn reproduzierbare Fehler gefunden und behoben:

1. `_require_private_directory()` akzeptierte Verzeichnisse hinter einem
   Symlink-Ancestor.
2. `_require_private_parent()` tat dasselbe.
3. `_chmod_private_directory()` konnte dadurch ein fremdes Ziel chmodden.
4. `_fsync_directory()` konnte dadurch ein fremdes Ziel fsyncen.
5. Ohne `O_DIRECTORY` wurden reguläre Dateien als Fsync-Ziel akzeptiert.
6. Fremde Besitzer wurden beim direkten Directory-Fsync nicht geprüft.
7. Ein `ENOTDIR` beim Directory-Fsync wurde als rohes `OSError` statt als
   validierter Pfadfehler weitergereicht.
8. Unterbrochenes `fsync()` brach Directory-Fsync ab.
9. Unterbrochenes `fsync()` brach atomisches Schreiben ab.
10. Unterbrochenes `sleep()` im Lock-Retry brach Lock-Erwerb ab.

Fix: Alle internen Directory-Helfer prüfen jetzt Symlink-Ancestors. Fsync
öffnet und validiert Typ sowie Besitzer per Descriptor; fehlende optionale
`O_DIRECTORY`-Flags bleiben sicher. `fsync()` und Lock-Wartepausen wiederholen
`InterruptedError`; `ENOTDIR` wird stabil als `ValueError` gemeldet.

Verifikation: **10 RED-Regressionstests** vor Fix reproduziert; danach **111
`tests/test_private_io.py`-Tests**, **1057 direkte Nutzerpfadtests**, 99%-
Branch-Coverage, `py_compile` und `git diff --check` grün.

## Runde 145: History-Sidecars und gespeicherte Datenintegrität

Fokus: `src/codex_usage/history.py`, SQLite-Sidecars, Timestamp-Grenzen und
Consumption-Fensterprojektion.

Zehn Root-Findings gefunden und behoben:

1. Sidecar-Dateien fremder Besitzer wurden vor `fchmod()` nicht abgewiesen.
2. `_chmod_private_regular()` konnte über Symlink-Ancestors fremde Dateien
   erreichen.
3. Fehlendes `O_NOFOLLOW` ließ einen finalen Sidecar-Symlink folgen.
4. Unterbrochenes Sidecar-`fchmod()` brach Persistenz ab.
5. `_prepare_path()` akzeptierte fremde bestehende History-Dateien bis zur
   späteren Descriptorprüfung.
6. `_to_millis()` interpretierte naive Datetimes implizit in Systemzeitzone.
7. `consumption_window_seconds()` gab korrupt gespeicherte Fensterdauer `0`
   oder über dem Maximum zurück.
8. Dasselbe Fensterlisting akzeptierte nichtnumerische SQLite-Werte nicht
   kontrolliert.
9. Pool-Keys konnten Terminal-Steuerzeichen enthalten.
10. Reset-Generationen und Quellen konnten ebenfalls Steuerzeichen enthalten.

Fix: Sidecars prüfen jetzt native Pfade, Ancestors, Besitzer, Regularität und
`O_NOFOLLOW`-Grenzen; `fchmod()` wird bei `InterruptedError` wiederholt.
History-Dateibesitzer wird früh validiert. Timestamp-Konvertierung verlangt
explizit aware Datetimes. Consumption-Listings validieren jede gespeicherte
Fensterdauer. Pool-, Reset- und Source-Metadaten weisen Steuerzeichen zurück.

Verifikation: **12 RED-Testfälle** vor Fix reproduziert; danach **151
`tests/test_history.py`-Tests**, **257 History/Consumption/Integration-Tests**,
**287 Scheduler-Tests**, 98%-Branch-Coverage, `py_compile` und
`git diff --check` grün.

## Runde 146: Bridge-Reaudit ohne neues Root-Finding

Fokus: `src/codex_usage/bridge.py`, HTTP-/TLS-Grenzen, Token-/Debug-Dateien,
Browser-Identität und die erzeugten Extension-Skripte.

Keine neue reproduzierbare Root-Ursache gefunden. Vorhandene Absicherungen
decken Payload-/Response-Limits, strikte Header-/Origin-/Token-Prüfung,
Identitätswechsel, Timestamp-Grenzen, private Transaktionen, TLS-Handshakes
und Browser-Lifecycle bereits ab. Die vier verbleibenden Branch-Lücken sind
defensive, nachgelagerte Typwächter ohne erreichbaren Produktionspfad.

Verifikation: **301 `tests/test_bridge.py`-Tests**, 99%-Branch-Coverage,
`py_compile` und `git diff --check` grün. Themenwechsel auf nächstes Modul.

## Runde 147: Strikter JSON-Parser ohne neues Root-Finding

Fokus: `src/codex_usage/json_utils.py`, Eingabetypen, Nesting-Limit,
Duplicate-Key- und Konstantenprüfung.

Keine neue reproduzierbare Root-Ursache gefunden. `loads_strict()` verlangt
native String-/Byte-Eingaben, scannt Strings korrekt ohne Strukturzeichen zu
verwechseln, begrenzt Verschachtelung vor dem Parser und lehnt doppelte Keys
sowie nicht standardkonforme Konstanten ab. Alle Funktionen sind getestet.

Verifikation: **13 `tests/test_json_utils.py`-Tests**, 100%-Branch-Coverage,
`py_compile` und `git diff --check` grün. Themenwechsel auf nächstes Modul.

## Runde 148: Integration-Snapshot-Reaudit ohne neues Root-Finding

Fokus: `src/codex_usage/integration_snapshot.py`, Current-Reader,
Schema-Kanonisierung, Secret-Scan und atomarer Cache-Publish.

Keine neue reproduzierbare Root-Ursache gefunden. Directory-/Dateiidentität,
Besitzer-/Modus-/Hardlink-/Symlinkprüfungen, Größenlimits, deterministische
Kanonisierung, Secret-Scan und Lock-/Publish-Rollback sind bereits abgesichert.
Die verbleibenden Branch-Lücken sind defensive Property-/Path-Typzweige.

Verifikation: **89 `tests/test_integration_snapshot.py`-Tests**, 99%-Branch-
Coverage, `py_compile` und `git diff --check` grün. Themenwechsel auf nächstes
Modul.

## Runde 149: Integration-Attestation-Reaudit ohne neues Root-Finding

Fokus: `src/codex_usage/integration_attestation.py`, Manifest-/RECORD-
Prüfung, Descriptorgebundene Release-Tree-Hashes und private Dateileser.

Keine neue reproduzierbare Root-Ursache gefunden. Pfad-/Besitzer-/Modus-/
Hardlink-/Symlink-Grenzen, `O_NOFOLLOW`-Descriptorbindung, Größen- und
Eintragslimits, kanonische Hash-/RECORD-Prüfung sowie Release-Tree-Drift sind
bereits fail-closed. Die verbleibenden Branch-Lücken sind erwartete
negative Manifest-/Digest-/Tree-Vergleiche.

Verifikation: **62 Attestation-Tests** (im Installer-Testmodul), 99%-Branch-
Coverage, `py_compile` und `git diff --check` grün. Themenwechsel auf nächstes
Modul.

## Runde 150: Direkter WHAM-/Auth-Pfad-Reaudit ohne neues Root-Finding

Fokus: `src/codex_usage/direct.py`, Auth-Descriptor, JWT-/Identity-Grenzen,
Redirect-/Content-Type-Prüfung, stabile Mehrfachantworten und Credit-Parsing.

Keine neue reproduzierbare Root-Ursache gefunden. Der Modulpfad ist bereits
vollständig branchgetestet; private Auth-Dateien, Token-/Claim-Typen,
Response-Provenienz, Zeitlimits, Reset-/Fensterkonsistenz und Fehlerredaktion
sind abgesichert.

Verifikation: **363 `tests/test_direct.py`-Tests**, 100%-Statement-/Branch-
Coverage, `py_compile` und `git diff --check` grün. Themenwechsel auf nächstes
Modul.

## Runde 151: Health-Event-Store-Reaudit ohne neues Root-Finding

Fokus: `src/codex_usage/health.py`, Event-/Tokenvalidierung,
Retention-/Count-/Byte-Limits und private JSON-I/O.

Keine neue reproduzierbare Root-Ursache gefunden. Health-Datei, Events,
Fehler-/Account-Tokens, Zeitfenster und Lock-/Recovery-Pfade bleiben begrenzt
und fail-closed; verworfene oder korrupte Daten erzeugen keine unbounded
Retention.

Verifikation: **51 `tests/test_health.py`-Tests**, 100%-Branch-Coverage,
`py_compile` und `git diff --check` grün. Themenwechsel auf nächstes Modul.

## Runde 152: Spark-Health-Reaudit ohne neues Root-Finding

Fokus: `src/codex_usage/spark_health.py`, Backend-ID-/Reason-Grenzen,
TTL-/Zeitstempelprüfung und private JSON-Persistenz.

Keine neue reproduzierbare Root-Ursache gefunden. Identifier, Reason,
Zeitstempel, Record-Limits und private Datei-/Lock-Pfade sind fail-closed.
Die zwei fehlenden Zeilen sind ein defensiver, bereits durch die umgebenden
Fehlerpfade abgesicherter Timestamp-Zweig.

Verifikation: **39 `tests/test_spark_health.py`-Tests**, 99%-Branch-Coverage,
`py_compile` und `git diff --check` grün. Themenwechsel auf nächstes Modul.

## Runde 153: Usage-Modelle-Reaudit ohne neues Root-Finding

Fokus: `src/codex_usage/models.py`, Fensteridentität, Restwert-/Prozent-
Normalisierung, Pool-Erschöpfung und JSON-Serialisierung.

Keine neue reproduzierbare Root-Ursache gefunden. Numerische Werte,
Fenster-/Pool-Identitäten, Status-/Cache-Redaktion, Duplikatmodelle und
malformed Container bleiben fail-closed. Die einzige Branch-Lücke betrifft
eine bereits getestete Schleifenrückkehr im Modell-Serializer.

Verifikation: **82 `tests/test_models.py`-Tests**, 99%-Branch-Coverage,
`py_compile` und `git diff --check` grün. Themenwechsel auf nächstes Modul.

## Runde 154: Producer-Entrypoint-Reaudit ohne neues Root-Finding

Fokus: `src/codex_usage/integration_entrypoint.py`, argv-/XDG-Grenzen,
Attestation-Reihenfolge, History-Kosten und Publish-Drift.

Keine neue reproduzierbare Root-Ursache gefunden. Exaktes argv-Schema,
absolute Runtime-Pfade, Lock-/Attestation-Reihenfolge, aware UTC-Zeit,
History-Limits und stabile Exit-Codes sind bereits abgesichert.

Verifikation: **38 `tests/test_integration_entrypoint.py`-Tests**, 100%-Branch-
Coverage, `py_compile` und `git diff --check` grün. Themenwechsel auf nächstes
Modul.

## Runde 155: Profil-Migrations-Reaudit ohne neues Root-Finding

Fokus: `src/codex_usage/profile_migration.py`, Plan-/Manifestvalidierung,
Auth-Quellen, Profilziele und Rollback-Identität.

Keine neue reproduzierbare Root-Ursache gefunden. Quellen und Ziele sind
absolut, privat, symlinkfrei und disjunkt; Migration prüft Digest-/Descriptor-
Identität und räumt nur eigene Dateien/Verzeichnisse auf. Rollback behandelt
geänderte oder fremde Ziele fail-closed.

Verifikation: **100 `tests/test_profile_migration.py`-Tests**, 98%-Branch-
Coverage, `py_compile` und `git diff --check` grün. Themenwechsel auf nächstes
Modul.

## Runde 156: Formatierungs-Selector und untrusted Schema-/Settings-Grenzen

Fokus: `files/codex-usage@H234598/format_table_selector.py`, Formatierungs-
Profile, kopierte Tabellen, Spalten-/Optionsvalidierung und gespeicherte
Settings.

Zehn reproduzierbare Fehler gefunden und behoben:

1. Ein übergroßer gespeicherter Integer löste bei Float-Prüfung
   `OverflowError` aus.
2. Ein `str`-Subclass konnte den UTF-8-Encode-Schritt mit einer unerwarteten
   Exception abbrechen.
3. Ein manipuliertes Spalten-Mapping konnte den `.get()`-Zugriff abbrechen.
4. Ein Options-Mapping konnte beim `.values()`-Zugriff abbrechen.
5. Ein Schlüssel-Subclass konnte die Profilauflösung beim Hashing abbrechen.
6. Eine fehlerhafte Deepcopy einer Spalte konnte die Tabellenmaterialisierung
   abbrechen.
7. Eine fehlerhafte Deepcopy einer Formatdefinition konnte den Selector
   abbrechen.
8. Ein fehlerhaftes Schema-Mapping konnte die Auflösung von
   `format-copy-of` abbrechen.
9. Ein fehlerhaftes Options-`.items()` konnte den gebundenen List-Widget-
   Aufbau abbrechen.
10. Ein fehlerhaftes Selector-Info-Mapping konnte den GUI-Aufbau abbrechen.

Fix: Text-/Spaltenwerte akzeptieren nur native sichere Typen; Float-, Options-,
Deepcopy- und Mapping-Grenzen fail-closed. Fehlerhafte Spalten werden
übersprungen, fehlerhafte Definitionen als leere Tabellen weitergeführt. Der
Selector behandelt unlesbare Tabellen-Metadaten als leer.

Verifikation: **10 RED-Regressionstests** vor Fix reproduziert; danach **140
`tests/test_format_table_selector.py`-Tests**, 84%-Branch-Coverage,
`py_compile` und `git diff --check` grün. Themenwechsel auf nächstes Modul.

## Runde 157: Prognosen-Selector und untrusted Tabellen-Metadaten

Fokus: `files/codex-usage@H234598/forecast_table_selector.py`, Tabellen-
Auswahl, Settings-Reload und Widget-Rollback.

Zehn reproduzierbare Fehler gefunden und behoben:

1. Ein fehlerhaftes Info-Mapping konnte den Konstruktor beim Tabellenzugriff
   abbrechen.
2. Ein fehlerhaftes Definitions-Mapping konnte die Schlüsselsuche abbrechen.
3. Ein fehlerhaftes Definitions-Item konnte die Definitionsermittlung abbrechen.
4. Ein fehlerhaftes Tabellen-Mapping konnte den Tabellenaufbau abbrechen.
5. Eine fehlerhafte Tabellen-Iteration konnte den gesamten Selector abbrechen.
6. Ein String-Subclass mit fehlerhaftem Hash konnte einen Tabellenwechsel
   abbrechen.
7. Derselbe Hash-Hook konnte einen Settings-Reload abbrechen.
8. Ein unhashbarer Key konnte `_ensure_table()` abbrechen.
9. Ein unhashbarer Key konnte `_discard_table()` abbrechen.
10. Ein unhashbarer aktiver Key konnte den Visibility-Rollback abbrechen.

Fix: Selector-Metadaten werden nur aus nativen JSON-Dicts/Listen übernommen;
fehlerhafte Mappings/Iterationen ergeben eine leere Auswahl. Tabellen-Keys
werden als native Strings validiert. Aufbau, Umschalten und Rollback bleiben
bei unhashbaren oder korrupten Keys fail-closed.

Verifikation: **10 RED-Regressionstests** vor Fix reproduziert; danach **37
`tests/test_forecast_table_selector.py`-Tests**, 87%-Branch-Coverage,
`py_compile` und `git diff --check` grün. Themenwechsel auf nächstes Modul.

## Runde 158: Gesammelte Hilfe und Schema-/Markup-Normalisierung

Fokus: `files/codex-usage@H234598/help_page.py`, schema-getriebene Hilfetexte,
Formatierungs-Kopien und GTK-Markup.

Zehn reproduzierbare Fehler gefunden und behoben:

1. Ein `str`-Subclass konnte `_clean_text()` beim Splitten abbrechen.
2. Ein `str`-Subclass konnte `_help_text()` beim Zeilen-Splitten abbrechen.
3. Ein Options-Mapping konnte `_option_text()` beim `.items()`-Aufruf abbrechen.
4. Eine fehlerhafte Optionsliste konnte `_option_text()` bei der Iteration
   abbrechen.
5. Ein fehlerhaftes Feld-Mapping konnte `_field_text()` abbrechen.
6. Ein fehlerhaftes Definitions-Mapping konnte `_definition_entry()` abbrechen.
7. Ein fehlerhaftes Tabellen-Definitions-Mapping konnte `_iter_table_keys()`
   abbrechen.
8. Ein fehlerhaftes Schema-Mapping konnte `build_help_groups()` abbrechen.
9. Ein Objekt mit fehlerhaftem `__str__()` konnte `_markup()` abbrechen.
10. Dasselbe Schema-Mapping konnte den Aufbau der GTK-Hilfe-Seite abbrechen.

Fix: Hilfetext- und Optionsnormalisierung akzeptieren nur native JSON-Typen
und behandeln fehlerhafte Iterationen leer. Feld-/Definitionszugriffe fallen
auf generische Hilfe zurück. Markup-Stringifizierung ist fail-closed; eine
korrupt gelieferte Schema-Struktur lässt die Seite mit Intro-Text weiterlaufen.

Verifikation: **10 RED-Regressionstests** vor Fix reproduziert; danach **25
`tests/test_help_page.py`-Tests**, 93%-Branch-Coverage, `py_compile` und
`git diff --check` grün. Themenwechsel auf nächstes Modul.

## Runde 159: Dynamische Serienliste und Provider-/Editor-Grenzen

Fokus: `files/codex-usage@H234598/dynamic_series_list.py`, Masterjet-Serien-
Abruf, gespeicherte Account-Zeilen und der dynamische Editor.

Zehn reproduzierbare Fehler gefunden und behoben:

1. Ein fehlerhaftes Info-Mapping konnte den Widget-Konstruktor abbrechen.
2. Eine fehlerhafte gespeicherte Zeilen-Iteration konnte den Reload abbrechen.
3. Ein fehlerhaftes Zeilen-Mapping konnte den Reload abbrechen.
4. Ein fehlerhaftes Spalten-Mapping konnte den Reload abbrechen.
5. Ein fehlerhaftes Spalten-Mapping konnte `_column_index()` abbrechen.
6. Ein fehlerhaftes Masterjet-Payload-Mapping konnte den Serienabruf abbrechen.
7. Ein fehlerhaftes Serien-Item-Mapping konnte denselben Abruf abbrechen.
8. Ein String-Subclass mit fehlerhaftem `strip()` konnte aktive Besitzerprüfung
   abbrechen.
9. Ein fehlerhaftes Provider-Iterable konnte die Serienauswahl abbrechen.
10. Ein fehlerhaftes Editor-Spaltenschema konnte den Add/Edit-Dialog abbrechen.

Fix: Info-Mappings werden in native Dicts kopiert; Reloads akzeptieren nur
native JSON-Listen/Dicts und überspringen korrupte Zeilen/Spalten. Provider-
Payloads, Serienwerte und Owner-Strings werden typgesichert. Fehlerhafte
Optionsiterationen und Editor-Schemata fallen leer zurück, ohne das originale
Schema zu verlieren.

Verifikation: **10 RED-Regressionstests** vor Fix reproduziert; danach **47
`tests/test_dynamic_series_list.py`-Tests**, 88%-Branch-Coverage,
`py_compile` und `git diff --check` grün. Themenwechsel auf nächstes Modul.

## Runde 160: Leisten-Editor und Wert-/Spaltenvalidierung

Fokus: `files/codex-usage@H234598/panel_settings_list.py`, Wertfeld-Migration,
Copy/Paste-Helfer, GTK-Spaltenpositionen und gespeicherte Leistenwerte.

Zehn reproduzierbare Fehler gefunden und behoben:

1. Ein String-Subclass konnte die GTK-Textprüfung beim Encode abbrechen.
2. Ein String-Subclass konnte `panel_value_count()` beim Strippen abbrechen.
3. Ein String-Subclass konnte `panel_edit_columns()` beim Strippen abbrechen.
4. Ein String-Subclass konnte `_panel_slot_id()` beim Präfixcheck abbrechen.
5. Ein fehlerhaftes Row-`.items()` konnte die Slot-Migration abbrechen.
6. Ein fehlerhaftes Column-`.get()` konnte Wertkopie abbrechen.
7. Eine fehlerhafte Row-Deepcopy konnte Wertpaste abbrechen.
8. Ein übergroßer Integer konnte Float-Wertvalidierung mit `OverflowError`
   abbrechen.
9. Ein fehlerhaftes Options-`.values()` konnte Wertvalidierung abbrechen.
10. Ein fehlerhaftes Spalten-`.get()` konnte Editorpositionen abbrechen.

Fix: Zahlen-/Textparser akzeptieren nur native sichere Typen. Slot-Migration,
Wertkopie/-paste und Spaltenpositionen fallen bei korrupten Mapping-/Deepcopy-
Grenzen fail-closed zurück. Float-Overflow und fehlerhafte Optionscontainer
werden als ungültige Werte verworfen.

Verifikation: **10 RED-Regressionstests** vor Fix reproduziert; danach **204
`tests/test_panel_settings_list.py`-Tests**, 85%-Branch-Coverage,
`py_compile` und `git diff --check` grün. Themenwechsel auf nächstes Modul.

## Runde 161: Applet-Leistenquellen und Rendering-Grenzen

Fokus: `files/codex-usage@H234598/applet.js`, Leistenquellen, Wertanzahl,
Fenster-/Resetauflösung, Zeilennormalisierung sowie Slot-Rendering.

Zehn reproduzierbare Fehler gefunden und behoben:

1. Ein fehlerhafter Property-Key konnte `_panelSourceValue()` durch
   `Symbol.toPrimitive` abbrechen.
2. Derselbe Coercion-Hook konnte `_panelSourceLabel()` abbrechen.
3. Ein Objekt mit fehlerhaftem `valueOf()` konnte `_panelValueCount()` beim
   `Number()`-Aufruf abbrechen.
4. Ein fehlerhafter Quellenwert konnte `_panelValueForSource()` bei einem
   numerischen Vergleich abbrechen.
5. Derselbe Quellenwert konnte `_panelWindowForSource()` abbrechen.
6. Ein fehlerhafter Fenster-Key konnte die Objektauflösung in
   `_panelWindowForKey()` abbrechen.
7. Ein fehlerhafter `order`-Getter konnte die Leistenzeilennormalisierung
   abbrechen.
8. Ein fehlerhafter Slot-Getter konnte dieselbe Normalisierung abbrechen.
9. Ein fehlerhafter `source`-Getter konnte formatiertes Slot-Rendering
   abbrechen.
10. Ein fehlerhafter `usage`-Getter konnte rohes Slot-Rendering abbrechen.

Fix: Quellen und Schlüssel werden vor Lookup/Comparison auf primitive,
erwartete Typen begrenzt. Wertanzahl, Zeilennormalisierung und Slot-Rendering
fallen bei fehlerhaften Gettern/Coercions geschlossen auf Default, `null` oder
eine unveränderte Darstellung zurück. Gültige native JSON-Werte bleiben
unverändert.

Verifikation: **10 RED-Regressionstests** vor Fix reproduziert; danach **648
`tests/applet_runtime.test.js`-Tests** grün, `node --check` und
`git diff --check` grün. Themenwechsel auf nächstes Modul.

## Runde 162: History-Extraktion und AccountUsage-Feldgrenzen

Fokus: `src/codex_usage/history.py`, `_iter_usage_samples()` sowie die
Übergabe von AccountUsage-Daten in Consumption-/History-Persistenz.

Zehn reproduzierbare Fehler gefunden:

1. Ein fehlerhafter `status`-Getter konnte die Sample-Extraktion abbrechen.
2. Ein fehlerhafter `stale`-Getter konnte sie abbrechen.
3. Ein fehlerhafter `cache_invalidated`-Getter konnte sie abbrechen.
4. Ein fehlerhafter `account_id`-Getter konnte sie abbrechen.
5. `account_id` wurde mehrfach gelesen; ein zustandsabhängiger Getter konnte
   beim zweiten Zugriff abbrechen.
6. Ein fehlerhafter `values_captured_at`-Getter konnte sie abbrechen.
7. Ein fehlerhafter Fallback-`captured_at`-Getter konnte sie abbrechen.
8. Ein fehlerhafter `backend_used`-Getter konnte sie abbrechen.
9. `backend_used` wurde doppelt gelesen; ein zustandsabhängiger Getter konnte
   beim zweiten Zugriff abbrechen.
10. Ein fehlerhafter `main`-Getter konnte die Pool-Extraktion abbrechen.

Fix: Kritische AccountUsage-Felder werden einmalig innerhalb einer lokalen
Fehlergrenze gelesen. Status-/Stale-/Invalidierungsprüfung, Identität,
Zeitstempel, Backendquelle und Main-Pool fallen bei Getter-/Bool-Fehlern
geschlossen auf keine Samples zurück. Gültige native AccountUsage-Objekte und
der bestehende `values_captured_at`-Fallback bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**161 `tests/test_history.py`-Tests** sowie **222 History-/Consumption-Tests**
grün, 98%-Branch-Coverage, `py_compile` und `git diff --check` grün.

## Runde 163: Scheduler-Snapshot-/Watchdog-Fehlergrenzen

Fokus: `src/codex_usage/scheduler.py`, Snapshot- und Current-State-Lesen im
Poll-/Watchdog-Pfad, Account-Isolation sowie Fehlerdarstellung.

Zehn reproduzierbare Fehler gefunden und behoben:

1. Ein `OSError` beim Snapshot-Lesen ließ `fetch_all()` statt des frischen
   Ergebnisses abbrechen.
2. Ein anderer unerwarteter Snapshot-Lesefehler hatte denselben Effekt.
3. Ein fehlerhafter Snapshot eines Accounts brach einen mehraccountigen,
   seriellen Auth-Poll vor dem gesunden Account ab.
4. Ein `OSError` beim initialen Watchdog-Snapshot-Lesen brach den gesamten
   Watchdog ab.
5. Ein anderer unerwarteter Watchdog-Snapshotfehler hatte denselben Effekt.
6. Ein Snapshot-Lesefehler eines Accounts verhinderte die Watchdog-Abfrage
   aller übrigen Accounts.
7. Ein `OSError` beim Lesen des Current-State ließ die Prüfung eines aktiven
   Block-Snapshots ungefangen abbrechen.
8. Ein anderer Current-State-Lesefehler hatte denselben Effekt.
9. Ein String-Subclass mit fehlerhaftem Hash konnte die Usage-Identitätskarte
   aus `_usage_map_for_accounts()` herauswerfen.
10. Ein fehlerhafter `error`-Getter eines bereits versuchten Usage-Ergebnisses
    konnte `_watch_failure_usages()` abbrechen.

Fix: Snapshot-/Current-State-Lesen ist optionale Cache-Information. Fehler
werden pro Account geloggt und führen zu einem sicheren erneuten Fetch; ein
frisches Ergebnis bleibt erhalten. Watchdog-Identitäts- und Fehleraufbereitung
fangen unerwartete Objekt-Hooks geschlossen ab.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**297 `tests/test_scheduler.py`-Tests**, 99%-Branch-Coverage, **222
History-/Consumption-Tests**, `py_compile` und `git diff --check` grün.

## Runde 164: Scheduler-Stabilisierung und Reset-Merge-Grenzen

Fokus: Resetzeit-Erkennung, vorherige Fensterwerte, Pool-Stabilisierung und
Watchdog-Block-Snapshot-Prüfungen in `src/codex_usage/scheduler.py`.

Zehn reproduzierbare Fehler gefunden und behoben:

1. Ein fehlerhafter `raw`-Getter ließ die relative Reset-Erkennung abbrechen.
2. Ein fehlerhafter `raw`-Getter ließ die Erkennung relativer Metadaten
   abbrechen.
3. Ein fehlerhafter `source`-Getter ließ die absolute Reset-Erkennung
   abbrechen.
4. Ein fehlerhafter `raw`-Getter ließ die Fensterdauer-Auswertung abbrechen.
5. Ein fehlerhafter Fenster-Getter ließ den konservativen Direct-Vergleich
   abbrechen.
6. Ein fehlerhafter `reset_at`-Getter ließ denselben Vergleich abbrechen.
7. Ein fehlerhafter `blocked_until`-Getter ließ Snapshot-Konsistenzprüfung
   abbrechen.
8. Ein fehlerhafter `state_generation`-Getter ließ Block-Generationsprüfung
   abbrechen.
9. Ein unerwarteter Fehler beim Auth-Identitätsabruf ließ Snapshot-Matching
   ausbrechen.
10. Ein fehlerhafter `windows`-Getter ließ Pool-Stabilisierung ausbrechen.

Fix: Reset-/Fenster-Helfer und Stabilisierung fallen bei unbekannten Objekt-
Hooks geschlossen auf `False`, `None` oder den aktuellen Pool zurück. Der
Watchdog verwirft bei unsicherer Block-/Identitätsprüfung den Snapshot, statt
den Pollfluss abzubrechen.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**307 `tests/test_scheduler.py`-Tests**, 99%-Branch-Coverage, **642
Scheduler-/State-Tests**, `py_compile` und `git diff --check` grün.

## Runde 165: Stabilisierung der Pool-Fenstersuche

Fokus: `_stabilize_main_pool()` und die Suche nach vorherigen/aktuellen
Fenstern in `src/codex_usage/scheduler.py`.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1.–4. Vier unterschiedliche Fehler eines `duration_seconds`-Getters im
     vorherigen Pool ließen Merge abbrechen.
5.–8. Vier unterschiedliche Fehler desselben Getters im aktuellen Pool
     ließen Merge abbrechen.
9. Ein fehlerhafter Fenster-`__eq__`-Hook ließ den Poolvergleich abbrechen.
10. Ein Fehler beim `dataclasses.replace()` ließ den Pool-Merge abbrechen.

Fix: Beide Fenstersuchen, Fallback-Auswahl, Poolvergleich und `replace()`
liegen in lokalen Fehlergrenzen. Unsichere Stabilisierung gibt aktuellen Pool
unverändert zurück; gültige Fensterübernahme bleibt unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**317 `tests/test_scheduler.py`-Tests**, 99%-Branch-Coverage, `py_compile`
und `git diff --check` grün.

## Runde 166: Watchdog-Entscheidungs- und Persistenz-Guards

Fokus: Block-Snapshot-Matching, Current-Supersession, Auth-Fetch-Auswahl und
Persistenzentscheidung in `src/codex_usage/scheduler.py`.

Zehn reproduzierbare Fehler gefunden und behoben:

1. Ein fehlerhafter `status`-Getter ließ Current-Supersession abbrechen.
2. Ein fehlerhafter `stale`-Getter hatte denselben Effekt.
3. Ein fehlerhafter Provenance-Guard ließ Block-Snapshot-Matching ausbrechen.
4. Ein fehlerhafter `backend_used`-Getter ließ Matching ausbrechen.
5. Ein fehlerhafter `backend_account_id`-Getter wurde als gültiger Match
   durchgewunken bzw. konnte abbrechen.
6. Ein fehlerhafter Auth-Identitätsvergleich ließ Matching abbrechen.
7. Ein fehlerhafter `backend_user_id`-Getter ließ Matching abbrechen.
8. Ein fehlerhafter Account-Backend-Getter ließ Auth-Fetch-Auswahl abbrechen.
9. Ein fehlerhafter Usage-`status`-Getter ließ Persistenzentscheidung
   abbrechen.
10. Ein fehlerhafter Usage-`backend_used`-Getter hatte denselben Effekt.

Fix: Entscheidungshelfer lesen kritische Felder innerhalb lokaler Fehlergrenzen
und fallen bei Unsicherheit auf `False` zurück; Auth-Fetch-Entscheidung fällt
konservativ auf `True`, Persistenz wird verworfen. Gültige Pfade bleiben gleich.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**327 `tests/test_scheduler.py`-Tests**, 99%-Branch-Coverage, `py_compile`
und `git diff --check` grün.

## Runde 167: App-Server-Fallback-Fehlertext

Fokus: App-Server→Direct-Fallback in `_fetch_one()` sowie sichere Übernahme
des Unavailable-Fehlertexts.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1.–9. Unterschiedliche Exception-Klassen mit fehlerhaftem `__str__()` ließen
     den Direct-Fallback nach App-Server-Ausfall selbst abbrechen.
10. Terminal-Steuerzeichen im Fallbacktext wurden unverändert weitergereicht.

Fix: Fallbackdetails laufen durch `_sanitize_failure_text()`. Fehlerhafte
Stringifizierung fällt auf den Exception-Klassennamen zurück; Steuerzeichen
werden entfernt und Zeilen normalisiert. Direct-Fallback und Provenance bleiben
erhalten.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**337 `tests/test_scheduler.py`-Tests**, 99%-Branch-Coverage, `py_compile`
und `git diff --check` grün.

## Runde 168: Renderer-Fail-Closed-Grenzen

Fokus: `src/codex_usage/render.py`, Tabellen-/Statusdarstellung und
Getter-/Formatter-Fehler im UI-Ausgabepfad.

Zehn reproduzierbare Fehler gefunden und behoben:

1. Ein fehlerhafter Cache-Status-Getter ließ Provenance-Prüfung abbrechen.
2. Ein fehlerhafter Main-Pool-Getter ließ Core-Fensterauflösung abbrechen.
3. Ein fehlerhafter Main-Pool-Getter ließ Zusatzlimitdarstellung abbrechen.
4. Ein fehlerhafter Spark-Pool-Getter ließ Sparkdarstellung abbrechen.
5. Ein fehlerhafter Raw-Getter ließ Usagewertdarstellung abbrechen.
6. Ein fehlerhafter Remaining-Getter ließ Prozentfensterprüfung abbrechen.
7. Ein fehlerhafter Limit-Getter ließ Prozentberechnung abbrechen.
8. Ein fehlerhafter Status-Getter ließ Statuszelle abbrechen.
9. Ein unerwarteter Float-Formatterfehler ließ Zahlenzelle abbrechen.
10. Ein fehlerhafter Text-`__str__()` ließ Zellformatierung abbrechen.

Fix: Renderer-Helfer fallen bei unbekannten Getter-/Formatterfehlern auf
`-`, `False`, `None` oder „nicht verfügbar“ zurück. Gültige Tabellenwerte und
die bisherige Terminalbereinigung bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**98 `tests/test_render.py`-Tests**, 100%-Statement-/Branch-Coverage, **435
Render-/Scheduler-Tests**, `py_compile` und `git diff --check` grün.

## Runde 169: State-Provenance- und Identity-Guards

Fokus: `src/codex_usage/state.py`, Provenance- und Identitätsprüfung vor
Cache-/Snapshot-Wiederverwendung.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter `backend_configured`-Getter ließ konfigurierte Provenance
   nach außen ausbrechen.
2. Ein fehlerhafter `backend_used`-Getter ließ dieselbe Prüfung ausbrechen.
3. `_backend_provenance_fields_valid()` propagierte Getterfehler.
4. `_backend_provenance_is_complete()` propagierte Getterfehler.
5. `_has_backend_fallback_proof()` propagierte Fehler beim Fallbackgrund.
6. `backend_provenance_matches()` propagierte Fehler aus linken Provenance-
   Feldern.
7. `backend_identity_matches()` propagierte Fehler aus linker Backend-
   Identität.
8. `backend_identity_matches()` propagierte Fehler aus rechter Account-ID.
9. `_backend_value_valid()` propagierte fehlerhafte Gleichheitsoperatoren.
10. `_backend_value_valid()` propagierte fehlerhafte Hash-/Membership-
    Operatoren.

Fix: Alle sicherheitsrelevanten State-Provenance-, Identity- und Backend-
Wertprüfungen behandeln unerwartete Getter-/Operatorfehler fail-closed als
`False`. Zusätzliche Regressionstests decken auch die äußeren Guard-
Ausnahmen ab; gültige Provenance- und Fallbackpfade bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**347 `tests/test_state.py`-Tests**, 99%-Branch-Coverage (ein bestehender,
unveränderter Transaktionsfehlerpfad bleibt ungetestet), `py_compile` und
`git diff --check` grün.

## Runde 170: State-Transaktions-Lock-Cleanup

Fokus: `_StateDeleteTransaction` und der frühe Rollbackpfad von
`_remove_account_state_unlocked()`.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Fehler beim Lock-Close ließ Commit-Transaktion offen.
2. Dasselbe passierte nach erfolgreichem Transaktionsverzeichnis-Cleanup.
3. Rollback propagierte Lock-Close-Fehler statt sie zu gruppieren.
4. Ein Rollbackfehler bei der Generation verdeckte den Lock-Close-Fehler.
5. Ein Transaktionsverzeichnis-Cleanupfehler verdeckte den Lock-Close-Fehler.
6. Ein Backup-Restorefehler verdeckte den Lock-Close-Fehler.
7. Früher Pfadfehler beim Target-Check verdeckte den Lock-Close-Fehler.
8. Fehler beim Generationsinkrement verdeckte den Lock-Close-Fehler.
9. Fehler beim Wiederherstellen der Generation verdeckte den Lock-Close-
   Fehler.
10. Fehler beim frühen Transaktions-Cleanup verdeckte den Lock-Close-Fehler.

Fix: Commit markiert Transaktion auch bei fehlgeschlagenem Lock-Close als
geschlossen. Rollback und früher Cleanup fangen Lock-Close-Fehler, gruppieren
sie mit allen bereits aufgetretenen Rollbackfehlern und schließen Zustand
deterministisch ab. Primärfehler bleiben sichtbar.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**357 `tests/test_state.py`-Tests**, 99%-Branch-Coverage (ein bestehender,
unveränderter Transaktionspfad für ungültiges Verzeichnis bleibt ungetestet),
`py_compile` und `git diff --check` grün.

## Runde 171: Usage-Limits-Payload-Grenzen

Fokus: `src/codex_usage/usage_limits.py`, WHAM-/App-Server-Payloads,
Spark-Katalog und Fensteridentitäten.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter Top-Level-`get()` ließ den WHAM-Parser ausbrechen.
2. Ein fehlerhafter verschachtelter Rate-Limit-`get()` tat dasselbe.
3. Ein fehlerhafter App-Server-Top-Level-`get()` ließ den Parser ausbrechen.
4. Ein fehlerhafter `rateLimitsByLimitId`-`get()` ließ den Parser ausbrechen.
5. Ein fehlerhafter `.items()`-Hook im verschachtelten Main-Bucket ließ die
   Payloadverarbeitung ausbrechen.
6. Ein fehlerhafter Spark-Bucket-`get()` ließ den App-Server-Pfad ausbrechen.
7. Ein fehlerhafter Model-Pool-Key ließ `merge_model_catalog()` ausbrechen.
8. Ein fehlerhafter Fenster-Duration-Getter ließ Identitätsprüfung ausbrechen.
9. Ein fehlerhafter String-`casefold()` ließ Spark-Erkennung ausbrechen.
10. Ein fehlerhafter Katalog-Iterator ließ `_unique()` ausbrechen.

Fix: Öffentliche WHAM-/App-Server-Parser fallen bei unerwarteten Mapping-/
Getterfehlern geschlossen auf `(None, ())` zurück. Katalog-, Fenster-
Identitäts-, Normalisierungs- und Unique-Helfer behandeln fehlerhafte
Providerobjekte als ungültig; gültige native JSON-Payloads bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**157 `tests/test_usage_limits.py`-Tests**, **1.032 Usage-Limits-/Direct- /
App-Server-/Scheduler-Tests**, 99%-Branch-Coverage, `py_compile` und
`git diff --check` grün.

## Runde 172: Consumption-/Tokendelta-Feldgrenzen

Fokus: `src/codex_usage/consumption.py`, Sample-Felder, Delta-Arithmetik,
Stale-/Forecast-Berechnung und EMA-Pfad.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter `account_id`-Getter ließ Sampleverarbeitung ausbrechen.
2. Ein fehlerhafter `pool`-Getter ließ Sampleverarbeitung ausbrechen.
3. Ein fehlerhafter `window_seconds`-Getter ließ Sampleverarbeitung
   ausbrechen.
4. Ein fehlerhafter `used_percent`-Getter wurde bei Einzel-Samples nicht
   geprüft.
5. Ein fehlerhafter `captured_at`-Getter ließ Sortierung/Prüfung ausbrechen.
6. Ein fehlerhafter Account-ID-Vergleich ließ Kontentrennung ausbrechen.
7. Ein fehlerhafter Pool-/Fenster-Vergleich ließ Kontentrennung ausbrechen.
8. Ein fehlerhafter `used_percent`-Float-Hook ließ Delta-Arithmetik ausbrechen.
9. Fehlerhafte Stale-Zeitstempel-Arithmetik ließ Verbrauchsberechnung
   ausbrechen.
10. Fehlerhafte Zeitstempel-/Sortier-/Forecast-Arithmetik ließ Schätzung
    ausbrechen.

Fix: Samples werden vor jeder Berechnung auf auslesbare, endliche Nutzungs-
werte geprüft. Account-/Pool-/Fensteridentität, Delta-Schleife und Forecast-
Arithmetik fallen bei Getter-/Operatorfehlern auf `ValueError("samples are
invalid")` zurück. Gültige Tokendelta-, Stale- und EMA-Berechnungen bleiben
unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**75 `tests/test_consumption.py`-Tests**, **236 Consumption-/History-Tests**,
100%-Statement-/Branch-Coverage, `py_compile`, Ruff und `git diff --check`
grün.

## Runde 173: HistoryStore-Sample- und Lifecycle-Grenzen

Fokus: `src/codex_usage/history.py`, Batch-Speicherung, SQLite-Zeilen und
Verbindungs-Lifecycle.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter `account_id`-Getter ließ `record_many()` roh ausbrechen.
2. Ein fehlerhafter `pool`-Getter ließ Batch-Speicherung ausbrechen.
3. Ein fehlerhafter `window_seconds`-Getter ließ Batch-Speicherung ausbrechen.
4. Ein fehlerhafter `captured_at`-Getter ließ Timestamp-Konvertierung ausbrechen.
5. Ein fehlerhafter `used_percent`-Getter ließ Float-Konvertierung ausbrechen.
6. Ein fehlerhafter `reset_at`-Getter wurde doppelt gelesen und konnte ausbrechen.
7. Ein fehlerhafter `reset_generation`-Getter ließ SQLite-Bindung ausbrechen.
8. Ein fehlerhafter `source`-Getter ließ SQLite-Bindung ausbrechen.
9. Ein fehlgeschlagenes `close()` ließ veraltete Verbindung im Store stehen.
10. Ein fehlerhafter SQLite-Row-Getter ließ `_sample_from_row()` roh ausbrechen.

Fix: Batch-Felder werden über einen kleinen Serializer einmalig und
fail-closed gelesen; Getter-/Konvertierungsfehler werden als
`ValueError("samples are invalid")` gemeldet. `close()` trennt Store-Zustand
vor dem eigentlichen Close-Aufruf. Row-Materialisierung normalisiert unerwartete
Fehler als `ValueError("history sample is invalid")`.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**171 `tests/test_history.py`-Tests**, **246 History-/Consumption-Tests**,
98%-Branch-Coverage (bestehende, unveränderte Cleanup-/Fehlerpfade bleiben
ungetestet), `py_compile`, Ruff und `git diff --check` grün.

## Runde 174: Private-IO-Datei- und Lock-Grenzen

Fokus: `src/codex_usage/private_io.py`, Deadline-Arithmetik, Descriptor-
Retries und Cleanup-Reihenfolge.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Fehlerhafte `monotonic()`-Arithmetik ließ `_lock_deadline()` roh ausbrechen.
2. Unterbrochenes `fchmod()` ließ Verzeichnis-Sicherung ausbrechen.
3. Unterbrochenes `fchmod()` ließ temporäre Datei-Sicherung ausbrechen.
4. Unterbrochenes `fchmod()` ließ Lock-Erwerb ausbrechen.
5. Ein Text mit UTF-8-Surrogat ließ `write_private_text()` als
   `UnicodeEncodeError` ausbrechen.
6. Descriptor-Close verdeckte `fchmod()`-Fehler.
7. Descriptor-Close verdeckte `fsync()`-Fehler.
8. Descriptor-Close verdeckte Lese-/`fdopen()`-Fehler.
9. Descriptor-Close verdeckte Fehler aus dem Lock-Kontextkörper.
10. Descriptor-Close verdeckte Schreibfehler.

Fix: Deadline-Berechnung fällt bei Provider-/Operatorfehlern auf den
   standardisierten Timeout-Fehler zurück. Alle drei `fchmod()`-Pfade
   wiederholen `InterruptedError`. Ungültige UTF-8-Strings werden als
   `ValueError` gemeldet. Gemeinsamer Descriptor-Cleanup bewahrt aktive
   Primärfehler, propagiert Close-Fehler ohne Primärfehler.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**122 `tests/test_private_io.py`-Tests**, **604 Config-/State-Tests**,
99%-Branch-Coverage (bestehende, unveränderte Parent-/Lock-Validierungsfehler
bleiben ungetestet), `py_compile`, Ruff und `git diff --check` grün.

## Runde 175: Terminal-/PTY-Providergrenzen

Fokus: `src/codex_usage/terminal.py`, Account-/Layout-Getter, Terminal-
Auflösung und Prozessstart.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter Account-`label`-Getter ließ Terminalstart roh ausbrechen.
2. Ein fehlerhafter Account-`profile_dir`-Getter ließ Terminalstart ausbrechen.
3. Ein fehlerhafter Account-`id`-Getter ließ Terminalstart ausbrechen.
4. Ein unerwarteter Layout-Providerfehler wurde nicht als `TerminalError`
   normalisiert.
5. Ein fehlerhafter `profile_dir.is_dir()`-Provider ließ rohe Exceptions leaken.
6. Ein fehlerhafter `profile_dir.lstat()`-Provider ließ rohe Exceptions leaken.
7. Ein fehlerhafter Auth-Parent-Symlinkprüfer ließ rohe Exceptions leaken.
8. Ein fehlerhafter `shutil.which()`-Aufruf für Codex ließ rohe Exceptions leaken.
9. Ein fehlerhafter `shutil.which()`-Aufruf für Terminalkandidaten ließ rohe
   Exceptions leaken.
10. Ein unerwarteter `Popen()`-Fehler ließ rohe Exceptions leaken.

Fix: Account-Felder werden einmalig gelesen und validiert. Layout-, Datei-,
   Resolver- und Prozessproviderfehler fallen fail-closed auf passende
   `TerminalError`-Meldungen zurück; gültige Terminalargumente und
   Umgebungsbereinigung bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**56 `tests/test_terminal.py`-Tests**, **312 Profile-/Config-Tests**,
99%-Branch-Coverage (ein bestehender, unveränderter Account-ID-Typfehler bleibt
ungetestet), `py_compile`, Ruff und `git diff --check` grün.

## Runde 176: Systemd-Service-/Fetch-Grenzen

Fokus: `src/codex_usage/service.py`, begrenzter `systemctl`-Runner,
Process-/Selector-Provider und Cleanup.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter `shutil.which("systemctl")`-Provider ließ `_systemctl()` roh
   ausbrechen.
2. Ein unerwarteter Runnerfehler wurde nicht als `ServiceError` normalisiert.
3. Ein fehlerhafter `stdout`-Property-Getter ließ den Runner roh ausbrechen.
4. Ein fehlerhafter `stderr`-Property-Getter ließ den Runner roh ausbrechen.
5. Ein fehlerhafter `poll()`-Getter verdeckte einen Selector-Primärfehler.
6. Ein fehlerhafter Selector-Close verdeckte den Primärfehler.
7. Ein fehlerhafter `stdout.close()` verdeckte den Primärfehler.
8. Ein fehlerhafter `stderr.close()` verdeckte den Primärfehler.
9. Ein Cleanupfehler ohne Primärfehler wurde nicht deterministisch propagiert.
10. Fehlerhafte Deadline-Arithmetik ließ den Runner roh ausbrechen.

Fix: Output-Properties und Deadline werden fail-closed validiert. Ein zentraler
   Resource-Cleanup schließt Selector und Streams vollständig; aktive
   Primärfehler bleiben sichtbar, Cleanupfehler ohne Primärfehler propagieren.
   `_systemctl()` normalisiert Resolver- und Runnerfehler als `ServiceError`.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**117 `tests/test_service.py`-Tests**, **218 CLI-/Terminal-Tests**,
99%-Branch-Coverage (bestehende Symlink-/Deadline-Cleanup-Ausnahmen bleiben
ungetestet), `py_compile`, Ruff und `git diff --check` grün.

## Runde 177: Browser-/Playwright-Providergrenzen

Fokus: `src/codex_usage/browser.py`, Browser-Response-Callbacks, Diagnose-
Authdatei, DOM-/Titel-Leser und Diagnose-Screenshot.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter Response-URL-Getter ließ `_capture_json_response()` roh
   ausbrechen.
2. Ein fehlerhafter Diagnose-Response-URL-Getter ließ den Callback ausbrechen.
3. Ein fehlerhafter Diagnose-Response-Status-Getter ließ den Callback ausbrechen.
4. Ein `Path.exists()`-/`is_symlink()`-Fehler ließ Authdiagnose ausbrechen.
5. Ein unerwarteter Fehler in `auth_metadata_from_payload()` ließ Authdiagnose
   ausbrechen.
6. Ein unerwarteter Playwright-Fehler in `page.locator("body")` ließ den
   sicheren Body-Leser ausbrechen.
7. Ein unerwarteter Fehler beim kombinierten HTML-Leser ließ den Fallback
   ausbrechen.
8. Ein unerwarteter Fehler im HTML-Leser ließ Diagnose ausbrechen.
9. Ein unerwarteter Fehler im Seitentitel-Leser ließ Diagnose ausbrechen.
10. Ein unerwarteter Screenshot-Providerfehler wurde nicht als sicherer
    Diagnosefehler normalisiert.

Fix: Response-Callbacks verwerfen fehlerhafte Providerobjekte vollständig.
Authdiagnose bleibt best effort und meldet Metadatenfehler typisiert. DOM-,
HTML- und Titel-Leser fallen leer bzw. auf den bestehenden Fallback zurück.
Screenshotfehler werden als `ValueError` normalisiert; echte
`PlaywrightError`-Fehler bleiben für den Diagnose-Resultpfad erkennbar.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**204 `tests/test_browser_profile.py`-Tests**, **499 CLI-/Scheduler-Tests**,
89%-Branch-Coverage im Browsermodul, `py_compile`, Ruff für geänderten Code
und `git diff --check` grün.

## Runde 178: Device-Login-/Subprozessgrenzen

Fokus: `src/codex_usage/profile_login.py`, Device-Login-Runner, isolierter
Subprozess, Selector-/Stream-Cleanup sowie Staging-/Auth-Provider.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein unerwarteter Command-Runnerfehler wurde nicht als
   `device_login_process_failed` normalisiert.
2. Ein fehlerhafter Prozess-PID-Getter ließ den Kill-Cleanup roh ausbrechen.
3. Ein unerwarteter `process.kill()`-Fehler ließ den Cleanup ausbrechen.
4. Ein unerwarteter `process.wait()`-Fehler ließ den Cleanup ausbrechen.
5. Ein Fehler beim Erzeugen des Selectors ließ den gestarteten Prozess laufen.
6. Ein Selector-Closefehler verdeckte den Primärfehler des Subprozesses.
7. Ein Stream-Closefehler verhinderte das Schließen weiterer Streams und
   verdeckte den Primärfehler.
8. Ein unerwarteter `layout_for_account()`-Fehler verließ den Login roh.
9. Ein unerwarteter `ensure_profile_layout()`-Fehler verließ den Login roh.
10. Ein unerwarteter Authdetail-Parserfehler verließ die Staged-Authprüfung roh.

Fix: Runner-, Layout-, Staging- und Authparserfehler werden in stabile
`DeviceLoginError`-Codes überführt. Prozess-Cleanup toleriert fehlerhafte
Provider und schließt alle Ressourcen; aktive Primärfehler bleiben erhalten.
Selector-Erzeugung liegt jetzt im geschützten Cleanupbereich. Output-Sink-
Fehler behalten ihren bisherigen Callback-Fehlertyp und lösen trotzdem
Prozessbeendigung aus.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**89 `tests/test_profile_login.py`-Tests**, **271 gekoppelte Profile-/CLI-/
Reactivation-Tests**, 98%-Branch-Coverage im Modul, `py_compile`, Ruff für
geänderten Code und `git diff --check` grün.

## Runde 179: Integration-Snapshot-Modelgrenzen

Fokus: `src/codex_usage/integration_snapshot.py`, Schema-1-Projektion und
UsagePool-/AccountUsage-Getter.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter `AccountUsage.account_id`-Getter ließ Projektion roh
   ausbrechen.
2. Ein fehlerhafter `stale`-Getter ließ Projektion roh ausbrechen.
3. Ein fehlerhafter `status`-Getter ließ Projektion roh ausbrechen.
4. Ein fehlerhafter `captured_at`-Getter ließ Projektion roh ausbrechen.
5. Ein fehlerhafter `models`-Getter ließ Limitprojektion roh ausbrechen.
6. Ein fehlerhafter `main`-Getter ließ Limitprojektion roh ausbrechen.
7. Ein fehlerhafter `usage_resets`-Getter ließ Projektion roh ausbrechen.
8. Ein fehlerhafter `UsagePool.key`-Getter ließ Poolprojektion roh ausbrechen.
9. Ein fehlerhafter `UsagePool.windows`-Getter ließ Poolprojektion roh
   ausbrechen.
10. Ein fehlerhafter `UsagePool.available`-Getter ließ Poolprojektion roh
    ausbrechen.

Fix: Gemeinsamer `_safe_attr()`-Zugriff normalisiert fehlerhafte Modellgetter
   auf `IntegrationInvalidSource`. Gültige Schema-1-Projektion, Sortierung,
   Limits und Secret-Scan bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**99 `tests/test_integration_snapshot.py`-Tests**, **266 gekoppelte
Integration-EntryPoint-/Installer-Tests**, 99%-Branch-Coverage im Modul,
`py_compile`, Ruff für geänderten Code und `git diff --check` grün.

## Runde 180: Auth-Migrations-/Rollback-Grenzen

Fokus: `src/codex_usage/profile_migration.py`, Account-/Layout-Auflösung,
Migrationsplan, Authparser und Rollback-Cleanup.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter Account-Tupel-Längenprovider ließ Planerstellung roh
   ausbrechen.
2. Ein fehlerhafter Search-Roots-Iterator ließ Planerstellung roh ausbrechen.
3. Ein fehlerhafter Account-`id`-Getter ließ Planerstellung ausbrechen.
4. Ein fehlerhafter Account-`label`-Getter ließ Layoutauflösung ausbrechen.
5. Ein fehlerhafter Account-`profile_dir`-Getter ließ Layoutauflösung
   ausbrechen.
6. Ein fehlerhafter Account-`auth_json_path`-Getter ließ Quellauflösung
   ausbrechen.
7. Ein unerwarteter `layout_for_account()`-Fehler blieb unnormalisiert.
8. Ein unerwarteter Datei-Cleanup-`lstat()`-Fehler blieb roh.
9. Ein unerwarteter Verzeichnis-Cleanup-`rmdir()`-Fehler blieb roh.
10. Ein unerwarteter Auth-JSON-Parserfehler blieb roh.

Fix: Plan-/Quell-/Layoutproviderfehler werden als stabile `ValueError`-
Validierungsfehler gemeldet. Authparserfehler werden als ungültige Quelle
klassifiziert. Rollback-Cleanup sammelt auch unerwartete Providerfehler und
verdeckt keine Primärfehler.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**110 `tests/test_profile_migration.py`-Tests**, **204 gekoppelte
Profile-/Config-/Reactivation-Tests**, 98%-Branch-Coverage im Modul,
`py_compile`, Ruff für geänderten Code und `git diff --check` grün.

## Runde 181: App-Server-Prozess- und I/O-Grenzen

Fokus: `src/codex_usage/app_server.py`, Auth-Kontext, Codex-Auflösung,
Subprozessstart, stdin/stdout-I/O, Deadline-Berechnung und Cleanup.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter `account.auth_json_path`-Getter verließ den Auth-Kontext
   mit einem rohen Providerfehler.
2. Ein unerwarteter Authdetail-Parserfehler verließ den Auth-Kontext roh.
3. Ein unerwarteter `shutil.which()`-Fehler verließ die Codex-Auflösung roh.
4. Ein unerwarteter `subprocess.Popen()`-Fehler verließ den App-Server-Start
   roh.
5. Ein unerwarteter `time.monotonic()`-Fehler verließ den stdin-Sender roh.
6. Ein unerwarteter `select.select()`-Fehler verließ den stdin-Sender roh.
7. Ein unerwarteter `os.write()`-Fehler verließ den stdin-Sender roh.
8. Ein unerwarteter Clockfehler verließ die Response-Warteschleife roh.
9. Ein `ValueError` beim Reader-Join verließ den Prozess-Cleanup roh.
10. Ein fehlerhafter Prozess-`stdout`-Getter ließ den gestarteten App-Server
    ohne normalisierten Fehler und ohne Cleanup ausbrechen.

Fix: Providerfehler an Auth-, Prozess- und I/O-Grenzen werden in stabile
`DirectAuthError`, `AppServerUnavailableError`, `AppServerProtocolError` oder
`AppServerFetchError` überführt. Reader werden erst nach erfolgreicher
Streamauflösung registriert; ein Fehler beim Streamzugriff beendet den Prozess
trotzdem. Deadline-, Selector- und Write-Fehler bleiben klassifiziert.
Reader-Cleanup toleriert alle gewöhnlichen Providerfehler.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**185 `tests/test_app_server.py`-Tests**, **1559 gekoppelte
Bridge-/CLI-/Config-/EntryPoint-/Scheduler-/Service-/State-Tests**,
97%-Branch-Coverage im
App-Server-Modul, `py_compile`, Ruff für geänderten Code und `git diff --check`
grün.

## Runde 182: OAuth-Browser-Providergrenzen

Fokus: `src/codex_usage/oauth_browser.py`, CLI-Hauptpfad, Browserkonfiguration,
Pfad-/Markerprüfung und isolierter Prozessstart.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein unerwarteter URL-Validierungsproviderfehler verließ die CLI roh.
2. Ein unerwarteter Konfigurationsproviderfehler verließ die CLI roh.
3. Ein unerwarteter Browser-Command-Fehler verließ die CLI roh.
4. Ein unerwarteter Umgebungsproviderfehler verließ die CLI roh.
5. Ein unerwarteter `subprocess.Popen()`-Fehler verließ die CLI roh.
6. Ein unerwarteter `Path()`-Fehler verließ die Executable-Prüfung roh.
7. Ein unerwarteter `os.access()`-Fehler verließ die Executable-Prüfung roh.
8. Ein unerwarteter Symlink-Ancestor-Fehler verließ die Profilprüfung roh.
9. Ein unerwarteter Profil-`stat()`-Fehler verließ die Profilprüfung roh.
10. Ein unerwarteter Marker-`is_symlink()`-Fehler verließ die Profilprüfung
    roh.

Fix: Die OAuth-CLI normalisiert alle gewöhnlichen Providerfehler und gibt
weiterhin nur bereinigte, begrenzte Fehlermeldungen aus. Executable-, Profil-,
Marker- und Eigentümerprüfungen kapseln nun auch unerwartete Dateisystem-
Providerfehler als `ValueError`; der Browser startet bei jedem Prüfungsfehler
nicht.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**124 `tests/test_reactivate.py`-Tests**, **546 gekoppelte
Reactivation-/Profile-/Browser-Tests**, 95%-Branch-Coverage im OAuth-Helper,
`py_compile`, Ruff für geänderten Code und `git diff --check` grün.

## Runde 183: Health-Event-Recovery-Grenzen

Fokus: `src/codex_usage/health.py`, private Health-Datei, JSON-Recovery,
Eventvalidierung und Retention-Trim.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein unerwarteter `Path.exists()`-Fehler verließ das Health-Lesen roh.
2. Ein unerwarteter Private-Reader-Fehler verließ das Health-Lesen roh.
3. Ein fehlerhafter `st_nlink`-Getter verließ das Health-Lesen roh.
4. Ein fehlerhafter `st_mode`-Getter verließ das Health-Lesen roh.
5. Ein unerwarteter JSON-Parserfehler verließ das Health-Lesen roh.
6. Ein fehlerhafter Event-Iterator verließ das Health-Lesen roh.
7. Ein unerwarteter Clock-/`astimezone()`-Fehler verließ das Retention-Trim
   roh.
8. Ein fehlerhafter Event-`__getitem__`-Getter verließ das Retention-Trim
   roh.
9. Ein fehlerhafter Event-`get()`-Getter verließ die Validierung roh.
10. Ein unerwarteter Timestamp-Parserfehler verließ die Validierung roh.

Fix: Health-Lesen, Eventvalidierung und Retention-Trim fail-closed. Beliebige
gewöhnliche Provider-/Dateisystemfehler verwerfen nur betroffene oder gesamte
ungültige Telemetrie; Schreibfehler bleiben weiterhin sichtbar und werden
nicht still verschluckt.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**61 `tests/test_health.py`-Tests**, **806 gekoppelte
Applet-/CLI-/Routing-/Scheduler-/Spark-Health-Tests**, 100%-Branch-Coverage im
Health-Modul, `py_compile`, Ruff für geänderten Code und `git diff --check`
grün.

## Runde 184: Integration-Attestation-Recovery-Grenzen

Fokus: `src/codex_usage/integration_attestation.py`, private Release-/Datei-
Prüfungen, Manifeststatus, No-Follow-Reader und RECORD-Validierung.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein unerwarteter `lstat()`-Fehler verließ die private Datei-Prüfung roh.
2. Ein unerwarteter UID-Providerfehler verließ die private Datei-Prüfung roh.
3. Ein unerwarteter `lstat()`-Fehler verließ die private Verzeichnisprüfung
   roh.
4. Ein unerwarteter UID-Providerfehler verließ die private
   Verzeichnisprüfung roh.
5. Ein unerwarteter Parent-`lstat()`-Fehler verließ den Datei-Reader roh.
6. Ein unerwarteter `Path()`-Fehler verließ die absolute Pfadprüfung roh.
7. Ein fehlerhafter Manifest-Statgetter verließ den Manifestreader roh.
8. Ein unerwarteter `fstat()`-Fehler verließ den No-Follow-Reader roh.
9. Ein unerwarteter RECORD-`relative_to()`-Fehler verließ die
   RECORD-Validierung roh.
10. Ein unerwarteter Parent-`lstat()`-Fehler verließ den No-Follow-Byte-Reader
    roh.

Fix: Private Sentinel-Prüfungen, Manifeststatus, Datei-Reader und RECORD-
Validierung kapseln gewöhnliche Providerfehler als
`IntegrationAttestationUnavailable`; Deskriptorpfade bleiben fail-closed.
Bewusste Tree-Iterator-/Sortierverträge und deren bestehende Fehlerklassen
bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**238 `tests/test_integration_installer.py`-Tests**, **137 gekoppelte
Integration-EntryPoint-/Snapshot-Tests**, 99%-Branch-Coverage im Attestation-
Modul, `py_compile`, Ruff für geänderten Code und `git diff --check` grün.

## Runde 185: Account-Lock-Providergrenzen

Fokus: `src/codex_usage/account_lock.py`, State-/Lock-Verzeichnis,
Descriptor-Validierung, Deadline und flock-Cleanup.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein unerwarteter State-Directory-Providerfehler verließ den Lock roh.
2. Ein unerwarteter Deadline-Providerfehler verließ den Lock roh.
3. Ein unerwarteter Lock-Directory-Providerfehler verließ den Lock roh.
4. Ein unerwarteter Directory-`open()`-Fehler verließ den Lock roh.
5. Ein unerwarteter Directory-`fstat()`-Fehler verließ den Lock roh.
6. Ein unerwarteter Lock-`stat()`-Fehler verließ den Lock roh.
7. Ein unerwarteter Lock-`open()`-Fehler verließ den Lock roh.
8. Ein unerwarteter UID-Providerfehler verließ den Lock roh.
9. Ein unerwarteter `fchmod()`-Fehler verließ den Lock roh.
10. Ein unerwarteter `flock()`-Fehler verließ den Lock roh.

Fix: State-/Verzeichnis-/Datei-/Deadline-/flock-Providerfehler werden als
stabile `AccountLockError` klassifiziert. Falsche Lockzustände bleiben
fail-closed; Cleanup toleriert auch unerwartete Unlock-/Close-Fehler und
verdeckt keine Primärfehler.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**40 `tests/test_account_lock.py`-Tests**, **1786 gekoppelte
Bridge-/CLI-/Config-/Profile-/Reactivation-/Scheduler-/State-Tests**, 86%-
Branch-Coverage im Lock-Modul, `py_compile`, Ruff für geänderten Code und
`git diff --check` grün.

## Runde 186: Routing-Policy-Datei- und JSON-Grenzen

Fokus: `src/codex_usage/routing.py`, Policy-Pfad, private Datei-/Statprüfung,
JSON-Parser, Policy-/Credit-Limits-Validator und Persistenzvorbereitung.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter Default-Policy-Parent-Getter verließ das Laden roh.
2. Ein fehlerhafter Policy-`exists()`-Getter verließ das Laden roh.
3. Ein fehlerhafter Policy-`is_symlink()`-Getter verließ das Laden roh.
4. Ein unerwarteter Symlink-Check-Fehler verließ das Laden roh.
5. Ein unerwarteter Private-Reader-Fehler verließ das Laden roh.
6. Ein fehlerhafter Policy-Statgetter verließ das Laden roh.
7. Ein unerwarteter JSON-Parserfehler verließ das Laden roh.
8. Ein fehlerhafter Policy-Mapping-Getter verließ die Validierung roh.
9. Ein fehlerhafter Credit-Limits-Mapping-Getter verließ die Validierung
   roh.
10. Ein unerwarteter Directory-Prepare-Fehler verließ die Policy-Persistenz
    roh.

Fix: Policy-Pfad-/Datei-/JSON-/Validator-/Prepare-Providerfehler werden als
stabile `ValueError`-Fehler klassifiziert. Bestehende konkrete Schema-,
Berechtigungs- und Größenfehler bleiben unverändert; Routing-Entscheidungs-
logik wurde nicht verändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**188 `tests/test_routing.py`-Tests**, **1330 gekoppelte
CLI-/Scheduler-/State-/Config-/Spark-Health-Tests**, 98%-Branch-Coverage im
Routing-Modul, `py_compile`, Ruff für geänderten Code und `git diff --check`
grün.

## Runde 187: Config-Datei- und Verzeichnis-Providergrenzen

Fokus: `src/codex_usage/config.py`, Konfigurationspfad, private TOML-Datei,
TOML-Parser und private Verzeichnisvorbereitung.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein unerwarteter Symlink-Prüfungsfehler verließ `load_config` roh.
2. Ein fehlerhafter Config-`exists()`-Getter verließ das Laden roh.
3. Ein fehlerhafter Config-`is_symlink()`-Getter verließ das Laden roh.
4. Ein unerwarteter Private-Reader-Fehler verließ den Config-Reader roh.
5. Ein fehlerhafter Config-Statgetter verließ die Berechtigungsprüfung roh.
6. Ein unerwarteter TOML-Parserfehler verließ das Laden roh.
7. Ein unerwarteter Verzeichnisauflösungsfehler verließ die Vorbereitung roh.
8. Ein unerwarteter Verzeichnis-Symlinkprüfungsfehler verließ die Vorbereitung
   roh.
9. Ein fehlerhafter Verzeichnis-Metadatengetter verließ die Vorbereitung roh.
10. Ein unerwarteter Fehler beim Sichern des Verzeichnisses verließ den
    Schreibpfad roh.

Fix: Pfad-, Reader-, TOML-, Auflösungs-, Metadaten- und Prepare-Providerfehler
werden als stabile `ValueError`-Fehler klassifiziert. Bestehende konkrete
Schema-, Symlink-, Größen- und Berechtigungsfehler bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**257 `tests/test_config.py`-Tests**, **734 gekoppelte
Account-Lock-/CLI-/State-/Profile-Tests**, 95%-Branch-Coverage im Config-
Modul, `py_compile`, Ruff und `git diff --check` grün.

## Runde 188: Spark-Health-Datei- und JSON-Recoverygrenzen

Fokus: `src/codex_usage/spark_health.py`, Health-Dateipfad, privater Reader,
JSON-/Mapping-Validierung, Statusrecord und Directory-Prepare.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter Health-`exists()`-Getter verließ den Statusreader roh.
2. Ein fehlerhafter Health-`is_symlink()`-Getter verließ den Statusreader roh.
3. Ein unerwarteter Private-Reader-Fehler verließ `_load_records` roh.
4. Ein fehlerhafter Health-Statgetter verließ `_load_records` roh.
5. Ein unerwarteter JSON-Parserfehler verließ `_load_records` roh.
6. Ein fehlerhafter Payload-Mapping-Getter verließ `_load_records` roh.
7. Ein fehlerhafter RECORD-Längengetter verließ `_load_records` roh.
8. Ein fehlerhafter Health-Record-Mapping-Getter verließ `_load_records` roh.
9. Ein fehlerhafter Statusrecord-Getter (inklusive Failure-Reason) verließ den
   Statusreader roh.
10. Ein unerwarteter Directory-Prepare-Fehler verließ den Health-Schreibpfad
    roh.

Fix: Health-Datei-/JSON-/Mapping-Providerfehler führen fail-closed zu einem
unbekannten Zustand. Symlink- und bestehende konkrete Schreibfehler bleiben
erhalten; unerwartete Directory-Prepare-Fehler werden als stabile
`ValueError`-Fehler klassifiziert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert, zusätzlich
Failure-Reason-Getter geprüft; danach **50 `tests/test_spark_health.py`-Tests**,
**350 gekoppelte Routing-/CLI-Tests**, 99%-Branch-Coverage im Spark-Health-
Modul, `py_compile`, Ruff und `git diff --check` grün.

## Runde 189: Identity-Auth- und Candidate-Grenzen

Fokus: `src/codex_usage/identity.py`, Auth-ID-Eingaben, Backend-Matcher und
Candidate-URL-Priorisierung.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein primitiver `auth_user_id` wurde erst im Accountvergleich fehlerhaft.
2. Ein nicht-stringartiger `auth_account_id` wurde still weiterverarbeitet.
3. Leere Auth-IDs wurden als fehlende IDs statt als ungültige Eingabe behandelt.
4. Whitespace-/Control-Character-Auth-IDs wurden nicht abgewiesen.
5. Überlange Auth-IDs wurden nicht abgewiesen.
6. String-Subclass-Hooks in `auth_user_id` konnten roh auslösen.
7. String-Subclass-Hooks in `auth_account_id` konnten roh auslösen.
8. Der Matcher akzeptierte malformed Backend-/Auth-IDs als Treffer.
9. Ein unerwarteter Candidate-URL-Providerfehler verließ die Priorisierung roh.
10. Mehrere obige malformed-ID-Fälle konnten je nach Bool-/Vergleichspfad
    unterschiedliche, unsichere Ergebnisse liefern.

Fix: Auth-IDs werden vor Bool-/Gruppenlogik strikt validiert. Der Identity-
Matcher fällt bei falschen Typen sicher auf `False`; fehlerhafte Candidate-
URLs erhalten neutrale Priorität 2. Gruppierungs- und Accountauswahlregeln
bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert (mit
parametrisierten Unterfällen); danach **60 `tests/test_identity.py`-Tests**,
**567 gekoppelte Direct-/Browser-Tests**, **101 fokussierte Bridge-Identity-
Tests**, 100%-Branch-Coverage im Identity-Modul, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 190: Profile-Layout-Datei- und Home-Providergrenzen

Fokus: `src/codex_usage/profile_layout.py`, Profilpfad, Codex-Home-Metadaten,
`config.toml`-Reader und Created-File-Tracking.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein unerwarteter Profilpfad-Providerfehler verließ `layout_for_account` roh.
2. Ein unerwarteter Profil-Ancestor-Check verließ das Layout roh.
3. Ein fehlerhafter Profil-Symlinkgetter verließ das Layout roh.
4. Ein unerwarteter Codex-Home-Auflösungsfehler verließ die Animationserkennung
   roh.
5. Ein unerwarteter Codex-Home-Ancestor-Check verließ die Animationserkennung
   roh.
6. Ein fehlerhafter Codex-Home-Symlink-/Directory-Getter verließ sie roh.
7. Ein fehlerhafter Codex-Home-Statgetter verließ sie roh.
8. Ein fehlerhafter Config-Datei-Metadatengetter verließ den Config-Reader roh.
9. Ein unerwarteter Config-Reader- oder Statfehler verließ ihn roh.
10. Ein unerwarteter Created-File-`lstat()`-Fehler verließ das Tracking roh.

Fix: Providerfehler an Profil-, Home- und Config-Grenzen werden als stabile
`ValueError`-Fehler klassifiziert; bestehende konkrete OSError-/Symlink-/
Berechtigungsfehler und Rollback-Verträge bleiben erhalten. Animation bleibt
standardmäßig `animations = false`.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert (mit
parametrisierten Unterfällen); danach **76 `tests/test_profile_layout.py`-
Tests**, **456 gekoppelte Config-/Migration-/Login-Tests**, 94%-Branch-
Coverage im Layout-Modul, `py_compile`, Ruff und `git diff --check` grün.

## Runde 191: Reactivation-Kernpfad- und Auth-Zielgrenzen

Fokus: `src/codex_usage/reactivate.py` außerhalb des bereits geprüften
`oauth_browser`-CLI-Teils: Auth-Backup, Auth-Ziel, Browserauswahl, private
Profile, Symlinkscanner, Executable und Manage-URL.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter Backup-`exists()`-Getter verließ die Reaktivierung roh.
2. Ein fehlerhafter Backup-Statgetter verließ die Sicherung roh.
3. Ein unerwarteter Auth-Ziel-Auflösungsfehler verließ die Validierung roh.
4. Ein fehlerhafter Auth-Parent-Metadatengetter verließ sie roh.
5. Ein unerwarteter Private-Parent-Providerfehler verließ sie roh.
6. Ein fehlerhafter Browser-`which()`-Provider verließ die Auswahl roh.
7. Ein unerwarteter OAuth-Profile-Symlinkgetter verließ den Prepare-Pfad roh.
8. Ein unerwarteter OAuth-Directory-Prepare-Fehler verließ ihn roh.
9. Ein fehlerhafter Symlink-Scanner-/Executable-Path-Provider verließ den
   Pfad roh.
10. Ein unerwarteter Manage-URL-Parserfehler verließ die URL-Prüfung roh.

Fix: Auth-/Profile-/URL-/Executable-Providerfehler werden als stabile
`ReactivationError`-Fehler klassifiziert; bestehende konkrete OSError-,
Symlink-, Lock-, Rollback- und Nicht-installiert-Verträge bleiben erhalten.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert (mit
parametrisierten Unterfällen); danach **135 `tests/test_reactivate.py`-Tests**,
**422 gekoppelte Profile-Jobs-/Login-/Browser-Tests**, 98%-Branch-Coverage im
Reactivation-Modul, `py_compile`, Ruff und `git diff --check` grün.

## Runde 192: State-Generation-Reader- und Increment-Grenzen

Fokus: `src/codex_usage/state.py`, State-Generation-Datei, private Reader-/
JSON-Prüfung und Generation-Directory-Increment.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein unerwarteter Generation-Ancestor-Check verließ den Reader roh.
2. Ein fehlerhafter Generation-`exists()`-Getter verließ ihn roh.
3. Ein fehlerhafter Generation-`is_symlink()`-Getter verließ ihn roh.
4. Ein unerwarteter privater Generation-Readerfehler verließ ihn roh.
5. Ein fehlerhafter Generation-Statgetter verließ ihn roh.
6. Ein unerwarteter Generation-JSON-Parserfehler verließ ihn roh.
7. Ein fehlerhafter Payload-Account-Getter verließ ihn roh.
8. Ein fehlerhafter Generation-Getter verließ ihn roh.
9. Ein unerwarteter Generation-Directory-Ancestor-/Symlinkfehler verließ den
   Increment-Pfad roh.
10. Ein unerwarteter Generation-Directory-Prepare-Fehler verließ ihn roh.

Fix: Unerwartete State-Generation-Providerfehler werden als stabile
`ValueError`-Fehler klassifiziert; bewusste Symlink-, Berechtigungs- und
Schemafehler behalten bestehende Meldungen. Snapshot-/Merge-/Expirylogik blieb
unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert (mit
parametrisierten Unterfällen); danach **368 `tests/test_state.py`-Tests**,
**73 gekoppelte CLI-/Bridge-/Scheduler-State-Tests**, 99%-Branch-Coverage im
State-Modul, `py_compile`, Ruff und `git diff --check` grün.

## Runde 193: State-Snapshot- und Current-Readergrenzen

Fokus: `src/codex_usage/state.py`, gemeinsamer `_load_usage`-Reader für
Snapshots und Current-State.

Zehn reproduzierbare Fehlerfälle geprüft:

1. Ein fehlerhafter Directory-Join verließ den Reader roh.
2. Ein fehlerhafter Snapshot-`exists()`-Getter verließ ihn roh.
3. Ein unerwarteter privater Readerfehler verließ ihn roh.
4. Ein fehlerhafter Snapshot-Statgetter verließ ihn roh.
5. Ein unerwarteter JSON-Parserfehler verließ ihn roh.
6. Ein fehlerhafter Payload-Getter verließ ihn roh.
7. Ein unerwarteter `usage_from_dict`-Fehler verließ ihn roh.
8. Ein fehlerhafter Backend-Provenienz-Completeness-Check verließ ihn roh.
9. Ein fehlerhafter konfigurierte-Provenienz-Check verließ ihn roh.
10. Ein unerwarteter State-Generation-Readerfehler verließ ihn roh.

Fix: Pfadbildung, Existenzprüfung und gesamter Snapshot-/Current-Reader liegen
jetzt in einer zusätzlichen generischen `Exception`-Fail-Closed-Grenze. Bereits
klassifizierte Datei-, JSON-, Schema- und Generationfehler bleiben unverändert;
ungültige oder nicht vertrauenswürdige Zustände liefern weiterhin `None`.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**378 `tests/test_state.py`-Tests**, **73 gekoppelte CLI-/Bridge-/Scheduler-
State-Tests**, 99%-Branch-Coverage im State-Modul, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 194: Usage-Reset-State- und Formatierungsgrenzen

Fokus: `src/codex_usage/usage_resets.py`, serialisierter Reset-State,
Anzeigeformatierung und Redeem-Gate.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter `available`-Getter verließ `as_dict()` roh.
2. Ein fehlerhafter `known`-Getter verließ `as_dict()` roh.
3. Ein fehlerhafter Redeem-Capability-Getter verließ `as_dict()` roh.
4. Ungültige Subclass-Felder wurden von `as_dict()` nicht erneut validiert.
5. Ein fehlerhafter `available`-Getter verließ die Formatierung roh.
6. Ein fehlerhafter `known`-Getter verließ die Formatierung roh.
7. Ein fehlerhafter Capability-Getter verließ das Redeem-Gate roh.
8. Ein fehlerhafter `known`-Getter verließ das Redeem-Gate roh.
9. Ein fehlerhafter `available`-Getter verließ das Redeem-Gate roh.
10. Ungültige Subclass-Felder konnten das Redeem-Gate ungeprüft erreichen.

Fix: Gemeinsame `_safe_reset_state_fields`-Validierung liest alle drei Felder
unter einer stabilen `ValueError("reset state is invalid")`-Grenze und prüft
erneut den Dataclass-Vertrag. Parser-, Nullanzeige- und nicht implementiertes
Redeem-Verhalten bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**40 `tests/test_usage_resets.py`-Tests**, 98%-Branch-Coverage im Reset-Modul,
68 gekoppelte Reset-/State-/App-Server-/Bridge-Tests, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 195: Bridge-Token- und Dateisystemgrenzen

Fokus: `src/codex_usage/bridge.py`, Bridge-Token-Erzeugung, Prüfung,
Widerruf und private Token-Datei.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein unerwarteter Token-Directory-Prepare-Fehler verließ Erzeugung roh.
2. Ein unerwarteter Token-Datei-Schreibfehler verließ Erzeugung roh.
3. Ein fehlerhafter Token-`exists()`-Getter verließ den Reader roh.
4. Ein unerwarteter privater Token-Readerfehler verließ ihn roh.
5. Ein fehlerhafter Token-Statgetter verließ ihn roh.
6. Ein unerwarteter Token-Directory-`exists()`-Fehler verließ Matching roh.
7. Ein unerwarteter Token-Directory-Prepare-Fehler verließ Matching roh.
8. Ein unerwarteter Token-Lock-Fehler verließ Matching roh.
9. Ein fehlerhafter Revoke-Directory-`exists()`-Getter verließ Widerruf roh.
10. Ein fehlerhafter Revoke-Token-Metadatengetter verließ Widerruf roh.

Fix: Token-Erzeugung und Widerruf klassifizieren unerwartete Providerfehler als
stabile `ValueError`; Token-Matching bleibt für jeden unerwarteten Fehler
fail-closed `False`. Bewusste Validierungs-, Berechtigungs- und Symlinkfehler
behalten ihre bisherigen Meldungen.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**311 `tests/test_bridge.py`-Tests**, **21 gekoppelte CLI-/App-Server-/Installer-
Bridge-Tests**, 99%-Branch-Coverage im Bridge-Modul, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 196: Cinnamon-Panel-Render- und Reloadgrenzen

Fokus: `files/codex-usage@H234598/applet.js`, sichtbare Panel-/Menü-Markups
und Fehlerisolation bei Anzeige-Sinks.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Panel-Markup löste nach `queue_relayout` kein Redraw aus.
2. Menü-Markup löste nach `queue_relayout` kein Redraw aus.
3. Ein fehlerhafter Panel-Label-Sink brach die Aktualisierung roh ab.
4. Ein fehlerhafter Tooltip-Sink brach die Aktualisierung roh ab.
5. Ein fehlerhafter Fehler-Style-Sink brach die Aktualisierung roh ab.
6. Ein fehlerhafter kritischer Style-Sink brach die Aktualisierung roh ab.
7. Ein fehlerhafter Warnungs-Style-Sink brach die Aktualisierung roh ab.
8. Ein fehlerhafter Symbolic-Icon-Sink brach die Aktualisierung roh ab.
9. Fehler im Fast-Mode-Icon und seinem Fallback waren nicht isoliert.
10. Fehler in Panel-Quellen, Content-Buildern oder Markup-Sinks konnten den
    gesamten Refresh abbrechen.

Fix: Panel- und Menü-Markup fordern nach Relayout explizit `queue_redraw` an.
Quellen, Content, Icons, Label, Markup, Style-Klassen und Tooltip werden im
Refresh jeweils fail-closed bzw. unabhängig behandelt; ein einzelner
Cinnamon-Sink verhindert keine weitere Formatierung.

Verifikation: **2 fokussierte RED-Regressionstests**, danach **650
`tests/applet_runtime.test.js`-Tests**, **169 Python-Applet-/Formatierungs-
Tests**, `node --check` und `git diff --check` grün.

## Runde 197: Render-/Account-Übersichtsgrenzen

Fokus: `src/codex_usage/render.py`, Account-Sortierung, Mapping-Zugriff und
Dateisystemstatus in CLI-Tabellen.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter Account-`id`-Getter verließ die Begrenzungsprüfung roh.
2. Fallback-Stringifizierung eines ungültigen Pfads konnte erneut roh fehlschlagen.
3. Ein fehlerhafter Profil-`is_dir()`-Aufruf verließ die Anzeige roh.
4. Ein fehlerhafter Profil-`exists()`-Aufruf verließ die Anzeige roh.
5. Ein fehlerhafter Auth-`is_file()`-Aufruf verließ die Anzeige roh.
6. Ein fehlerhafter Auth-`exists()`-Aufruf verließ die Anzeige roh.
7. Eine fehlerhafte Truthiness eines Usage-Mappings brach die Übersicht ab.
8. Ein fehlerhafter Usage-Mapping-`get()`-Aufruf brach die Account-Übersicht ab.
9. Ein fehlerhafter Usage-Mapping-`get()`-Aufruf brach die Werte-Tabelle ab.
10. Hostile Account-IDs konnten Sortierung in beiden Tabellen abbrechen.

Fix: Accountlisten werden nach Validierung unter stabiler `ValueError`-Grenze
sortiert. Mapping-Lookups liefern bei Providerfehlern fehlende Werte; Mapping-
Truthiness wird nicht mehr ausgewertet. Pfadstatus und Pfad-Fallbacks sind
fail-closed und zeigen `ungültig` bzw. `-`.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**108 `tests/test_render.py`-Tests**, **499 gekoppelte CLI-/Scheduler-Tests**,
99%-Branch-Coverage im Render-Modul, `py_compile`, Ruff und `git diff --check`
grün.

## Runde 198: Scheduler-Stabilisierungsgrenzen

Fokus: `src/codex_usage/scheduler.py`, authenticated-usage stabilization,
Provider- und Modellfeldzugriffe während Reset-Übergängen.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Ein fehlerhafter Previous-Status-Getter verließ Stabilisierung roh.
2. Ein fehlerhafter Previous-Backend-Getter verließ sie roh.
3. Ein fehlerhafter Current-Backend-Getter verließ sie roh.
4. Ein fehlerhafter Previous-`stale`-Getter verließ sie roh.
5. Ein fehlerhafter Previous-Fallback-Getter verließ sie roh.
6. Ein fehlerhafter Current-5h-Getter verließ sie roh.
7. Ein fehlerhafter Previous-5h-Getter verließ sie roh.
8. Ein fehlerhafter Current-Wochen-Getter verließ sie roh.
9. Ein fehlerhafter Previous-Wochen-Getter verließ sie roh.
10. Ein fehlerhafter Current-`main`-Getter verließ sie roh.

Fix: Stabilisierung läuft durch eine gemeinsame äußere Fail-Closed-Grenze.
Jeder unerwartete Property-/Providerfehler verwirft nur Stabilisierung und
liefert unveränderten Current-Usage zurück; gültige Reset-Übergänge bleiben
unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**347 `tests/test_scheduler.py`-Tests**, **299 gekoppelte CLI-/EntryPoint-/
Snapshot-Tests**, 99%-Branch-Coverage im Scheduler-Modul, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 199: Modell-Property- und Serialisierungsgrenzen

Fokus: `src/codex_usage/models.py`, LimitWindow-/UsagePool-Properties und
AccountUsage-Serialisierung.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Fehler in `LimitWindow.is_complete` konnten roh austreten.
2. Fehler in `LimitWindow.has_known_identity` konnten roh austreten.
3. Fehler in `LimitWindow.remaining_percent` konnten roh austreten.
4. Fehler im Usage-Source-Getter konnten roh austreten.
5. Fehler im UsagePool-`available`-Getter konnten `exhausted` abbrechen.
6. Fehler im UsagePool-`allowed`-Getter konnten `exhausted` abbrechen.
7. Fehler im UsagePool-`limit_reached`-Getter konnten `exhausted` abbrechen.
8. Fehler im AccountUsage-`models`-Getter konnten `model_pool` abbrechen.
9. Fehler im Model-Pool-`key`-Getter konnten `model_pool` abbrechen.
10. Fehler im AccountUsage-`usage_resets`-Getter konnten `as_dict` abbrechen.

Fix: Window- und Pool-Properties fail-closed (`False`, `None`, `True` je
Semantik). Model-Katalogzugriff liefert bei fehlerhaften Einträgen `None`;
Serialisierung nutzt bei fehlerhaftem Reset-State den sicheren unbekannten
Fallback.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**92 `tests/test_models.py`-Tests**, **873 gekoppelte Render-/Scheduler-/State-
/Reset-Tests**, 99%-Branch-Coverage im Modellmodul, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 200: Consumption-DTO- und EMA-Grenzen

Fokus: `src/codex_usage/consumption.py`, CLI-/Integration-DTO-Serialisierung
und private EMA-Berechnung.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Fehler im DTO-`lookback_seconds`-Getter brachen `as_dict` roh ab.
2. Fehler im DTO-`pool`-Getter brachen `as_dict` roh ab.
3. Fehler im DTO-Limitfenster-Getter brachen `as_dict` roh ab.
4. Fehler im DTO-Verbrauchs-Getter brachen `as_dict` roh ab.
5. Fehler im DTO-Coverage-Getter brachen `as_dict` roh ab.
6. Fehler im DTO-Sample-Count-Getter brachen `as_dict` roh ab.
7. Fehler im DTO-Prognose-Getter brachen `as_dict` roh ab.
8. Fehler im DTO-Baseline-Getter brachen `as_dict` roh ab.
9. Fehler in EMA-Zeitstempeldifferenz brachen Glättung roh ab.
10. Fehler in EMA-Verbrauchswert brachen Glättung roh ab.

Fix: Fehlerhafte Consumption-DTOs serialisieren fail-closed als leeres Objekt;
der Frontend-Validator verwirft sie. EMA gibt bei unerwarteten Sample-/Provider-
Feldern `0.0` zurück; gültige Berechnungen und Reset-/Gap-Semantik bleiben
unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**85 `tests/test_consumption.py`-Tests**, **299 gekoppelte CLI-/EntryPoint-/
Snapshot-Tests**, 100%-Branch-Coverage im Consumption-Modul, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 201: Integration-Snapshot-Projektionsgrenze

Fokus: `src/codex_usage/integration_snapshot.py`, Schema-1-Projektion und
unerwartete Mapping-/Iterator-/Providerfehler.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Fehlerhafte Usage-Tupel-Längenprüfung verließ Projektion roh.
2. Fehlerhafter Usage-Iterator verließ Projektion roh.
3. Fehlerhafter Model-Iterator verließ Projektion roh.
4. Fehlerhafter Usage-Reset-Serializer verließ Projektion roh.
5. Fehlerhaftes Cost-Mapping-`__contains__` verließ sie roh.
6. Fehlerhaftes Cost-Mapping-`__getitem__` verließ sie roh.
7. Fehlerhafte Cost-Längenprüfung verließ sie roh.
8. Fehlerhafter Cost-Iterator verließ sie roh.
9. Fehlerhafter Availability-Source-Iterator verließ sie roh.
10. Fehlerhafter Pool-Key-Hash verließ sie roh.

Fix: `build_schema1_document` hat jetzt äußere Fehlergrenze. Bereits
klassifizierte `IntegrationSnapshotError` bleiben erhalten; unerwartete
Provider-/Containerfehler werden stabil als `IntegrationInvalidSource`
klassifiziert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**109 `tests/test_integration_snapshot.py`-Tests**, **200 gekoppelte CLI-/
Integration-EntryPoint-Tests**, 99%-Branch-Coverage im Snapshot-Modul,
`py_compile`, Ruff und `git diff --check` grün.

## Runde 202: Strict-JSON-Zahlen bleiben endlich

Fokus: `src/codex_usage/json_utils.py`, Float-Parsing in JSON-Payloads.

Zehn reproduzierbare Fehlerfälle gefunden und behoben:

1. Positiver Exponentenüberlauf auf Top-Level wurde als `inf` akzeptiert.
2. Negativer Exponentenüberlauf auf Top-Level wurde als `-inf` akzeptiert.
3. Großgeschriebener positiver Exponent wurde als `inf` akzeptiert.
4. Großgeschriebener negativer Exponent wurde als `-inf` akzeptiert.
5. Extrem großer Exponent wurde als `inf` akzeptiert.
6. Überlauf in einem Array wurde als `inf` akzeptiert.
7. Überlauf in einem Objektfeld wurde als `inf` akzeptiert.
8. Gemischte positive und negative Überläufe wurden akzeptiert.
9. Verschachtelter Array-Überlauf wurde als `inf` akzeptiert.
10. Verschachtelter Objekt-Überlauf wurde als `-inf` akzeptiert.

Fix: `loads_strict` verwendet beim Float-Parsing eine gemeinsame
`math.isfinite`-Grenze und weist überlaufende JSON-Zahlen als
`ValueError("JSON number is not finite")` zurück. Endliche Gleitkommazahlen,
Duplikatschlüssel, Konstanten- und Nesting-Schutz bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**24 `tests/test_json_utils.py`-Tests**, 100%-Branch-Coverage im JSON-Modul,
**2443 gekoppelte State-/Routing-/Auth-/Browser-/Extractor-Tests**,
`py_compile`, Ruff und `git diff --check` grün.

## Runde 203: Usage-Limit-Quellen strikt validiert

Fokus: `src/codex_usage/usage_limits.py`, WHAM-/App-Server-Poolparser und
die Herkunftsangabe jedes erzeugten Limitfensters.

Elf reproduzierbare Eingabefälle gefunden und behoben:

1. `None` als Quelle wurde in ein verfügbares WHAM-Fenster übernommen.
2. Integerquelle wurde unverändert gespeichert.
3. Boolesche Quelle wurde unverändert gespeichert.
4. Listenquelle wurde unverändert gespeichert.
5. Mappingquelle wurde unverändert gespeichert.
6. Beliebiges Objekt wurde unverändert gespeichert.
7. Bytesquelle wurde unverändert gespeichert.
8. Bytearrayquelle wurde unverändert gespeichert.
9. Leere Zeichenkette wurde als gültige Quelle akzeptiert.
10. Steuerzeichen in Quelle wurden akzeptiert.
11. Eine `str`-Subclass mit fehlerhaftem `casefold()` wurde gespeichert.

Fix: Beide öffentlichen Poolparser und ihre internen Grenzen verlangen jetzt
eine exakte, nichtleere Quelle mit höchstens 64 Zeichen und ohne ASCII-/C1-
Steuerzeichen. Ungültige Quellen liefern fail-closed `(None, ())`; spätere
Extractor-Pfade können dadurch nicht mehr auf Fremdtypen oder fehlerhafte
String-Hooks zugreifen.

Verifikation: **11 RED-Regressionstestfälle** vor Fix reproduziert; danach
**168 `tests/test_usage_limits.py`-Tests**, 99%-Branch-Coverage im
Usage-Limits-Modul, **952 gekoppelte Direct-/App-Server-/Extractor-/Routing-
Tests**, `py_compile`, Ruff und `git diff --check` grün.

## Runde 204: Identity- und Plan-Felder blockieren C1-Steuerzeichen

Fokus: `src/codex_usage/identity.py`, Backend-/Auth-Identitäten und
Plan-Typ-Normalisierung.

Zehn reproduzierbare Fehlerfälle gefunden und behoben: Je ein C1-Zeichen aus
`U+0080…U+009F` wurde in Backend-User-ID, Backend-Account-ID, Auth-ID und
Plan-Typ akzeptiert. Diese Werte konnten danach als Identität gruppiert und in
UI-/History-Pfade weitergereicht werden, obwohl die übrigen Textgrenzen
Steuerzeichen bereits ausschließen.

Fix: Identity-, Auth- und Plan-Typ-Prüfungen weisen jetzt den kompletten
ASCII-/C1-Steuerzeichenbereich (`0x7F…0x9F`) zurück. Bestehende Leerzeichen-,
Längen-, Subclass- und Account-Matching-Regeln bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**70 `tests/test_identity.py`-Tests**, 100%-Branch-Coverage im Identity-Modul,
**783 gekoppelte Direct-/Browser-/Extractor-Tests**, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 205: Config-Textgrenzen blockieren C1-Steuerzeichen

Fokus: `src/codex_usage/config.py`, Textfeldvalidierung und TOML-
Serialisierung für Labels, Tags und Pfade.

Zehn reproduzierbare Fehlerfälle gefunden: Je ein C1-Steuerzeichen
(`U+0080…U+0089`) wurde vom gemeinsamen `_validate_text_field` akzeptiert.
Dadurch konnten solche Zeichen in Konfiguration und späterer Anzeige landen;
der Serializer schrieb sie zudem unescaped in TOML.

Fix: Textfelder weisen C1-Steuerzeichen jetzt kontrolliert zurück. `_quote`
escaped den gesamten C1-Bereich zusätzlich zum bestehenden C0-/DEL-Schutz,
damit auch direkte Serialisierungsaufrufe gültiges, reproduzierbares TOML
erzeugen.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**268 `tests/test_config.py`-Tests**, 95%-Branch-Coverage im Config-Modul,
**566 gekoppelte Profile-/CLI-Tests**, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 206: Direct-Auth- und Spark-Identifier blockieren C1

Fokus: `src/codex_usage/direct.py`, Auth-Account-ID, Token-Identität,
Plan-Typ und Spark-Identifier-Normalisierung.

Zehn reproduzierbare Fehlerfälle gefunden: C1-Steuerzeichen aus
`U+0080…U+0089` wurden in `_safe_auth_identity`, `_safe_auth_plan_type`,
`_auth_account_id_from_payload` und `_normalized_response_identifier`
akzeptiert. Dadurch konnten beschädigte Token-Claims oder Spark-Metadaten in
Identitätsvergleich und Stabilitätsauswahl gelangen.

Fix: Alle vier Direct-Grenzen weisen jetzt den kompletten C1-Bereich
(`0x7F…0x9F`) zusätzlich zu C0-/Whitespace-Zeichen zurück. Vorhandene
Subclass-, Längen- und Aliasregeln bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**373 `tests/test_direct.py`-Tests**, 100%-Branch-Coverage im Direct-Modul,
**482 gekoppelte App-Server-/Reaktivierungs-/CLI-Tests**, `py_compile`, Ruff
und `git diff --check` grün.

## Runde 207: App-Server-Modell-IDs blockieren C1-Steuerzeichen

Fokus: `src/codex_usage/app_server.py`, `model/list`-Antwortvalidierung.

Zehn reproduzierbare Fehlerfälle gefunden: Modell-IDs mit je einem C1-
Steuerzeichen aus `U+0080…U+008A` wurden als gültige Katalogeinträge
übernommen. Solche IDs konnten anschließend in Spark-/Modellkatalog und
Anzeige gelangen.

Fix: Die Modell-ID-Grenze weist jetzt zusätzlich zu Whitespace, C0 und DEL
den gesamten C1-Bereich (`0x7F…0x9F`) zurück. Länge, Trim- und
Duplikatregeln bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**195 `tests/test_app_server.py`-Tests**, 97%-Branch-Coverage im App-Server-
Modul, **509 gekoppelte CLI-/Scheduler-Tests**, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 208: Routing-Backend-Identitäten blockieren C1

Fokus: `src/codex_usage/routing.py`, Identitätsvalidierung vor
Routing-Entscheidungen.

Zehn reproduzierbare Fehlerfälle gefunden: C1-Steuerzeichen aus
`U+0080…U+008A` wurden in `backend_user_id` bzw. `backend_account_id` eines
Usage-Objekts akzeptiert. Damit konnte Routing beschädigte Identität als
verifiziert behandeln, obwohl vorgelagerte Identity-/Direct-Grenzen solche
Werte ablehnen.

Fix: `_backend_identity_is_valid` weist jetzt den gesamten C1-Bereich
(`0x7F…0x9F`) zusätzlich zu C0-/Whitespace-Zeichen zurück. Backend- und
Provenance-Regeln bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**198 `tests/test_routing.py`-Tests**, 98%-Branch-Coverage im Routing-Modul,
**535 gekoppelte CLI-/Direct-Tests**, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 209: Profile-Job-Textfelder blockieren C1

Fokus: `src/codex_usage/profile_jobs.py`, Job-Erzeugung und Event-Normalisierung.

Zehn reproduzierbare Eingabefälle gefunden: Je ein C1-Steuerzeichen wurde in
Label, Tag, erwarteter Backend-Account-ID und Event-Wert akzeptiert. Diese
Werte konnten in Manifest-/Eventdatei und Statusausgabe landen.

Fix: Alle vier Profile-Job-Grenzen weisen jetzt zusätzlich zu C0/DEL den
kompletten C1-Bereich (`0x7F…0x9F`) zurück. Job-Schema, Größen- und URL-Regeln
bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**139 `tests/test_profile_jobs.py`-Tests**, 100%-Branch-Coverage im
Profile-Job-Modul, **361 gekoppelte Profile-Login-/Migration-/CLI-Tests**,
`py_compile`, Ruff und `git diff --check` grün.

## Runde 210: Extractor-Metadaten und JSON-Schlüssel fail-closed

Fokus: `src/codex_usage/extractor.py`, Zusammenführung strukturierter und
gerenderter Limitfenster sowie Traversierung manuell gelieferter JSON-
Kandidaten.

Zehn reproduzierbare Fehlerfälle gefunden: `_json_window_has_usage_metadata`
vertraute auf `source.casefold()` und Regex-Eingabe aus `raw`; nicht-string-
Werte lösten damit `AttributeError` bzw. `TypeError` aus. Zusätzlich führte
die JSON-Traversierung Schlüssel per impliziter String-Konvertierung in Pfade
ein. Ein Schlüssel mit fehlerhaftem `__str__` konnte deshalb
`_walk_dicts`/`_flatten_mapping` und den gesamten Extractor-Abruf abbrechen.

Fix: Metadatenprüfung liest Felder geschützt, akzeptiert nur echte Strings und
liefert bei fremden/fehlerhaften Werten `False`. Walker und Flattening
überspringen nicht-stringige Schlüssel; JSON-Schlüssel aus `loads_strict` sind
ohnehin Strings, somit bleibt gültige Verarbeitung unverändert.

Verifikation: **20 RED-Regressionstestfälle** (10 Metadatenfälle, 10
Schlüsselvarianten) vor Fix reproduziert; danach **236
`tests/test_extractor.py`-Tests**, 98%-Branch-Coverage im Extractor-Modul,
`py_compile`, Ruff und `git diff --check` grün.

## Runde 211: App-Server-Planfelder blockieren C1

Fokus: `src/codex_usage/app_server.py`, Validierung des vom App-Server
gemeldeten `planType` gegen `auth.json`.

Zehn reproduzierbare Fehlerfälle gefunden: C1-Steuerzeichen `U+0080…U+0084`
und `U+0086…U+0090` im Server-Planwert passierten die bisherige Prüfung, weil
sie nur C0 und DEL sperrte. Bei identischem Erwartungswert wurde der
kontaminierte Plan danach als gültige Authentifizierungsmetadaten behandelt.

Fix: Die Planfeldgrenze weist jetzt den kompletten Bereich `0x7F…0x9F`
zusätzlich zu Whitespace und C0 zurück. Vergleich, Alias-Normalisierung und
Längenlimit bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**205 `tests/test_app_server.py`-Tests**, 97%-Branch-Coverage im App-Server-
Modul, **509 gekoppelte CLI-/Scheduler-Tests**, `py_compile`, Ruff und
`git diff --check` grün.

## Runde 212: Browser-Diagnose-Schlüssel fail-closed

Fokus: `src/codex_usage/browser.py`, Diagnose- und Probe-Zusammenfassung von
JSON-Objekten.

Zehn reproduzierbare Fehlerfälle gefunden: `_diagnostic_keys` und
`_top_level_keys` konvertierten jeden Mapping-Schlüssel implizit mit `str()`.
Ein Schlüssel mit fehlerhaftem `__str__` konnte dadurch Diagnoseausgabe und
Probe-Pfad mit einem rohen Providerfehler abbrechen.

Fix: Beide Ausgabefunktionen akzeptieren nur echte String-Schlüssel, prüfen
den Mapping-Typ strikt und liefern bei unerwarteten Providerfehlern eine leere
Schlüsselliste. Echte JSON-Schlüssel bleiben vollständig sortiert und begrenzt
ausgegeben.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**273 Browser-Diagnose-/Profil-Tests**, 99%-Branch-Coverage im Browser-Modul,
`py_compile`, Ruff und `git diff --check` grün.

## Runde 213: Browser-Diagnose-Stringgrenzen

Fokus: `src/codex_usage/browser.py`, sichere Darstellung diagnostischer
Skalarwerte und Begrenzung von Fehlermeldungstext.

Zehn reproduzierbare Fehlerfälle gefunden: `_diagnostic_value` leitete
String-Subklassen an `str()` weiter; ein fehlerhafter `__str__`-Hook brach die
Diagnose roh ab. `_diagnostic_text` hatte dieselbe ungeprüfte Konvertierung und
keine fail-closed Typ-/Limitgrenze.

Fix: Nur echte Strings werden formatiert; fremde Stringtypen werden als Typname
angezeigt. `_diagnostic_text` prüft Wert und Limit strikt, fängt Formatterfehler
ab und hält auch sehr kleine Limits ein.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**283 Browser-Diagnose-/Profil-Tests**, 99%-Branch-Coverage im Browser-Modul,
`py_compile`, Ruff und `git diff --check` grün.

## Runde 214: Browser-URL-Grenzen gegen String-Subklassen

Fokus: `src/codex_usage/browser.py`, Vertrauens-, Relevanz- und Redaktions-
prüfung von Browser-URLs.

Zehn reproduzierbare Fehlerfälle gefunden: URL-String-Subklassen wurden von
`_is_trusted_browser_url` akzeptiert. `_looks_relevant_url` rief danach
überschriebene Methoden wie `lower()` auf; `_redact_url` gab solche Werte sogar
unverändert verarbeitet zurück.

Fix: Alle drei URL-Grenzen akzeptieren nur echte `str`-Werte. Fremde
Stringtypen werden verworfen bzw. als leere Redaktionsausgabe behandelt;
Playwrights normale Stringwerte bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**293 Browser-Diagnose-/Profil-Tests**, 99%-Branch-Coverage im Browser-Modul,
`py_compile`, Ruff und `git diff --check` grün.

## Runde 215: Browser-Response-Textgrenzen

Fokus: `src/codex_usage/browser.py`, JSON- und Diagnose-Response-Capture.

Zehn reproduzierbare Fehlerfälle gefunden: Content-Type, Content-Length und
Response-Body akzeptierten String-Subklassen. Überschriebene `lower()`,
`strip()`, `encode()` oder `split()`-Methoden konnten beide Capture-Pfade mit
rohen Providerfehlern abbrechen.

Fix: Response-Header und Body werden nur bei exakt `str` verarbeitet;
fremde Stringtypen werden verworfen. Normale Playwright-Response-Werte bleiben
unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**303 Browser-Diagnose-/Profil-Tests**, 99%-Branch-Coverage im Browser-Modul,
`py_compile`, Ruff und `git diff --check` grün.

## Runde 216: Browser-Seitenstatus-Textgrenzen

Fokus: `src/codex_usage/browser.py`, Erkennung des Seitenzustands und Status-
Ableitung aus URL, Titel und Body.

Zehn reproduzierbare Fehlerfälle gefunden: `_detect_page_state` und
`_status_for_result` akzeptierten String-Subklassen und riefen deren
überschriebene `__str__`, `strip()` oder `lower()`-Methoden auf. Ein fremder
Textwert konnte damit Statusermittlung und Login-/Cloudflare-Erkennung roh
abbrechen.

Fix: URL, Titel und Body werden nur bei exakt `str` in Statuslogik übernommen;
andere Werte werden als leer behandelt. Normale Browser-Texte bleiben
unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**313 Browser-Diagnose-/Profil-Tests**, 99%-Branch-Coverage im Browser-Modul,
`py_compile`, Ruff und `git diff --check` grün.

## Runde 217: Device-Login-Textfelder blockieren C1

Fokus: `src/codex_usage/profile_login.py`, Codex-Befehl und erwartete
Backend-Account-ID im Device-Login.

Zehn reproduzierbare Fehlerfälle gefunden: C1-Steuerzeichen `U+0080…U+0084`
und `U+0086…U+0090` wurden in beiden Eingabefeldern akzeptiert; die bisherigen
Grenzen sperrten nur C0 und DEL. Solche Werte konnten in Subprozess-Argument
und Identitätsvergleich gelangen.

Fix: Beide Grenzen verlangen echte `str`-Werte und weisen den kompletten
Bereich `0x7F…0x9F` zusätzlich zu C0/Whitespace zurück. UTF-8-, Längen- und
Prozessisolationsregeln bleiben unverändert.

Verifikation: **10 RED-Regressionstestfälle** vor Fix reproduziert; danach
**99 `tests/test_profile_login.py`-Tests**, 98%-Branch-Coverage im
Profile-Login-Modul, **166 gekoppelte Profile-CLI-/CLI-Tests**, `py_compile`,
Ruff und `git diff --check` grün.

## Runde 218: Tokendelta-Zähler über Reset-Grenzen

Fokus: `src/codex_usage/consumption.py`, Rohverbrauch, EMA und Reset-Erkennung
für Token- und Creditverbrauch.

Zwei reproduzierbare Fehlerfälle gefunden: Nach Verbrauch im alten Zyklus
blieb dieser Wert im Akkumulator, wenn ein geplanter Reset auf einen neuen
Resetdatensatz wechselte. Bei einem ungeplanten Reset ohne Metadaten wurde die
Rückkehr auf 100 % als gewöhnlicher fallender Zähler behandelt; der alte
Verbrauch blieb dadurch ebenfalls im Tokendelta.

Fix: Bestätigte Reset-Grenzen setzen den Verbrauch auf den aktuellen Wert des
neuen Zyklus statt ihn zu addieren. Ein positiver Zähler, der ohne Metadaten
auf exakt null fällt (Limit wieder 100 %), gilt als beobachteter Reset und
behält vollständige Datenabdeckung. Die EMA verwirft bei derselben Grenze ihre
alte Rate. Damit starten Tokendelta, Tokenende und Creditverbrauch je Fenster
mit jedem geplanten oder ungeplanten Reset neu.

Verifikation: **89 `tests/test_consumption.py`-Tests**, **568 gekoppelte
Consumption-/History-/Integration-Snapshot-Tests** bestanden; JSON-Schema,
`git diff --check` und fokussierte CLI-/Integrationstests grün.

## Runde 219: History-Reset-Generation blockiert DEL

Fokus: `src/codex_usage/history.py`, Metadaten-Grenze von `UsageSample`.

Ein reproduzierbarer Fehler: `reset_generation` wies C0-Steuerzeichen und
nicht-ASCII-C1-Zeichen ab, akzeptierte aber DEL (`U+007F`). Dadurch konnte ein
Steuerzeichen in der Reset-Generation persistiert und bis in Diagnose-/History-
Ausgaben weitergereicht werden.

Fix: `UsageSample` weist jetzt auch `U+007F` zurück. Bestehende ASCII- und
Längenregeln bleiben unverändert.

Verifikation: **172 `tests/test_history.py`-Tests**, Mypy, Ruff,
Python-Compile und `git diff --check` grün.

## Runde 220: Credits-State vollständig reset-/cache-sicher

Fokus: `src/codex_usage/state.py`, top-level `AccountUsage.credits` in
Snapshot-Laden, Reset-Ablauf und Partial-Merges.

Fünf reproduzierbare Lücken gefunden: abgelaufene Credit-Fenster wurden nicht
verworfen; beschädigte Credit-Slots und ungültige Credit-Werte markierten den
Cache nicht als stale; unvollständige Backend-Provenance ließ Credits stehen;
Partial-Merges übernahmen fehlende Credits nicht; Login-required löschte nur
Token-/Pool-Fenster. Zusätzlich ignorierte die Block-Expiry-Prüfung ein noch
vorhandenes Credit-Fenster.

Fix: Credit-Fenster durchlaufen jetzt dieselben Ablauf-, Parsing-,
Provenance-, Merge- und Terminal-Clear-Pfade wie übrige Limitwerte. Resetlose
Credit-Fenster werden beim Browser-Merge ebenfalls als Resetless-Signal
berücksichtigt.

Verifikation: **384 `tests/test_state.py`-Tests**, Mypy, Ruff,
Python-Compile, JSON-Schema-Parse und `git diff --check` grün.

## Runde 221: Applet-Credit-Cache als echter Cachewert

Fokus: `files/codex-usage@H234598/applet.js`, Cache-/Browser-Merge-Helfer.

Zwei gekoppelte Lücken: Ein Credit-only-Cache wurde bei Backend-/Identitäts-
Konflikten nicht als vorhandener Cache erkannt; ein resetloser Browser-Credit-
Wert konnte deshalb nicht dieselbe Schutzlogik wie Tokenfenster auslösen.

Fix: `credits` zählt jetzt in `_hasCachedWindows` und
`_hasResetlessBrowserUsage` als Limitfenster.

Verifikation: **650 `tests/applet_runtime.test.js`-Tests** grün.

## Runde 222: Usage-Limits-Partial-Merge und Identitätsgrenzen

Fokus: `src/codex_usage/usage_limits.py`, App-Server-Partial-Snapshots,
Spark-/Katalog-Identitäten und Legacy-Fensterprojektion.

Fünfzehn reproduzierbare Fälle gefunden: Ein partieller verschachtelter
`rateLimitsByLimitId.codex`-Slot ersetzte einen vollständigen Top-Level-Slot
und verlor dadurch Resetdaten. Ungültige verschachtelte Felder (`usedPercent`,
`resetsAt`, `windowDurationMins`) konnten gültige Top-Level-Werte löschen.
Zusätzlich wurden partielle gültige Slots nicht feldweise zusammengeführt;
ein verschachteltes `rateLimitReachedType` musste dabei erhalten bleiben.
String-Subklassen konnten über `casefold()` oder gefälschte Gleichheit Spark-
Identitäten, Katalogeinträge und erreichte Limittypen vortäuschen. Ein
fehlerhafter Pool-Hook ließ `legacy_windows()` ungefangen abbrechen.

Fix: Nested-App-Server-Fenster werden nach Feld validiert und nur mit
vorhandenen, nicht-`None`-Werten über den Top-Level-Slot gelegt. Ungültige
Nested-Felder markieren den Hauptpool nicht verfügbar, lassen aber den letzten
gültigen Top-Level-Slot sichtbar. Nicht-Fenster-Metadaten bleiben beim Merge
erhalten. Identitäts-, Katalog-, Limittyp- und Provenance-Werte verlangen jetzt
echte `str`-/`tuple`-Werte; `legacy_windows()` fällt bei fehlerhaften Pools
sicher auf leere Slots zurück.

Verifikation: **184 `tests/test_usage_limits.py`-Tests**, **762 gekoppelte
Usage-Limits-/App-Server-/Direct-Tests**, 98%-Branch-Coverage im
Usage-Limits-Modul, Mypy, Ruff, Python-Compile und `git diff --check` grün.

## Runde 223: Reset-Zähler-Provenance

Fokus: `src/codex_usage/usage_resets.py`, Zusammenführung von Legacy-,
App-Server- und kanonischen Reset-Zählern.

Zwölf reproduzierbare Fälle gefunden: Ein Top-Level-`available` wurde neben
`resets`, `available_resets`, `usage_resets` oder
`rateLimitResetCredits` ignoriert. Dadurch konnte ein widersprüchlicher
Top-Level-Wert unbemerkt durch eine andere Quelle ersetzt werden. Selbst bei
gleichem Wert wurde die unvollständige Top-Level-Form akzeptiert und wich vom
Applet-Parser ab. Legacy-Mappings mit `known` oder `redeem_capability` wurden
auf `available` reduziert; widersprüchliche Metadaten blieben dadurch
unsichtbar.

Fix: Unvollständige Top-Level-`available`-Daten bleiben außerhalb eines
vollständigen kanonischen Zustands unbekannt. Alle Legacy-Quellen müssen
untereinander konsistent sein. Legacy-Mappings mit kanonischen Metadaten
werden verworfen; vollständige `usage_resets`-Duplikate werden weiterhin
explizit verglichen.

Verifikation: **52 `tests/test_usage_resets.py`-Tests**, **1417 gekoppelte
Reset-/State-/App-Server-/Direct-/Bridge-/Model-Tests**, 98%-Branch-Coverage
im Reset-Modul, Mypy, Ruff, Python-Compile und `git diff --check` grün.

## Runde 224: Identitätsgruppen unabhängig von Antwortreihenfolge

Fokus: `src/codex_usage/identity.py`, Zusammenführung partieller Backend-
Identitäten.

Ein reproduzierbarer Fehler: Eine Antwort mit nur `user_id` und eine zweite
mit nur `account_id` wurden als getrennte Gruppen angelegt, wenn die vollständige
`user_id`/`account_id`-Antwort erst danach eintraf. Die vollständige Antwort
konnte dann nur mit einer der beiden Gruppen verbunden werden. Je nach
Antwortreihenfolge wurde derselbe Account deshalb fälschlich als mehrere
Backend-Accounts abgelehnt.

Fix: Jede neue Identität verbindet jetzt alle bereits kompatiblen Gruppen in
einem Schritt. Identitätsfelder werden dabei vereinigt; Kandidatenreihenfolge
bleibt für die Rückgabe erhalten. Widersprüchliche vollständige Identitäten
bleiben getrennte Gruppen und werden weiterhin fail-closed behandelt.

Verifikation: **72 `tests/test_identity.py`-Tests** inklusive zweier
Reihenfolge-Regressionen bestanden; zusätzlich exhaustive Drei-Kandidaten-
Permutation ohne reihenfolgeabhängiges Ergebnis, Ruff und `git diff --check`
grün.

## Runde 225: Reset bei identischem Capture-Zeitpunkt

Fokus: `src/codex_usage/consumption.py`, Rohverbrauch und EMA nach Reset.

Zwei zusammenhängende reproduzierbare Lücken: Wenn der Provider den alten
Verbrauch und den auf 100 % zurückgesetzten Wert mit identischem
`captured_at`-Zeitpunkt meldete, wurde die Nullzeit-Lücke vor der Resetprüfung
verworfen. Der Tokendelta-/Creditverbrauch behielt dadurch den alten Zyklus.
Die EMA übersprang denselben Reset ebenfalls und mischte die alte Rate in die
neue Prognose.

Fix: Bestätigte Resetgrenzen werden vor der `gap <= 0`-/Großlückenprüfung
ausgewertet. Rohzähler und EMA verwerfen damit auch bei doppeltem Zeitstempel
ihre alte Zyklushistorie; die Beobachtung bleibt bei nichtpositiver Lücke
weiterhin als `partial` markiert.

Verifikation: **92 `tests/test_consumption.py`-Tests** inklusive zweier
Reset-/EMA-Regressionen bestanden; fokussierte History-/CLI-/Integration-
Tests, Ruff, Mypy, Python-Compile und `git diff --check` folgen vor Commit.

## Runde 226: History-Resetwerte und Capture-Zeit

Fokus: `src/codex_usage/history.py`, Persistenz von Consumption-Samples.

Zwei reproduzierbare Lücken: Die Tabelle behandelte denselben
`captured_at_ms`-Schlüssel mit `INSERT OR IGNORE`; ein neuer Resetwert bei
identischem Capture-Zeitpunkt wurde verworfen. Der bereits gespeicherte alte
Zyklus blieb dadurch nach Neustart aktiv. Zusätzlich wurde ein
`values_captured_at`, das nach `AccountUsage.captured_at` lag, unverändert als
History-Zeitpunkt übernommen und konnte zukünftige Verbrauchsbeobachtungen
erzeugen.

Fix: Gleichschlüssel-Upserts übernehmen geänderte Nutzungs-, Reset- und
Provenancefelder, bleiben bei identischen Samples idempotent und melden nur
geänderte Zeilen. History verwendet `values_captured_at` nur, wenn es nicht
nach dem äußeren Capture-Zeitpunkt liegt; sonst fällt es auf
`captured_at` zurück.

Verifikation: **174 `tests/test_history.py`-Tests** inklusive zweier
Regressionen und **92 Consumption-Tests** bestanden; Mypy, Ruff, Python-
Compile und `git diff --check` folgen vor Commit.

## Runde 227: Geplanter Reset mit unverändertem Resetziel

Fokus: `src/codex_usage/consumption.py`, Rücksetzung von Tokendelta,
Creditverbrauch und EMA.

Ein reproduzierbarer Fehler: Wenn ein Provider den geplanten Resetzeitpunkt
auch im ersten Messpunkt nach dem Rollover unverändert meldete, wurde die
Resetgrenze wegen des gleichen `reset_at` verworfen. Ein neuer Zyklus mit
höherem Startverbrauch wurde dadurch als normale Delta-Steigerung des alten
Zyklus gezählt.

Fix: Das Überschreiten eines zuvor gültigen geplanten Resetzeitpunkts gilt
auch bei unverändertem `reset_at` als Zyklusgrenze. Rohzähler und EMA nutzen
gemeinsame Reset-Erkennung; Rückkehr auf 100 % Limit bleibt weiterhin die
metadatenlose Resetgrenze.

Verifikation: **93 `tests/test_consumption.py`-Tests**, **312 gekoppelte
History-/CLI-/Integration-Tests**, 100%-Statement- und Branch-Coverage im
Consumption-Modul, Mypy, Ruff, Python-Compile und `git diff --check` grün.

## Runde 228: Verschachtelte App-Server-Metadaten

Fokus: `src/codex_usage/usage_limits.py`, Merge von Top-Level- und
`rateLimitsByLimitId.codex`-Daten.

Ein reproduzierbarer Fehler: Ein verschachteltes `rateLimitReachedType: null`
überschrieb den gültigen Top-Level-Status. Dadurch wurde ein erschöpftes
Limit als nicht erreicht dargestellt, obwohl die Fensterdaten korrekt waren.

Fix: Nicht-null Metadaten aus dem verschachtelten Bucket überschreiben die
Top-Level-Angabe; `null` lässt den bereits vorhandenen Wert unverändert.
Fensterfelder behalten weiterhin ihr partielles Merge- und Fail-Closed-Verhalten.

Verifikation: **185 `tests/test_usage_limits.py`-Tests**, 98%-Branch-Coverage
im Usage-Limits-Modul, Mypy, Ruff, Python-Compile und `git diff --check` grün.

## Runde 229: C1-Steuerzeichen in Snapshot-Fensternamen

Fokus: `src/codex_usage/state.py`, Laden persistierter Limit-Fenster.

Zwei reproduzierbare Eingaben (`U+0080` und `U+009F`) passierten die
Fensternamenvalidierung. C0 und DEL wurden bereits verworfen, der übrige
C1-Bereich jedoch nicht. Der Name kann später in Status-, Hover- oder
Klickausgaben landen.

Fix: Snapshot-Fensternamen weisen jetzt den kompletten Bereich `0x7F…0x9F`
zusätzlich zu Whitespace und C0 zurück. Bestehende Identitäts- und
Längenregeln bleiben unverändert.

Verifikation: **386 `tests/test_state.py`-Tests** inklusive zweier C1-
Regressionen bestanden; State-Coverage weiterhin 99%, Mypy, Ruff,
Python-Compile und `git diff --check` folgen vor Commit.

## Runde 230: C1-Steuerzeichen in Snapshot-Identitäten

Fokus: `src/codex_usage/state.py`, optionale Pool- und Model-Identitäten aus
persistierten Snapshots.

Zwei reproduzierbare Eingaben (`U+0080` und `U+009F`) passierten die
Identitätsvalidierung. Solche Steuerzeichen können als Snapshot-Schlüssel in
spätere Status- und Zuordnungslogik gelangen und dort Darstellung oder
Vergleiche verfälschen.

Fix: Optionale Snapshot-Identitäten weisen jetzt den kompletten Bereich
`0x7F…0x9F` zusätzlich zu Whitespace und C0 zurück. Längen- und
Typvalidierung bleiben unverändert.

Verifikation: **388 `tests/test_state.py`-Tests**, **961 gekoppelte
State-/Scheduler-/Service-/Snapshot-Tests**, State-Coverage weiterhin 99%,
Mypy, Ruff, Python-Compile und `git diff --check` grün.
