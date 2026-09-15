# Installation

## Voraussetzungen

Die Distribution verlangt Python 3.11 oder neuer. Sie verwendet Playwright;
die vom gewählten Browser-Backend benötigte Browserlaufzeit muss auf dem
jeweiligen System verfügbar sein. Welche Browserartefakte eine konkrete
Playwright-Installation benötigt, wird in diesem Repository nicht festgelegt.

Installation aus einem ausgecheckten Release:

```bash
python -m pip install .
codex-usage --version
codex-usage --help
```

Der Paketname ist `codex-usage`; die installierten Console-Scripts sind
`codex-usage`, `codex-usage-browser` und
`codex-usage-integration-watchdog`.

## Erster, nicht sensitiver Check

`paths` zeigt den ausgewählten Konfigurationspfad, ohne Konfiguration oder
Accountdaten auszugeben:

```bash
codex-usage paths
```

Die Standardkonfiguration liegt unter dem XDG-Konfigurationsstamm in
`codex-usage/config.toml`; ein alternativer Pfad wird global vor dem Subcommand
übergeben:

```bash
codex-usage --config /absolute/path/config.toml paths
```

Der Pfad ist ein Platzhalter. Private Konfiguration, Profile und jede
Authentifizierung dürfen nicht in Tickets, Terminalmitschnitten oder
Dokumentation geteilt werden.

## Nächste Schritte

1. Einen Account und sein Profil über die
   [Account-/Auth-Provisionierung](accounts-and-authentication.md) anlegen.
2. Mit `once` einen einzelnen Abruf ausführen.
3. Erst nach einem erfolgreichen Abruf bei Bedarf den
   [systemd-User-Timer](operations.md) installieren und aktivieren.

`service install` und `service enable` sind schreibende Systemoperationen und
sollten nicht als bloßer Installationscheck ausgeführt werden. Sie sind in der
Betriebsdokumentation beschrieben.

**Belege:** [Paketmetadaten und Scripts](../pyproject.toml),
[CLI-Parser](../src/codex_usage/cli.py) und
[Konfigurationsstandard](../src/codex_usage/config.py).
