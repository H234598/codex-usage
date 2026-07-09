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

Install the one-shot watchdog and timer:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/codex-usage.service systemd/codex-usage.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-usage.timer
systemctl --user list-timers codex-usage.timer
```

Check output:

```bash
journalctl --user -u codex-usage.service -n 100 --no-pager
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
