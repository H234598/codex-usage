# Troubleshooting

## Erstdiagnose ohne Zugangsdaten

```bash
codex-usage --version
codex-usage paths
codex-usage account overview --config-only --format json
codex-usage latest --format json
codex-usage health --format json
```

`--config-only` verhindert beim Account-Überblick einen Live-Abruf. Die
Ausgaben können dennoch private Accountmetadaten enthalten und sollten nur
lokal verwendet werden.

## Häufige Ergebniszustände

| Beobachtung | Sichere Reaktion |
| --- | --- |
| `login_required` | Sichtbaren Login oder die [Reaktivierung](accounts-and-authentication.md) für genau diesen Account durchführen. |
| Browser meldet Cloudflare | Nicht als leeres Limit behandeln; `diagnose ACCOUNT --headed` verwenden und den interaktiven Zustand prüfen. |
| `partial` oder unbekannte Werte | Keine Routing- oder Forecast-Annahme daraus ableiten; nochmals abrufen und Herkunft prüfen. |
| Routing `blocked` | Die JSON-`reason` auswerten; nur frische, gültige Usage kann eine Freigabe begründen. |
| V2-Fehler 64/65/69/70/75 | Fehlercode gemäß [Integrations-API](integration-api.md) einordnen; keine alte V1-Datei als Ersatz lesen. |

`diagnose` und `probe` können geschützte Diagnoseartefakte schreiben. Die
Optionen `--screenshot` und `--save-dir` deshalb nur mit einem privaten
lokalen Ziel benutzen; Rohkandidaten nicht in Issues oder Chats einfügen.

## Service prüfen

```bash
codex-usage service status --format json
systemctl --user status codex-usage.service
journalctl --user -u codex-usage.service
```

Der Service arbeitet fail-closed. Ein fehlender attestationstauglicher
Producer, ungültige Source oder ein Timeout wird nicht durch einen freien
Fallback ersetzt. Prüfen Sie daher Paketversion, Konfiguration und die
geschützten lokalen Berechtigungen, bevor Sie einen Service neu installieren.
Die im Repository fest verdrahtete Service-Distribution ist derzeit
`codex-usage==0.6.542`.

## Was diese Anleitung nicht klären kann

Providerseitige Limits, Accountberechtigungen, externe Masterjet- oder
Google-Gegenstellen und Browserinstallation liegen teilweise außerhalb dieses
Repositorys. Ohne lokale, redigierte Evidenz bleibt die konkrete Ursache
unbekannt.

**Belege:** [CLI-Diagnosebefehle](../src/codex_usage/cli.py),
[Browser-Statusbehandlung](../src/codex_usage/browser.py),
[Service-Prüfung](../src/codex_usage/service.py) und
[V2-Vertrag](codex-usage-v2.md).
