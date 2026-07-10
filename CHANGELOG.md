# Changelog

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
