# Routing und Pool Authority

## Lokale Routingentscheidung

Die Routing-Policy wertet gespeicherte Usage aus und gibt eine JSON-Entscheidung
zurück. Sie verwaltet nicht selbst einen Modellanbieter. Für nicht ausgenommene
Rollen ist die Reihenfolge:

1. Bei gültigem, frischem Spark-Pool und frischem, gesundem Spark-Status:
   `spark` mit `gpt-5.3-codex-spark`.
2. Andernfalls bei sicherem Haupt-Pool: `main` mit `gpt-5.4-mini`.
3. Bei niedrigem Haupt-Pool und explizit erlaubter paid overage: `credits` mit
   `gpt-5.4-mini`.
4. Bei unbekannter, alter, inkonsistenter oder gesperrter Usage: `blocked`.

Die Hauptpool-Schwelle beträgt derzeit mehr als 10 Prozent Restwert je
relevantem Fenster. Standardmäßig darf die Usage für eine Bewertung höchstens
600 Sekunden alt sein; `--max-age` akzeptiert einen bewussten Override ab
60 Sekunden. Die Entscheidung ist eine lokale Auswertung und keine
Garantie, dass ein Modell extern verfügbar ist.

```bash
codex-usage policy evaluate alpha --role arbeitsbiene --format json
codex-usage policy status --role arbeitsbiene --format json
```

Rollen wie `teamleiterin`, `teamlead`, `leader`, `manager`, `master` und
`admin` sind von dieser Modellumschaltung ausgenommen und erhalten die
Entscheidung `unchanged`.

## PoolAuthority

PoolAuthority-Eigentümerdaten sind explizite Konfigurationseingaben. Ein
Datensatz umfasst Account- und Pool-ID, Provider, Verfügbarkeit, erlaubte
Modellfamilien und Lifecycles sowie Leadership- und Reasoning-Grenzen. Der
Owner-Modulpfad materialisiert daraus eine versionierte Source für den
Producer, ohne Nutzungsbeobachtungen oder sonstige Accountmetadaten als
Authority zu verwenden.

Die Generation ist ein optimistischer Schreibschutz: eine Änderung akzeptiert
nur die erwartete aktuelle Generation und fordert eine exakte Übereinstimmung
der Authority-Accountinventur mit der konfigurierten Accountinventur. Dieses
Repository bietet keine öffentliche CLI zum manuellen Verfassen von
PoolAuthority-Records; das konkrete Provisioning ist daher außerhalb dieses
Dokuments unbekannt bzw. durch die aufrufende Integration bestimmt.

Die Consumer-Sicht ist Teil der attestierten V2-Evidenz. Pfadnamen,
Dateisatz, Schema und Sicherheitsanforderungen stehen ausschließlich im
[V2-Contract](codex-usage-v2.md).

**Belege:** [Routinglogik](../src/codex_usage/routing.py),
[PoolAuthority-Owner](../src/codex_usage/pool_authority_owner.py),
[PoolAuthority-Validierung](../src/codex_usage/config.py) und
[V2-Contract](codex-usage-v2.md).
