# codex-usage v2: Verbrauch, Profile und attestierter Integrationsvertrag

## Autorität und Sicherheitsgrenze

Codex Usage ist alleinige Autorität für aktuelle Accountlimits, lokale
Raw-History, Resetgenerationen, EMA-Berechnung und den lokalen Usage-Cache. Der
Integrationsproducer veröffentlicht ausschließlich berechnete, sanitierte
Evidenz. Er schaltet keinen Fast Mode, startet keine Agents und verändert
keinen Masterjet-, Applet- oder Reminderzustand.

Der einzige Producer-Aufruf ist der attestierte Release-Launcher mit exakt:

```text
integration-snapshot --schema 2 --format json
```

Andere Argumente, PATH-Auflösung, ein allgemeiner `codex-usage`-CLI-Aufruf,
Schema-1-Fallback und erneutes Lesen alter Snapshotquellen sind unzulässig.
Einzige Consumerquelle sind der separate Pointer `current.json` und die von
ihm referenzierte immutable V2-Generation mit exakt `account-usage-v2.json`,
`pool-authority-v2.json` und `account-usage-v2.binding.json`.
Der frühere feste V1-Cachepfad ist nach dem Cutover keine Consumerquelle;
es gibt weder Legacy-Read noch Dual-Write.

## Exaktes Schema 2

Top-Level erlaubt exakt `schema_version`, `generated_at`, `accounts`.
`schema_version` ist der exakte Integer `2`; bool, float, String und unbekannte
Felder werden abgelehnt. JSON wird mit sortierten Schlüsseln, kompakten
Trennzeichen, ASCII-Escaping und ohne NaN/Inf kanonisch serialisiert.

```yaml
schema_version: 2
generated_at: "UTC timestamp"
accounts:
  - account_id: "[A-Za-z0-9_.-]{1,64}"
    freshness:
      captured_at: "UTC timestamp"
      fresh_until: "UTC timestamp"
      stale: false
    limits:
      - pool: "printable bounded source pool key"
        window_seconds: 18000
        used_percent: 25.0
        remaining_percent: 75.0
        reset_at: "optional UTC timestamp"
    status: "ok|partial|error|login_required|unknown"
    tracker_evidence:
      - coverage: "complete|partial|insufficient|stale"
        ema_time_constant_seconds: 3600
        first_sample_at: "UTC timestamp"
        last_sample_at: "UTC timestamp"
        limit_window_seconds: 18000
        pool: "main|gpt-5.3-codex-spark"
        projected_used_percent_at_reset: 26.8
        rate_percentage_points_per_second: 0.0001
        reset_generation: "printable bounded opaque token"
        sample_count: 2
```

Feld-Allowlist:

- Account: exakt `account_id`, `freshness`, `limits`, `status`,
  `tracker_evidence`.
- Freshness: exakt `captured_at`, `fresh_until`, `stale`.
- Limit: exakt `pool`, `window_seconds`, `used_percent`,
  `remaining_percent`; optional nur `reset_at`.
- Tracker-Evidenz: exakt die zehn oben gezeigten Felder; keine optionalen
  Felder.
- Fenster-Allowlist für Limits und Trends: exakt `18000`, `604800`,
  `2592000` Sekunden. `2592000` ist das explizite begrenzte 30-Tage-/Credit-
  Sonderfenster.
- Limit-Pools kommen nur aus den validierten Source-Pools und sind druckbare
  ASCII-Tokens mit 1 bis 64 Zeichen, ohne lokale Pfadform. Tracker-Evidenz wird
  ausschließlich für `main` und `gpt-5.3-codex-spark` erzeugt. `credits` darf
  als Limit-Pool vorkommen, nie als Trackertrend.
- Die Credit-Repräsentationsart wird ausschließlich aus den explizit
  vorhandenen `used`-, `remaining`-, `limit`- und Prozentfeldern bestimmt,
  nicht aus einer generisch abgeleiteten `remaining_percent`-Property. Jeder
  endliche nichtnegative Scalar-`remaining`-Wert ohne `limit` und ohne
  explizites Prozentfeld ist unabhängig vom Zahlenbereich ein denominatorloser
  Absolutbetrag. Das gilt insbesondere für `0`, `12`, `80`, `100`, `100.01`
  und `794`. Im denominatorlosen Zweig ist ausschließlich dieses einzelne
  logische `remaining` zulässig; zusätzliches `used` ohne expliziten Nenner
  kann nicht korreliert werden und ist invalid.
- Denominatorlose absolute Credits sind im ausschließlich prozentualen
  V2-Schema nicht darstellbar. Snapshot und History lassen deshalb Credit-
  Limit und Credit-Sample aus; valide Main-Fenster desselben Accounts und
  valide weitere Accounts bleiben veröffentlichbar. Weder Rohbetrag noch
  erfundener Nenner oder Credit-Trend werden serialisiert. Explizite Prozente
  und konsistente limitbasierte Fenster behalten ihre exakte Projektion.
- Eine vollständig fehlende optionale Creditquelle bleibt `None`. Eine
  vorhandene negative, nicht endliche, unparsebare oder widersprüchliche
  Quelle wird dagegen als sanitisiertes Invalid-Sentinel ohne Providerrohwert
  durch den kanonischen State-Roundtrip erhalten. Snapshot und Entrypoint
  stoppen dann den gesamten Multi-Account-Publish vor einem neuen
  `current.json`-Commit. Die Creditvalidierung läuft vor statusabhängiger
  Limitunterdrückung und kann daher auch bei `blocked`, `error` oder jedem
  anderen Accountstatus nicht umgangen werden. Invalide Main-Quellen bleiben
  ebenfalls fail-closed.
- Der Adapter sammelt sämtliche erkannten nativen Creditkandidaten aus den
  Top-Level-Aliassen, beiden `rateLimits`-/`rate_limits`-Containern, jedem
  bounded `rateLimitsByLimitId`-Eintrag und `account.credits`. Es gelten die
  vorhandene kleine JSON-Grenze von höchstens 50 variablen Containern und
  höchstens 50 Kandidaten; der jeweils 51. Eintrag wird ohne weitere
  Materialisierung als invalid verworfen. Jeder Kandidat wird einzeln
  validiert, danach müssen Repräsentationsart, Projektion und alle expliziten
  `used`-, `remaining`-, `limit`- und Prozentangaben vollständig
  übereinstimmen. Eine zusätzliche invalide oder widersprüchliche Quelle kann
  nicht durch Branchpriorität verdeckt werden.
- Innerhalb jeder numerischen Aliasgruppe wird jeder vorhandene Alias vor
  ULP-Vergleich und Reduktion einzeln in seiner Domain validiert:
  `used`/`consumed` sowie `remaining`/`available`/`balance`/`credit_balance`
  sind endlich und nichtnegativ; `limit`/`total`/`maximum` ist endlich und
  strikt positiv; `percent`/`remaining_percent`/`remainingPercentage` ist
  endlich und liegt in `[0,100]`. Erst danach dürfen valide Aliasse mit der
  Vier-ULP-Regel korreliert werden. Ein valider Alias maskiert niemals einen
  vorhandenen invaliden Alias.
- Innerhalb jedes Creditkandidaten werden beide anerkannten Reset-Aliasse
  `reset_at` und `resetAt` gesammelt. Jeder vorhandene Wert muss ein
  whitespace-exakter, höchstens 64 Zeichen langer ISO-Zeitstempel mit
  expliziter Zeitzone sein. Mehrere Aliasse dürfen verschiedene UTC-Offsets
  verwenden, müssen aber denselben Instant bezeichnen. Malformed, naive oder
  widersprüchliche Resetwerte erzeugen das sanitierte Invalid-Sentinel;
  vollständige Reset-Abwesenheit bleibt optional. Die Kandidat-zu-Kandidat-
  Korrelation vergleicht weiterhin den normalisierten UTC-Instant.
- Float-Konsistenz wird in beide Richtungen anhand exakt vier darstellbarer
  `nextafter`-Schritte geprüft; der fünfte Schritt ist ein Konflikt. Für
  `0 <= used <= limit` und `0 <= remaining <= limit` wird immer das echte
  Verhältnis berechnet. Nur eine tatsächliche geringe Überschreitung darf bis
  zur Vier-Schritt-Grenze auf den Endpoint normalisiert werden. Es gibt keinen
  In-Range-Clamp, keinen ergänzten Nenner und kein erfundenes Prozentfeld. Ein
  gültiger Payload kann nach bestehender Reader-Policy wegen fehlender
  Trend-Evidenz weiterhin `partial` sein.
- Prozentwerte und Projektionen sind endlich und in `[0,100]`.
  `used_percent + remaining_percent` muss mit ausschließlich absoluter
  Toleranz `1e-9` und relativer Toleranz `0` genau `100` sein. Die Rate ist
  endlich und in `[0,100]` Prozentpunkten pro Sekunde.
- Account-IDs sind maximal 64 Zeichen, Pool-Keys maximal 64 Zeichen,
  Resetgenerationen maximal 128 Zeichen und kanonische UTC-Zeitstrings maximal
  64 Zeichen.
- Maximal 100 Accounts, 32 Limits und 32 Trends pro Account, 3200
  Trackerreihen, 500000 Samples pro geladener Reihe und 2 MiB kanonisches
  Dokument. Jede Quelliteration wird höchstens bis `MAX+1` gelesen.
- Doppelte Account-IDs, doppelte `(pool, window_seconds)`-Limits und doppelte
  `(pool, limit_window_seconds)`-Trends werden abgelehnt. Trends müssen ein
  gleichnamiges Limit mit zukünftigem `reset_at` besitzen.
- `error`, `login_required` und `unknown` dürfen weder Limits noch Trends
  tragen. Unbekannte oder secretähnliche Schlüssel, lokale Pfade, Bearer/JWT,
  Private Keys und nicht erlaubte Werttypen werden abgelehnt.

## Nichtgeheimes, handabgeleitetes Golden Example

Zwei positive Samples derselben Main-/5h-/Resetgeneration liegen um 09:00 und
10:00 UTC. Verbrauch steigt von 24,64 auf 25,00 Prozentpunkte. Damit ist die
Intervallrate `(25,00 - 24,64) / 3600 = 0,0001` Prozentpunkte pro Sekunde. Bis
zum Reset um 15:00 verbleiben ab letztem Sample 18000 Sekunden; Projektion ist
`min(100, 25,00 + 0,0001 * 18000) = 26,8`.

```json
{
  "accounts": [
    {
      "account_id": "alpha",
      "freshness": {
        "captured_at": "2026-08-15T10:00:00Z",
        "fresh_until": "2026-08-15T10:15:00Z",
        "stale": false
      },
      "limits": [
        {
          "pool": "main",
          "remaining_percent": 75.0,
          "reset_at": "2026-08-15T15:00:00Z",
          "used_percent": 25.0,
          "window_seconds": 18000
        }
      ],
      "status": "ok",
      "tracker_evidence": [
        {
          "coverage": "complete",
          "ema_time_constant_seconds": 3600,
          "first_sample_at": "2026-08-15T09:00:00Z",
          "last_sample_at": "2026-08-15T10:00:00Z",
          "limit_window_seconds": 18000,
          "pool": "main",
          "projected_used_percent_at_reset": 26.8,
          "rate_percentage_points_per_second": 0.0001,
          "reset_generation": "main-5h-r7",
          "sample_count": 2
        }
      ]
    }
  ],
  "generated_at": "2026-08-15T10:05:00Z",
  "schema_version": 2
}
```

Das Beispiel enthält keine Raw-Samples; die beiden Werte stehen nur in der
Herleitung. Kanonische Transportbytes sind dieselbe Struktur kompakt und mit
sortierten Schlüsseln.

## EMA60, Zeitbasis und Projektion

Zeitbasis sind UTC-Samplezeitpunkte in Sekunden. Für jedes positive Intervall
`i` derselben Account-/Pool-/Fenster-/Resetgeneration gilt:

```text
delta_t_i = captured_at_i - captured_at_(i-1)
rate_i    = (used_i - used_(i-1)) / delta_t_i
alpha_i   = 1 - exp(-delta_t_i / 3600)
ema_1     = rate_1
ema_i     = ema_(i-1) + alpha_i * (rate_i - ema_(i-1))
projected = min(100, used_last + ema_last * (reset_at - captured_at_last))
```

`ema_time_constant_seconds` ist immer exakt `3600`. Das ist eine
zeitgewichtete exponentielle Rate mit 60-Minuten-Zeitkonstante, kein
60-Minuten-Lookback, Median oder arithmetischer Mittelwert. Intervalle müssen
strikt chronologisch, positiv und höchstens 3600 Sekunden lang sein. Rate und
Projektion werden erst nach Endlichkeits- und Bereichsprüfung veröffentlicht.

## Reset, Coverage, Freshness und Poolisolation

- Nur die letzte belegte `reset_generation` wird verwendet; ältere
  Generationen werden vollständig abgeschnitten. Alle verwendeten Samples
  brauchen dasselbe Account-, Pool-, Fenster-, `reset_at`- und
  Resetgenerations-Tupel.
- Duplikate, Zeitrücklauf, Zukunftssamples, nichtpositive Deltas,
  Zählerrückgang ohne belegten Reset, Gap über 3600 Sekunden, fehlender oder
  bereits abgelaufener Reset und unbekannte Pool-/Fensterkombination ergeben
  keinen aktivierbaren Trend.
- Ein einzelnes valides Sample kann nur `coverage=insufficient`, Rate `0` und
  die aktuelle Nutzung als Projektion liefern. Mindestens zwei valide Samples
  sind für einen positiven Trend erforderlich.
- `coverage=complete|partial` verlangt mindestens zwei valide Samples und ein
  letztes Sample, das höchstens exakt 900 Sekunden alt ist. Bei mehr als 900
  Sekunden ist ausschließlich `stale` zulässig; bei höchstens 900 Sekunden
  wird `stale` abgelehnt. Diese Beziehung wird beim kanonischen Serialisieren
  und erneut vor dem Publish geprüft. Ein einzelnes `insufficient`-Sample
  behält seine eigene Semantik unabhängig vom Alter. Der aktuelle Producer
  erzeugt `partial` für automatische Trends nicht. Automatische
  verbrauchs-/trendbasierte Consumeraktionen sind ausschließlich bei
  `complete` zulässig. Accountlokale PoolAuthority folgt zusätzlich der unten
  beschriebenen restriktiven Projektion.
- `captured_at` stammt aus `values_captured_at`, sofern vorhanden, sonst aus
  dem echten Usage-Capture. `fresh_until = captured_at + 900 Sekunden`.
  `stale` ist wahr, wenn die Quelle stale meldet oder `generated_at` nach
  `fresh_until` liegt. `generated_at` allein darf Freshness nie verlängern.
- Main und Spark sind getrennte Identitäten. Samples, Limits und Trends werden
  nie zwischen `main` und `gpt-5.3-codex-spark` ersetzt oder zusammengeführt.

Raw-History bleibt ausschließlich in der privaten lokalen SQLite-Datenbank.
Der Producer exportiert keine Sampleliste, Labels, Backends, Providerantworten,
Prompts, Agentnamen oder Plantexte.

## Gebundene Ergebnis- und Fehlercodes

| Exit | Öffentlicher Token | Begrenzte Bedeutung |
| ---: | --- | --- |
| 0 | kanonisches V2-JSON auf stdout | Erfolg |
| 64 | `integration_snapshot_invalid_arguments` | `invalid` |
| 65 | `integration_snapshot_invalid_source` | `invalid` |
| 69 | `integration_snapshot_unavailable` | `unavailable` |
| 70 | `integration_snapshot_secure_io_failed` | `unavailable` an Secure-I/O-Grenze |
| 75 | `integration_snapshot_busy` | `busy` |
| 0 | `integration_producer_install_ok` / `integration_producer_rollback_ok` | Installererfolg |
| 64/69 | `integration_producer_unavailable` | `invalid` / `unavailable` |
| 70 | `integration_producer_cleanup_failed` | begrenzter Cleanupfehler |

`stale` und `partial` sind gebundene Datenqualitätszustände im Dokument, keine
Rohfehlerausgaben. Tokens enden mit genau einem Newline. Exceptions,
Terminalausgaben, Providerantworten, Credentials und lokale Pfade werden nicht
ausgegeben.

## Release 0.6.537 und Attestierung

Projekt, Producer-Wheel, Dist-Info, Manifest und Runtime-Attestierung tragen
gemeinsam Version `0.6.537`. Das aktive Manifest hat exakt Integer-Schema `2`.
Es bindet Release-ID und Source-Manifest-SHA-256 sowie die SHA-256-Werte von
Entry Point, Wheel, RECORD, Launcher und gesamtem Releasebaum.

Jedes aktuelle, vorherige und nur für Upgrade lesbare Manifest erlaubt exakt
die folgenden 16 Felder und keine weiteren, auch keine secretähnlichen
Erweiterungen:

```text
schema_version, version, release_id, source_manifest_sha256,
state_home, data_home, release_dir, launcher_path, entrypoint_path,
wheel_path, record_path, entrypoint_sha256, wheel_sha256, record_sha256,
launcher_sha256, release_tree_sha256
```

Fehlende oder unbekannte Felder werden unmittelbar nach dem bounded/no-follow
Lesen abgelehnt, bevor Versions-, Pfad- oder Hashwerte ausgewertet werden.

Kanonische Pfade innerhalb des privaten Releasebaums sind fest:

```text
producer.whl
venv/bin/codex-usage
venv/lib/python*/site-packages/codex_usage/integration_entrypoint.py
venv/lib/python*/site-packages/codex_usage_integration_producer-0.6.537.dist-info/RECORD
```

Launcher, Wheel oder Dist-Info unter alternativen Pfaden werden auch bei
passenden Einzelhashes abgelehnt. RECORD bindet jedes Wheelmitglied; Metadata
bindet Distribution `codex-usage-integration-producer` und Version `0.6.537`.
Der Releasebaumhash umfasst sortiert jeden no-follow Verzeichnis-/Dateieintrag
mit Typ, relativem Pfad, Modus, Dateigröße und Datei-SHA-256. Symlinks,
Hardlinks, fremde Owner, falsche Modi, Sonderdateien, Device-/Inodewechsel,
Races, zusätzliche oder fehlende Einträge schlagen fehl.

Runtimeattestierung und Rollback akzeptieren nur `0.6.537`/Schema 2.
Ausschließlich der Installer darf beim atomaren Cutover vollständig
hash-/RECORD-/Baum-attestierte `0.6.536`/Schema-2-Generationen als exakt
enumerierte Upgradequelle lesen. Bei dieser Vorgängerprüfung wird allein ein
producer-eigenes `codex_usage/__pycache__` außerhalb des attestierten
Baumhashs zugelassen: jedes enthaltene Bytecodefile muss privat, regulär,
einfach verlinkt, größenbegrenzt, zur laufenden CPython-Version passend und
über seinen exakten Namen einer vorhandenen attestierten `.py`-Quelldatei
zugeordnet sein. Der Installer führt diese Dateien weder aus noch löscht oder
übernimmt er sie; unbekannte Cache-Namen, Cacheorte und sonstige Zusätze
bleiben fail-closed. Die `0.6.537`-Runtime- und Releasebaumprüfung besitzt
diese einmalige Cutover-Ausnahme nicht. Unter denselben Release→Current-EX-Locks
klassifiziert er den vorhandenen Evidencebestand ausschließlich für dessen
Pensionierung: kanonischer Pointer-Schema-1-Record, exakt gebundene
Binding-Schema-1-/Usage-Zwei-Dateien-Generationen, private Metadaten sowie
Manifest-, Release-, Source-, Payload- und Binding-Digests müssen zum
attestierten `0.6.536`-Active passen. Erst danach werden `current.json` und der
gesamte alte `generations/`-Baum in eine gebundene Cutover-Transaktionslage
verschoben und durch einen leeren privaten `generations/`-Baum ersetzt.

Der anschließende atomare Active-Swap ist der Cutover-Commitpunkt. Jeder
normale Fehler davor oder währenddessen stellt Active, Current und den alten
Generationsbaum byte- beziehungsweise inodegebunden wieder her. Nach Erfolg
werden die pensionierten Artefakte entfernt; `current.json` bleibt bis zum
ersten dreiteiligen `0.6.537`-Publish absichtlich abwesend. Nach einem Hard
Crash stellt ausschließlich der nächste Installerlauf vor einem erneut strikt
attestierten `0.6.536`→`0.6.537`-Cutover einen exakt korrelierten, privaten und
vollständig V1-validierten
`.evidence-v1-cutover-{current,generations}-<32hex>`-Zustand wieder her. Dabei
wird höchstens der nachweislich zugehörige leere neue `generations/`-Baum
entfernt; unbekannte, unvollständige, widersprüchliche oder fremde Artefakte
stoppen ohne Mutation fail-closed. Dieser einmalige destruktive Installerpfad
ist kein Runtime-Reader-Compatpfad: Reader und Publisher akzeptieren weiterhin
ausschließlich Binding-Schema 2 und dreiteilige Generationen.

Ältere Releases werden als `previous.json` erhalten, sind aber weder
runtime-verifizierbar noch durch Rollback reaktivierbar. Es gibt keinen
generischen Altversionsfallback. Ein ungültiges oder nicht exakt als
`0.6.536` attestierbares Active oder ein abweichender Binding-V1-Bestand wird
nicht aus `previous.json`, Cache oder anderen Altpfaden repariert oder
gelöscht.

## V2-Evidence-Consumervertrag

Dieser Abschnitt ist maschinenbindender Producer-Handoff für Masterjet. Es gibt
keine Payload unter `data_home` und keine Legacy-Datei als Consumerquelle. Der
einzige relative Payloadpfad unter `state_home` lautet:

```text
codex-usage/integration/generations/<generation_id>/account-usage-v2.json
```

`<generation_id>` ist exakt 32 Kleinbuchstaben-HEX-Zeichen. Derselbe immutable
Generationsordner ist das einzige Generationbundle und enthält exakt diese
drei regulären Dateien:

```text
account-usage-v2.json
account-usage-v2.binding.json
pool-authority-v2.json
```

Der atomare Pointer liegt ausschließlich unter
`state_home/codex-usage/integration/current.json`; `generations/` liegt im
selben `integration/`-Verzeichnis. Jeder Generationordner bleibt unveränderlich
nach dem Publish. Rollback tauscht nur Pointer-current/previous; Audit und
Recovery behalten die referenzierten Ordner.

### Maschinenprüfbare Kette

1. Reader attestiert zuerst `active.json` vollständig gegen erwarteten
   Entry-Point: Schema 2, Version `0.6.537`, Release-ID, Source-Manifest,
   kanonische Pfade, Entry-Point, Wheel, RECORD, Launcher und Releasebaum.
   Das Ergebnis ist `VerifiedActiveManifest`, einschließlich
   `active_manifest_sha256`.
2. `current.json` ist kanonisches JSON, höchstens 4096 Byte, mit exakt fünf
   Feldern: `pointer_schema_version=1`, `current_generation_id`,
   `current_binding_sha256`, `previous_generation_id`,
   `previous_binding_sha256`. Beide vorherigen Felder sind gemeinsam `null`
   oder gemeinsam 32-HEX/64-HEX.
3. Der aktuelle Ordnername muss `current_generation_id` entsprechen.
   SHA-256 der kanonischen Binding-Bytes muss `current_binding_sha256`
   entsprechen.
4. Das äußere Binding ist kanonisches JSON, höchstens 32_768 Byte, mit exakt
   fünf Feldern: `binding_schema_version=2`, `pool_authority_filename`,
   `pool_authority_sha256`, `pool_authority_size_bytes`, `usage_binding`.
   `pool_authority_filename` ist exakt `pool-authority-v2.json`.
   `usage_binding` hat exakt zehn Felder:
   `usage_binding_schema_version=2`, `active_manifest_sha256`,
   `generation_id`, `payload_filename`, `payload_sha256`,
   `payload_size_bytes`, `published_at`, `producer_version`, `release_id`,
   `source_manifest_sha256`. `payload_filename` ist exakt
   `account-usage-v2.json`; `producer_version` ist `0.6.537`.
5. Usage-Binding-`generation_id`, `active_manifest_sha256`, `release_id` und
   `source_manifest_sha256` müssen exakt zu Ordner und
   `VerifiedActiveManifest` passen. Payloadgröße und SHA-256 müssen dem
   Usage-Binding entsprechen; danach wird das strikte Schema-2-Dokument erneut
   kanonisch validiert.
6. Danach liest der Reader `pool-authority-v2.json` bounded/no-follow. Größe
   und SHA-256 müssen dem äußeren Binding entsprechen. Die Projektion hat
   exakt neun Felder: `pool_authority_schema_version=2`,
   `producer_version=0.6.537`, `release_id`, `generation_id`, `issued_at`,
   `expires_at`, `usage_payload_sha256`, `usage_binding_sha256`,
   `authorities`. `usage_payload_sha256` bindet die exakten Usage-Bytes;
   `usage_binding_sha256` bindet die kanonischen Bytes des gesamten
   zehnfeldrigen `usage_binding`. Bereits der Producer-Builder verlangt
   kanonische Textgleichheit von Usage-`generated_at` und
   Usage-Binding-`published_at` und übernimmt ausschließlich dieses gebundene
   `published_at` als Authority-`issued_at`. Release, Generation und
   `issued_at` müssen beim Reader zusätzlich identisch zum Usage-Binding sein.
   Die Gültigkeit endet exklusiv bei `expires_at` und spätestens 15 Minuten
   nach `issued_at`.
7. Jeder Authority-Eintrag hat exakt `account_id`, `pool_id`, `provider`,
   `hive_available`, `allowed_model_families`, `reasoning_minimum`,
   `reasoning_maximum`, `allowed_lifecycles`,
   `persistent_leadership_eligible` und
   `long_running_leadership_eligible`. Einträge und Listen sind eindeutig und
   sortiert; Accountmenge und Usage-Accountmenge müssen exakt übereinstimmen.
   Der optional referenzierte previous Binding wird
   beim normalen Reader mindestens mit Pointer/Generation/Binding-Digest
   validiert. Rollback verlangt für die zu promotende Generation zusätzlich
   vollständige Bindung an das aktuell attestierte Release.

Nach einer gültigen Active-Release-Rotation bleiben ältere Generationen
Audit- und Retentionmaterial. Publisher und GC prüfen solche Current-,
Previous- und ungeschützten Generationen vollständig auf kanonischen Pointer,
Binding, Usage, Authority, Größe, Hash, Cross-Bindung und `published_at`,
verlangen aber keine
Übereinstimmung ihrer historischen Manifest-/Release-/Source-Digests mit dem
neuen Active. Neu publizierte Generation und normal gelesener Current bleiben
streng an das neue `VerifiedActiveManifest` gebunden. Rollback darf eine unter
Release A erzeugte Previous-Generation unter aktivem Release B nicht
promotieren.

Diese Retentionregel gilt für dreiteilige Binding-Schema-2-Generationen nach
dem Cutover. Der einmalige, oben beschriebene `0.6.536`→`0.6.537`-Übergang
pensioniert den Zwei-Dateien-/Binding-Schema-1-Bestand vollständig, damit er
die geschlossene V2-Namespaceprüfung nicht blockieren kann.

Jede fehlende, zusätzliche, nichtkanonische oder abweichende Bindung ist
`invalid`, niemals Fallback.

### Producer-owned Authority-Quelle und Consumer-Handoff

Authority wird ausschließlich aus der privaten Datei
`state_home/codex-usage/integration/pool-authority-source-v2.json`
übernommen. Der Installer erzeugt diese Datei absichtlich nicht. Vor dem
ersten Publish muss der zuständige Producer sie als owner-eigene reguläre
`0600`-Datei bereitstellen. Fehlen, Race, falscher Modus, mehr als 131_072
Byte, nichtkanonische Bytes oder unvollständige Accountabdeckung brechen den
Publish vor dem Pointercommit ab. Die Quelle hat exakt
`pool_authority_source_schema_version=2` und `authorities`; ihre Einträge sind
dieselben geschlossenen zehn Felder wie oben. Es werden weder `pool=main`,
Kontostand, Trends, Label noch Accountname in Eligibility übersetzt. Secrets,
Credentials, freie Erweiterungsfelder und Provider-/Pool-Mappings sind nicht
zulässig. Die Quelldatei selbst wird nie in den Generationordner kopiert.

Die Source-Accountmenge und die Usage-Accountmenge bleiben immer exakt gleich
und damit gemeinsam digestgebunden. Ein Account mit `status != ok`,
`stale=true`, abgelaufener Freshness oder widersprüchlicher Zeitbeziehung
verhindert jedoch nicht den Publish frischer anderer Accounts. Für jeden
betroffenen Account übernimmt der Producer keine positive Usage-Ableitung,
sondern klemmt ausschließlich restriktiv `hive_available`,
`persistent_leadership_eligible` und `long_running_leadership_eligible` auf
`false`. Seine Einträge bleiben zur Accountmengenbindung vorhanden, aber jede
Authority-Auswertung schlägt fail-closed fehl. Nur frische `ok`-Accounts
begrenzen `expires_at`; ohne einen solchen Account existiert eine frische,
vollständig geschlossene Projektion ohne positive Authority.

Consumer lesen keine Einzelfile und implementieren keine zweite Kette. Der
Producer-API-Einstieg `read_current_generation_bundle(...)` liefert Usage,
PoolAuthority und Binding nur aus demselben vollständig validierten
Generationbundle plus aggregiertem Usage-Status. `busy`, `unavailable`,
`invalid` oder jeder unbekannte Transportstatus liefern kein auswertbares
Bundle. `stale` oder `partial` können dagegen von einem anderen Account
stammen und sperren deshalb nicht pauschal einen frischen Account. Der
Consumer prüft die angeforderte Identität ausschließlich über
`evaluate_pool_authority(...)`; diese Funktion prüft exakt Account, Pool,
Provider, Modellfamilie, Reasoning-Grenzen, Lifecycle, Hive-Verfügbarkeit,
beide Leitungseignungen, Release, Generation, beide Usage-Digests und
Ablaufzeit. Der oben geschlossene Eintrag garantiert dabei für jeden
betroffenen stale/partial/unknown/nicht-`ok`-Account `false`. Es gibt keinen
V1-, Compat-, Cache-, Mapping- oder Fallbackpfad.

Die repo-eigenen, ausschließlich synthetischen Vertragsartefakte liegen unter
`tests/fixtures/pool_authority_v2/`: kanonische Source-, Usage- und positive
Authority-Bytes sowie `negative-vectors-v2.json`. Die Negativmatrix bindet
stale, Replay, Provider, Pool, Hive, Modell, Reasoning, Lifecycle, beide
Leitungseignungen, Release, Generation, Usage-Payload-Digest,
Usage-Binding-Digest, abweichendes Binding-`published_at` und partiellen
Accountstatus. Sie enthält keine realen Accountdaten oder Secrets.

### Synchronisation und sichere I/O

Reader nehmen beide persistenten `flock`-Inodes in dieser festen Reihenfolge:
erst Releaseziel `state_home/codex-usage/integration/producer-install`, dann
Pointerziel `state_home/codex-usage/integration/current.json`. Reader nehmen
beide `LOCK_SH`, Publish, Rollback, Staging-Recovery und GC beide `LOCK_EX`.
Nicht sofort verfügbare Locks ergeben `busy`; Runtime erzeugt fehlende Lockdateien
nie. Nur Installer-Bootstrap darf sie einmal anlegen.

Die Lockdateien liegen unter
`pwd.getpwuid(os.geteuid()).pw_dir/.local/state/codex-usage/locks/` als
`<sha256(os.fsencode(os.path.abspath(abs-ziel)))>.lock`. `HOME`, XDG-Werte und
reale UID wählen keinen Lockraum. Lockdateien sind der effektiven UID eigene
reguläre `0600`-Dateien mit Linkcount 1 und höchstens 4096 Byte. Lockroot und
kontrollierte Eltern ab passwd-Home sind reale, derselben effektiven UID
gehörende `0700`-Verzeichnisse. Vertrauensgrenze: Prozesse unter derselben
effektiven UID kooperieren; ein bösartiger Prozess derselben UID ist nicht
abgewehrt.

EntryPoint erwirbt beide EX-Locks vor Uhrzeit-, Quellen-, History-, Build- und
Serialisierungsschritt und hält sie durch Retention und Current-Commit. Der
interne Publisher erwirbt sie nicht erneut. Auch direkte Publisheraufrufe
verwerfen unter denselben Locks ein `generated_at` vor dem gültigen aktuellen
`published_at`; dafür wird historischer Current vollständig inhaltlich, aber
nicht gegen ein inzwischen rotiertes Active geprüft. Eine ältere Invocation
kann Current daher nicht zurücksetzen, während die erste Publikation nach
einer gültigen Release-Rotation möglich bleibt.

Nach `openat`/`dir_fd`-Traversal mit `O_NOFOLLOW` prüft Reader vor und nach
jeder Lektüre Device, Inode, Modus, UID, Linkcount, Größe, mtime und ctime,
sowie den gebundenen Namen erneut. Er liest und vergleicht `active.json` und
`current.json` vor/nach der Generation erneut, inklusive Parents und
`generations/`-Identität. Ein Rename-, Hash- oder Identitätswechsel führt zu
`invalid`; niemals zu gemischter Generation. Der atomare `rename` von
`current.json` ist Commitpunkt, ersetzt aber weder Lock noch Vor-/Nachprüfung.

Ein Hard-Crash direkt nach `O_CREAT|O_EXCL` oder nach dem `fsync` der Pointer-
Tempdatei kann ausschließlich
`integration/.tmp-current.json-<32-lowercase-hex>` hinterlassen. Die öffentliche
Recovery und alle Publish-, Evidence-Rollback-, GC-, Installer- und
Installer-Rollback-Pfade klassifizieren diesen Root-Namespace unter derselben
Release→Current-EX-Sperre vor Pointer-Publikation beziehungsweise vor dem
Installer-Artefaktscan. Es werden höchstens 128 Root-Einträge und darin
höchstens 64 exakt benannte Pointer-Temps materialisiert. Damit sind bis zu
64 Crashreste zusätzlich zum 64-Einträge-Vertrag des bereinigten Installer-
Namespaces endlich behandelbar; der 129. Root-Eintrag oder 65. Pointer-Temp
ist vor jeder Löschung `invalid`.

Ein löschbarer Pointer-Temp ist eine reguläre, nicht verlinkte, der effektiven
UID eigene `0600`-Datei mit Linkcount 1 und 0..4096 Byte. Größe 0 ist exakt
der legitime Create-before-Write-Crashrest; `current.json` selbst bleibt
zwingend 1..4096 Byte. Recovery öffnet den Temp FD-gebunden mit `O_NOFOLLOW`,
prüft unmittelbar vor `unlinkat` nochmals die vollständige Dateiidentität und
den gebundenen Namen und hält den FD bis zur Löschung offen. Danach wird das
`integration/`-Verzeichnis `fsync`'t. Fremde, malformed, zu große, falsch
geschützte, verlinkte oder im letzten Moment ersetzte Einträge bleiben
unangetastet und machen Recovery fail-closed. Unterbrochener Cleanup ist
wiederholbar; kooperierende Live-Publisher sind durch die beiden EX-Locks
ausgeschlossen.

`state_home`, `codex-usage`, `integration`, `generations` und jeder
Generationordner müssen reale UID-eigene `0700`-Verzeichnisse sein. Pointer,
Binding, Usage-Payload, Authority-Projektion und producer-owned Authority-
Quelle müssen reguläre, nicht verlinkte UID-eigene `0600`-Dateien mit
Linkcount 1 sein. Pointer ist 1..4096 Byte, Binding 1..32_768 Byte,
Usage-Payload 1..2_097_152 Byte, Authority-Projektion 1..262_144 Byte und
Authority-Quelle 1..131_072 Byte. Symlinks, Sonderdateien, falscher
Owner/Modus, Hardlinks, Größenüberschreitungen oder Namespace-/Identitätsdrift
sind fail-closed. Keine Home-, Profil- oder Vaultdaten werden kopiert oder
gesnapshottet; die Projektionen enthalten nur begrenztes sanitisiertes JSON.

### Datenqualität, Retention und Status

`window_seconds` und `limit_window_seconds` erlauben nur exakt `18000`,
`604800` oder `2592000`. Es gibt keine weitere Sonderfenster-Allowlist.
`complete`, `stale` und `partial` sind Datenqualitätsresultate des gültigen
Payloads. Reader gibt ausschließlich `complete`, `stale`, `partial`, `busy`,
`unavailable` oder `invalid` zurück: fehlender/temporär nicht lesbarer Pointer
ist `unavailable`; Lockkonflikt `busy`; jede Vertrauens-, Format- oder
Raceverletzung `invalid`.

Eine einzige begrenzte Namespace-Klassifikation akzeptiert höchstens 257
vollständige Generationen und 16 gültige `.tmp-<generation_id>`-Stagings;
fremder Name, 258. vollständige oder 17. Staging-Generation ist sofort
`invalid`. Publish verwendet dasselbe Scanergebnis für Recovery und Retention,
löscht unter derselben EX-Transaktion vor neuem Staging so viele älteste
ungeschützte Generationen, dass höchstens 255 bleiben, und committet dadurch
nie mehr als 256. Current und previous bleiben geschützt. Expliziter GC kann
einen gültigen 257-Zustand auf 256 reduzieren; ein vorhandener 258-Zustand ist
vor jeder Löschung fail-closed. Jede Löschung prüft Pointer und Identität
erneut, benennt den Kandidaten FD-gebunden in Staging um und fsync't Parents.
Historische Manifest-/Release-Digests ändern diese Löschreihenfolge nicht;
malformed Binding, Usage- oder Authority-Hash-/Größendrift, abweichende
Cross-Bindung oder ungültiges `published_at` bleiben auch bei ungeschützten
Generationen fail-closed.

## Kanonisches verifiziertes Installationsverfahren

Alle fünf Argumente sind absolute, owner-eigene Pfade. Checkout, State-, Data-
und Temporary-Root sind reale Verzeichnisse mit Modus `0700`; Python ist ein
absoluter regulärer ausführbarer Interpreter. Aufruf erfolgt aus genau dem zu
veröffentlichenden Checkout, nicht über einen PATH-gefundenen Producer:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=ABS_CHECKOUT/src ABS_PYTHON -B ABS_CHECKOUT/scripts/install_integration_producer.py --source-root ABS_CHECKOUT --state-home ABS_STATE --data-home ABS_DATA --python ABS_PYTHON --temporary-root ABS_TEMP
```

Der Installer kopiert nur die feste Source-Manifest-Allowlist, baut offline
mit `--no-index` ein begrenztes Wheel, extrahiert in einen privaten Stagingbaum,
entfernt allgemeine Venv-Aktivierungseinträge, schreibt den festen Launcher,
prüft RECORD und alle Digests, benennt den unveränderlichen Releasebaum um,
attestiert ihn erneut und ersetzt erst dann atomar `active.json`. Manuelles
Kopieren eines Releasebaums oder Editieren von `active.json` ist verboten.

Verifizierter Nachweis liest anschließend `active.json` nur bounded/no-follow
und prüft: Schema `2`, Version `0.6.537`, Release-ID, kanonische vier Pfade,
Manifest-/Launcher-/Wheel-/RECORD-/Entry-Point-/Releasebaumhashes, Owner, Modi,
Linkcount sowie Device/Inode-Identität. Berichtsfähig sind nur Version, Schema,
Release-ID und Digests; absolute lokale Pfade bleiben ausschließlich im lokalen
Installationsreport. Schlägt Installer oder Nachprüfung fehl, endet das
Verfahren ohne stärkeren Workaround; vorherige aktive Generation bleibt aktiv.

Rollback ist ausschließlich:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=ABS_CHECKOUT/src ABS_PYTHON -B ABS_CHECKOUT/scripts/install_integration_producer.py --rollback --state-home ABS_STATE --data-home ABS_DATA
```

Auch hier erfolgt vor und nach dem atomaren `active.json`-Swap vollständige
V2-Attestierung. Schema 1, alte Versionen oder Drift werden nicht aktiviert.

## Profile, Device-Login und lokale Daten

Jeder konfigurierte Account besitzt genau einen kanonischen Profilroot. Auth
liegt unter `<profile>/codex-home/auth.json`; Jobs, Migration und Metadaten
bleiben im selben privaten Profil. Auth-Dateien werden nicht erraten oder
automatisch kopiert. `profile migrate-auth --dry-run` schreibt nicht; Apply,
Rollback und Finalize sind getrennte explizite Operationen.

Device-Login verwendet ein privates Staging-`CODEX_HOME`, File-Credential-
Storage, begrenzte Ausgabe und keine geerbten API-Key-Variablen. Timeout,
Abbruch oder Outputoverflow beendet die Prozessgruppe. Nur validiertes Auth
wird atomar veröffentlicht; Secrets erscheinen nie in Manifest, Logs,
Fortschritt oder Integrationssnapshot.

History ist privat, SQLite/WAL-basiert und bounded. `HistoryStore.prune`
entfernt nur Samples vor explizitem Cutoff. Account-Purge braucht explizite
Bestätigung und ist von normalem Accountlöschen getrennt. Redemption bleibt
absichtlich nicht verfügbar: CLI und Applet lösen keine automatische oder
manuelle Einlösung ohne belegte Capability-, Nonce-, Lock-, Bestätigungs- und
Postcondition-Gates aus.

## Betrieb

Nur eigene Accounts und niedrige Pollingfrequenz verwenden. Profil-, State-,
Data- und Integrationsverzeichnisse bleiben `0700`; private Dateien `0600`,
Launcher `0700`. Bridge-Endpunkte nicht ohne TLS außerhalb Loopback
veröffentlichen.

Relevante Benutzerchecks:

```text
codex-usage consumption --account ACCOUNT --amount N --unit minutes|hours|days|weeks --format json
codex-usage history status --format json
codex-usage profile layout --account ACCOUNT --format json
```

Der Integration-Check selbst läuft nur über den attestierten Release-Launcher
und den festen V2-argv, nicht über diese allgemeine CLI.
