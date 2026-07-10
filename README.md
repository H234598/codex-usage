# codex-usage

Polls the ChatGPT Codex analytics page for multiple accounts and prints only the current usage values plus reset times.

This is browser automation against `https://chatgpt.com/codex/cloud/settings/analytics`, not an official public API. Use it only for accounts you control, at low frequency, and do not use it to bypass limits or access controls. Enterprise workspaces with Codex Enterprise Analytics access should prefer the official API surface.

## Install

```bash
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

## Configure Accounts

Each account gets its own persistent Playwright profile. These profiles contain login state and are created with private permissions.

```bash
codex-usage account add privat --label "Privat"
codex-usage account add arbeit --label "Arbeit"
codex-usage account overview
```

Each account can use the existing direct WHAM reader or the Codex App Server.
Existing accounts keep `direct` until changed explicitly:

```bash
codex-usage account backend privat app-server
codex-usage account backend arbeit direct
codex-usage account overview --format json
```

The App Server path calls only `account/read` and `account/rateLimits/read`.
It does not start a model thread or turn and therefore does not consume model
usage tokens. Codex can refresh its own OAuth tokens through this path.

Firefox is the default browser. If Cloudflare blocks one browser, change the account browser and log in again:

```bash
codex-usage account add privat --browser firefox
codex-usage account add privat --browser chromium
```

Log in once per account:

```bash
codex-usage login privat
codex-usage login arbeit
```

The login command opens a visible Chromium window. Sign in normally, including MFA, then press Enter in the terminal.

Remove an account from the config:

```bash
codex-usage account delete privat
```

This keeps the browser profile by default. To also delete the stored profile:

```bash
codex-usage account delete privat --delete-profile
```

## Run

One poll:

```bash
codex-usage
codex-usage once
codex-usage once --format json
```

`codex-usage` ohne Subcommand ist gleichbedeutend mit `codex-usage once`.
`once`, `watch` und `watchdog` holen Accounts mit `auth_json_path` direkt ab
und fallen fuer die anderen Accounts auf den Browser zurueck, solange
`--headed` nicht gesetzt ist.

Terminal dashboard, refreshed every five minutes:

```bash
codex-usage watch --interval 300
```

One-shot watchdog that blocks exhausted accounts until the next reset and
releases them afterwards:

```bash
codex-usage watchdog --format table
codex-usage watchdog --format json
```

Probe one account if extraction is incomplete:

```bash
codex-usage probe privat
codex-usage probe privat --save-dir probe-output
```

`probe` prints only response summaries by default. `--save-dir` writes local raw JSON/body fixtures with `0600` permissions; use that only when debugging extraction.

Diagnose login, Cloudflare, and page state without printing cookies or tokens:

```bash
codex-usage diagnose privat
codex-usage diagnose privat --headed --screenshot --save-dir diagnose-output
codex-usage diagnose privat --auth-json ~/.codex/auth.json
```

## systemd User Timer

Install and enable the generated, hardened user timer:

```bash
codex-usage service enable
codex-usage service status --format json
```

Use `codex-usage service disable` or `codex-usage service uninstall` to stop or
remove the managed units. The generated service grants write access only to
the configured codex-usage config, state, account profile and auth paths plus
the Playwright browser cache.

Check output:

```bash
journalctl --user -u codex-usage.service -n 100 --no-pager
```

## Cinnamon Applet

Install the local Cinnamon applet and add `Codex Usage` to the panel:

```bash
make install-local
```

The applet loads saved snapshots immediately and runs a fresh mixed-mode poll
every five minutes. Its settings control whether the panel shows one value per
account slot. Each account has two independent slots; a slot can be off, show
the five-hour value, the weekly value, or the mean of both. The slots can be
ordered, muted, tagged with a short label, and separated with `|`, `·`, `//`,
or brackets. Duplicate sources in one account are normalized to one visible
slot. Muting affects only the panel; hover, click-menu values, polling, and
notifications continue to work. Warnings are available but disabled by
default.

The panel always labels its sources as `5h`, `W`, or `Ø`. Reset dates and times
remain attached to their corresponding values. The click menu exposes
persistent per-account switches for panel visibility, warnings, and errors.

The applet settings include an account table for switching each account between
the direct and App Server readers. Poll ownership is selectable between the
applet, the systemd user timer, and automatic detection. Per-account locks
prevent concurrent token refreshes when both surfaces overlap.

The `Date & Time` settings page keeps separate rows for every account's date
and time. Each part has its own display format, font family, font size, bold,
italic, font color, and background setting; the theme remains the default until
changed. Every style supports four modes: always format, format only below the
threshold, always format with a separate below-threshold style, or disable
formatting. The threshold and both style profiles are configured per account.
The corresponding five-hour or weekly remaining value is evaluated
independently for each reset timestamp.

`Restlaufzeit` is the live countdown until the corresponding five-hour or
weekly reset. It has its own per-account format, threshold, font, size, bold,
italic, font color, background, and below-threshold style settings. The formats
are compact (`2h 05m`), clock-like (`02:05`), long German text, and total hours.
It can independently be enabled for the status bar, hover tooltip, and click
menu, and refreshes once per minute without triggering a new backend fetch.

Percentage values have the same font, size, emphasis, font color, background,
threshold, and below-threshold profile controls. A separate per-account target
table selects whether percentage, date, time, and restlaufzeit formatting
applies to the panel status line, hover tooltip and click menu. Enabling date,
time, or restlaufzeit for the panel or tooltip also shows the relevant reset
component there; the click menu always keeps its reset text visible. Mode
`Aus` leaves a targeted value visible but unformatted.

Expired direct-auth accounts get a reactivation action in the applet menu. It
runs `codex login` against that account's configured `auth_json_path` and opens
the OAuth page in a dedicated browser profile. Normal Vivaldi, Chrome, or
Firefox cookies are not reused. The isolated browser can be selected in the
applet settings; automatic mode prefers Vivaldi.

The same flow is available from the terminal:

```bash
codex-usage reactivate ACCOUNT --browser auto
```

## Health and recovery

The CLI keeps a bounded, redacted health log containing only timestamps,
component/event codes, optional durations, account ids, and error classes:

```bash
codex-usage health
codex-usage health --format json
codex-usage health --clear
```

The applet protects Cinnamon with bounded incremental process output,
generation checks for stale callbacks, idempotent timer/process cleanup, a
15-minute circuit breaker after repeated refresh failures, and a safe mode
after repeated internal failures. Safe mode keeps the last valid panel value
and offers only retry, health, analytics, and settings. It never reloads
Cinnamon or the applet automatically.

The `auto` poll owner installs and enables the managed systemd user timer when
needed. The timer has explicit runtime, memory, task, and stop limits. If the
cache becomes stale, the applet repairs the managed timer first and allows at
most one fallback poll every 15 minutes. Managed unit files, configuration,
snapshots, current values, and health data are written atomically.

Remove the applet files with:

```bash
make uninstall-local
```

## Output

Example:

```text
Stand: 08.06.2026 04:20

Account  5h genutzt    5h Reset          Woche genutzt  Woche Reset       Status
Privat   42 / 100 42%  08.06.2026 04:26  310 / 1000 31% 14.06.2026 04:26 ok
```

## Checks

```bash
python -m ruff check .
python -m pytest
node --test tests/applet_runtime.test.js
```
