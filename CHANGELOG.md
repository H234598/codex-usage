# Changelog

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
