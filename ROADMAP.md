# Roadmap

Dieser Status bezieht sich ausschließlich auf den attestierten D297-Nachfolger `0.6.538`. „Integriert“ bedeutet: Implementierung und fokussierte Tests sind in diesem Stand vorhanden. Es bedeutet nicht, dass eine Funktion auf einem beliebigen Rechner installiert, konfiguriert oder vom Anbieter freigeschaltet ist.

## Integriert

- Multi-Account-Konfiguration mit getrennten Profilverzeichnissen, Browserauswahl (`firefox` oder `chromium`) sowie den Abrufwegen `direct` und `app-server`.
- CLI für Accounts, Login/Reaktivierung, einmaligen Abruf, Watch, Watchdog, Historie, Verbrauch/Forecast, Health, Browser-Bridge, Profiljobs, Routing/Pool-Authority und Dienstverwaltung.
- Validierte Nutzungsfenster für fünf Stunden, Woche und 30 Tage sowie providerabhängige Modellpools, optionale Credits und Reset-Zähler.
- Cinnamon-Applet mit konfigurierbarer Wertanzeige. Die Legacy-Konfiguration enthält die vier Kompatibilitätsfelder `slot1` bis `slot4`.
- Verwalteter, gehärteter `systemd --user`-Dienst mit dediziertem Integrations-Watchdog und versioniertem V2-Integrations-Snapshot.

## Aktuell in Arbeit

Für diesen Basisstand wurde in den geprüften Repository-Quellen kein belastbarer Eintrag zu einer aktuell laufenden Dokumentations- oder Produktarbeit festgestellt. Diese Kategorie bleibt absichtlich leer; sie ist keine Aussage, dass außerhalb dieses Repositories niemand arbeitet.

## Blockiert

- **Reset-Einlösung:** Der Code akzeptiert einen bekannten Reset-Zähler, aber die Einlösungsfunktion endet auch bei gesetzter Fähigkeit mit `NotImplementedError`. CLI und Applet bieten deshalb keine Einlösung an. Eine Anbieterfähigkeit allein reicht nicht als Freigabe.
- **Anbieterwerte:** Limits, Credits, Resetzeitpunkte und zusätzliche Pools hängen von den tatsächlich gelieferten, konsistenten Anbieterantworten ab. Fehlende, widersprüchliche oder ungültige Werte bleiben unbekannt bzw. ungültig; sie werden nicht geschätzt oder erfunden.

## Geplant

Für diesen Basisstand wurde kein belastbarer Plan, Issue oder Termin in den geprüften Repository-Quellen festgestellt. Neue Absichten gehören erst nach überprüfbarer Evidenz in diese Kategorie.

## Pflegehinweis

Bei Änderungen zuerst den Status gegen Implementierung, Tests und den Ziel-Release prüfen. Eine Absicht, ein lokaler Prototyp oder ein nicht konfigurierter externer Dienst darf nicht als integrierte Livefunktion beschrieben werden.
