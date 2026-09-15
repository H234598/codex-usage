# Integrations-API

## V2 ist der einzige Consumervertrag

Die externe Integrationsoberfläche ist die attestierte Schema-2-Evidenz. Der
einzige Produceraufruf lautet exakt:

```text
integration-snapshot --schema 2 --format json
```

Argumentvarianten, ein allgemeiner `codex-usage`-Aufruf, PATH-Auflösung,
Schema-1-Fallback oder ein V1-Legacy-Read sind keine zulässige Producer-API.
Der Producer veröffentlicht keine Rohhistorie und keine Zugangsdaten.

Consumer lesen ausschließlich den separaten Pointer `current.json` und die
von ihm referenzierte immutable Generation. Diese enthält exakt
`account-usage-v2.json`, `pool-authority-v2.json` und
`account-usage-v2.binding.json`. Ein Consumer darf die Generation nicht aus
einem vermuteten festen Cachepfad ableiten.

## Transport und Fehler

Das Nutzungsdokument ist kanonisches JSON mit den Top-Level-Feldern
`schema_version`, `generated_at` und `accounts`; `schema_version` ist der
Integer `2`. Der Producer prüft und sanitisiert Source, Historie und
PoolAuthority vor dem Publish. Ungültige oder widersprüchliche Quelle führt
zu einem Fehler statt zu einer teilweisen neuen Generation.

Für den Launcher sind die stabilen Fehlerausgaben/Statuscodes:

| Status | Bedeutung |
| --- | --- |
| 64 | ungültige Argumente |
| 65 | ungültige Source |
| 69 | nicht verfügbar |
| 70 | Secure-I/O-Fehler |
| 75 | belegt oder Timeout |

Der vollständige Transportvertrag enthält zusätzlich Feld-Allowlisten,
Größenlimits, Credit-Sonderfälle, Tracker-Evidenz, Binding und Attestierung:
[codex-usage-v2.md](codex-usage-v2.md). Er ist gegenüber dieser Übersicht
maßgeblich.

## Nicht Teil dieser API

Browser-Bridge, lokale CLI-Ausgaben, die Routing-Policy und Masterjet- bzw.
Google-Controlbefehle sind keine V2-Consumerquellen. Externe API-Versionen,
Authentifizierung und SLA eines Consumers sind außerhalb dieses Repositorys
unbekannt.

**Belege:** [V2-Vertrag](codex-usage-v2.md),
[Snapshot-Validierung](../src/codex_usage/integration_snapshot.py) und
[attestierter Entry Point](../src/codex_usage/integration_entrypoint.py).
