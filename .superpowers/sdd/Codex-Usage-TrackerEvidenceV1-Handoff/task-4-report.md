# Task 4 – Versioned Schema-V2 Producer Release, Attestation und Installnachweis

## Status

Task 4 umgesetzt, fokussiert verifiziert und über den kanonischen
Repository-Installer extern aktiviert. Round-1-Finding zur fehlenden
Post-Swap-Rücksicherung ist mit echten Install-/Rollback-Regressionen
korrigiert. Round 2 bindet diese Rücksicherung zusätzlich an die tatsächlich
publizierte Device-/Inode-Identity und schließt Capture-Fehler in den Guard
ein. Round 3 beseitigt den verbleibenden Check-then-Act-Spalt: Published-
Identity wird vor dem Publish gebunden; Restore verschiebt das aktuelle Target
parent-fd-gebunden aus dem Active-Namen und entscheidet erst an der atomar
verschobenen Inode. Der nach Round 1 vorgeschriebene erneute Installerlauf traf erwartbar
den unveränderlichen, bereits aktiven identischen Runtime-Release; Details und
Unverändertheitsnachweis stehen unten.

- Branch: `codex-usage-v2-producer-handoff`
- Basiscommit: `62e6f992911a7c9e7c8fa9f9cc7fee33ef2727ab`
- Task-4-Commit: `cd1be9f00e6e619439ab484009c652ce67b3c85b`
- Round-1-Fix: caller-seitige Active-Manifest-Transaktion in diesem
  Folgecommit `10d92033dd1f4f7ee2296e2e3d5c66bd5870dc64`
- Round-2-Fix: Identity-Capture-/Replacement-Race-Härtung in diesem
  Folgecommit `83d27b21a80ea3f5c6b07fc59d92553cfbefc91d`
- Round-3-Fix: bedingte parent-fd-gebundene Active-Publish-/Rollback-
  Transaktion in diesem Folgecommit; dessen SHA wird mit dem Agentenstatus
  zurückgegeben
- Projekt-/Producer-/Applet-Version: `0.6.533`
- aktives Dokument-/Manifest-Schema: exakter Integer `2`
- Finaler Vault-Handoffbericht: absichtlich noch nicht erzeugt. Finaler Commit,
  Patch-SHA-256 und unabhängiger Review-SHA-256 gehören in den Controller-
  Abschluss nach unabhängiger Review.

## Bindende Ergebnisse

Projektversion, Package-`__version__`, Applet-Metadaten, Producer-Wheel,
Dist-Info, Installer-Expected-Wheel, Attestierungs-Expected-Version,
Release-ID, Launcherpfad und Manifest wurden gemeinsam genau einmal von
`0.6.532` auf `0.6.533` angehoben. Der neue Active-Manifestvertrag ist Schema
2; Runtimeattestierung und Rollback akzeptieren ausschließlich
`0.6.533`/Schema 2.

Ein vorhandener vollständig attestierter `0.6.532`/Schema-1-Release kann nur
im Installer als Einweg-Cutoverquelle gelesen und als `previous.json` erhalten
werden. Öffentlicher Active-Verifier und Rollback lehnen ihn ab; ein alter
Launcher kann dadurch nicht wieder aktiv werden. Corruptes Schema 1 wird auch
im Cutover abgelehnt.

Manifestpfade sind zusätzlich zur Containmentprüfung kanonisch gebunden:

```text
producer.whl
venv/bin/codex-usage
venv/lib/python*/site-packages/codex_usage/integration_entrypoint.py
venv/lib/python*/site-packages/codex_usage_integration_producer-0.6.533.dist-info/RECORD
```

Gleich gehashte alternative Wheel-/Launcherpfade oder verschobene
Site-Packages-/Entry-Point-/Dist-Info-Bäume werden abgelehnt. Bestehende
Owner-/Mode-/Link-/Device-/Inode-/Race-, RECORD-, Source-Drift-,
Releasebaum- und atomare Publishprüfungen bleiben aktiv. Fehler vor dem
Active-Swap erhalten die vorherige Generation. Der exakt getestete
Post-Swap-Vertrag ist exakt: Kandidatenbytes werden zuerst als private
exklusive Datei erzeugt; ihre Device-/Inode-/Owner-/Type-/Mode-Identity ist
damit vor dem Active-Publish bekannt. Vorhandenes Active wird unter gebundenem
Parent-FD per `RENAME_NOREPLACE` in einen eindeutigen Prior-Namen verschoben und
dort gegen die zuvor gelesene Identity geprüft. Erst danach wird die bekannte
Kandidateninode per `RENAME_NOREPLACE` als Active veröffentlicht.

Bei Fehler vor Abschluss von finaler Attestierung und
`previous.json`-Fortschreibung ist ein vorheriges positives Pfadprädikat nur
Preflight, nicht Autorität. Rollback verschiebt das aktuelle Active atomar per
Parent-FD/`RENAME_NOREPLACE` in einen eindeutigen Evidence-Namen und prüft erst
diese verschobene Inode gegen die vorab gebundene Published-Identity. Nur bei
exaktem Device-/Inode-/Owner-/Type-/Mode-Match wird Prior zurück an den nun
freien Active-Namen bewegt. Bei Mismatch wird die verschobene Fremdinode per
`RENAME_NOREPLACE` zurückbewegt; sie wird nie per Path-Write überschrieben und
bounded `IntegrationCleanupError` folgt. Dies gilt auch für byteidentische
Fremdbytes und für Replacement exakt zwischen Preflight und finalem Rename.

Ein reiner Capture-`OSError` bei unveränderter Published-Inode stellt Prior
wieder her. Modedrift ist dagegen eine Identity-Abweichung und bleibt als
Cleanup-Evidenz aktiv; sie wird nicht mehr still korrigiert. `previous.json`
bleibt in allen getesteten Fehlerpfaden unverändert. Scheitert die
Rücktransaktion selbst, bleiben die sicher benannten Inodes als Evidenz liegen
und bounded Cleanup folgt. Kein advisory Lock wird als CAS-Ersatz behandelt;
kein stärkerer Workaround folgt.

## Dokumentvertrag

Kanonische Dokumentation ist jetzt `docs/codex-usage-v2.md`.
`docs/codex-usage-v1.md` wurde entfernt und der einzige gefundene Callsite
atomar aktualisiert. Kein stale kanonisches V1-Dokument bleibt.

Dokumentiert sind vollständig:

- exakte Top-Level-/Account-/Freshness-/Limit-/Tracker-Feldallowlists;
- Statuswerte `ok|partial|error|login_required|unknown`;
- Fenster `18000|604800|2592000`, Limitsource-Pools und exakte
  Trackerpools `main|gpt-5.3-codex-spark`;
- Limits 100 Accounts, 32 Limits/Trends pro Account, 3200 Trackerreihen,
  500000 Samples je Reihe und 2 MiB Dokument;
- handabgeleitetes nichtgeheimes Golden Example;
- EMA-Formel mit `alpha = 1 - exp(-delta_t / 3600)`, Sekundenzeitbasis,
  positiver Rate, 100-Prozent-Projektionscap;
- Resetgenerations-Cut, Chronologie, Gap-, Counterfall-, Coverage-,
  900-Sekunden-Freshness- und Main/Spark-Isolation;
- bounded Snapshot-/Installer-Ergebnis- und Fehlercodes;
- exaktes kanonisches Installations-/Attestierungs-/Rollbackverfahren.

Der stabile private Cache-Dateiname `account-usage-v1.json` bleibt bewusst
erhalten; Inhalt ist ausschließlich Schema 2. Keine zweite Ledgerdatei, kein
Dual-Write und kein Schema-1-Snapshotfallback.

## TDD – beobachtetes RED

Vor den jeweiligen Produktänderungen wurden folgende Fehler beobachtet.

### Release, Manifest, Rollback und Launcher

```text
python3 -m pytest -q tests/test_integration_installer.py -k 'release_version_is_06533 or install_creates_attested_private_active_release or attestation_requires_exact_integer_schema_version or rollback_rejects_schema1_previous or launcher_rejects_schema1_active'
```

Ergebnis: `8 failed, 216 deselected in 12.65s`.

Relevante erwartete Ursachen:

- Projekt meldete `0.6.532` statt `0.6.533`.
- Installer erzeugte Manifest-Schema `1` statt `2`.
- Schema-1-Previous und Schema-1-Active wurden noch nicht vom neuen Vertrag
  zurückgewiesen.
- installierter Launcher war noch an den alten Schema-1-Active-Vertrag
  gebunden.

### Einweg-Cutover

```text
python3 -m pytest -q tests/test_integration_installer.py -k 'install_cutover_accepts_only_attested_schema1'
```

Ergebnis: `1 failed, 224 deselected in 2.26s`.

Ursache: `_install_release` rief für vorhandenes `active.json` nur den neuen
V2-Verifier auf. Ein real aufgebauter, hash-/RECORD-/Releasebaum-attestierter
`0.6.532`/Schema-1-Release konnte deshalb nicht atomar auf V2 angehoben werden.

### Launcher-/Wheelpfaddrift

```text
python3 -m pytest -q tests/test_integration_installer.py -k 'attestation_rejects_manifest_path_drift_even_when_hashes_match'
```

Ergebnis: `2 failed, 225 deselected in 3.78s`.

Ursache: alternative Launcher- und Wheelpfade innerhalb des Releasebaums
wurden bei passenden Hashes akzeptiert.

### Entry-Point-/Dist-Info-Pfaddrift

```text
python3 -m pytest -q tests/test_integration_installer.py -k 'entrypoint_and_dist_info_outside_canonical_site_packages'
```

Ergebnis: `1 failed, 227 deselected in 2.36s`.

Ursache: gemeinsam verschobener Entry Point und Dist-Info unter einem
nichtkanonischen Site-Packages-Pfad konnten bei passendem RECORD/Baumhash
attestiert werden.

### Round 1: finale Post-Swap-Attestierung

Nach unabhängiger Review wurden zwei echte Abläufe ergänzt: eine zweite reale
Release-Installation und ein realer Rollback. Nur der abschließende Verifier
des bereits ausgetauschten `active.json` wurde zum Fehlschlag gezwungen;
vorherige Kandidaten- und Active-/Previous-Attestierungen liefen real.

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py -k 'final_install_attestation_failure_restores_active_and_preserves_previous or final_rollback_attestation_failure_restores_active_and_preserves_previous'
```

Beobachtetes RED vor Produktfix:
`2 failed, 228 deselected in 5.76s`.

Beide Fehler zeigten die Reviewursache direkt: nach `IntegrationInstallError`
enthielt `active.json` weiterhin den neuen bzw. zurückgerollten Manifesttext,
nicht die bytegenauen vorherigen Active-Bytes. Beim Installationsfall war
zusätzlich `previous.json` bereits überschrieben.

### Round 2: Identity-Capture und Replacement-Inode

Sechs reale parametrische Abläufe decken Install und Rollback ab: jeweils
einmaliger `_file_identity`-`OSError`, Modedrift während Identity-Capture sowie
Replacement durch eine neue private Inode mit bytegleichem publiziertem
Manifesttext nach erfolgreicher Capture und vor der absichtlich
fehlschlagenden finalen Attestierung. Die Tests halten Published- und
Replacement-Device/Inode getrennt fest und beweisen, dass die Replacement-
Identity nach dem bounded Fehler weiter aktiv ist.

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py -k 'post_swap_identity_capture_failure or post_swap_active_inode_replacement'
```

Beobachtetes RED vor Produktfix:
`6 failed, 231 deselected in 16.14s`.

In allen vier Capture-Fällen blieb der publizierte neue Manifesttext aktiv,
obwohl die Operation einen Fehler meldete. In beiden Replacement-Fällen
überschrieb die Round-1-Rücksicherung die raced Fremdinode; statt bounded
Cleanup kam nur der ursprüngliche Installationsfehler zurück.

### Round 3: finaler Restore-Boundary-Race

Vier echte Abläufe decken Install und Rollback ab:

- Fremdinode mit anderen Bytes wird exakt nach positivem Restore-Preflight und
  damit an der finalen Replacement-Grenze eingesetzt.
- Fremdinode mit byteidentischen publizierten Bytes wird während
  `_file_identity`-Capture eingesetzt; unmittelbar danach schlägt Capture fehl.

Published-/Replacement-Device/Inode werden getrennt festgehalten. Erwartung:
bounded Cleanup, Replacement-Inode und -Bytes bleiben aktiv,
`previous.json` bleibt bytegenau unverändert.

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py -k 'restore_boundary_inode_replacement or capture_failure_never_adopts_byte_identical_replacement'
```

Beobachtetes RED vor Produktfix:
`4 failed, 237 deselected in 11.58s`.

Die Boundary-Fälle überschrieben die Fremdinode nach dem positiven Prädikat.
Die Capture-Fälle adoptierten die byteidentische Fremdinode als vermeintlich
publizierte Identity und überschrieben sie beim anschließenden Restore.

## GREEN und Verifikation

### Enge GREEN-Zyklen

```text
python3 -m pytest -q tests/test_integration_installer.py -k 'release_version_is_06533 or install_creates_attested_private_active_release or attestation_requires_exact_integer_schema_version or rollback_revalidates_prior_manifest_and_swaps_only_active_json or rollback_rejects_schema1_previous or launcher_uses_isolated_python_and_fixed_environment or launcher_rejects_schema1_active'
```

Ergebnis: `10 passed, 214 deselected in 15.15s`.

```text
python3 -m pytest -q tests/test_integration_installer.py -k 'install_cutover_accepts_only_attested_schema1 or attestation_rejects_manifest_path_drift_even_when_hashes_match or rollback_rejects_schema1_previous or launcher_rejects_schema1_active'
```

Ergebnis: `5 passed, 222 deselected in 8.32s`.

```text
python3 -m pytest -q tests/test_integration_installer.py -k 'entrypoint_and_dist_info_outside_canonical_site_packages or install_cutover_accepts_only_attested_schema1 or attestation_rejects_manifest_path_drift_even_when_hashes_match'
```

Ergebnis: `4 passed, 224 deselected in 6.32s`.

### Finaler kombinierter Fokus-Gate

Round-1-Installer-Gate:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py
```

Ergebnis: `231 passed in 115.27s`.

Enger Post-Swap-GREEN einschließlich künstlich scheiternder Rücksicherung:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py -k 'final_install_attestation_failure_restores_active_and_preserves_previous or final_rollback_attestation_failure_restores_active_and_preserves_previous or failed_active_restore_raises_cleanup_error_and_keeps_failure_evidence'
```

Ergebnis: `3 passed, 228 deselected in 7.14s`.

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py tests/test_integration_entrypoint.py tests/test_integration_snapshot.py tests/test_consumption.py tests/test_private_io.py
```

Round-1-Ergebnis: `576 passed, 1 warning in 111.45s`.

Round-2-Installer-Gate:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py
```

Ergebnis: `237 passed in 125.21s`.

Enger Round-2-GREEN einschließlich Round-1-Rücksicherungen und Bootstrap-
Revalidation: `11 passed, 226 deselected in 24.96s`.

Finales Ergebnis nach Round-2-Fix:
`582 passed, 1 warning in 124.87s`.

Round-3 enger Transaktionsgate, einschließlich normaler Round-1-Rücksicherung,
Round-2-Capture-/Replacement-Fälle und neuer Boundary-/byteidentischer Races:
`15 passed, 226 deselected in 40.28s`.

Round-3-Installer-Gate:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py
```

Ergebnis: `241 passed in 138.62s`.

Finales kombiniertes Ergebnis nach Round-3-Fix:
`586 passed, 1 warning in 137.73s`.

Warnung: bestehender `runpy`-Hinweis in
`tests/test_integration_entrypoint.py::test_module_main_guard_executes`, weil
das Modul vor `runpy` bereits in `sys.modules` liegt. Keine Failure und nicht
durch Task-4-Code verursacht. Keine Vollsuite ausgeführt; Controller besitzt
das finale breite Releasegate.

### Ruff, Compileall und Diff

```text
python3 -m ruff check pyproject.toml src/codex_usage/__init__.py src/codex_usage/integration_installer.py src/codex_usage/integration_attestation.py src/codex_usage/integration_entrypoint.py src/codex_usage/integration_snapshot.py src/codex_usage/consumption.py src/codex_usage/private_io.py scripts/install_integration_producer.py tests/test_integration_installer.py tests/test_integration_entrypoint.py tests/test_integration_snapshot.py tests/test_consumption.py tests/test_private_io.py
```

Erster Lauf: genau ein eigener `UP012` in der synthetischen Schema-1-
Testfixture. Korrigiert; finaler Lauf: `All checks passed!`.

Compileall lief mit privatem temporärem `PYTHONPYCACHEPREFIX`:

```text
PYTHONPYCACHEPREFIX="$compile_cache" python3 -m compileall -q src scripts tests
```

Ergebnis: Exit `0`, keine Ausgabe.

```text
git diff --check
```

Ergebnis: Exit `0`, keine Ausgabe.

## Kanonische Installation und bounded Evidenz

Vorprüfung: Standard-State- und Data-Root waren reale owner-eigene
`0700`-Verzeichnisse; es existierte kein aktives Integrationsmanifest. Der
Checkout wurde für die bereits verlangte private Source-Grenze auf `0700`
gesetzt. Externe Aktivierung war ausdrücklich autorisiert.

Exakter Installationsaufruf:

```text
chmod 0700 .
install_tmp=$(mktemp -d)
PYTHONPATH="$PWD/src" /usr/bin/python3 "$PWD/scripts/install_integration_producer.py" --source-root "$PWD" --state-home /home/teladi/.local/state --data-home /home/teladi/.local/share --python /usr/bin/python3 --temporary-root "$install_tmp"
```

Ergebnis: Exit `0`, einziges Token
`integration_producer_install_ok`.

Kein manuelles Editieren von `active.json`, kein manuelles Kopieren von Wheel,
Launcher oder Releasebaum, kein stärkerer Workaround.

Lokaler Active-Pfad, nur für diesen lokalen Taskreport:

```text
/home/teladi/.local/state/codex-usage/integration/active.json
```

Bounded no-follow Nachprüfung über `verify_active_release`, `_file_bytes`,
`_read_manifest` und `_release_tree_sha256`:

```text
attestation=verified
schema_version=2
version=0.6.533
release_id=0.6.533-d929d7fcf4976ac7
active_manifest_sha256=a0f659660573a1b159fa448a89f78fd5e99203f06e23ade09b855a25cec006d5
source_manifest_sha256=d929d7fcf4976ac79fa067384090711a2444165f1082da840e8612c953b52f3a
entrypoint_sha256=19b1444e49cf38f72dc5c0266412e18c9054eebe1d9b4a217002fc683a57cbed
wheel_sha256=08f0d2364dae5a4612ff6878fbd94477611ba87723f56ff3343160c7b293f6be
record_sha256=6d475cc4726b448c56266316f03718b2d21848c7743aa29b05b52ca1d705e568
launcher_sha256=07a39792d595ec200ecf103351efd109320592339deaf7456284ce25a93d6fc1
release_tree_sha256=4557d5c7bc096d146fd5446ef654284a29a6e27d511eef7b6ed688ffc6d1e855
```

Alle Manifestwerte entsprachen den unabhängig neu berechneten Datei- und
Baumdigests. Manifest blieb unter 128 KiB. Keine Auth-, Token-, Browser-,
Provider-, History-, Account- oder Promptdaten wurden ausgegeben.

### Round-1-Nachinstallation: erwartete Immutable-Kollision

Nach Änderung ausschließlich des Host-Installers und der Tests wurde derselbe
kanonische Aufruf mit neuem privaten `mktemp`-Verzeichnis genau einmal erneut
ausgeführt. Ergebnis: Exit `69`, einziges Token
`integration_producer_unavailable`. Gemäß Handoff kein stärkerer Workaround,
kein manuelles Editieren/Kopieren und kein zweiter Installationsversuch.

Lesende Eingrenzung bestätigte die erwartete Schutzreaktion:

```text
src/codex_usage/integration_installer.py in SOURCE_MANIFEST_FILES = false
source_manifest_file_count=16
checkout_source_manifest_sha256=d929d7fcf4976ac79fa067384090711a2444165f1082da840e8612c953b52f3a
active_source_manifest_sha256=d929d7fcf4976ac79fa067384090711a2444165f1082da840e8612c953b52f3a
expected_release_id=0.6.533-d929d7fcf4976ac7
active_release_id=0.6.533-d929d7fcf4976ac7
```

`integration_installer.py` gehört bewusst weder zum Runtime-Source-Manifest
noch zur verpackten Producer-Closure. Runtimeartefakt, Quelldigest und
Release-ID blieben deshalb identisch. Das bereits vorhandene unveränderliche
Releaseverzeichnis führte noch vor einem Active-/Previous-Swap zum bounded
Exit 69. Eine Neuaktivierung war nicht erforderlich.

Read-only `verify_active_release` attestierte danach weiter exakt
Schema 2/Version 0.6.533. Der dokumentierte vorherige Active-Bytehash blieb
bytegenau gleich:

```text
active_manifest_sha256_before=a0f659660573a1b159fa448a89f78fd5e99203f06e23ade09b855a25cec006d5
active_manifest_sha256_after=a0f659660573a1b159fa448a89f78fd5e99203f06e23ade09b855a25cec006d5
previous_before=absent
previous_after=absent
attestation_after=verified
```

`previous_before=absent` folgt zusätzlich aus der dokumentierten ersten
Installation ohne vorheriges Active-Manifest; die lesende Nachprüfung fand
weiterhin keinen Previous-Pfad. Keine geheimen Daten wurden gelesen oder
ausgegeben.

### Round-2-Read-only-Aktivnachweis

Round 2 ändert erneut nur `integration_installer.py`, Installer-Tests und
diesen Report. `integration_installer.py` bleibt außerhalb der 16 Dateien des
Runtime-Source-Manifests. Deshalb kein Live-Reinstall. Bounded read-only
Nachprüfung nach allen Gates:

```text
attestation=verified
schema_version=2
version=0.6.533
release_id=0.6.533-d929d7fcf4976ac7
active_manifest_sha256=a0f659660573a1b159fa448a89f78fd5e99203f06e23ade09b855a25cec006d5
previous=absent
installer_in_runtime_source_manifest=false
checkout_source_manifest_sha256=d929d7fcf4976ac79fa067384090711a2444165f1082da840e8612c953b52f3a
active_source_manifest_sha256=d929d7fcf4976ac79fa067384090711a2444165f1082da840e8612c953b52f3a
source_digests_equal=true
```

Active-Bytehash, Runtime-Quelldigest, Release-ID und Previous-Abwesenheit sind
gegenüber Round 1 unverändert. Keine Auth-/Token-/Account-/Historydaten wurden
ausgegeben.

### Round-3-Read-only-Aktivnachweis

Round 3 ändert weiterhin nur Host-Installer, Installer-Tests und Report;
Runtime-Source-Manifest bleibt unverändert. Kein Live-Reinstall. Bounded
read-only Nachprüfung nach allen Round-3-Gates ergab erneut exakt:

```text
attestation=verified
schema_version=2
version=0.6.533
release_id=0.6.533-d929d7fcf4976ac7
active_manifest_sha256=a0f659660573a1b159fa448a89f78fd5e99203f06e23ade09b855a25cec006d5
previous=absent
installer_in_runtime_source_manifest=false
checkout_source_manifest_sha256=d929d7fcf4976ac79fa067384090711a2444165f1082da840e8612c953b52f3a
active_source_manifest_sha256=d929d7fcf4976ac79fa067384090711a2444165f1082da840e8612c953b52f3a
source_digests_equal=true
```

## Geänderte Repositorydateien

- `CHANGELOG.md`
- `docs/2026-08-19-anzeige-schalter-audit.md`
- `docs/codex-usage-v1.md` – entfernt
- `docs/codex-usage-v2.md` – neue kanonische vollständige Dokumentation
- `files/codex-usage@H234598/metadata.json`
- `pyproject.toml`
- `src/codex_usage/__init__.py`
- `src/codex_usage/integration_attestation.py`
- `src/codex_usage/integration_installer.py`
- `tests/test_integration_installer.py`
- `.superpowers/sdd/Codex-Usage-TrackerEvidenceV1-Handoff/task-4-report.md`

Keine Task-1–3-Snapshot-/Consumption-/Entrypoint-Produktlogik wurde
umgeschrieben.

## Self-Review

- Versionen und Schemaflächen repo-weit nach stale `0.6.532`, Schema 1,
  `codex-usage-v1.md` und Schema-1-argv gescannt. Verbleibendes `0.6.532` ist
  auf Changelog, feste Legacy-Cutoverkonstanten und reale Schema-1-Testfixture
  begrenzt.
- Runtime und Rollback rufen nur `_verify_manifest` mit fixem Schema 2 und
  Version 0.6.533 auf. `_verify_legacy_manifest_for_upgrade` hat feste
  `0.6.532`-/Schema-1-Parameter und wird ausschließlich beim Installer-Cutover
  nach aktuellem Verifierfehler verwendet.
- Migrationstest baut einen echten privaten synthetischen Schema-1-Release mit
  kanonischem RECORD, Metadata, Einzelhashes und Releasebaumhash; kein Mock-
  Assertionstest.
- Pfaddrifttests recomputen bewusst Einzel-/Baumhashes. Sie beweisen den
  Pfadvertrag statt bloß Konstantentext zu prüfen.
- Echte Post-Swap-Install- und Rollbackfehler stellen Prior nur wieder her,
  wenn die atomar aus dem Active-Namen verschobene Inode exakt der vor Publish
  gebundenen Kandidatenidentity entspricht. Reiner Capture-`OSError` stellt
  wieder her; Modedrift, post-Capture-Replacement, byteidentisches Replacement
  während Capture und Replacement nach positivem Preflight werden nicht
  überschrieben und führen zu bounded Cleanup. `previous.json` bleibt
  unberührt. Normaler Post-Attestierungsfehler stellt weiterhin bytegenau die
  alte Generation her. Existierende Race-/Mode-/Owner-/Link-Tests blieben grün.
- Dokumentgoldenwert ist handabgeleitet, nicht mit Produkthelpern erzeugt.
- Kein Final-Vault-Handoffreport erstellt. Keine unabhängige Review vorgetäuscht.

## Offene Bedenken

Keine funktionalen Task-4-Blocker. Concern: Host-Installerfix ist bewusst nicht
Teil der verpackten Runtime-Closure; aktiver Runtime-Release blieb deshalb
unverändert und der verpflichtende erneute kanonische Installationsversuch
endete an der erwarteten Immutable-Release-ID-Kollision mit Exit 69. Aktiver
Runtime-Release attestiert weiterhin vollständig. Weiterer Hinweis ist die
oben genannte vorbestehende `runpy`-Warnung. Controller muss wie geplant
unabhängige Review, finalen Commitbezug und Patch-/Review-SHA-256 im späteren
Vault-Handoff ergänzen.

## Fix-Round 4 – atomarer Active-Austausch und bounded Artefakt-Recovery

### Reviewursache und korrigierter Vertrag

Round 3 hatte die Inode-Entscheidung korrekt an parent-FDs gebunden, aber der
Zustandswechsel selbst blieb zweistufig. Sowohl Publish als auch Restore
verschoben zuerst `active.json` unter einen versteckten Namen und bewegten erst
danach Kandidat beziehungsweise Prior nach `active.json`. Ein Fehler des
zweiten Rename ließ deshalb einen beobachtbaren und im Fehlerfall dauerhaften
fehlenden Active-Namen zurück.

Round 4 ersetzt diesen bestehenden-Target-Pfad durch genau einen
parent-fd-gebundenen Linux-`renameat2(RENAME_EXCHANGE)`. Alter und neuer Inode
tauschen atomar ihre Namen; zu keinem Zeitpunkt fehlt `active.json`. Bei einem
anfangs fehlenden Target bleibt `RENAME_NOREPLACE` korrekt. Eine spätere
fehlgeschlagene Validierung entfernt dann nicht den einzigen publizierten
Active-Inode, weil es keine vorherige Generation gibt, zu der atomar
zurückgetauscht werden könnte.

Der Transaktionsname bindet Operation, Kandidaten-Device/Inode und – falls
vorhanden – Prior-Device/Inode. Startup beziehungsweise die nächste Install-
oder Rollbackoperation scannt unter dem gebundenen Integrations-FD höchstens
64 Verzeichniseinträge und höchstens acht einschlägige Artefakte. Vor jeder
Entscheidung werden exakter Name, Owner, Modus `0600`, regulärer Typ,
Linkanzahl `1`, Parent-Device, Inode-Stabilität und Größe bis 128 KiB über
nofollow-Stat plus geöffneten FD geprüft.

Deterministische Zustände sind:

- Bound-Artefakt enthält Kandidaten-Inode und Active enthält gebundenen Prior:
  Pre-Swap-Crash; Kandidat darf identity-konditional bereinigt werden.
- Bound-Artefakt enthält Prior-Inode und Active enthält Kandidaten-Inode:
  Post-Swap-Crash oder retained Commit-Evidence. Active wird erneut vollständig
  attestiert; bei Install wird `previous.json` aus dem erneut attestierten
  Prior finalisiert, bei Rollback bleibt `previous.json` unverändert; erst
  danach folgt identity-konditionale Bereinigung.
- Ungebundene `publish-new`-, alte `publish|prior|failed`-, malformed,
  fremde, ersetzte oder sonst mehrdeutige Artefakte: bounded Fehler; keine
  Löschung und kein Überschreiben der Evidenz.

`MAX_ACTIVE_TRANSACTION_ARTIFACTS` ist exakt `8`; das neunte Artefakt stoppt
vor jeder Bereinigung. Ein Cleanup-only-Fehler nach vollständig validiertem
und durablem Active-/Previous-Commit ändert den erfolgreichen öffentlichen
Operationsstatus nicht. Das eine gebundene Artefakt bleibt als bounded
Evidenz; der nächste Eintritt löst es deterministisch oder stoppt sicher. Die
bestehenden öffentlichen Ergebnis-Tokens wurden nicht erweitert.

### TDD – beobachtetes RED

Erster vollständiger neuer Fault-/Recovery-Gate vor Produktänderung:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py -k 'rename_exchange_swaps or second_exchange_failure or publish_exchange_fsync_failure or commit_cleanup_failure_returns_success or next_operation_recovers_post_exchange or startup_recovery_accepts_eight or startup_recovery_rejects_ninth or startup_recovery_preserves_ambiguous or startup_cleanup_never_unlinks or failed_publish_to_initially_absent'
```

Ergebnis: `21 failed, 241 deselected in 39.96s`.

Die Fehler belegten die fehlende Exchange-Primitive, erfolgreiche statt
injizierter Exchange-/Fsync-Fehler, `IntegrationCleanupError` nach bereits
committetem Zustand, ignorierte Crashartefakte, fehlende MAX-Grenze, gelöschte
beziehungsweise ignorierte Fremdevidenz und erneut entfernten Active-Namen bei
anfangs fehlendem Target. Ein anfänglicher Testfixture-Pfadfehler im
Install-Fall wurde vor Produktcode korrigiert; der danach erneut ausgeführte
Initial-Target-Gate zeigte den erwarteten Vertragsfehler:
`2 failed, 260 deselected in 4.75s`.

Self-Review ergänzte vor Abschluss einen weiteren Test: Ein formal gültiges,
aber noch nicht Device/Inode-gebundenes `publish-new` kann nach Prozessabbruch
nicht von einer gleich-ownerigen Fremdinode unterschieden werden und darf
nicht automatisch gelöscht werden.

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py -k 'startup_recovery_preserves_ambiguous_or_foreign_artifact and raw'
```

Beobachtetes RED: `1 failed, 262 deselected in 2.46s` – die erste Recovery-
Fassung löschte dieses ungebundene Artefakt und setzte Rollback fort.

### GREEN und finale Gates

Der ursprüngliche neue 21-Fall-Gate lief nach dem ersten Produktinkrement mit
`21 passed, 241 deselected in 36.24s`. Nach dem zusätzlichen ungebundenen-
Artefakt-RED wurden die MAX-Fälle auf echte identity-gebundene Pre-Swap-
Artefakte umgestellt. Enger Recovery-GREEN:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py -k 'startup_recovery_accepts_eight or startup_recovery_rejects_ninth or startup_recovery_preserves_ambiguous_or_foreign_artifact and raw or startup_cleanup_never_unlinks'
```

Ergebnis: `4 passed, 259 deselected in 7.21s`.

Round-1–3 plus Round-4-Transaktionsgate:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py -k 'final_install_attestation_failure or final_rollback_attestation_failure or failed_active_restore or post_swap_identity_capture or post_swap_active_inode_replacement or restore_boundary_inode_replacement or capture_failure_never_adopts or rename_exchange_swaps or second_exchange_failure or publish_exchange_fsync_failure or commit_cleanup_failure_returns_success or next_operation_recovers_post_exchange or startup_recovery or startup_cleanup_never_unlinks or failed_publish_to_initially_absent'
```

Ergebnis: `35 passed, 228 deselected in 81.22s`.

Vollständiger Installer-Gate:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py
```

Ergebnis: `263 passed in 189.72s`.

Erforderlicher kombinierter Task-4-Gate:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_integration_installer.py tests/test_integration_entrypoint.py tests/test_integration_snapshot.py tests/test_consumption.py tests/test_private_io.py
```

Ergebnis: `608 passed, 1 warning in 182.43s`. Warnung bleibt der bereits oben
dokumentierte vorbestehende `runpy`-Hinweis.

```text
python3 -m ruff check pyproject.toml src/codex_usage/__init__.py src/codex_usage/integration_installer.py src/codex_usage/integration_attestation.py src/codex_usage/integration_entrypoint.py src/codex_usage/integration_snapshot.py src/codex_usage/consumption.py src/codex_usage/private_io.py scripts/install_integration_producer.py tests/test_integration_installer.py tests/test_integration_entrypoint.py tests/test_integration_snapshot.py tests/test_consumption.py tests/test_private_io.py
```

Ergebnis: `All checks passed!`.

`python3 -m compileall -q src scripts tests` mit privatem temporärem
`PYTHONPYCACHEPREFIX`: Exit `0`, keine Ausgabe. `git diff --check`: Exit `0`,
keine Ausgabe.

### Round-4-Read-only-Aktivnachweis

Round 4 ändert ausschließlich Host-Installer, Installer-Tests und diesen
Report. `integration_installer.py` bleibt außerhalb des Runtime-Source-
Manifests. Entsprechend bindender Runtime-Closure-Vorgabe erfolgte kein
Live-Reinstall. Frische bounded read-only Attestierung nach allen Gates:

```text
attestation=verified
schema_version=2
version=0.6.533
release_id=0.6.533-d929d7fcf4976ac7
active_manifest_sha256=a0f659660573a1b159fa448a89f78fd5e99203f06e23ade09b855a25cec006d5
release_tree_sha256=4557d5c7bc096d146fd5446ef654284a29a6e27d511eef7b6ed688ffc6d1e855
previous=absent
installer_in_runtime_source_manifest=false
checkout_source_manifest_sha256=d929d7fcf4976ac79fa067384090711a2444165f1082da840e8612c953b52f3a
active_source_manifest_sha256=d929d7fcf4976ac79fa067384090711a2444165f1082da840e8612c953b52f3a
source_digests_equal=true
```

Active-Bytehash, Releasebaum, Runtime-Quelldigest, Release-ID und
Previous-Abwesenheit sind gegenüber Round 3 bytegenau unverändert. Keine
Auth-, Token-, Account-, History-, Provider- oder Promptdaten wurden gelesen
oder ausgegeben.

### Round-4-Self-Review und verbleibende Bedenken

- Bestehendes Active wechselt nur noch über `RENAME_EXCHANGE`; anfangs
  fehlendes Active nur über `RENAME_NOREPLACE`. Kein Publish-/Restorepfad
  besitzt weiterhin die alte Active-zu-hidden-/hidden-zu-Active-Sequenz.
- Zweiter Exchange-, post-Exchange-Fsync- und Cleanupfehler sind für Install
  und Rollback deterministisch injiziert. Jeder Fehler lässt einen regulären
  Active-Namen oder stoppt vor der Transition; Prior/Kandidat/Fremdinode bleibt
  unter genau einem Namen erhalten.
- MAX/MAX+1, Pre-/Post-Swap-Crash, ungebundene und alte Artefakte,
  Mode/Link/Size/Symlink/Directory sowie Replacement direkt vor Cleanup sind
  real abgedeckt. Assertions prüfen Bytes und Device/Inode, nicht Mockaufrufe.
- `previous.json` wird nur beim erfolgreichen Install-Commit beziehungsweise
  dessen eindeutigem Post-Swap-Recovery finalisiert. Rollback und alle
  mehrdeutigen Fehlerpfade lassen es bytegenau unverändert.
- Linux `renameat2` war bereits für `RENAME_NOREPLACE` bindende
  Plattformvoraussetzung; Round 4 fügt keine neue Plattformfamilie hinzu.
- Absichtlich verbleibende Concern: Ein ungebundenes, altes oder sonst
  mehrdeutiges Artefakt wird nicht automatisch entfernt. Nächste Operation
  stoppt bounded und erhält Evidenz für explizite Untersuchung. Automatisches
  Löschen wäre bei same-owner Race nicht identity-sicher.
- Kein unabhängiger Reviewer vorgetäuscht: Round 4 lief unter explizitem
  Subagentverbot. Finaler Commit-SHA wird mit Agentenstatus zurückgegeben;
  Controller ergänzt unabhängigen Review- und Patch-SHA-256 wie geplant im
  finalen Vault-Handoff.
