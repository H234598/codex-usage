# Codex Usage V2 Producer Handoff — Final Fix Report

Date: 2026-08-24
Starting HEAD: `9c10004faaf6ab3e90669decc5bf32fabf71a158`
Worktree: `/home/teladi/.codex-worktrees/codex-usage-v2-producer-handoff`
Target release: `0.6.534` / manifest schema `2`

## Scope and binding inputs

Implemented the single final whole-branch fix wave against:

- `Codex-Usage-TrackerEvidenceV1-Handoff.md`
- `.superpowers/sdd/Codex-Usage-TrackerEvidenceV1-Handoff/global-constraints.md`
- Controller findings 1–4 and release consequences

The binding Obsidian plan was read completely before changes. No matching
Annotation Marker sidecar existed. The open `codex-question` note under the
vault was unrelated to this work. `codegraph` was unavailable, so bounded
`rg` structure/callsite scans were used as fallback.

## Outcome

All four final-review findings are fixed and covered by observed RED/GREEN
tests. Release identity is `0.6.534` across project, package, applet, producer,
wheel and Dist-Info surfaces. The canonical installer atomically cut over the
fully attested active `0.6.533`/schema-2 release to `0.6.534`/schema 2. Read-only
post-install attestation verified every bound artifact and the release tree.

During the first full Python gate, seven pre-existing rollback tests exposed a
latent compatibility defect introduced when private writer locks became
persistent: transaction-created `.lock` files prevented deletion of newly
created profile trees. The final closure now records only lock files created by
the current transaction using create-exclusive ownership plus device/inode
identity. Rollback deletes only those exact recorded files. Pre-existing or
replaced locks are never claimed. This root fix restored all seven rollback
paths without weakening persistent lock serialization.

## Finding 1 — Remove unattested general CLI producer path

### RED

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q \
  tests/test_cli.py \
  -k 'root_help_lists_all_commands or general_cli_does_not_expose_integration_snapshot'

2 failed, 144 deselected in 1.00s
```

### Fix

- Removed `integration-snapshot` from the general CLI parser, help and
  `KNOWN_COMMANDS`.
- Removed direct producer/cache-writer imports and handler from `cli.py`.
- Removed direct success/bypass tests tied to caller-controlled current/cache
  paths.
- Kept publishing exclusively in the fixed attested
  `codex_usage.integration_entrypoint` launcher path.
- Updated help regression and release documentation.

### GREEN

```text
same focused command
2 passed, 142 deselected in 1.18s
```

Bounded callsite scans found no remaining general CLI producer handler or
caller-controlled cache publish path.

## Finding 2 — Bind coverage to exact evidence age

### RED

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q \
  tests/test_integration_snapshot.py \
  -k 'coverage_inconsistent_with_sample_age or publisher_enforces_coverage_age or exact_900_second_boundary or retains_old_single_sample'

5 failed, 7 passed, 128 deselected
```

### Fix

Canonical schema-2 validation now computes
`sample_age = generated_at - last_sample_at` and enforces:

- `complete|partial`: valid through exactly 900 seconds; invalid above it.
- `stale`: invalid through exactly 900 seconds; valid above it.
- `insufficient`: retains its single-sample semantics regardless of age.
- Future samples remain rejected by existing timestamp ordering rules.

The same canonicalizer is used by serializer and publisher, so invalid bytes
cannot mutate the cache.

### GREEN

```text
same focused command
12 passed, 128 deselected

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q \
  tests/test_integration_snapshot.py
140 passed
```

Subsequent tolerance regressions raised the final snapshot suite count to 144.

## Finding 3 — Exact manifest field allowlists

### RED

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q \
  tests/test_integration_installer.py -k 'requires_exact_canonical_fields'

8 failed, 4 passed, 267 deselected in 22.81s
```

Candidate seam timing was then tightened with a pre-rename assertion:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q \
  tests/test_integration_installer.py \
  -k 'candidate_manifest_requires_exact_canonical_fields'

3 failed, 277 deselected in 11.19s
```

### Fix

- Added explicit versioned 16-field allowlists for current schema 2, previous
  schema 2 and legacy schema 1 manifests.
- `_verify_manifest_contract()` now requires exact keys immediately after the
  bounded manifest read.
- Candidate manifests are checked before the staging tree rename.
- Active, candidate, previous/rollback and legacy-upgrade verification reject
  unknown, secret-like and missing fields.
- Tests use real installed/rehashed release manifests where applicable.

### GREEN

```text
exact active/candidate/previous/legacy matrix
12 passed

candidate pre-rename matrix
3 passed, 277 deselected

adjacent verifier/cutover selection
14 passed, 266 deselected
```

## Finding 4 — Absolute-only percentage complement tolerance

### RED

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q \
  tests/test_integration_snapshot.py \
  -k 'limit_complement_uses_only_absolute_tolerance'

2 failed, 2 passed, 140 deselected
```

### Fix

`math.isclose()` now uses `rel_tol=0.0, abs_tol=1e-9`. Tests cover positive and
negative deltas inside (`0.5e-9`) and outside (`2e-9`) the boundary. Docs state
the same absolute-only contract.

### GREEN

```text
focused complement/canonicalization selection
9 passed, 135 deselected
```

## Release and installer-only cutover

### RED

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q \
  tests/test_integration_installer.py \
  -k 'release_version_is_06534 or cutover_accepts_attested_schema2_06533'

2 failed, 278 deselected in 4.68s
```

Failures proved the old `0.6.533` surfaces remained and no exact
`0.6.533`/schema-2 installer-only verifier existed.

### Fix

- Bumped project/package/applet/producer exactly once from `0.6.533` to
  `0.6.534`.
- Current runtime and rollback verification accept only `0.6.534`/schema 2.
- Installer cutover sources are tightly enumerated:
  - `0.6.533` / schema 2
  - `0.6.532` / schema 1, retained because the binding source specification
    explicitly requires this transition source.
- Neither prior release is accepted by current runtime verification or
  rollback reactivation.
- Wheel name, Dist-Info path, launcher, manifest paths and all generated hashes
  derive from the single `0.6.534` release identity.

### GREEN

```text
same focused release/cutover command
2 passed, 278 deselected in 4.50s

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q \
  tests/test_integration_installer.py
280 passed in 300.39s
```

## Full-gate rollback closure

### Observed RED

First whole-repository Python gate after the four findings:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q

7 failed, 4478 passed, 1 skipped, 61 warnings in 330.12s
```

The seven failures were:

1. `test_test_home_rejects_hardlinked_auth_source`
2. `test_test_home_rejects_missing_canonical_auth_path`
3. `test_test_home_state_cleanup_failure_restores_auth_and_profile`
4. `test_account_update_config_save_failure_removes_only_new_profile`
5. `test_account_update_state_cleanup_failure_removes_only_new_profile`
6. `test_profile_rollback_removes_new_empty_ancestors`
7. `test_auth_migration_rolls_back_previous_profile_when_later_setup_fails`

Common evidence was `Directory not empty` or unexpected profile data caused by
transaction-created persistent lock files such as
`.codex-usage-profile.lock`, `config.toml.lock`, `profile.json.lock` and
`auth.json.lock`.

An explicit private-lock ownership regression was added before the fix.

```text
targeted private-lock plus seven rollback tests
8 failed in 4.47s
```

### Root fix

- `private_path_lock()` optionally records only a lock created by the current
  transaction. The tracked path is created with `O_CREAT|O_EXCL` and captured
  with device/inode identity.
- `write_private_text()` propagates the optional ownership recorder.
- Config/profile and auth-migration transactions remove recorded lock files
  only after identity validation, then remove their own created directories.
- Existing persistent locks are opened normally and never recorded or removed.

### GREEN

```text
same targeted command
8 passed in 0.44s

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q \
  tests/test_private_io.py tests/test_config.py tests/test_profile_layout.py \
  tests/test_profile_migration.py

425 passed in 2.59s
```

## Verification gates

### Focused and Task-4 gates

```text
tests/test_integration_entrypoint.py tests/test_integration_snapshot.py
176 passed, 1 warning in 1.56s

tests/test_cli.py focused removal selection
2 passed, 142 deselected in 1.18s

Task-4 combined gate before rollback closure
641 passed, 1 warning in 322.25s

Task-4 combined gate after final runtime closure
642 passed, 1 warning in 260.30s
```

Final Task-4 command:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q \
  tests/test_integration_installer.py tests/test_integration_entrypoint.py \
  tests/test_integration_snapshot.py tests/test_consumption.py \
  tests/test_private_io.py
```

The single warning is the known `runpy` warning from
`test_module_main_guard_executes`.

### Final full Python

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q

4486 passed, 1 skipped, 61 warnings in 365.34s
```

Warnings are existing environment/test noise:

- GLib Unix signal deprecation
- `runpy` already-imported module warnings for CLI, integration entrypoint,
  profile jobs and OAuth browser guards
- GTK/XApp/Cinnamon widget deprecations

No warning contains producer secrets or unbounded provider output.

### Final Node

The repository has no root `package.json`; an initial `npm test -- --runInBand`
probe returned ENOENT before running tests. Repository CI and README identify
the canonical suite:

```text
node --test tests/applet_runtime.test.js

tests 540
pass 540
fail 0
duration_ms 2711.697917
```

### Static gates

```text
python3 -m ruff check <Task-4 closure plus changed cleanup files>
All checks passed!

PYTHONPYCACHEPREFIX=<private /tmp cache> python3 -m compileall -q src scripts
exit 0

node --check files/codex-usage@H234598/applet.js
exit 0

git diff --check
exit 0
```

`tests/test_cli.py` still contains 58 pre-existing E501 findings outside the
edited lines; the final Ruff closure intentionally checks `cli.py` and every
other changed Python file without reformatting unrelated legacy tests.

## Canonical install and bounded attestation

### Pre-install source

```text
active_schema=2
active_version=0.6.533
active_release_id=0.6.533-d929d7fcf4976ac7
active_field_count=16
release_dir_private=True
```

Checkout, state home, data home and integration state were user-owned private
directories. The installer temporary root was created with mode `0700`.

### Exact installer invocation

```text
PYTHONPATH="$PWD/src" /usr/bin/python3 \
  "$PWD/scripts/install_integration_producer.py" \
  --source-root "$PWD" \
  --state-home /home/teladi/.local/state \
  --data-home /home/teladi/.local/share \
  --python /usr/bin/python3 \
  --temporary-root /tmp/codex-usage-v2-install.R0eqwh

integration_producer_install_ok
```

No release/manifest file was manually copied or edited.

### Read-only post-install verification

```text
schema=2
version=0.6.534
release_id=0.6.534-f6626d95f4da8149
source_manifest_sha256=f6626d95f4da81494d4c631afa490bb030d1ac7add57126df93c7c4bdd3f64bf
active_manifest_sha256=3635d5493aaa0e350858c32342eaac91552cd4b2815bfa801cfdf3ac04d30b96
entrypoint_sha256=19b1444e49cf38f72dc5c0266412e18c9054eebe1d9b4a217002fc683a57cbed
wheel_sha256=9402976bfb3f63203f6edb50c670e8a049115a0e343f489c8094b97ac4c91b70
record_sha256=4293bcce4b6c6bf4f43e4a027f2bdf18b8b24274ab0b43b0716f8e6e01399a2c
launcher_sha256=d3798f5a7a3f56a5bd3fe556fead114630cf3bdf6c8422e8b0ae920533d41cec
release_tree_sha256=d99c5128448f3aa18473634074ff960e3ae6501c2313a8c9fc9e14b0e3186ef3
```

Every stored digest matched its no-follow bounded recomputation. Additional
checks:

```text
verified_version=0.6.534
verified_release_tree_matches=True
state_data_paths_exact=True
release_id_path_exact=True
release_mode=0700
canonical_entrypoint=True
canonical_launcher=True
canonical_wheel=True
canonical_record=True
previous_schema=2
previous_version=0.6.533
previous_release_id=0.6.533-d929d7fcf4976ac7
previous_manifest_sha256=a0f659660573a1b159fa448a89f78fd5e99203f06e23ade09b855a25cec006d5
previous_runtime_rejected=True
```

Canonical relative paths:

```text
venv/lib/python3.14/site-packages/codex_usage/integration_entrypoint.py
venv/bin/codex-usage
producer.whl
venv/lib/python3.14/site-packages/codex_usage_integration_producer-0.6.534.dist-info/RECORD
```

## Changed files

- `CHANGELOG.md`
- `docs/codex-usage-v2.md`
- `files/codex-usage@H234598/metadata.json`
- `pyproject.toml`
- `src/codex_usage/__init__.py`
- `src/codex_usage/cli.py`
- `src/codex_usage/config.py`
- `src/codex_usage/integration_attestation.py`
- `src/codex_usage/integration_installer.py`
- `src/codex_usage/integration_snapshot.py`
- `src/codex_usage/private_io.py`
- `src/codex_usage/profile_layout.py`
- `src/codex_usage/profile_migration.py`
- `tests/test_cli.py`
- `tests/test_integration_installer.py`
- `tests/test_integration_snapshot.py`
- `tests/test_private_io.py`
- `.superpowers/sdd/Codex-Usage-TrackerEvidenceV1-Handoff/final-fix-report.md`

## Self-review

- General CLI no longer imports, constructs or publishes integration evidence.
- Fixed producer launcher remains the only publish trigger and retains release
  lock plus active attestation.
- Coverage logic uses `generated_at - last_sample_at`, exact 900-second
  inequality direction and unchanged insufficient semantics.
- Complement tolerance has no relative component.
- Manifest exact-key checks happen before semantic field reads; candidate
  rejection happens before staging rename.
- Old release verification is private and installer-only. Public runtime and
  rollback paths still invoke current `0.6.534` verification only.
- Candidate, active and previous bytes remain unchanged on validation failure.
- Transaction cleanup removes only create-exclusive, identity-recorded lock
  files; no filename glob or broad directory deletion was added.
- No secrets, provider output or absolute release paths appear in user-facing
  producer output. This local engineering report contains only explicitly
  requested install paths and bounded digests.
- No Vault handoff document was created; controller requested re-review of the
  post-fix commit first.

## Concerns

- No material product concern remains.
- Existing 61 Python warning messages remain unchanged.
- Two agent-created `/tmp` roots remain because the environment rejected the
  attempted recursive cleanup before execution: the installer root is empty;
  the compileall cache contains 128 regular directory/file entries and no
  symlinks. Neither path is part of repository or active release state.
