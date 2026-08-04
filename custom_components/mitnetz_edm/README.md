# Mitnetz Strom EDM – Home Assistant Integration

<img src="../../logo.mitnetz.svg" alt="Mitnetz Strom Logo" width="200">

> Das Mitnetz-Strom-Logo ist eine Marke der MITNETZ STROM GmbH und wird hier
> ausschließlich zur Identifikation des Portals verwendet, mit dem diese
> Integration kommuniziert. Dieses Projekt ist inoffiziell, steht in keiner
> Verbindung zu MITNETZ STROM GmbH und wird nicht von ihr unterstützt oder
> autorisiert.

Custom-Integration für das Smartmeter-Portal der Mitnetz Strom
(`https://edm.mitnetz-strom.de`). Ruft Messwerte ausschließlich über die
HTTP-API ab (aiohttp) – kein Selenium, kein Playwright, kein Browser.

## Funktionsweise

1. **Login**: `GET /login` (holt Session-Cookies + CSRF-Token aus dem HTML),
   dann `POST /login_check` mit Benutzername/Passwort/CSRF-Token. Erfolg wird
   am Redirect nach `/analytics` erkannt.
2. **Messpunkte**: `GET /sidebarMultiMp/rlm` liefert alle Messpunkte und deren
   OBIS-Lines automatisch – keine manuelle Konfiguration nötig.
3. **Zeitreihen**: `GET /data/mpline/genericto/<mp>/<line>/<from>/<to>/base`
   liefert die eigentlichen Messwerte.
4. **Session-Wiederherstellung**: Bei 401/403, Redirect auf `/login` oder
   HTML statt JSON wird automatisch neu eingeloggt (max. 3 Versuche, Backoff
   5 s / 15 s / 30 s). Nach Ausschöpfung werden keine weiteren Login-Versuche
   unternommen (Schutz vor Kontosperrung); der Coordinator wird
   `unavailable`, ein Fehler wird geloggt und eine Persistent Notification
   erzeugt.

## Installation (HACS)

1. Dieses Repository als "Custom repository" (Kategorie: Integration) in
   HACS hinzufügen.
2. "Mitnetz Strom EDM" installieren.
3. Home Assistant neu starten.
4. Einstellungen → Geräte & Dienste → Integration hinzufügen → "Mitnetz
   Strom EDM" auswählen.

## Manuelle Installation

Den Ordner `custom_components/mitnetz_edm` in das `custom_components`-
Verzeichnis der Home-Assistant-Konfiguration kopieren und Home Assistant neu
starten.

## Konfiguration

Beim Einrichten werden abgefragt:

- **Benutzername / Passwort** – Zugangsdaten für das EDM-Portal.
- **Updateintervall** – 1–1440 Minuten (Standard: 15 Minuten). Später über
  die Options-Flow änderbar.
- **Nachladefenster** – 15 Minuten bis 30 Tage (Standard: 24 Stunden). Bei
  jedem Poll werden Daten vom letzten bekannten Zeitstempel minus diesem
  Überlappungsfenster bis "jetzt" nachgeladen, um verspätet eintreffende
  Messwerte automatisch zu korrigieren.
- **Timeout** – HTTP-Timeout in Sekunden.

## Sensoren

Für jede vom Portal gemeldete OBIS-Line wird automatisch ein Sensor
angelegt. Bekannte Codes werden benannt:

| OBIS-Code      | Name         |
|----------------|--------------|
| `1-1:1.29.0`   | Bezug        |
| `1-1:2.29.0`   | Einspeisung  |

Unbekannte OBIS-Codes werden ebenfalls als Sensor angelegt (Name = Code).

Die Integration erkennt automatisch, ob eine Line Zählerstände (monoton
steigend) oder Intervallenergie liefert, und setzt die `state_class`
entsprechend, damit die Sensoren direkt im Energy Dashboard genutzt werden
können.

## Diagnostics

Über "Diagnose herunterladen" stehen u. a. Sessionalter, letzter Login,
letztes Update, Anzahl Messpunkte/Sensoren und letzte API-Antwortzeit zur
Verfügung.

## Service: `mitnetz_edm.import_history`

Erlaubt das gezielte Nachladen eines beliebigen Zeitraums:

```yaml
service: mitnetz_edm.import_history
data:
  start: "2026-06-01T00:00:00"
  end: "2026-06-30T23:59:59"
```

## Hinweise

- Es wird ausschließlich `aiohttp` verwendet (kein `requests`, keine
  Blocking Calls, keine Threads).
- Die Messpunkt-IDs werden nicht konfiguriert, sondern zur Laufzeit über die
  API ermittelt.
