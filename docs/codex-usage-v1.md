# codex-usage v1: Verbrauch, Profile und Integrationsvertrag

## Prozentpunkte und Coverage

`consumption` reports consumed percentage points from stored, successful
authenticated samples. It does not interpolate across an offline gap.
Each window also carries an optional linear
`estimated_seconds_to_exhaustion` projection. Fresh partial coverage is
explicitly marked as an approximation; stale or insufficient coverage stays
unknown. The applet formats and displays this backend value but does not
recalculate the rate.

- `complete`: required samples cover requested interval.
- `partial`: some interval is covered, but a gap exists.
- `stale`: latest sample is outside the freshness bound.
- `insufficient`: no positive, verified delta is available.

Values are per account and per selected limit window. The applet reuses its
existing account style and target settings; panel, hover, and click-menu
visibility remain independent.

History is private and uses a local SQLite database with WAL. It stores bounded
raw samples; `HistoryStore.prune` removes samples older than the explicit
cutoff supplied by the caller. Account purge needs an explicit confirmation
and is separate from normal account deletion.

## Profile layout and migration

Each configured account has one canonical profile root. The canonical auth
source is `<profile>/codex-home/auth.json`; jobs, migration manifests, and
metadata stay below the same private profile root. Existing auth files are not
guessed or copied automatically.

`profile migrate-auth --dry-run` reports candidates and conflicts without
writing. Apply creates a private staging copy and manifest; rollback uses the
manifest; finalize is a separate explicit operation. Secrets never enter the
manifest, logs, CLI progress output, or integration snapshots.

## Device-Login

`profile device-login --account ACCOUNT` first checks that the installed Codex
CLI explicitly supports device authentication. The process runs with a
private staging `CODEX_HOME`, file credential storage, bounded output, and no
inherited API-key variables. Only a bounded URL/code event is exposed to the
Applet. The subprocess runs in its own process group; timeout, cancellation,
or output overflow terminates descendants together with the login process.
Successful validated auth is published atomically to the canonical profile
path.

Persistent login uses `profile create`, `profile jobs`, `profile job-status`,
and `profile cancel`. `profile create` carries all account browser options,
including the per-account reactivation browser. Job manifests contain no secrets and are private and
size-bounded. URL/code events live in a separate private file, are exposed only
while a job is active, and are removed at terminal state. Worker cancellation
uses an ownership-checked process group and an atomic status transition, so a
late success cannot overwrite `cancel_requested`.

Worker success also requires a completion postcondition: Config-Account,
account options, and private canonical auth file must be published and match
the job. Removing an account from settings cancels an active persistent job
before issuing `account delete`, so late finalization cannot recreate it.

The applet shows live URL/code events while the process is running and offers
an explicit account-level cancellation action. Cancellation terminates the
child, removes live state, and releases queued auxiliary work. Active persistent
jobs are found after applet restart and resumed through bounded status polling;
stale poll responses cannot replace newer state. Device-Login uses a 15-minute
timeout; ordinary auxiliary commands use 30 seconds.

In the settings GUI, `Abrufwege und Accounts` exposes `+` and `-`. Removing an
account removes its Config-/State-assignment but keeps its profile directory
unless explicit CLI profile deletion is requested. `Hervorhebungen und Design:`
contains the per-account styles; `Anzeige:` chooses percentage, reset,
consumption, forecast, Usage-Resets, Account-ID, Label, and Kürzel
independently for panel, hover, and click menu. Separate hover/click spacer
switches insert visual separators before accounts.

## Reset display and redemption

Reset state has three values: `unknown`, known `0`, and a known positive
balance. Invalid, contradictory, negative, boolean, or oversized values fail
closed. Zero can be hidden by display settings. Panel, hover, and click-menu
targets are independent.

Redemption is intentionally unavailable. No provider endpoint currently meets
all required gates: explicit capability, nonce/replay protection, account
lock, user confirmation, and postcondition verification of balance reduction
or limit reset. CLI and applet expose no redeem action and never redeem
automatically. This is a safety boundary, not an omitted UI feature.

## Masterjet integration snapshot

`integration-snapshot --schema 1 --format json` exports only sanitized,
bounded account IDs/labels, status/freshness, limit windows, percentage-point
costs, and reset state. It exports no auth paths, tokens, cookies, raw browser
responses, Device-Login output, or arbitrary provider payloads. Unknown schema
versions and malformed identities fail closed.

The snapshot is written atomically and is the only supported cross-process
consumer contract. `codex-master` must treat stale, partial, and unknown values
as data-quality states rather than inventing current usage. Forecast values are
optional bounded integers and inherit the window's data-quality state.

## Privacy and operations

Use only accounts you control and low polling frequency. Browser profiles and
auth files are private data. Keep profile directories at `0700`, auth files at
`0600`, and do not expose bridge endpoints beyond loopback without TLS.

Relevant checks:

```text
codex-usage consumption --account ACCOUNT --amount N --unit minutes|hours|days|weeks --format json
codex-usage history status --format json
codex-usage profile layout --account ACCOUNT --format json
codex-usage integration-snapshot --schema 1 --format json
```
