# Entwicklung

## Lokale Umgebung

Die Paketmetadaten definieren Python 3.11+ sowie Playwright als Laufzeit-
Abhängigkeit. Für Entwicklung existieren die optionalen Abhängigkeiten
`pytest` und `ruff`.

```bash
python -m pip install -e '.[dev]'
pytest -q tests/test_cli.py
ruff check src tests
```

Die Auswahl der Tests soll zur Änderung passen. Änderungen am V2-Producer
benötigen beispielsweise fokussierte Snapshot-, Entrypoint- und
PoolAuthority-Tests; Änderungen an Parsern und CLI-Beispielen mindestens den
zugehörigen CLI-Test. Eine Dokumentationsänderung erhält Link-, Pfad-,
Beispiel- und Befehlsprüfung statt einer unverbundenen Vollsuite.

## Verträge und Sicherheitsregeln

- [codex-usage-v2.md](codex-usage-v2.md) ist der detaillierte, versionierte
  V2-Integrationsvertrag. Änderungen daran müssen Source, Tests und
  Consumervertrag gemeinsam betrachten.
- Private Konfiguration, Profile, Historie und Authentifizierung sind keine
  Testfixtures für Dokumentation oder Review-Ausgaben. Verwenden Sie
  synthetische Werte.
- Browser-, Bridge- und Service-Änderungen können Runtime- oder
  Systemzustand verändern. Tests sollen diese Seiteneffekte isolieren.
- Datiere Audits und Checklisten unter `docs/` sind historische Artefakte,
  keine normative Spezifikation. Sie bleiben unverändert; aktuelle Verträge
  stehen im Code, in Tests und im V2-Contract.

## Nützliche fokussierte Bereiche

| Thema | Tests |
| --- | --- |
| CLI und Beispiele | `tests/test_cli.py` |
| Limits und Fenster | `tests/test_usage_limits.py`, `tests/test_usage_resets.py` |
| Forecast/History | `tests/test_consumption.py`, `tests/test_history.py` |
| Routing | `tests/test_routing.py` |
| Integration V2 | `tests/test_integration_snapshot.py`, `tests/test_integration_entrypoint.py`, `tests/test_integration_pool_authority.py` |
| systemd/Service | `tests/test_systemd.py`, `tests/test_service.py` |

**Belege:** [Build-Konfiguration](../pyproject.toml),
[Testinventar](../tests), [V2-Contract](codex-usage-v2.md) und die oben
verlinkten fokussierten Tests.
