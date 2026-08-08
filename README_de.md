# Mitnetz Strom EDM – Home Assistant Integration

> MITNETZ STROM und das zugehörige Logo sind Marken der MITNETZ STROM GmbH. Der
> Name wird hier ausschließlich zur Identifikation des Portals verwendet, mit
> dem diese Integration kommuniziert. Dieses Projekt ist inoffiziell, steht in
> keiner Verbindung zu MITNETZ STROM GmbH und wird nicht von ihr unterstützt
> oder autorisiert. Dieses Repository enthält kein Logo; das Integrationssymbol
> wird separat bei [home-assistant/brands](https://github.com/home-assistant/brands)
> eingereicht.

*English version: [README.md](README.md)*

Custom-Integration für das Smartmeter-Portal der Mitnetz Strom
(`https://edm.mitnetz-strom.de`). Ruft Messwerte ausschließlich über die
HTTP-API ab (aiohttp) – kein Selenium, kein Playwright, kein Browser.

## Funktionsweise

1. **Login**: `GET /login` (holt Session-Cookies + CSRF-Token aus dem HTML),
   dann `POST /login_check` mit Benutzername/Passwort/CSRF-Token. Erfolg wird
   am Redirect nach `/analytics` erkannt.
2. **Messpunkte**: `GET /sidebarMultiMp/rlm` liefert alle Messpunkte und deren
   OBIS-Lines automatisch – keine manuelle Konfiguration nötig. Die Antwort
   verschachtelt die Einträge unter einem `root`-Knoten; der reine OBIS-Code
   wird aus `details."measured quantity"` bzw. ersatzweise aus dem Teil von
   `name` vor dem ersten Komma extrahiert.
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
- **Wert gilt für max. N Messintervalle** – 1–96 (Standard: 3). Siehe
  [Veraltete Werte](#veraltete-werte).
- **Timeout** – HTTP-Timeout in Sekunden.

Zugangsdaten lassen sich später ändern, ohne den Eintrag zu löschen:
Einstellungen → Geräte & Dienste → Mitnetz Strom EDM → ⋮ → **Neu
konfigurieren**. Die neuen Daten werden vor dem Speichern mit einem echten
Login geprüft, ein Tippfehler kann also keinen funktionierenden Eintrag
zerstören – und die Entity-Registry (und damit die aufgezeichnete Historie)
bleibt erhalten.

## Sensoren

Für jede vom Portal gemeldete OBIS-Line wird automatisch ein Sensor angelegt.
Einheit, Device-Class und Anzeigename werden aus dem OBIS-Code abgeleitet,
nicht angenommen – siehe [Einheiten und Messgrößen](#einheiten-und-messgrößen).
Unbekannte Codes bekommen weiterhin einen Sensor, aber ohne Einheit und
Device-Class statt mit einer falschen.

Attribute je Sensor: `obis_code`, `measurement_point`, `quantity`,
`direction`, `interval_start`, `interval_end`, `interval_minutes`,
`data_age_minutes`, `intervals_in_window`.

### Abgeleitete Leistungssensoren

Jede Intervallenergie-Line (OBIS `D` = 29) bekommt zusätzlich einen
**kW-Sensor**, z. B. „Bezug Leistung" und „Einspeisung Leistung". Er trägt die
mittlere Leistung des jeweils letzten Messintervalls:

    P[kW] = E[kWh] / t[h]

Beim üblichen 15-Minuten-Intervall also schlicht Faktor vier. Ein Wert pro
Intervall — die Verlaufsgrafik zeigt damit eine echte Leistungskurve statt
eines einzelnen Tagespeaks.

Das ist keine eigene Näherung. Der portaleigene Endpoint

    /data/timeseries/moment/0/<start>/<value>/<end>/<rkmgId>/<period>

macht serverseitig exakt diese Division — `<value>` ist ein *Eingabewert*, die
größte Intervallenergie in kWh, und `<rkmgId>` ist die Line-ID aus dem Feld
`rkmgId` der Zeitreihen-Antwort. Nachgemessen:

| Eingabe (kWh) | Periode  | Ausgabe (kW) |
|---------------|----------|--------------|
| 1.1104        | `15mins` | 4.4416       |
| 2.2208        | `15mins` | 8.8832       |
| 2.0           | `15mins` | 8            |
| 1.1104        | `hours`  | 1.1104       |

Da das Intervall ohnehin aus den Zeitstempeln bekannt ist, rechnet die
Integration lokal, statt pro Wert einen zusätzlichen HTTP-Request zu stellen.

Zähler mit echten Leistungs-Lines (`D` = 4/5/6/7) behalten diese und bekommen
keinen abgeleiteten Zwilling. Kumulative Zählerstände (`D` = 8/9) werden nicht
umgerechnet.

### Wo die Min-/Max-Werte stecken

Home Assistant legt Leistungsstatistiken als `mean`/`min`/`max` pro Stunde ab.
Die abgeleitete kW-Reihe liefert damit Minimum, Maximum und Mittelwert je
Stunde für jeden beliebigen Zeitraum über die Statistik-Karte — ohne
zusätzlichen Sensor. Das entspricht dem, was auch das Portal zeigt: fragt man
die Zeitreihe mit `aggregationperiod=hours` ab, sind die Arrays
`minima`/`maxima`/`minTimestamps`/`maxTimestamps` gefüllt und enthalten die
Extremwerte der vier Viertelstunden je Stunde. In der 15-Minuten-Basisauflösung
bleiben sie leer, weil es dort nichts mehr zu aggregieren gibt.

## Einheiten und Messgrößen

Die Integration leitet die physikalische Bedeutung jeder Line aus dem
OBIS-Code ab (IEC 62056-61), statt pauschal kWh anzunehmen. Maßgeblich ist
das D-Feld:

| D-Feld | Bedeutung             | Einheit | `state_class`      |
|--------|-----------------------|---------|--------------------|
| 4/5    | Mittelwert (Leistung) | kW      | `measurement`      |
| 6      | Maximum (Leistung)    | kW      | `measurement`      |
| 7      | Momentanwert          | kW      | `measurement`      |
| 8      | Zählerstand           | kWh     | `total_increasing` |
| 29     | Intervallenergie      | kWh     | `measurement`      |

Das C-Feld bestimmt die Richtung (`1` = Bezug, `2` = Einspeisung); daraus
entsteht der Anzeigename, z. B. `1-1:2.6.0` → "Einspeisung Maximum".

Wichtig: Maximum- und Mittelwert-Lines sind **Leistungswerte in kW**, die pro
Messintervall steigen *und fallen*. Würden sie als `total_increasing`
veröffentlicht, wertete Home Assistant jeden Rückgang als Zählerreset — das
erzeugt unsinnige Treppenkurven.

## Messintervalle, Aktualität und Verlaufsgrafik

### Welche Intervallkante ein Zeitstempel benennt

Das Portal benennt ein Intervall nach seinem **Ende**. Sein eigener CSV-Export
sagt das unmissverständlich: ein Wert von 0.803275 kWh, der in der Zeile
„14:00:00 – 14:15:00" steht, taucht in der Übersicht als „local maximum
0.803275 kWh, Maximum time 14:15" auf.

Die JSON-Antwort liefert beide Kanten — `timestampsLeft` (Beginn) und
`timestamps` (Ende); in einer aufgezeichneten Antwort unterscheiden sie sich
bei allen 157 Punkten um exakt ein Intervall. Die Integration verankert jeden
Punkt am **Beginn**: bevorzugt über `timestampsLeft`, ersatzweise durch
Zurückschieben von `timestamps` um den Medianabstand.

Das ist folgenreicher als es aussieht. Liest man `timestamps` und behandelt es
als Beginn, rutscht jeder Wert ein Intervall in die Zukunft — die
HA-Kurve läuft der Portalgrafik hinterher, und ein Intervall auf einer vollen
Stunde (14:45–15:00, benannt als 15:00) wird in die *nächste* Stunde der
Langzeitstatistik einsortiert.

### Intervallerkennung

Das Messintervall wird automatisch aus den Zeitstempeln der Portaldaten
abgeleitet (Median der Abstände, damit einzelne Lücken oder ein
Zeitumstellungssprung das Ergebnis nicht verfälschen).

**Wichtig:** Home Assistant kann Entity-States nicht rückdatieren. Ein State
wird immer zum Zeitpunkt des Pollings aufgezeichnet. Liefert ein Poll mehrere
neue Intervalle auf einmal, erreicht nur das neueste den State – die übrigen
stehen ausschließlich in den Langzeitstatistiken (mit korrektem Zeitstempel).
Damit die Verlaufsgrafik vollständig ist, sollte das Updateintervall höchstens
so groß sein wie das Messintervall; andernfalls wird eine Warnung geloggt.

### Veraltete Werte

Min-/Max- und Leistungswerte (OBIS `D` = 4/5/6/7) gelten immer nur für *ein*
Messintervall. Wird kein neuerer Wert geliefert, meldet der Sensor nach
`stale_intervals` Intervallen `unbekannt`, statt den alten Wert weiter
auszugeben. Ohne diese Regel zieht sich der letzte Spitzenwert als flaches
Plateau durch die Grafik – auch nachts, wenn gar nichts eingespeist wird.

Kumulative Zählerstände (OBIS `D` = 8/9) sind davon ausgenommen: ein
Zählerstand bleibt gültig, bis ein neuerer kommt.

Der Wert ist über die Option **"Wert gilt für max. N Messintervalle"**
konfigurierbar (Standard: 3, Bereich 1–96).

## Langzeitstatistik

Das Portal liefert Messwerte rückwirkend: ein Poll um 12:00 kann Werte von
09:15, 09:30 usw. enthalten. Ein Sensor-State kann immer nur "Wert zum
Poll-Zeitpunkt" abbilden. Deshalb schreibt die Integration die Intervalle
zusätzlich als *externe* Statistik unter dem Präfix `mitnetz_edm:` — dort
behält jeder Wert seinen eigenen Zeitstempel.

Statistikzeilen müssen auf volle Stunden ausgerichtet sein, viertelstündliche
Werte werden daher pro Stunde aggregiert:

- Energie-Lines → `sum` (fortlaufende Gesamtsumme, Energy-Dashboard-tauglich)
- Leistungs-Lines → `mean` / `min` / `max`

Jede Intervallenergie-Line liefert zwei Reihen: ihre Energie unter
`mitnetz_edm:<line_id>` und die abgeleitete Leistung unter
`mitnetz_edm:<line_id>_power`.

Das Nachladefenster überlappt bewusst mit bereits geschriebenen Stunden, damit
verspätete Messwerte korrigiert werden. Die Summenberechnung setzt dabei an
der Stunde *vor* dem Importfenster an, sodass nichts doppelt gezählt wird.

## Diagnostics

Über "Diagnose herunterladen" stehen u. a. Sessionalter, letzter Login,
letztes Update, Anzahl Messpunkte/Sensoren, das erkannte Messintervall und die
letzte API-Antwortzeit zur Verfügung. Zugangsdaten werden geschwärzt.

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
- Es wird ein expliziter `Accept-Language`-Header gesendet: das Portal
  formatiert einige numerische Felder lokalisiert und lieferte sonst deutsche
  Zahlenformatierung (Komma als Dezimaltrenner), die `float()` nicht parsen
  kann.
- Die Messpunkt-IDs werden nicht konfiguriert, sondern zur Laufzeit über die
  API ermittelt.
- Die Integration hängt für den Statistikimport von `recorder` ab.

## Tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-test.txt
pytest tests/ --cov=custom_components.mitnetz_edm
```

Die Suite läuft über `pytest-homeassistant-custom-component` gegen eine echte
Home-Assistant-Instanz (inkl. Recorder) und benötigt daher die von Home
Assistant geforderte Python-Version (3.13+). HTTP wird mit dem `aioclient_mock`
aus demselben Paket simuliert statt mit `aioresponses`, das an einer internen
aiohttp-Signaturänderung zerbrochen ist.

| Datei                          | Schwerpunkt                                                   |
|--------------------------------|---------------------------------------------------------------|
| `test_obis.py`                 | OBIS-Klassifizierung, kW-statt-kWh-Regression                  |
| `test_power.py`                | kWh-zu-kW-Umrechnung gegen portal-verifizierte Werte           |
| `test_interval_edges.py`       | Punkte am Intervallbeginn, geprüft gegen echte Portalantwort   |
| `test_derived_power_sensor.py` | Abgeleitete kW-Entity, Staleness, eigene Statistik-ID          |
| `test_api.py`                  | Login/CSRF, XSRF- und XHR-Header, Datumsformat, Sessionablauf, `root`-Knoten, OBIS-Extraktion |
| `test_coordinator.py`          | Re-Login, Backoff, Sperrschutz, Nachladefenster                |
| `test_sensor.py`               | Einheiten, State-Classes, Wert folgt dem Intervall             |
| `test_interval_staleness.py`   | Intervallerkennung und Staleness-Regel                         |
| `test_statistics.py`           | Stundenaggregation (reine Logik)                               |
| `test_statistics_import.py`    | Schreiben in den Recorder, keine Doppelzählung                 |
| `test_config_flow.py`          | Einrichtung, Fehlerfälle, Options-Flow, Reconfigure-Flow       |
| `test_diagnostics_services.py` | Diagnostics ohne Zugangsdaten, `import_history`                |
