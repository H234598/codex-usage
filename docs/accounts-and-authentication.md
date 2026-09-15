# Accounts und Authentifizierungs-Provisionierung

Jeder Codex-Usage-Account hat eine ID, ein Label, ein separates Profil und ein
konfiguriertes Backend. IDs und Labels dürfen nicht als Zugangsdaten verstanden
werden. Die Unterstützung für Mehraccountbetrieb betrifft die lokale
Konfiguration; sie erteilt keine Berechtigung für einen fremden Account.

## Profil als Device-Login-Job anlegen

Der `profile create`-Befehl legt einen asynchronen Accountprofil-Job an. Er
verlangt Account-ID, Label, Browser, Backend und absoluten Profilpfad:

```bash
codex-usage profile create \
  --account-id alpha \
  --label Alpha \
  --browser firefox \
  --backend direct \
  --profile-dir /absolute/private/profile-directory
```

Der Befehl gibt JSON aus. Den Status liefert der Job nicht implizit nach;
dafür gibt es:

```bash
codex-usage profile jobs --account alpha
codex-usage profile job-status JOB_ID
```

Alternativ kann für einen bereits konfigurierten Account ein Device-Login
gestartet werden:

```bash
codex-usage profile device-login --account alpha
```

Der Vorgang prüft, ob der aufgerufene Codex-Client den expliziten
`--device-auth`-Modus anbietet, arbeitet mit einer privaten Staging-Umgebung
und veröffentlicht ein Ergebnis nur bei Erfolg. Die Login-Ausgabe kann
interaktive Werte enthalten: nicht kopieren oder speichern.

## Browser-Login und Reaktivierung

Für einen sichtbaren Login in das accountgebundene Browserprofil:

```bash
codex-usage login alpha
```

Bei abgelaufener Authentifizierung führt die isolierte Reaktivierung einen
Codex-Login aus und meldet danach den erforderlichen Auth-Sync-Status:

```bash
codex-usage reactivate alpha --browser auto --format json
```

`account auth-sync` ist eine explizite Integration mit einem konfigurierten
Masterjet-Endpunkt. Ob diese externe Gegenstelle vorhanden oder autorisiert
ist, kann dieses Repository nicht feststellen; ohne diese Voraussetzung ist
der Sync blockiert bzw. fehlgeschlagen.

## Schutzmodell

Profilverzeichnisse sind absolut, nicht als Symlink zugelassen und werden
privat angelegt. `profile layout --account alpha` kann die lokale Struktur
anzeigen; die Ausgabe enthält jedoch private Pfade und gehört nicht in
geteilte Logs. Auth-Dateien, ihre Speicherorte, ihr Format und ihr Inhalt sind
absichtlich nicht dokumentiert.

**Belege:** [CLI-Argumente](../src/codex_usage/cli.py),
[Profilstruktur](../src/codex_usage/profile_layout.py),
[Device-Login](../src/codex_usage/profile_login.py),
[Reaktivierung](../src/codex_usage/reactivate.py) und
[Konfigurationsvalidierung](../src/codex_usage/config.py).
