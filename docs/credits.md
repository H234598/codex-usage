# Credits

Credits sind in Codex Usage kein allgemeines Kontobuch. Es gibt zwei getrennte
Konzepte:

1. Ein vom Provider gemeldetes Credit-Limit kann als spezieller Limit-Pool
   `credits` erscheinen.
2. Die Routing-Policy steuert, ob bezahlte Übernutzung („paid overage“) für
   einen Scope erlaubt ist und setzt optionale stündliche, wöchentliche und
   monatliche Obergrenzen.

Ein roher absoluter Creditbetrag ohne Nenner ist im ausschließlich
prozentualen V2-Schema nicht darstellbar. Der Producer veröffentlicht dafür
keinen erfundenen Prozentwert und keinen Credit-Trend. Ein fehlender optionaler
Creditwert bleibt unbekannt; ein widersprüchlicher oder invalider Wert stoppt
den V2-Publish fail-closed. Details stehen im
[V2-Contract](codex-usage-v2.md).

## Routing-Policy

Die lokale Policy ist standardmäßig nicht vorhanden und wird dann als leere,
restriktive Policy behandelt. Regeln können global oder für `account`, `group`,
`agent` und `job` gesetzt werden. `allow`, `deny` und `inherit` entsprechen
den CLI-Werten:

```bash
codex-usage policy set global deny
codex-usage policy set account allow --id alpha
codex-usage policy set-limits --scope account --id alpha --hourly 5 --weekly 20
codex-usage policy overview
```

Der spezifischste passende Scope entscheidet über paid overage; Credit-Limits
werden ebenfalls scopeweise aufgelöst. Ein Scope-Limit von `0` wird als
„globales Limit erben“ behandelt, während ein globales `0` die bestehende
Bedeutung „Cap deaktiviert“ hat.

`policy evaluate` benötigt eine Rolle und aktuelle, zum Account passende
Usage-Provenance:

```bash
codex-usage policy evaluate alpha --role arbeitsbiene --format json
```

Bei stale, ungültiger oder unvollständiger Usage blockiert die Bewertung
fail-closed. Die Entscheidung `credits` ist nur möglich, wenn der Hauptpool
unter der Schwelle liegt und paid overage explizit erlaubt wurde; sie bucht
keine Credits und ruft keinen externen Zahlungsdienst auf.

**Belege:** [Policy-CLI](../src/codex_usage/cli.py),
[Policy-Auflösung und fail-closed Routing](../src/codex_usage/routing.py) und
[Credit-Validierung im V2-Contract](codex-usage-v2.md).
