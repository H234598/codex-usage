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
codex-usage account list
```

Log in once per account:

```bash
codex-usage login privat
codex-usage login arbeit
```

The login command opens a visible Chromium window. Sign in normally, including MFA, then press Enter in the terminal.

## Run

One poll:

```bash
codex-usage once
codex-usage once --format json
```

Terminal dashboard, refreshed every five minutes:

```bash
codex-usage watch --interval 300
```

Probe one account if extraction is incomplete:

```bash
codex-usage probe privat
codex-usage probe privat --save-dir probe-output
```

`probe` prints only response summaries by default. `--save-dir` writes local raw JSON/body fixtures with `0600` permissions; use that only when debugging extraction.

## systemd User Timer

Install the one-shot poller and timer:

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
