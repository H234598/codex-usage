# Architektur

Codex Usage erfasst und verwaltet lokale Nutzungsdaten für konfigurierte
ChatGPT-Codex-Accounts. Die Paketbeschreibung lautet „Poll ChatGPT Codex
analytics usage limits for multiple accounts.“ Die Anwendung ist eine lokale
Python-Distribution; ihr Einstiegspunkt ist `codex-usage`.

## Datenfluss und Zuständigkeiten

```text
Konfiguration/Accountprofil
        │
        ├─ direkter Abruf | App-Server | Browser/Bridge
        ▼
 aktuelle AccountUsage ──► private aktuelle Speicherung + Historie
        │                         │
        ├─ CLI: Übersicht, Verbrauch, Routing
        └─ attestierter V2-Producer ──► immutable Evidenzgeneration
```

Die Konfiguration enthält Accounts, Abrufintervall, Analytics-URL, Headless-
Vorgabe, Masterjet-Verbindung und PoolAuthority-Eigentümerdaten. Standardwerte
sind ein Intervall von 300 Sekunden, die Codex-Analytics-URL auf
`chatgpt.com` und `headless=true`. Profile gehören zu einzelnen Accounts und
werden vor Zugriff auf private, nicht verlinkte Pfade geprüft.

`AccountUsage` hält den Status eines Accounts sowie den Haupt-Pool, optionale
Modell-Pools und Limitfenster. Verarbeitete Accountstatus sind `ok`, `partial`,
`login_required`, `error` und `blocked`; das V2-Transportformat hat eine
eigene, engere Status-Allowlist. Ein fehlender oder nicht beweisbarer Wert ist
kein freier Schätzwert.

## Abrufwege

- `direct` und `app-server` sind die konfigurierbaren Backends.
- Der Browser-Abruf nutzt ein persistentes, accountgebundenes Profil und die
  konfigurierte Analytics-Seite. Login- und Cloudflare-Zustände werden als
  Ergebnisstatus behandelt, nicht als Limitwerte interpretiert.
- Die Browser-Bridge kann lokal auf Loopback laufen. Eine nicht lokale Bindung
  benötigt TLS und muss explizit freigegeben werden.

Die genaue Behandlung von Backend-Identität, Cache-Invaliderung und Rohdaten
ist Implementierungs- und Sicherheitslogik, keine stabile externe API.

## Lokale Speicherung und V2-Integration

Der lokale Zustand und die Historie sind Produzenteneingaben. Der
Integrationsproducer liest sie unter privaten Dateibedingungen, erzeugt daraus
sanitisierte Evidenz und veröffentlicht nur eine immutable V2-Generation.
Consumer sollen ausschließlich dem Pointer `current.json` und dessen
referenzierter Generation folgen. Der frühere V1-Festpfad ist nach dem Cutover
keine Consumerquelle.

Das vollständige, normative V2-Protokoll – Aufrufform, Dateisatz, Schema,
Grenzen, Fehler und Attestierung – steht in
[codex-usage-v2.md](codex-usage-v2.md). Diese Seite ist eine Architekturkarte
und ersetzt den Vertrag nicht.

## Sicherheitsgrenzen

Konfigurations-, Profil-, Routing- und Integrationspfade werden als private
lokale Daten behandelt. Der Producer entfernt Accountlabel, Backend-Identitäten
und andere nicht erlaubte Quellen aus dem V2-Dokument. Zugangsdaten,
Auth-Dateiinhalte und Token gehören weder in diese Dokumentation noch in
Integrationspayloads.

**Belege:** [Paket- und CLI-Einstiegspunkt](../pyproject.toml),
[Konfigurationsmodell](../src/codex_usage/config.py),
[Datenmodell](../src/codex_usage/models.py),
[Browser-Abruf](../src/codex_usage/browser.py) und
[V2-Vertrag](codex-usage-v2.md).
