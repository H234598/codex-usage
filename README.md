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
account or one combined value for the most constrained account. The displayed
percentage can use the five-hour limit, the weekly limit, or the mean of both.
Warnings are available but disabled by default.

The applet settings include an account table for switching each account between
the direct and App Server readers. Poll ownership is selectable between the
applet, the systemd user timer, and automatic detection. Per-account locks
prevent concurrent token refreshes when both surfaces overlap.

The `Date & Time` settings page keeps separate rows for every account's date
and time. Each part has its own display format, font family, font size, bold,
italic, and background setting; the theme remains the default until changed.
Date and time styling can each be limited to values below a per-account
threshold. The corresponding five-hour or weekly remaining value is evaluated
independently for each reset timestamp.

Expired direct-auth accounts get a reactivation action in the applet menu. It
runs `codex login` against that account's configured `auth_json_path` and opens
the OAuth page in a dedicated browser profile. Normal Vivaldi, Chrome, or
Firefox cookies are not reused. The isolated browser can be selected in the
applet settings; automatic mode prefers Vivaldi.

The same flow is available from the terminal:

```bash
codex-usage reactivate ACCOUNT --browser auto
```

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
ruff check .
pytest
```
