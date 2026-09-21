# Releases

## Versionierte Fakten

Die Paketversion steht in `pyproject.toml`; auf der dokumentierten Basis ist
sie `0.6.541`. Die Service-Implementierung bindet ihre Installation ebenfalls
an die erwartete Distribution `codex-usage==0.6.541` und verweigert bei
abweichender oder nicht verifizierbarer Script-, RECORD-, Metadaten- oder
Modulbindung das Schreiben der Unit.

Änderungshistorie, soweit im Repository vorhanden, steht in
[CHANGELOG.md](../CHANGELOG.md). Git-Tags, veröffentlichte Pakete, Changelogs
außerhalb dieses Arbeitsbaums und Deploymentstatus sind hier nicht verifiziert
und daher unbekannt.

## Release-Check für diesen Repositoryteil

1. Paketversion und Console-Scripts in `pyproject.toml` gegen die Release-
   Absicht prüfen.
2. Betroffene fokussierte Tests ausführen; keine unverbundene Vollsuite als
   Ersatz für Vertragsprüfung ausgeben.
3. Bei V2-Änderungen den vollständigen
   [V2-Contract](codex-usage-v2.md), Snapshot-/Entrypoint-Tests und die
   attestierte Servicekette gemeinsam prüfen.
4. Bei CLI-Änderungen `codex-usage --help` und die betroffenen Subcommand-
   Hilfen gegen Beispiele und Defaults prüfen.
5. Bei Unit-Änderungen den User-Service-Test und die statischen Unitdateien
   prüfen. Installation, Reload und Aktivierung gehören zu einem explizit
   freigegebenen Betriebsfenster.

## Kompatibilitätsgrenze

V2 ersetzt den Legacy-V1-Consumerpfad; es gibt laut Contract weder Legacy-Read
noch Dual-Write. Ein Release darf daher keinen alten Snapshot als
Kompatibilitätsfallback dokumentieren oder stillschweigend konsumieren.

**Belege:** [Paketversion](../pyproject.toml),
[Service-Bindung](../src/codex_usage/service.py),
[Änderungshistorie](../CHANGELOG.md) und [V2-Contract](codex-usage-v2.md).
