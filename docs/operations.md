# Betrieb

## Einmalige und periodische Ausführung

Für einen kontrollierten Einzelabruf:

```bash
codex-usage once --account alpha --format json
```

`watch` wiederholt Abrufe und akzeptiert ein Intervall; die Konfiguration
fordert mindestens 60 Sekunden. `watchdog` führt einen einzelnen
Überwachungsdurchlauf aus und gibt nur dann Status 0 zurück, wenn die
ausgewählten Ergebnisse für den Watchdog sicher sind.

```bash
codex-usage watchdog --account alpha --format json
```

## systemd-User-Service

Nach einem erfolgreichen manuellen Abruf kann der verwaltete User-Timer
installiert und aktiviert werden:

```bash
codex-usage service install
codex-usage service enable
codex-usage service status --format json
```

Der Timer läuft nach einer Minute an und anschließend alle fünf Minuten; seine
Accuracy beträgt 30 Sekunden und er ist persistent. Die Unit startet den
dedizierten `codex-usage-integration-watchdog`, nicht direkt die allgemeine
CLI. Der Wrapper führt erst den Watchdog und danach den attestierten V2-
Producer aus. Install und Enable prüfen die gebundene installierte
Distribution vor dem Schreiben der Unit fail-closed.

Systemd-Status und Journal können read-only eingesehen werden:

```bash
systemctl --user status codex-usage.timer
journalctl --user -u codex-usage.service
```

Die Unit hat `TimeoutStartSec=270`, eine private UMask (`0077`) und entfernt
Python- sowie Loader-Shadow-Umgebungsvariablen. Sie begrenzt Schreibzugriff
auf Playwright-Cache sowie die vorgesehenen Codex-Usage-Daten-, State- und
Lockpfade.

## Zustandsbeobachtung

```bash
codex-usage latest --format json
codex-usage history status --format json
codex-usage health --format json
```

Historie und Health sind lokale Daten. `history prune --apply` und
`health --clear` verändern sie; diese Befehle gehören in einen bewusst
freigegebenen Betriebsprozess, nicht in einen Diagnose-Standardablauf.

**Belege:** [CLI-Betriebsbefehle](../src/codex_usage/cli.py),
[Service-Implementierung](../src/codex_usage/service.py),
[Timer](../systemd/codex-usage.timer), [Service-Unit](../systemd/codex-usage.service)
und [V2-Vertrag](codex-usage-v2.md).
