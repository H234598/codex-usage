# Verbrauch, Forecast und Tokenende

## Aktuelle Limits

Ein einmaliger Abruf erfasst alle konfigurierten Accounts oder ausgewählte
Accounts. Für maschinenlesbare Ausgabe:

```bash
codex-usage once --account alpha --format json
```

Limitfenster werden nur verarbeitet, wenn ihre Identität bekannt und die
Werte brauchbar sind. Der aktuelle V2-Vertrag erlaubt für Limits und Trends
die Fenster 18&nbsp;000 Sekunden (5h), 604&nbsp;800 Sekunden (Woche) und
2&nbsp;592&nbsp;000 Sekunden (30 Tage/Credit-Sonderfenster). Nicht jedes
Provider-Payload liefert jedes Fenster.

## Verbrauchsberechnung

`consumption` berechnet einen Rückblick in Prozentpunkten aus der privaten
Historie. Der Zeitraum besteht aus `--amount` und einer der Einheiten
`minutes`, `hours`, `days`, `weeks`; `--limit-window` akzeptiert `short`,
`weekly`, `monthly`, `spark` oder `all`.

```bash
codex-usage consumption \
  --account alpha --amount 1 --unit hours \
  --pool main --limit-window short --format json
```

Optionale EMA-Glättungen sind `ema-5`, `ema-10`, `ema-20`, `ema-40`, `ema-80`,
`ema-160`, `ema-320` und `ema-640`; ohne Angabe gilt `none`. Ein berechnetes
`estimated_seconds_to_exhaustion` ist nur dann vorhanden, wenn die vorliegenden
Samples eine valide Berechnung zulassen. Es ist keine Zusage über künftige
Providerlimits oder Tokenverfügbarkeit.

## Tracker-Evidenz im V2-Export

Der Producer kann Tracker-Evidenz aus gültigen Historiensamples nur für `main`
und `gpt-5.3-codex-spark` bilden. Die Zeitkonstante der dafür implementierten
EMA60 beträgt 3&nbsp;600 Sekunden.
Für eine vollständige Trendreihe gelten unter anderem dieselbe Account-/Pool-/
Fenster-/Resetgeneration, zeitlich aufsteigende Samples, positive
Verbrauchsänderung und kein Intervall größer als 3&nbsp;600 Sekunden. Nach
mehr als 900 Sekunden seit dem letzten Sample wird die Evidenz als `stale`
klassifiziert; ein einzelnes gültiges Sample ist `insufficient`.

Die Projektion ist auf 100 Prozent begrenzt und beschreibt den erwarteten
verbrauchten Prozentsatz am Reset. Sie ist nicht „Tokenende“ im Sinn einer
garantierten Restlaufzeit. Bei fehlender Evidenz, Resetzeit oder einem
unbekannten Providerwert ist das Tokenende unbekannt.

Die exakten Formeln, Felder und Validierungsregeln sind normativ in
[codex-usage-v2.md](codex-usage-v2.md) festgelegt.

**Belege:** [CLI-Verbrauchsoptionen](../src/codex_usage/cli.py),
[Berechnung und EMA60](../src/codex_usage/consumption.py),
[Fensterparser](../src/codex_usage/usage_limits.py) und
[V2-Contract](codex-usage-v2.md).
