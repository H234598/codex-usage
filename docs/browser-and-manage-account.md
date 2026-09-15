# Browser und „Manage Account“

Der Browser-Abruf verwendet pro Account ein persistentes Profil. Unterstützte
Abrufbrowser sind `firefox` und `chromium`; die isolierte Reaktivierung erlaubt
`auto`, `vivaldi`, `chromium` und `firefox`. `auto` bevorzugt laut CLI-Hilfe
Vivaldi.

## Sichtbarer Login und Account-Verwaltung

```bash
codex-usage login alpha
codex-usage account manage alpha --format json
```

`login` öffnet die konfigurierte Analytics-Seite im Accountprofil. `account
manage` öffnet die Accountverwaltung im isolierten Reaktivierungsbrowser.
Beide Befehle sind interaktive Browseroperationen; ein Erfolg bedeutet nicht,
dass Limits abgerufen oder ein externer Accountzustand geändert wurde.

Für einen einmaligen, sichtbaren Nutzungsabruf:

```bash
codex-usage once --account alpha --headed --format json
```

Der Browser erkennt Login- und Cloudflare-Seiten als solche und erzeugt dafür
keine erfundenen Limitwerte. Zu Diagnosezwecken stehen zur Verfügung:

```bash
codex-usage diagnose alpha --headed
codex-usage probe alpha --headless
```

`diagnose` kann optional Dateien schreiben; `probe` kann Rohkandidaten
speichern. Beide Optionen sind deshalb nur in einem geschützten lokalen
Arbeitsbereich zu verwenden. Die Dokumentation enthält keine Rohdatenbeispiele.

## Browser-Bridge

Die Bridge ist eine lokale Alternative, um browserseitig erfasste Daten an
Codex Usage zu übergeben. Ihr Server bindet standardmäßig an `127.0.0.1:8765`.
Eine Remote-Bindung erfordert sowohl explizite Freigabe als auch ein
TLS-Zertifikat und den zugehörigen privaten Schlüssel; diese Materialien werden
hier nicht beschrieben.

```bash
codex-usage bridge-server --host 127.0.0.1 --port 8765
```

Snippet- und Extension-Befehle erzeugen bzw. zeigen lokale Artefakte und sind
damit schreibend beziehungsweise potentiell sensitiv. Endpunkt-, Token- und
Artefaktwerte gehören nicht in geteilte Beispiele.

**Belege:** [CLI-Browserbefehle](../src/codex_usage/cli.py),
[Browser-Abruf und Statusbehandlung](../src/codex_usage/browser.py),
[isolierter Browser-Helper](../src/codex_usage/oauth_browser.py) und
[Bridge-TLS-Prüfung](../src/codex_usage/bridge.py).
