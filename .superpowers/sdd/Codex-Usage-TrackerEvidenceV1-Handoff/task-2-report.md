# Task 2 report — schema-v2 document, validation and atomic publish

Date: 2026-08-24

Base commit: `22df35648d1c995bef106c5bf68f6c334f61e8ed fix: restrict tracker evidence windows`

## Implementation

`src/codex_usage/integration_snapshot.py` now exposes only the schema-v2 snapshot API:

- `build_schema2_document()`
- `serialize_schema2_document()`
- `publish_schema2_cache()`

The former public V1 builder, serializer and publisher were removed. No alias,
fallback or dual-write remains in this module.

### Document projection

- Emits exact root fields `schema_version`, `generated_at`, `accounts` with
  `schema_version: 2`.
- Emits exact account fields `account_id`, `status`, `freshness`, `limits`,
  `tracker_evidence`.
- Uses `values_captured_at` when explicitly present; otherwise uses
  `captured_at`. `fresh_until` is that real value-capture time plus the existing
  900-second freshness boundary. Old captures force `stale: true`; generation
  time never becomes the freshness origin.
- Projects bounded Main, model-pool and credit limits. Allowed windows are only
  repository constants `18000`, `604800`, `2592000`. Credit limit uses the
  existing bounded `credits` pool and never invents tracker evidence.
- Accepts tracker history only as a bounded mapping keyed by exact
  `(account_id, pool, window_seconds)` identity. Each source iterable is read
  through `MAX_HISTORY_SAMPLES + 1`, then passed to Task 1's
  `calculate_tracker_evidence()`.
- Omits calculator result `None`. Preserves returned `insufficient` and `stale`
  evidence. Raw `UsageSample` objects and their source strings never enter the
  document.
- Requires emitted evidence to match a current limit's account, pool, window and
  reset target. Main, Spark, credits and arbitrary model pools cannot substitute
  for each other.

### Strict schema and bounds

- Exact-field validation at root, account, freshness, limit and tracker levels;
  unknown and secret-like keys fail closed.
- Bounds document bytes, accounts, directory entries, model pools, availability
  sources, source windows, aggregate limits, tracker series, tracker entries,
  sample history, field counts, timestamps, IDs and tokens.
- Accounts and history iterables stop at `MAX+1`; tests include exact MAX,
  MAX+1 and infinite sources.
- Rejects duplicate accounts, pool/window limits and tracker identities.
- Rejects non-finite/out-of-range percentages, projections and rates; invalid
  window values; overlong IDs/reset generations; absolute local path tokens;
  malformed timestamps; non-integer EMA constant; inconsistent coverage/sample
  counts; reversed sample timestamps; and limit/evidence reset mismatches.
- EMA constant is type-exact integer `3600`. Projection remains within
  `[0, 100]`; rate has finite `0..100` percentage-points/second cap.
- Canonical JSON uses ASCII escaping, sorted keys, compact separators and
  `allow_nan=False`.

### Publish behavior

- Validates and byte-compares complete canonical V2 payload before touching
  cache state.
- Preserves existing parent/owner/mode/file/link checks, zero-timeout private
  lock and atomic `write_private_text()` replacement.
- Reuses sole existing cache `account-usage-v1.json` as the single ledger; V2
  replaces its contents atomically. No parallel `account-usage-v2.json` is
  created.
- Validation, secret, lock, replace, path, owner or IO failure leaves previous
  cache bytes unchanged.

## Changed files

- `src/codex_usage/integration_snapshot.py`
- `tests/test_integration_snapshot.py`
- `.superpowers/sdd/Codex-Usage-TrackerEvidenceV1-Handoff/task-2-report.md`

No entrypoint, CLI, installer, attestation, model, history or consumption file
changed in Task 2.

## TDD evidence

Commands ran in
`/home/teladi/.codex-worktrees/codex-usage-v2-producer-handoff`.

### 1. V2 golden builder and serializer

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_golden_document_contains_only_current_limits_and_tracker_evidence
ImportError: cannot import name 'build_schema2_document'
1 failed in 0.15s
```

GREEN: same command.

```text
1 passed in 0.11s
```

### 2. V2-only public API

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_is_only_active_snapshot_api
AssertionError: assert False  # publish_schema2_cache absent
1 failed in 0.15s
```

GREEN: same command.

```text
1 passed in 0.09s
```

### 3. Source-window cap before projection

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_builder_rejects_limit_source_over_cap_before_projection
Failed: DID NOT RAISE IntegrationInvalidSource
1 failed in 0.15s
```

GREEN: same command.

```text
1 passed in 0.10s
```

### 4. Real value-capture freshness

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_builder_rejects_invalid_explicit_values_capture
Failed: DID NOT RAISE IntegrationInvalidSource
1 failed in 0.14s
```

GREEN: same command.

```text
1 passed in 0.10s
```

### 5. Type-exact EMA constant

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_serializer_requires_integer_ema_time_constant
Failed: DID NOT RAISE IntegrationInvalidSource
1 failed in 0.15s
```

GREEN: same command.

```text
1 passed in 0.10s
```

### 6. Fractional timestamp reversal

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_serializer_rejects_fractional_sample_time_reversal
Failed: DID NOT RAISE IntegrationInvalidSource
1 failed in 0.16s
```

GREEN: same command.

```text
1 passed in 0.11s
```

### 7. Absolute local path rejection

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_serializer_rejects_absolute_local_path_tokens
Failed: DID NOT RAISE IntegrationInvalidSource
1 failed in 0.14s
```

GREEN: same command.

```text
1 passed in 0.09s
```

### 8. Single-ledger atomic cutover

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_publish_replaces_existing_cache_without_parallel_file
IntegrationSecureIOError  # publisher incorrectly required account-usage-v2.json
1 failed in 0.17s
```

GREEN: same command.

```text
1 passed in 0.11s
```

### 9. Duplicate pool/window across reset targets

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_builder_rejects_duplicate_pool_window_with_different_resets
Failed: DID NOT RAISE IntegrationInvalidSource
1 failed in 0.16s
```

GREEN: same command.

```text
1 passed in 0.11s
```

### 10. Coverage/sample-count consistency

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_serializer_rejects_inconsistent_coverage_sample_count
4 failed in 0.23s
```

GREEN: same command.

```text
4 passed in 0.10s
```

### 11. Availability-source iterable cap

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_builder_rejects_availability_source_over_cap
AttributeError: module has no attribute '_MAX_AVAILABILITY_SOURCES_PER_POOL'
1 failed in 0.15s
```

GREEN: same command.

```text
1 passed in 0.11s
```

### 12. Credit-limit projection

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_projection_includes_credit_limit_without_tracker_evidence
AssertionError: projected limits omitted credits/2592000
1 failed in 0.15s
```

GREEN: same command.

```text
1 passed in 0.10s
```

### 13. Source callback containment

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_snapshot_pool_projection_covers_invalid_pool_and_window_shapes
RuntimeError: synthetic remaining callback
1 failed in 0.18s
```

GREEN: same command.

```text
1 passed in 0.13s
```

Additional strict-schema, MAX/MAX+1/infinite iterable, secret, duplicate,
NaN/Inf, history omission/preservation, race and atomicity cases were added
after their owning V2 behavior existed and remained focused regression coverage.

## Final verification

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py
........................................................................ [ 67%]
...................................                                      [100%]
107 passed in 0.25s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_consumption.py
........................................................................ [ 86%]
...........                                                              [100%]
83 passed in 0.12s

$ ruff check src/codex_usage/integration_snapshot.py tests/test_integration_snapshot.py src/codex_usage/consumption.py tests/test_consumption.py
All checks passed!

$ git diff --check
(exit 0; no output)
```

No full suite or unrelated suite ran, per task instruction.

## Self-review

- Exact V2 output contains no label, backend/user identity, URLs, credentials,
  raw provider data, raw history, sample source, terminal output, exception,
  prompt, plan text, agent name or absolute path.
- V1 public snapshot symbols are absent from `integration_snapshot.py`; tests
  assert absence rather than maintaining compatibility aliases.
- Every builder source with potentially large cardinality has an explicit bound
  before traversal. Canonical list and mapping field counts are checked before
  recursive secret scanning.
- Invalid tracker series from Task 1 are omitted; returned non-activatable
  `insufficient` and `stale` evidence remains visible for the consumer's
  fail-closed decision.
- Single existing cache path avoids second ledger and dual-write. Validation and
  canonical byte equality precede all cache mutation.
- Mutation checks covered wrong schema version, missing V2 API, relaxed EMA
  type, lexicographic time comparison, duplicate identity, unbounded source,
  local-path leak and wrong cache filename.

## Concerns / Task 3 handoff

- By task boundary, `integration_entrypoint.py` and `cli.py` still import removed
  V1 APIs. Task 3 must atomically cut both callers to V2 before their suites or a
  runnable release can be green. This is expected intermediate state, not a
  compatibility alias.
- Cache filename deliberately remains `account-usage-v1.json` because the
  binding constraint forbids a parallel file. Its content contract is now
  exclusively schema 2. Task 3 must keep this single fixed path unless the same
  atomic release explicitly migrates/removes the old path without dual-write.
- `build_schema2_document()` accepts bounded sample series prepared from the
  repository `HistoryStore`; Task 3 owns the existing history query wiring. No
  new history store, file or ledger was introduced.
- Freshness duration is 900 seconds, matching Task 1's stale boundary. Rate cap
  is 100 percentage-points/second; both are explicit finite producer bounds.
- Broader entrypoint/CLI/installer/attestation verification is intentionally
  deferred to Tasks 3–4. No full suite ran.

## Review round 1/5 fixes

Date: 2026-08-24

Round changed four focused files:

- `src/codex_usage/integration_snapshot.py`
- `src/codex_usage/private_io.py`
- `tests/test_integration_snapshot.py`
- `tests/test_private_io.py`

### Implementation

- Tracker evidence now verifies every calculator-accepted sample belongs to the
  outer account before evidence can be attached.
- Canonical evidence requires its matching limit reset to be strictly after
  both last sample and document generation. Reset generation remains an opaque
  bounded identity, per schema contract.
- Builder and serializer reject source capture timestamps after generation.
- Available pools reject unknown/unsupported windows, malformed resets,
  invalid types, overflow, NaN/Inf and percentages outside `[0,100]` instead of
  silently omitting those limits.
- `partial` accounts preserve valid limits and tracker evidence. Error,
  login-required and unknown accounts remain data-empty and fail closed during
  canonicalization.
- Absolute-path scanning now detects path forms after opaque prefixes such as
  `file:///home/...` and `reset:/home/...`, while valid bounded tokens such as
  `reset:main/5h` remain accepted.
- `write_private_text()` now creates and identity-checks a private hard-link
  rollback generation before replacement. Directory-fsync or rollback-cleanup
  failure atomically restores the previous inode; a failed first-generation
  publish restores absence. Existing ownership, link-count, regular-file,
  symlink and atomic-replace checks remain active.
- Current-state reader treats bounded `.json.rollback-*` artifacts as transient,
  matching existing private temp/lock handling.
- Added tracker-series mapping MAX/MAX+1, directory-entry exact-MAX and
  availability-source exact-MAX tests. Existing guards already passed these
  characterization boundaries; no product defect was fabricated to force RED.

### Review TDD evidence

Commands ran in
`/home/teladi/.codex-worktrees/codex-usage-v2-producer-handoff`.

#### Same-account authority

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_builder_rejects_tracker_samples_from_another_account
Failed: DID NOT RAISE IntegrationInvalidSource
1 failed in 0.16s
```

GREEN: same command.

```text
1 passed in 0.10s
```

#### Reset chronology

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_serializer_rejects_tracker_reset_not_after_generation
Failed: DID NOT RAISE IntegrationInvalidSource
2 failed in 0.17s
```

GREEN: same command.

```text
2 passed in 0.10s
```

#### Future capture rejection

Builder RED/GREEN:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_builder_rejects_capture_after_generation
Failed: DID NOT RAISE IntegrationInvalidSource
2 failed in 0.18s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_builder_rejects_capture_after_generation
2 passed in 0.12s
```

Serializer RED/GREEN:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_serializer_rejects_capture_after_generation
Failed: DID NOT RAISE IntegrationInvalidSource
1 failed in 0.15s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_serializer_rejects_capture_after_generation
1 passed in 0.10s
```

#### Strict source-limit rejection

Invalid remaining values RED/GREEN:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_projection_rejects_unusable_remaining_values
Failed: DID NOT RAISE IntegrationInvalidSource
5 failed in 0.24s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_projection_rejects_unusable_remaining_values
5 passed in 0.11s
```

Unsupported window RED/GREEN:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_projection_rejects_nonallowlisted_limit_windows
Failed: DID NOT RAISE IntegrationInvalidSource
1 failed in 0.15s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_projection_rejects_nonallowlisted_limit_windows
1 passed in 0.10s
```

Malformed reset RED/GREEN:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_projection_rejects_malformed_limit_reset
Failed: DID NOT RAISE IntegrationInvalidSource
2 failed in 0.17s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_projection_rejects_malformed_limit_reset
2 passed in 0.11s
```

Unknown-window guard RED, then focused group GREEN after aligning prior omission
assertions with strict rejection:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_snapshot_pool_projection_covers_invalid_pool_and_window_shapes
Failed: DID NOT RAISE IntegrationInvalidSource
1 failed in 0.16s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_snapshot_pool_projection_covers_invalid_pool_and_window_shapes tests/test_integration_snapshot.py::test_schema2_numeric_boundaries_reject_subclasses_before_operations tests/test_integration_snapshot.py::test_schema2_projection_rejects_unusable_remaining_values tests/test_integration_snapshot.py::test_schema2_projection_rejects_nonallowlisted_limit_windows tests/test_integration_snapshot.py::test_schema2_projection_rejects_malformed_limit_reset
10 passed in 0.11s
```

#### Partial account data

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_projection_preserves_valid_partial_limits_and_evidence
IntegrationInvalidSource
1 failed in 0.18s
```

GREEN: same command.

```text
1 passed in 0.10s
```

#### Prefixed local paths

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_serializer_rejects_prefixed_absolute_local_path_tokens
Failed: DID NOT RAISE IntegrationInvalidSource
2 failed in 0.18s
```

GREEN: same command.

```text
2 passed in 0.11s
```

#### Transactional directory-fsync rollback

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_publish_schema2_cache_restores_old_bytes_when_directory_fsync_fails
assert schema-v2 bytes == previous bytes
1 failed in 0.19s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_restores_old_value_when_directory_fsync_fails
AssertionError: assert 'new' == 'old'
1 failed in 0.12s
```

GREEN:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_publish_schema2_cache_restores_old_bytes_when_directory_fsync_fails tests/test_private_io.py::test_write_private_text_restores_old_value_when_directory_fsync_fails
2 passed in 0.17s
```

Rollback-artifact reader RED/GREEN:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_current_reader_ignores_private_lock_and_temporary_files
IntegrationInvalidSource
1 failed in 0.17s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_current_reader_ignores_private_lock_and_temporary_files
1 passed in 0.11s
```

#### Missing boundary coverage

These tests were GREEN on first execution because corresponding guards were
already correct; this finding concerned absent regression coverage, not broken
runtime behavior:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_builder_accepts_exact_availability_source_cap tests/test_integration_snapshot.py::test_schema2_tracker_series_mapping_accepts_max_and_rejects_max_plus_one tests/test_integration_snapshot.py::test_current_reader_accepts_exact_directory_entry_cap
3 passed in 0.12s
```

### Round self-review

- Same-account check happens only after Task 1 accepts a series, preserving the
  binding rule that calculator-invalid series returning `None` are omitted.
- Reset chronology is enforced at canonical/publish boundary, not trusted only
  because builder produced the document.
- Hard-link rollback records exact previous inode and validates device, inode,
  regular-file shape, link count and owner before replacement. Link/replace/
  fsync/unlink errors occur under existing private path lock in publisher.
- Successful commit has directory durability before rollback link removal. No
  error is reported after prior generation becomes unavailable.
- No schema-1 API, parallel ledger, raw history or compatibility alias was
  reintroduced.

### Review round final verification

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py
........................................................................ [ 59%]
..................................................                       [100%]
122 passed in 0.34s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_consumption.py
........................................................................ [ 86%]
...........                                                              [100%]
83 passed in 0.16s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py
........................................................................ [ 79%]
...................                                                      [100%]
91 passed in 0.24s

$ ruff check src/codex_usage/integration_snapshot.py src/codex_usage/private_io.py tests/test_integration_snapshot.py tests/test_private_io.py src/codex_usage/consumption.py tests/test_consumption.py
All checks passed!

$ git diff --check
(exit 0; no output)
```

No full or unrelated suite ran.

## Review round 3/5 fixes

### Implementation

- Stale rollback recovery now refuses multi-linked candidates before restoring a
  missing target. Different-inode rollback files remain intact until the live
  target passes caller-required validation; schema-2 publication supplies its
  strict `0o600` target-mode requirement.
- Each transaction owns a collision-proof
  `.<name>.rollback-<pid>-<token>` artifact. Cleanup ownership begins before
  copy creation, so injected read, write, or fsync failure removes only that
  transaction's partial artifact and leaves the live target unchanged.
- Rollback copies have a 64 MiB source-size and streamed-byte ceiling. Oversize
  input fails before replacement without removing or altering the live target.
- `write_private_text()` serializes all writers on the existing per-target
  `private_path_lock()`. Same-thread lock ownership is reentrant, preserving
  existing callers that already lock the target; different threads and
  processes remain serialized by `flock`. No extra writer-lock artifact or
  transient live-target hard link is introduced.
- Current-record boundary fixture counts the persistent per-record `.json.lock`
  files created by the now-universal writer lock.

Files changed in this round:

- `src/codex_usage/private_io.py`
- `src/codex_usage/integration_snapshot.py`
- `tests/test_private_io.py`
- `tests/test_integration_snapshot.py`
- `.superpowers/sdd/Codex-Usage-TrackerEvidenceV1-Handoff/task-2-report.md`

### Behavioral RED/GREEN evidence

Hardlinked stale rollback was incorrectly moved into the missing live path.

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_rejects_hardlinked_rollback_without_restoring_it
assert not path.exists() failed
1 failed in 0.13s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_rejects_hardlinked_rollback_without_restoring_it
1 passed in 0.11s
```

Strict publisher validation happened after valid rollback destruction.

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_publish_schema2_cache_retains_rollback_when_live_mode_is_invalid
FileNotFoundError reading .integration-v2.json.rollback
1 failed in 0.21s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_publish_schema2_cache_retains_rollback_when_live_mode_is_invalid
1 passed in 0.16s
```

Rollback-copy cleanup ownership was established after copy completion. Each
injected failure left its partial artifact behind. The final test version
injects through the actual `os.read`, `os.write`, and rollback-fd `os.fsync`
operations.

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_cleans_owned_partial_rollback_on_copy_failure
3 failed in 0.17s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_cleans_owned_partial_rollback_on_copy_failure
3 passed in 0.09s
```

Existing live targets had no rollback-copy byte bound.

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_rejects_oversized_rollback_source_before_copy
Failed: DID NOT RAISE ValueError
1 failed in 0.14s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_rejects_oversized_rollback_source_before_copy
1 passed in 0.11s
```

Overlapping generic writers shared and deleted the deterministic rollback;
first writer then reported an unpreserved post-replacement failure.

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_overlapping_write_private_text_transactions_preserve_each_other
OSError: [Errno 5] could not roll back value
1 failed in 0.19s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_overlapping_write_private_text_transactions_preserve_each_other
1 passed in 0.32s
```

Initial serialization used a second `.write.lock`, leaving a new persistent
artifact even when caller already held the canonical target lock.

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_reuses_same_thread_private_path_lock
AssertionError: '.value.json.write.lock' != 'value.json'
1 failed in 0.14s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_reuses_same_thread_private_path_lock tests/test_private_io.py::test_overlapping_write_private_text_transactions_preserve_each_other tests/test_integration_snapshot.py::test_current_reader_ignores_private_lock_and_temporary_files
3 passed in 0.39s
```

Focused suite regressions exposed two fixtures that assumed write operations
created no lock file or that the first `fstat()` always inspected the temporary
payload. Fixtures were corrected to include canonical lock entries and preserve
lock-fd validation.

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py
2 failed, 99 passed in 0.54s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py
101 passed in 0.46s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py
1 failed, 127 passed in 0.37s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_current_reader_accepts_exact_directory_entry_cap
1 passed in 0.12s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py
128 passed in 0.29s
```

### Review round 3 final verification

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py
102 passed in 0.51s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py
128 passed in 0.37s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_consumption.py
83 passed in 0.15s

$ ruff check src/codex_usage/integration_snapshot.py src/codex_usage/private_io.py tests/test_integration_snapshot.py tests/test_private_io.py src/codex_usage/consumption.py tests/test_consumption.py
All checks passed!

$ git diff --check
(exit 0; no output)
```

An initial Ruff gate reported B904 at stale missing-target recovery. Adding
explicit `from None` preserved intended public error context; final Ruff gate
above is clean. No full or unrelated suite ran.

### Round 3 self-review and concerns

- Live targets stay single-linked. Rollbacks are private copies, bounded before
  and during streaming, and uniquely owned by one transaction.
- Recovery checks regular-file type, owner, private mode, link count, and exact
  legacy hard-link identity before mutation. Publisher-specific target mode is
  checked before stale rollback deletion.
- Reentrancy is thread-local and keyed by normalized absolute lock path. It
  bypasses only a lock already acquired by the same thread; other writers still
  reach kernel `flock` serialization.
- 64 MiB is a deliberate universal preservation ceiling for current
  `write_private_text()` call sites. A larger existing file now fails closed
  before replacement rather than risking unbounded copy amplification.
- No unresolved correctness or security concern identified within round scope.

## Review round 2/5 fixes

Date: 2026-08-24

### Implementation

- Embedded local-path detection now treats any non-identifier punctuation as a
  possible absolute-path prefix. It rejects `reset-/home/...`,
  `reset;/home/...`, `reset./home/...`, URI-style and assignment-style paths,
  while retaining benign bounded identifiers such as `provider/model`,
  `team/name/v2` and `reset:main/5h`.
- Replaced live-target hard-link rollback with a private copied rollback file.
  Source is opened with no-follow/close-on-exec/nonblocking protections, copied
  through bounded chunks, fsynced, then revalidated against original and live
  device/inode/type/link/owner/mode/size/mtime before replacement.
- Rollback copy uses requested private mode (`0600` for integration cache), is
  directory-fsynced before replacement and never changes live target link
  count. Post-replace directory-fsync or cleanup failure restores copied old
  bytes atomically; absent prior state is restored to absence.
- Stale deterministic copied rollback files and legacy Round-1 hard-link
  rollback files are bounded to one, validated for regular-file type, private
  mode, owner, inode/link relationship, then recovered or removed safely.
- Integration publisher performs stale recovery inside existing private lock,
  then re-runs strict cache mode/type/link/owner validation before writing.
- Current-state reader ignores both legacy `.json.rollback-*` and deterministic
  `.json.rollback` transaction artifacts.

### Review TDD evidence

Commands ran in
`/home/teladi/.codex-worktrees/codex-usage-v2-producer-handoff`.

#### Embedded punctuation-prefixed paths

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_serializer_rejects_prefixed_absolute_local_path_tokens
3 failed, 2 passed in 0.24s
```

GREEN: same command.

```text
5 passed in 0.13s
```

#### Live target remains single-linked

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_keeps_live_target_single_linked_before_replace
AssertionError: assert [2] == [1]
1 failed in 0.13s
```

GREEN after copied rollback redesign: same command.

```text
1 passed in 0.09s
```

Private rollback mode follow-up RED/GREEN:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_keeps_live_target_single_linked_before_replace
AssertionError: assert [420] == [384]
1 failed in 0.12s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_keeps_live_target_single_linked_before_replace
1 passed in 0.08s
```

#### Stale rollback recovery

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_recovers_stale_rollback_artifact
ValueError: live target had st_nlink == 2
AssertionError: copied rollback artifact remained
2 failed in 0.15s
```

GREEN: same command.

```text
2 passed in 0.09s
```

Insecure stale artifact RED/GREEN:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_rejects_insecure_stale_rollback_artifact
Failed: DID NOT RAISE ValueError
1 failed in 0.13s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_rejects_insecure_stale_rollback_artifact
1 passed in 0.09s
```

Publisher-level legacy crash recovery RED/GREEN:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_publish_schema2_cache_recovers_stale_hardlink_rollback
IntegrationSecureIOError
1 failed in 0.19s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_publish_schema2_cache_recovers_stale_hardlink_rollback
1 passed in 0.12s
```

Deterministic rollback reader RED/GREEN:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_current_reader_ignores_private_lock_and_temporary_files
IntegrationInvalidSource
1 failed in 0.18s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_current_reader_ignores_private_lock_and_temporary_files
1 passed in 0.11s
```

#### True post-replace directory-fsync failure

Tests were tightened so first directory fsync persists rollback and second,
post-replace fsync fails.

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_restores_old_value_when_directory_fsync_fails tests/test_integration_snapshot.py::test_publish_schema2_cache_restores_old_bytes_when_directory_fsync_fails
Failed: DID NOT RAISE OSError
Failed: DID NOT RAISE IntegrationSecureIOError
2 failed in 0.25s
```

GREEN: same command.

```text
2 passed in 0.20s
```

#### Parent validation regression caught during focused suite

RED:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py
NotADirectoryError from stale recovery before parent validation
1 failed, 93 passed in 0.27s
```

GREEN after restoring validation order:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py::test_write_private_text_rejects_non_directory_parent
1 passed in 0.08s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py
94 passed in 0.20s
```

### Round self-review

- Live cache inode remains single-linked throughout backup preparation and
  until atomic replacement; concurrent strict readers no longer reject it.
- Crash before replacement leaves complete old target plus separately fsynced,
  private rollback copy. Next locked publish removes stale copy before strict
  revalidation and creates a fresh transaction.
- Legacy hard-link crash artifact is repaired only when exact target/candidate
  device and inode match with link count two. Arbitrary multi-link or foreign,
  nonregular, nonprivate artifacts fail closed.
- No public schema fields, cache location, caller API or unrelated IO behavior
  changed.

Final embedded-path mutation RED/GREEN:

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_serializer_rejects_prefixed_absolute_local_path_tokens
Failed: DID NOT RAISE IntegrationInvalidSource for reset_/home/synthetic/private
1 failed, 5 passed in 0.17s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py::test_schema2_serializer_rejects_prefixed_absolute_local_path_tokens
6 passed in 0.12s
```

### Review round 2 final verification

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_integration_snapshot.py
........................................................................ [ 56%]
.......................................................                  [100%]
127 passed in 0.34s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_private_io.py
........................................................................ [ 75%]
.......................                                                  [100%]
95 passed in 0.23s

$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_consumption.py
........................................................................ [ 86%]
...........                                                              [100%]
83 passed in 0.13s

$ ruff check src/codex_usage/integration_snapshot.py src/codex_usage/private_io.py tests/test_integration_snapshot.py tests/test_private_io.py src/codex_usage/consumption.py tests/test_consumption.py
All checks passed!

$ git diff --check
(exit 0; no output)
```

No full or unrelated suite ran.
