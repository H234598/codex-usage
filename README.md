# codex-usage

`codex-usage` ist ein lokales Multi-Account-Werkzeug für aktuelle ChatGPT-Codex-Nutzungs- und Limitstände. Es bietet eine Python-CLI, persistente, voneinander getrennte Accountprofile, einen Cinnamon-Applet und einen optionalen `systemd --user`-Dienst. Die erfassten Werte stammen je Account aus dem konfigurierten Abrufweg; sie sind keine Zusage zu einem bestimmten Tarif, Limit oder Resetzeitpunkt.

Die Dokumentation beschreibt den attestierten D297-Nachfolger `0.6.539`. Die vollständige Navigation steht in [docs/README.md](docs/README.md); der Implementierungsstatus steht in [ROADMAP.md](ROADMAP.md).

## Schnellstart

Voraussetzung ist Python 3.11 oder neuer. Für den Standardbrowser Firefox müssen die zugehörigen Playwright-Browserdateien verfügbar sein.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m playwright install firefox

codex-usage account add privat --label Privat
codex-usage login privat
codex-usage once --format json
```

`login` öffnet den persistenten Browser des ausgewählten Accounts sichtbar. Melde dich dort selbst an; Zugangsdaten, MFA-Codes und Token gehören weder in Befehle noch in diese Dokumentation. Mit `codex-usage account overview --config-only` lässt sich die lokale Konfiguration prüfen, ohne einen Live-Abruf anzufordern. Die vollständige Befehlsreferenz liefert `codex-usage --help`.

## Accounts und Profile

Jeder Account hat eine eindeutige ID, ein Label, ein eigenes Profilverzeichnis, einen Browser und einen Abrufweg. Firefox und `direct` sind die Defaults; zulässige Account-Browser sind Firefox und Chromium, zulässige Abrufwege `direct` und `app-server`.

- Der Standardprofilpfad liegt unter dem XDG-Datenverzeichnis in `codex-usage/profiles/<account-id>`.
- Ein Accountprofil darf nicht mit einem anderen Account geteilt werden. Die Konfiguration akzeptiert höchstens 100 Accounts und eine Polling-Periode ab 60 Sekunden; der Default beträgt 300 Sekunden.
- Profil- und Authentisierungszustände bleiben je Account isoliert. Beim direkten Abruf muss die lokale Authentisierung dem gewählten Account eindeutig zugeordnet sein; nicht eindeutig zuordenbare Quellen ergeben keinen behaupteten Accountstatus.
- `codex-usage account backend ACCOUNT direct|app-server` stellt den Abrufweg um. Die App-Server-Kontostatusabfrage startet keine Modellanfrage.

Details zu Profileinrichtung, Authentifizierung, Browserverwaltung und Migration stehen in [docs/accounts-and-authentication.md](docs/accounts-and-authentication.md) und [docs/browser-and-manage-account.md](docs/browser-and-manage-account.md).

## Limits, Zeitfenster, Credits und Resets

Das Datenmodell erkennt diese kanonischen Fensteridentitäten: fünf Stunden (`18000` Sekunden), Woche (`604800` Sekunden) und 30 Tage (`2592000` Sekunden). Die kompakten Hauptfelder eines Account-Snapshots sind `five_hour` und `weekly`; zusätzliche Modellpools und Credits bleiben providerabhängig. Die attestierte D297-V2-Producerlinie lehnt jede Spark-Pool- oder Modellquellenevidenz transparent fail-closed ab: Spark ist abgeschafft und kein Fallback.

Credits sind optional. Ein absoluter Credit-Saldo ohne Nenner wird nicht in einen Prozentsatz umgerechnet; widersprüchliche oder ungültige Creditdaten werden nicht als gültiger Stand ausgegeben. Reset-Zähler können als bekannter, unbekannter oder nullwertiger Stand dargestellt werden. Eine Reset-Einlösung ist nicht implementiert und wird nicht automatisch ausgeführt.

Verbrauchsberechnung, Forecasts, Tokenende und die Behandlung von unbekannten oder veralteten Werten sind in [docs/usage-forecast-and-token-end.md](docs/usage-forecast-and-token-end.md) beschrieben. Die Creditregeln stehen in [docs/credits.md](docs/credits.md).

## CLI und Dienste

Für einen einmaligen Abruf, fortlaufende Anzeige oder Limit-Sperrlogik gibt es:

```bash
codex-usage once --format table
codex-usage watch --interval 300
codex-usage watchdog --format json
```

Der verwaltete Benutzer-Timer wird nur auf ausdrücklichen Aufruf eingerichtet:

```bash
codex-usage service enable
codex-usage service status --format json
```

Weitere wichtige Gruppen und Befehle sind `account`, `profile`, `history`, `consumption`, `health`, `bridge-snippet`, `bridge-extension`, `bridge-server`, `policy` und `masterjet`. Der Dienst startet den dedizierten Integrations-Watchdog, nicht einen beliebigen CLI-Aufruf. Betrieb, Integration und Fehlerdiagnose sind in [docs/operations.md](docs/operations.md), [docs/integration-api.md](docs/integration-api.md) und [docs/troubleshooting.md](docs/troubleshooting.md) dokumentiert.

## Cinnamon-Applet

Das Cinnamon-Applet trägt die UUID `codex-usage@H234598`. Die Installation prüft die ausgelieferten Applet-Dateien und installiert sie lokal:

```bash
make install-local
```

Die Kompatibilitätstabelle `account-panel-settings` enthält vier Legacy-Wertfelder: `slot1`, `slot2`, `slot3` und `slot4`. Sie sind ausdrücklich **nicht** auf zwei Slots beschränkt. Der Applet-Editor kann die sichtbare Anzahl der Wertspalten konfigurieren und erhält versteckte Legacy-Werte beim Wechsel der Anzahl. Installation, Konfiguration und sichere Deinstallation werden in [docs/installation.md](docs/installation.md) und [docs/operations.md](docs/operations.md) erläutert.

## Sicherheit und bekannte Grenzen

- Nutze nur Accounts, die du verwalten darfst. Der Abruf hängt von Anbieteroberflächen und deren Daten ab; Login, Cloudflare, unvollständige Werte und geänderte Anbieterantworten können einen Accountstatus auf `partial`, `login_required` oder `error` setzen.
- Konfiguration, Profile und diagnostische Ausgabepfade werden als private Pfade behandelt; symbolische Links an sicherheitsrelevanten Stellen werden abgewiesen. Diagnose- und Probeausgaben können dennoch sensible Nutzungs- oder Seitendaten enthalten und gehören in einen geschützten lokalen Ordner.
- Die Browser-Bridge lauscht ohne `--allow-remote` nur auf Loopback. Remote-Bindung erfordert TLS-Zertifikat und privaten Schlüssel.
- Der verwaltete `systemd --user`-Dienst arbeitet mit eingeschränkten Schreibpfaden und Hardening-Optionen. Installation oder Aktivierung ist keine Voraussetzung für die CLI.
- Es gibt keine offizielle öffentliche Codex-Usage-API-Garantie in diesem Repository und keine automatische Umgehung oder Einlösung von Limits.

Siehe außerdem [docs/troubleshooting.md](docs/troubleshooting.md) und [docs/releases.md](docs/releases.md).
