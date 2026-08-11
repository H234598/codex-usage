# Account- und Schwellen-Einstellungen

Datum: 2026-08-11

## Ziel

Applet-Einstellungen sollen Account-Verwaltung, Anzeigeziele, Schwellenformatierung,
Warnungen und Reaktivierung vollständig und verständlich abbilden. Die CLI bleibt
Quelle für Account-Konfiguration. Das Applet schreibt Account-Änderungen über CLI-
Befehle, nicht direkt in `config.toml`.

## Ansatz

Die vorhandenen Cinnamon-Listen und die vorhandenen CLI-Pfade werden erweitert.
Keine eigene GTK-Dialogschicht und keine direkten TOML-Schreibzugriffe. Die
generische Listenkomponente erzeugt Dialoge und Trennlinien zwischen Feldern.

## Abrufwege und Accounts

Die Sektion `Abrufwege` heißt `Abrufwege und Accounts`.

Die zentrale Account-Tabelle enthält:

1. Account-ID
2. Label
3. Auth JSON (optional, Dateiauswahl)
4. Profilordner (Verzeichnisauswahl)
5. Browser für normalen Login/Abruf
6. Reaktivierungsbrowser (`auto`, Vivaldi, Chromium, Firefox)
7. Abrufweg (Direktabruf, Codex App Server)

Die Tabelle bietet `+` und Bearbeiten. Löschen und Umsortieren werden nicht als
neue destruktive Applet-Aktion eingeführt. Account-ID bleibt bei bestehenden
Zeilen Identität; neue Accounts werden über `+` angelegt. Label und alle übrigen
Account-Optionen sind änderbar.

Unter der Tabelle steht der Hinweis:

> Der Profilordner wird automatisch angelegt, wenn er noch nicht vorhanden ist.

Beim Anlegen oder Aktualisieren bleibt die bestehende CLI-Logik aktiv: absolute
Pfade werden validiert, Profilordner werden mit privaten Rechten angelegt und
`auth.json` bleibt optional. Leerer Auth-JSON-Wert bedeutet kein Auth-JSON.

Die Account-Übersicht liefert für die UI zusätzlich Profilordner, Auth-JSON-Pfad
und Reaktivierungsbrowser. Token-Inhalte werden nie ausgegeben.

## Formatierungsorte und Account-Anzeige

Unter `Formatierungsorte` entsteht eine zentrale Account-Anzeige-Tabelle:

1. Account-ID
2. Kürzel
3. Leiste
4. Hover
5. Klick-Menü

Die drei Anzeige-Spalten wählen jeweils zwischen `Account-ID`, `Label` und
`Kürzel`. Das Kürzel wird separat pro Account festgelegt und maximal auf die
bisherige Länge begrenzt. Standardwerte bleiben:

- Leiste: Kürzel
- Hover: Label
- Klick-Menü: Label

Die bisherige Spalte `Kürzel` in Tabelle `Leiste` entfällt. Vorhandene Kürzel
werden beim ersten Laden in die zentrale Tabelle migriert. Danach ist zentrale
Anzeigeeinstellung einzige Quelle für Kürzel und Account-/Label-Darstellung.

## Schwellenformatierung

In Prozent-, Datums-, Uhrzeit- und Restlaufzeit-Tabellen bleibt Account-ID oben.
Danach folgen Felder unabhängig von der Schwelle, dann Felder für den Bereich
oberhalb der Schwelle, danach die Schwelle selbst und zuletzt Felder für den
Bereich unterhalb der Schwelle.

Die sichtbaren Titel werden eindeutig gruppiert:

`Über der Schwelle Schriftart`, `Über der Schwelle Größe`, `Über der Schwelle
Fett` usw. sowie weiterhin `Unter der Schwelle Schriftart`, `Unter der Schwelle
Größe`, `Unter der Schwelle Fett` usw.

`Formatierungsmodus` und konkrete Datums-/Uhrzeit-/Dauerformate bleiben ohne
Schwellenpräfix und stehen im unabhängigen Block. Schwellen behalten ihre
fachlich korrekte Einheit: Prozenttabellen zeigen `Schwelle %`, die
Restlaufzeit-Tabelle `Schwelle Minuten`.

Die Listenreihenfolge erzeugt die Trennlinien im Cinnamon-Dialog automatisch.

## Warnungen

Die Benachrichtigungstabelle erhält die Spalte `Spark %` neben den bestehenden
5h- und Wochen-Schwellen. Der Wert ist eine eigene Account-Schwelle von 0–100 %
für alle bekannten Spark-Fenster. Warnungen und Fehler bleiben separat schaltbar.

Ein Account gilt nur dann als `no Spark`, wenn die Nutzungsdaten sicher kein
echtes Spark-Limit enthalten. Dann zeigt die Spalte exakt `no Spark`; es wird
keine Spark-Schwelle gespeichert oder akzeptiert. Spark-Warnungen werden
übersprungen, 5h- und Wochenwarnungen bleiben aktiv.

Fehlerhafte, unvollständige oder nicht bestätigte Spark-Daten sind nicht
`no Spark`; sie bleiben unbekannt und dürfen die Einstellung nicht löschen.
Erscheint später ein echtes Spark-Limit, wird das Feld wieder numerisch und
editierbar.

Neue Accounts übernehmen zunächst die globale `warning-threshold`. Alte
Warnungszeilen ohne `Spark %` erhalten denselben Default.

## Reaktivierung

Die Seite/Sektion `Reaktivierung` entfällt.

Der Schalter `Reaktivierungsoptionen...` wandert in den Tab `Codex Usage`.
Der globale sichtbare Schalter für einen isolierten Reaktivierungsbrowser
entfällt. Der Reaktivierungsbrowser wird pro Account in der zentralen
Account-Tabelle gewählt. Der normale Browser bleibt davon getrennt, weil Login,
Polling und OAuth-Reaktivierung unterschiedliche Codepfade nutzen.

Bestehende globale Reaktivierungsbrowser-Einstellung wird einmalig auf vorhandene
Accounts übertragen. Neue Accounts erhalten `auto`. Der CLI-Override beim
expliziten `reactivate`-Aufruf bleibt erhalten.

## CLI-Anbindung und Migration

Die CLI-Konfiguration erweitert `Account` um `reactivation_browser`. Validierung,
TOML-Laden und TOML-Speichern akzeptieren `auto`, `vivaldi`, `chromium` und
`firefox`; Standard ist `auto`.

Account-Anlegen/Aktualisieren liefert strukturierte Daten für die UI. Der
Applet-Reconcile-Pfad verarbeitet Label, Auth-JSON, Profilordner, normalen
Browser, Reaktivierungsbrowser und Backend in einem zentralen Ablauf. Fehler
bleiben im Applet sichtbar und dürfen keine halbe lokale Tabellenänderung als
erfolgreich markieren.

Settings-Migrationen müssen alte Tabellenwerte übernehmen:

- alte Account-Labels und Backend-Zuordnung
- alte Profil-/Auth-Pfade, sofern vorhanden
- alte Kürzel aus `account-panel-settings`
- globalen Reaktivierungsbrowser je Account
- fehlende Spark-Schwellen mit globalem Warn-Default

## CLI/Applet-Paritätsbericht

Nach der Implementierung wird ein optionaler Vergleich erstellt. Er ordnet jede
CLI-Funktion einer sichtbaren Applet-Einstellung oder Aktion zu und markiert
bewusst nicht abgebildete CLI-Aktionen, insbesondere Account-Löschen,
Diagnose-/Servicebefehle und einmalige Kommandozeilen-Overrides.

## Verifikation

Tests decken mindestens ab:

- Schema-Titel, Tabellenfelder und gewünschte Feldreihenfolge
- Profilordner-Erzeugung bei fehlendem Ordner
- optionales Auth JSON
- Label-/Account-Reconcile und Reaktivierungsbrowser
- Migration alter Kürzel und globaler Reaktivierungsbrowser-Einstellung
- Spark-Schwelle für 5h, Woche und sonstige Spark-Fenster
- `no Spark` bei sicher fehlendem Limit
- Erhalt numerischer Werte bei unbekannten/fehlerhaften Spark-Daten
- unveränderte 5h-/Wochenwarnungen
- Entfernung alter Leisten-Kürzel- und Reaktivierungs-UI

Ein kleiner assert-basierter Laufzeitcheck ergänzt die bestehenden Python- und
Applet-Tests.
