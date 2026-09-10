# Spezifikation: Household Task Agent

## 1. Ziel und Umfang

Der Household Task Agent verteilt Haushaltsaufgaben automatisch auf die Bewohner eines Haushalts. Er läuft als schlanker Python-Hintergrunddienst auf einem Raspberry Pi 3B und veröffentlicht täglich zu einer konfigurierbaren Uhrzeit die heutigen Zuteilungen in einem Telegram-Chat.

Verbindliche Ziele:

- Standard-Ausführungszeit: `06:00` in der konfigurierten lokalen Zeitzone.
- Bewohner, Rollen und feste freie Wochentage sind über JSON konfigurierbar.
- Ein separat bearbeitbarer JSON-Aufgabenpool enthält wöchentliche und monatliche Aufgaben.
- Aufgaben werden zufällig, aber belastungsabhängig über ihren Zeitraum verteilt, nicht vollständig am Montag oder Monatsersten vergeben.
- Kinder erhalten nur allgemein freigegebene Aufgaben. Erwachsenenaufgaben haben bei der Planung Vorrang vor allgemeinen Aufgaben.
- Die normale Kapazität beträgt eine Aufgabe pro Bewohner und aktivem Tag; dieser Wert ist konfigurierbar.
- Zusätzliche Aufgaben und aufgabenfreie aktive Tage werden möglichst gerecht verteilt.
- Feste freie Tage erzeugen keine Nachholpflicht.
- Zuteilungen und Statistiken bleiben über Neustarts erhalten.
- Am Monatsersten wird vor der neuen Zuteilung die Statistik des Vormonats über Telegram ausgegeben.
- Sämtliche Telegram-Ausgaben erfolgen auf Deutsch.

Die erste Version zählt **zugewiesene**, nicht nachweislich erledigte Aufgaben. Eine Zuteilung verbraucht die Aufgabe für den jeweiligen Zeitraum. Telegram-Schaltflächen zur Erledigungsbestätigung, manuelle Umbuchungen, Weboberfläche, Aufwandsgewichte und mehrere Haushalte gehören nicht zum Umfang.

## 2. Laufzeit und Betrieb

- Hardware: Raspberry Pi 3B, 1 GB RAM.
- Betriebssystem: gepflegtes Raspberry Pi OS Lite, 32 oder 64 Bit.
- Sprache: Python; mindestens Python 3.9 kompatible Sprachmittel. Für den Produktivbetrieb ist eine noch sicherheitsgepflegte Python-Version erforderlich.
- Speicherung: lokale UTF-8-JSON-Dateien, kein Datenbankdienst.
- Betrieb: `systemd` unter einem eigenen, nicht privilegierten Benutzer.
- Projektdateien werden relativ zum Projektverzeichnis aufgelöst, unabhängig vom Arbeitsverzeichnis des Dienstes.
- Keine rechenintensive Dauerplanung: Planung beim Start, bei Perioden-/Konfigurationswechseln und vor der Tageszuteilung; dazwischen wartender Scheduler.
- Lokale Datumsberechnung mit IANA-Zeitzone, beispielsweise `Europe/Berlin`; technische Zeitstempel in UTC.

## 3. Geplante Dateien

```text
household-agent/
  SPEC.md
  README.md
  main.py
  requirements.txt
  .gitignore
  .env.example
  .env
  config/
    Settings.json
    tasks.json
  storage/
    state.json
  household_agent/
    config.py
    allocation.py
    state.py
    telegram.py
  tests/
  deploy/
    household-agent.service
```

`config/Settings.json` ist der verbindliche, unter Linux groß-/kleinschreibungssensitive Dateiname. `.env` und `storage/` werden nicht versioniert. `.env.example` enthält ausschließlich Platzhalter. Die Modulaufteilung darf bei der Umsetzung schlank bleiben; sie ist keine Forderung nach einem Framework.

## 4. Konfiguration

### 4.1 Telegram-Zugang: `.env`

```dotenv
TELEGRAM_BOT_TOKEN="replace_with_bot_token"
TELEGRAM_CHAT_ID="replace_with_chat_id"
```

- Beide Werte sind erforderlich. Die Chat-ID darf insbesondere eine negative Gruppen-ID sein.
- Bereits gesetzte Prozess-Umgebungsvariablen haben Vorrang vor `.env`.
- Token und vollständige Telegram-Anfrage-URLs dürfen nicht geloggt werden.
- `.env` ist nur für den Dienstbenutzer lesbar zu machen.
- Der Bot benötigt die Berechtigung, Nachrichten im Zielchat zu senden. Eingehende Nachrichten müssen in Version 1 nicht verarbeitet werden.

### 4.2 Bewohner und Ausführung: `config/Settings.json`

```json
{
  "daily_execution_time": "06:00",
  "timezone": "Europe/Berlin",
  "tasks_per_active_day": 1,
  "residents": [
    {
      "id": "alice",
      "name": "Alice",
      "role": "adult",
      "off_days": ["Saturday", "Sunday"]
    },
    {
      "id": "bob",
      "name": "Bob",
      "role": "adult",
      "off_days": []
    },
    {
      "id": "charlie",
      "name": "Charlie",
      "role": "child",
      "off_days": ["Wednesday"]
    }
  ]
}
```

Regeln:

- `daily_execution_time`: gültige Uhrzeit im Format `HH:MM`, Standard `06:00`.
- `timezone`: gültige IANA-Zeitzone, Standard `Europe/Berlin`.
- `tasks_per_active_day`: positive ganze Zahl, Standard `1`; normale Kapazität, keine harte Obergrenze.
- `residents`: Liste mit mindestens einem Bewohner.
- `id`: nicht leere, eindeutige, dauerhaft stabile technische Kennung.
- `name`: nicht leerer Anzeigename.
- `role`: ausschließlich `adult` oder `child`.
- `off_days`: Liste ohne Duplikate; erlaubte Werte sind `Monday`, `Tuesday`, `Wednesday`, `Thursday`, `Friday`, `Saturday`, `Sunday`. Eine leere Liste bedeutet keine festen freien Tage.
- Ein Bewohner mit sieben freien Tagen bleibt konfigurierbar, erhält aber keine Aufgaben und wird bei der Belastungsberechnung nicht durch null dividiert.

### 4.3 Aufgabenpool: `config/tasks.json`

```json
{
  "tasks": [
    {
      "id": "empty_dishwasher",
      "name": "Spülmaschine ausräumen",
      "allowed_roles": ["child", "adult"],
      "frequency": "weekly"
    },
    {
      "id": "take_out_trash",
      "name": "Müll rausbringen",
      "allowed_roles": ["child", "adult"],
      "frequency": "weekly"
    },
    {
      "id": "clean_windows",
      "name": "Fenster putzen",
      "allowed_roles": ["adult"],
      "frequency": "monthly"
    },
    {
      "id": "clean_oven",
      "name": "Backofen reinigen",
      "allowed_roles": ["adult"],
      "frequency": "monthly"
    }
  ]
}
```

- `tasks` darf leer sein.
- Jede Aufgabe benötigt eine eindeutige, nicht leere `id` und einen nicht leeren `name`.
- `allowed_roles` enthält entweder nur `adult` oder beide Rollen `child` und `adult`, ohne Duplikate; die Reihenfolge ist unerheblich.
- Allgemeine beziehungsweise Kinderaufgaben dürfen damit auch Erwachsene übernehmen. Ausschließlich Kindern vorbehaltene Aufgaben sind nicht vorgesehen.
- `frequency` ist ausschließlich `weekly` oder `monthly`.
- Jede Aufgabe zählt unabhängig von ihrer Art als eine Belastungseinheit.
- Häufigkeiten beziehen sich auf die Aufgabe im gesamten Haushalt, nicht auf jeden einzelnen Bewohner.

### 4.4 Validierung und Änderungen

Die Anwendung prüft beide JSON-Dateien vollständig beim Start und vor jeder Tageszuteilung. Ungültige Typen, unbekannte Felder, doppelte IDs, ungültige Werte oder unlesbare Dateien führen zu einer verständlichen Fehlermeldung. Es werden keine teilvalidierten Konfigurationen verwendet.

Bei fehlerhafter Konfiguration bleibt der bisherige Zustand unverändert. Neue Zuteilungen pausieren bis zur Korrektur; bereits gespeicherte Telegram-Nachrichten dürfen weiter zugestellt werden. Wiederholte identische Fehler sollen Logs und Chat nicht fluten.

Gültige Änderungen gelten für noch nicht verbindlich zugeteilte Aufgaben. Bestehende Zuteilungen und historische Namen bleiben erhalten. Entfernte Aufgaben verschwinden aus dem vorläufigen Plan; entfernte Bewohner bleiben in historischen Statistiken sichtbar. IDs dürfen nicht für andere Personen oder Aufgaben wiederverwendet werden.

Eine Frequenzänderung setzt keine Sperre zurück: Der Verlauf wird nach der neuen Frequenz geprüft. Eine bereits diese Woche vergebene Aufgabe bleibt beim Wechsel zu `weekly` gesperrt; eine bereits diesen Monat vergebene Aufgabe beim Wechsel zu `monthly` ebenfalls.

## 5. Kalender und Aufgabenstatus

### 5.1 Zeiträume

- Woche: Montag bis einschließlich Sonntag, nach lokalem Datum; Kennung ist das Datum des Montags, zum Beispiel `2026-09-07`.
- Monat: Kalendermonat, Kennung `YYYY-MM`.
- Wochen- und Monatsgrenzen sind unabhängig. Ein Monatswechsel setzt keine laufende Wochensperre zurück.
- Neue Zeiträume entstehen durch neue Periodenkennungen, nicht durch Löschen historischer Zuteilungen.

### 5.2 Status einer Aufgabe

- **Offen:** für den Zeitraum noch nicht zugeteilt.
- **Geplant:** vorläufig auf einen künftigen Bewohner-Tag gelegt; noch keine Sperre und keine Statistikbuchung.
- **Zugeteilt:** für das heutige Datum atomar gespeichert; Sperre und Statistik gelten sofort, unabhängig vom Telegram-Versand.
- **Nicht zuweisbar:** im verbleibenden Zeitraum existiert kein erlaubter Bewohner-Tag; die Aufgabe bleibt offen und wird als Engpass gemeldet.

Pro Aufgabe und Zeitraum erfolgt höchstens eine verbindliche Zuteilung. Jede zuweisbare Aufgabe soll innerhalb ihres Zeitraums einmal zugeteilt werden. Nicht vergebene Aufgaben aus abgelaufenen Zeiträumen werden nicht als zusätzliche Nachholaufgaben aufgestapelt; im neuen Zeitraum besteht wieder die normale einmalige Aufgabe. Ausfälle werden dokumentiert.

## 6. Kapazität und Fairness

### 6.1 Normale Kapazität

Ein aktiver Bewohner-Tag bietet `tasks_per_active_day` normale Plätze. Ein fester freier Tag bietet null Plätze. Wöchentliche und monatliche Aufgaben teilen sich dieselbe Kapazität; sie erhalten keine getrennten Tagesbudgets.

Zusätzliche Plätze dürfen erst verwendet werden, wenn sich die noch offenen Aufgaben unter Berücksichtigung von Rollen, freien Tagen und Fristen nicht vollständig auf normale geeignete Plätze verteilen lassen. Eine lokal ungünstige Zufallsentscheidung allein rechtfertigt keine Zusatzaufgabe: Vorher müssen noch unverbindliche Platzierungen umgeordnet werden.

Erwachsenenaufgaben können Zusatzplätze bei Erwachsenen erfordern, obwohl bei Kindern normale Plätze frei bleiben. Rollen und freie Tage bleiben immer verbindlich.

### 6.2 Fairnessziel

Bei gleichen verfügbaren Tagen und gleichen Berechtigungen sollen Bewohner am Ende der Woche möglichst gleich viele Aufgaben erhalten haben. Unterschiedliche freie Tage werden durch eine vergleichbare Aufgabenlast **pro aktivem Tag** berücksichtigt, nicht durch gleiche absolute Wochensummen.

Für Bewohner `r` und Woche `w` gilt als Vergleichsgröße:

```text
Belastung(r, w) = (bereits zugeteilte + vorläufig geplante Aufgaben in w)
                 / berücksichtigte aktive Tage in w
```

Die aktiven Tage beziehen sich auf die ganze berücksichtigte Woche, nicht nur auf die heute bereits verstrichenen aktiven Tage. Freie Tage erzeugen dadurch keinen künstlichen Rückstand. Bei erstmaligem Start oder Eintritt eines Bewohners mitten in der Woche zählen nur Tage ab Teilnahmebeginn. Konfigurationsänderungen passen zukünftige Verfügbarkeiten an; vergangene Verfügbarkeiten bleiben im Zustand nachvollziehbar.

Beispiel: Bei vergleichbarer Berechtigung sind fünf Aufgaben in fünf aktiven Tagen und sieben Aufgaben in sieben aktiven Tagen gleich belastend. Der Bewohner mit zwei freien Tagen muss nicht auf sieben Aufgaben aufholen.

Prioritäten bei der Planung:

1. Rollen, feste freie Tage und Periodensperren einhalten.
2. Zuweisbare Aufgaben innerhalb ihrer Frist unterbringen; Erwachsenenaufgaben zuerst berücksichtigen.
3. Zusätzliche Plätze vermeiden beziehungsweise deren Anzahl minimieren.
4. Wochenbelastung pro aktivem Tag möglichst angleichen; Mehrfachzuteilungen auf Bewohner und aktive Tage verteilen.
5. Bei vergleichbarer Belastung zufällig zwischen Aufgaben, Bewohnern und Tagen wählen.

Gerechtigkeit ist wegen Rollenbeschränkungen, ganzzahliger Aufgaben und knapper Verfügbarkeit ein Optimierungsziel, keine Zusage absolut gleicher Zahlen. Ein einzelner Erwachsener kann durch ausschließlich für Erwachsene zulässige Aufgaben stärker belastet sein.

Bei Unterlast bleiben zufällig ausgewählte aktive Bewohner-Tage leer. Wochenhistorie und geplante Wochenlast verhindern, dass Zufall unabhängig von bisheriger Belastung immer dieselben Bewohner bevorzugt. Freie aktive Tage müssen nicht separat als Urlaub oder fester freier Tag gespeichert werden.

## 7. Planung und Tagesablauf

### 7.1 Vorläufige Periodenplanung

Der Agent plant Aufgaben vorläufig über mehrere Tage, statt täglich alle noch offenen Aufgaben zu vergeben.

- Der Planungshorizont reicht vom heutigen Datum bis zum Sonntag der Woche, die den letzten Tag des aktuellen Monats enthält.
- Berücksichtigt werden offene Monatsaufgaben des aktuellen Monats und die wöchentlichen Aufgabenvorkommen aller Wochen im Planungshorizont. So werden freie Plätze für Monatsaufgaben nicht fälschlich ohne die kommenden Wochenaufgaben berechnet.
- Jede Aufgabe darf nur zwischen Beginn und Ende ihres eigenen Zeitraums liegen, frühestens heute. Beim Start mitten in einem Zeitraum wird nur der verbleibende Zeitraum geplant.
- Monatsaufgaben des Folgemonats kommen beim Monatswechsel hinzu. Vorläufige Platzierungen dürfen dann angepasst werden; sie sind keine Zusage gegenüber Bewohnern.
- Bereits verbindliche Zuteilungen sind unveränderlich und zählen für Last und Sperren.
- Erwachsenenaufgaben werden zuerst platziert; allgemeine Aufgaben nutzen danach verbleibende geeignete Plätze. Fristen müssen auch bei dieser Priorisierung berücksichtigt werden.
- Normale Plätze werden unter Berücksichtigung der Wochenlast und mit zufälligen Gleichstandsentscheidungen genutzt. Bei Engpässen werden vorläufige Platzierungen umgeordnet, bevor Zusatzplätze entstehen.
- Reichen geeignete normale Plätze insgesamt nicht, werden die nötigen Zusatzaufgaben möglichst gleichmäßig verteilt.
- Fehlt jede geeignete Person im Zeitraum, erfolgt keine ersatzweise Zuweisung an Kinder oder an Personen mit freiem Tag.
- Ein gültiger Plan wird gespeichert und weiterverwendet. Er wird bei Ausfällen, relevanten Konfigurationsänderungen oder neuen Zeiträumen angepasst, nicht jeden Morgen vollständig neu ausgelost. Dadurch werden Monatsaufgaben nicht fortlaufend nach hinten verschoben.

Die konkrete Platzierungsimplementierung soll ohne schweren Optimierungsdienst auskommen. Tests müssen insbesondere verhindern, dass ein einfacher Greedy-Algorithmus vermeidbare Mehrfachzuteilungen erzeugt. Der Zufallszahlengenerator und das aktuelle Datum müssen für Tests kontrollierbar sein.

### 7.2 Täglicher Lauf

1. Konfiguration validieren und lokalen Kalendertag bestimmen.
2. Noch ausstehende Berichte abgeschlossener Monate vorbereiten und chronologisch zustellen. Vor der ersten Zuteilung eines neuen Monats muss der Vormonatsbericht zugestellt sein.
3. Prüfen, ob heute bereits verbindlich zugeteilt wurde. Falls ja, nur ausstehende Nachrichten weiter zustellen; keine zweite Zuteilung erzeugen.
4. Perioden und gespeicherten Plan prüfen; bei Bedarf offene Aufgaben neu einplanen.
5. Heutige Platzierungen übernehmen und unmittelbar vor der Buchung Rollen, freie Tage und Sperren erneut prüfen.
6. Heutige Zuteilungen, Historie, Statistiken, Ausführungsdatum und ausstehende Tagesnachricht gemeinsam atomar speichern. Auch ein Tag ohne Aufgaben gilt als ausgeführt.
7. Gespeicherte Tagesnachricht über Telegram versenden und bestätigten Versand speichern.

Scheitert der Monatsbericht, wird die neue Zuteilung bis zur erfolgreichen Zustellung zurückgestellt. Der Agent versucht sie anschließend für den dann aktuellen Tag nachzuholen. Diese bewusste Abhängigkeit erfüllt die geforderte Reihenfolge „Statistik vor neuer Zuteilung“.

Ein Fehler beim Versand einer bereits gebuchten Tageszuteilung rollt Aufgaben und Zähler dagegen nicht zurück; er wird über die Versandwarteschlange behandelt.

## 8. Telegram-Ausgaben

### 8.1 Tageszuteilung

```text
Guten Morgen!
Aufgaben für den 09.09.2026:

Alice: Spülmaschine ausräumen
Bob: Fenster putzen; Müll rausbringen
Charlie hat heute einen freien Tag.
```

Aktive Bewohner ohne Aufgabe werden ausdrücklich genannt, beispielsweise `Alice hat heute keine Aufgabe erhalten.` Feste freie Tage und zufällig aufgabenfreie aktive Tage müssen sprachlich unterscheidbar bleiben.

### 8.2 Keine Aufgaben und Engpässe

```text
Guten Morgen!
Für den 09.09.2026 sind heute keine Aufgaben eingeplant.
Charlie hat heute einen freien Tag.
```

Sind Aufgaben offen, aber nicht zuweisbar, folgt ein sachlicher Hinweis statt der Behauptung, es gebe keine offenen Aufgaben:

```text
Hinweis: Backofen reinigen kann diesen Monat nicht zugeteilt werden,
da kein erwachsener Bewohner an einem verbleibenden Tag verfügbar ist.
```

### 8.3 Monatsstatistik

```text
Monatsstatistik für August 2026:

Alice: 14 Aufgaben zugewiesen
Bob: 15 Aufgaben zugewiesen
Charlie: 8 Aufgaben zugewiesen

Für den neuen Monat beginnen neue Zähler.
```

- Grundlage sind Zuteilungen nach lokalem Zuteilungsdatum, nicht Versanddatum.
- Der Bericht enthält auch Bewohner mit null Aufgaben, die im Berichtsmonat zum Haushalt gehörten.
- Entfernte Bewohner mit Teilnahme im Berichtsmonat bleiben enthalten.
- Historische Monatsdaten werden nicht gelöscht oder überschrieben.
- Nach mehrmonatigem Ausfall werden ausstehende Berichte seit dem ersten Betriebsmonat chronologisch nachgeholt, einschließlich Monaten ohne Zuteilungen. Vor dem ersten Betriebsmonat werden keine Berichte erfunden.
- Deutsche Monatsnamen werden unabhängig von installierten Betriebssystem-Locales erzeugt.
- Lange Texte werden an Telegram-konformen Grenzen in geordnete Teile zerlegt. Anzeigenamen und Aufgabentexte werden als Klartext gesendet oder für den gewählten Formatierungsmodus korrekt maskiert.

## 9. Persistenter Zustand

### 9.1 Struktur: `storage/state.json`

Beispiel eines initialisierten Zustands vor der ersten Zuteilung:

```json
{
  "schema_version": 1,
  "started_on": "2026-09-09",
  "last_observed_date": "2026-09-09",
  "last_execution_date": null,
  "availability_by_week": {},
  "planned_assignments": [],
  "assignment_history": [],
  "monthly_statistics": {},
  "reported_months": [],
  "outbox": [],
  "config_history": [],
  "plan_context": null,
  "unassignable": []
}
```

Feldbedeutung und erforderlicher Inhalt:

- `schema_version`: Formatversion; unbekannte Versionen werden nicht stillschweigend geladen.
- `started_on`: erster Betriebstag; begrenzt nachzuholende Berichte und initiale Verfügbarkeiten.
- `last_observed_date`: höchstes bereits verarbeitetes Kalenderdatum, auch ohne Tagesbuchung. Ein Uhr-Rücksprung darf weder abgeschlossene Monate wieder öffnen noch Konfigurationsänderungen rückdatieren.
- `last_execution_date`: zuletzt verbindlich verarbeiteter lokaler Tag, auch ohne Aufgaben.
- `availability_by_week`: je Wochenbeginn und Bewohner-ID die berücksichtigten aktiven Datumswerte; Grundlage für nachvollziehbare Fairness bei Änderungen während einer Woche.
- `planned_assignments`: vorläufige Einträge mit Aufgaben-ID, Frequenz, Periodenkennung, geplantem Datum und Bewohner-ID. Jeder Aufgabenvorgang erscheint höchstens einmal.
- `assignment_history`: verbindliche Einträge mit stabiler Zuteilungs-ID, Datum, Aufgaben-ID, Aufgabenname, Frequenz, Periodenkennung, Bewohner-ID und Bewohnername als historische Momentaufnahme.
- `monthly_statistics`: je `YYYY-MM` und Bewohner-ID ein Eintrag mit Anzeigename und Anzahl zugewiesener Aufgaben; auch teilnehmende Bewohner ohne Zuteilung werden erfasst.
- `reported_months`: Monate mit vollständig bestätigtem Berichtsversand.
- `outbox`: geordnete Nachrichten mit stabiler ID, Typ, Bezugsdatum beziehungsweise Berichtsmonat, unveränderlichem Text sowie Versandstatus und Wiederholungsinformationen pro Teilnachricht.
- `config_history`: akzeptierte Konfigurationsstände mit lokalem Wirksamkeitsdatum; Änderungen gelten ab erfolgreichem Einlesen, nicht ab einem vermuteten Bearbeitungszeitpunkt während eines Ausfalls.
- `plan_context`: Wochen-/Monatskennung und Fingerabdruck der planungsrelevanten Einstellungen zur Wiederverwendung gültiger Pläne.
- `unassignable`: Namen aktuell nicht zuweisbarer Aufgaben für deutsche Engpasshinweise.

Wochenzähler und Periodensperren können aus der Historie abgeleitet werden. Werden sie zusätzlich zwischengespeichert, müssen sie mit derselben Zustandsänderung aktualisiert und auf Konsistenz geprüft werden. Monatszähler und Historie dürfen nicht unabhängig voneinander gebucht werden.

### 9.2 Dateisicherheit

- Jede Zustandsänderung schreibt eine temporäre Datei im selben Verzeichnis, synchronisiert sie und ersetzt anschließend `state.json` atomar. Die Implementierung berücksichtigt auch die Synchronisierung des Verzeichniseintrags.
- Eine Prozesssperre verhindert zwei gleichzeitig schreibende Agenteninstanzen.
- Ein fehlender Zustand wird nur als ausdrücklich protokollierter Erststart initialisiert. Die Betriebsanleitung muss erklären, dass Löschen des Zustands Sperren und Historie verliert.
- Ein beschädigter oder unbekannter Zustand führt zum sicheren Stopp neuer Zuteilungen, nicht zu einem leeren Ersatz. Wiederherstellung erfolgt bewusst aus einer Sicherung.
- Fehler beim Speichern verhindern den Versand noch nicht verbindlich gespeicherter Zuteilungen.
- Historische Daten bleiben in Version 1 erhalten. Keine automatische Löschung oder aufwendige Datenbankmigration ohne konkrete Notwendigkeit.

## 10. Neustarts, Zeitsteuerung und Versandfehler

- Start vor der Ausführungszeit: bis zur heutigen Ausführungszeit warten; ausstehende Nachrichten dürfen bereits zugestellt werden.
- Start nach der Ausführungszeit: heutige Zuteilung nachholen, sofern noch nicht gebucht.
- Verpasste vergangene Tage werden nicht rückwirkend zugeteilt. Ihre offenen Planungen werden auf noch zulässige zukünftige Tage umgelegt oder nach Periodenende als ausgefallen behandelt.
- Ein Neustart am selben Tag erzeugt weder neue Aufgaben noch doppelte Statistikbuchungen.
- Sommerzeit: Bei nicht existierender lokaler Ausführungszeit läuft der Agent zur ersten gültigen Zeit danach; bei doppelt vorkommender Uhrzeit nur einmal pro lokalem Datum.
- Bei rückwärts springender Uhr werden bereits verarbeitete Tage nicht erneut ausgeführt. Die Prüfung erfolgt nicht nur durch Vergleich auf Gleichheit mit dem letzten Datum.
- Telegram-Versand nutzt Zeitlimits und begrenztes exponentielles Backoff; `retry_after` bei Rate-Limits wird beachtet. Wiederholungen dürfen den Prozess nicht durch eine enge Endlosschleife belasten.
- Dauerhafte Fehler wie ungültige Zugangsdaten werden klar geloggt und nur mit langen Abständen erneut versucht; Nachrichten bleiben erhalten.
- Die Outbox bleibt geordnet. Eine mehrteilige Nachricht gilt erst nach Bestätigung aller Teile als zugestellt; Monatsberichte erst dann als berichtet.
- Nach längeren Ausfällen behalten Tagesnachrichten ihr ursprüngliches Datum und werden als nachträgliche Mitteilung kenntlich gemacht.
- Genau einmalige Telegram-Zustellung ist nicht garantierbar: Akzeptiert Telegram eine Nachricht und geht die Antwort verloren, kann ein Wiederholungsversuch ein Duplikat erzeugen. Lokale Zuteilungen und Statistiken müssen trotzdem idempotent bleiben.

## 11. Technischer Umsetzungsansatz

- `main.py`: Start, Prozesssperre, zeitgesteuerter Ablauf und Fehlerbehandlung.
- `config.py`: Laden und Validieren der JSON-Konfiguration und Umgebungsvariablen.
- `allocation.py`: Perioden, Verfügbarkeit, vorläufige Planung und Tageszuteilung; ohne Netzwerkzugriffe und mit kontrollierbarer Uhr/Zufallsquelle testbar.
- `state.py`: Zustandsformat, Historie, Statistiken und atomare Speicherung.
- `telegram.py`: deutsche Texte, Monatsberichte, Nachrichtenteilung und Outbox-Versand.
- Tests mit Standardbibliothek oder einer schlanken Testabhängigkeit; keine Telegram-Zugangsdaten für automatisierte Tests erforderlich.

Vorgesehene Laufzeitabhängigkeiten sind `python-telegram-bot` und `python-dotenv`. Kalenderberechnung erfolgt mit `datetime` und `zoneinfo`. Ein zusätzlicher Scheduler ist nur nötig, wenn er gegenüber einer schlanken, zeitzonenbewussten Warteschleife einen konkreten Nutzen bietet.

Die Umsetzung muss zueinander und zur eingesetzten Python-Version passende Versionen in `requirements.txt` festhalten. Unbeschränkte Mindestversionen allein sind keine ausreichende Zusage für Python-3.9-Kompatibilität. Asynchrone Telegram-APIs werden in einem konsistenten Ereigniszyklus verwendet.

Die Installationsanleitung umfasst virtuelle Umgebung, Konfiguration, Bot-/Chat-Einrichtung, Dateirechte, Sicherung des Zustands, `systemd`-Installation und Loganzeige. Der Dienst startet nach Netzwerkverfügbarkeit, kann spätere Netzwerkausfälle verarbeiten und startet nach unerwartetem Prozessende automatisch neu.

## 12. Abnahmekriterien und Tests

1. Standardzeit ist `06:00`; eine gültige abweichende Uhrzeit und Zeitzone werden berücksichtigt.
2. Kinder bekommen niemals Erwachsenenaufgaben, auch nicht bei Überlast oder fehlenden Erwachsenen.
3. Allgemeine Aufgaben dürfen an beide Rollen gehen; Erwachsenenaufgaben werden vor allgemeinen Aufgaben eingeplant.
4. An festen freien Tagen entstehen keine Zuteilungen, auch keine Zusatzaufgaben.
5. Eine Wochenaufgabe wird höchstens einmal je Montag-Sonntag-Zeitraum vergeben und ist ab der nächsten Woche wieder verfügbar. Tests umfassen ISO-Jahreswechsel.
6. Eine Monatsaufgabe wird höchstens einmal je Kalendermonat vergeben. Tests umfassen Februar, Schaltjahr und Dezember/Januar.
7. Monatswechsel mitten in einer Woche setzt keine Wochensperren oder laufende Wochenlast zurück.
8. Bei hinreichender normaler Kapazität entstehen keine unnötigen Zusatzaufgaben; konkurrierende Wochen-/Monatsfristen und Rollenengpässe werden getestet.
9. Bei zwei gleich berechtigten Bewohnern mit je sieben aktiven Tagen und 21 Wochenaufgaben entstehen insgesamt sieben Zusatzaufgaben; Wochenzahlen sind 10 und 11, Tageslasten unterscheiden sich je Bewohner höchstens um eins.
10. Bei fünf beziehungsweise sieben aktiven Tagen und zwölf allgemeinen Wochenaufgaben erhält jeder Bewohner eine Aufgabe pro aktivem Tag, sofern keine anderen Aufgaben konkurrieren: fünf beziehungsweise sieben insgesamt.
11. Bei Unterlast verteilt die kontrollierbare Zufallsquelle leere aktive Tage unter Berücksichtigung der Wochenlast. Über mehrere Test-Seeds gibt es keine feste Bevorzugung nach Listenreihenfolge.
12. Aufgaben werden auf geeignete Tage im Zeitraum geplant. Persistierte Monatsplanungen werden ohne Änderungsgrund nicht jeden Tag nach hinten verschoben.
13. Wenn kein Erwachsener im Restzeitraum verfügbar ist, bleiben Erwachsenenaufgaben offen; ein verständlicher deutscher Engpasshinweis wird erzeugt.
14. Leerer Pool, alle Bewohner am selben Tag frei und Bewohner mit sieben freien Tagen führen nicht zu Fehlern oder Division durch null.
15. Der Monatsbericht entspricht den gespeicherten Zuteilungen, enthält Nullwerte und wird vor der ersten neuen Monatszuteilung zugestellt. Versandfehler blockieren diese Zuteilung bis zur Zustellung.
16. Neustart und wiederholter Tagesaufruf erzeugen keine doppelten Zuteilungen oder Zähler. Auch Tage ohne Aufgaben sind idempotent.
17. Abstürze vor/nach Zustandsersetzung sowie vor/nach Telegram-Bestätigung werden getestet; bestätigte Teilnachrichten werden regulär nicht erneut versandt.
18. Ungültige Konfiguration oder beschädigter Zustand erzeugen keine neuen Zuteilungen und überschreiben keine gültige Historie.
19. Änderungen an Rollen, freien Tagen, Pool und Frequenzen passen nur offene Planungen an; historische Anzeigenamen und Sperren bleiben korrekt.
20. Sommer-/Winterzeit, Start vor/nach Ausführungszeit, mehrtägiger Ausfall und rückwärts springende Uhr werden mit kontrollierter Zeit getestet.
21. Telegram-Texte sind deutsch, unterscheiden frei von nicht zugeteilt und behaupten keine bestätigten Erledigungen. Lange Nachrichten und Sonderzeichen funktionieren.
22. Smoke-Test auf dem Raspberry Pi: Installation, Start über `systemd`, Testnachricht, Neustart mit erhaltenem Zustand und kontrollierter Netzwerkausfall.

## 13. Implementierungsreihenfolge

1. Projektgrundgerüst, Beispielkonfigurationen, `.env.example`, Abhängigkeiten und Konfigurationsvalidierung anlegen.
2. Zustandsmodell, atomare Speicherung, Prozesssperre und Perioden-/Historienfunktionen implementieren und testen.
3. Rollenfilter, Verfügbarkeiten, gemeinsame Kapazitätsplanung und Fairness mit deterministischen Szenarien entwickeln.
4. Deutsche Telegram-Texte, Monatsstatistik und persistente Versandwarteschlange ergänzen.
5. Tagesablauf, zeitzonenbewusste Zeitsteuerung und Wiederanlaufverhalten integrieren.
6. Grenzfall-/Ausfalltests ausführen; Installation, Sicherung und `systemd`-Betrieb dokumentieren; Pi-Smoke-Test durchführen.

Diese Spezifikation beschreibt das Zielverhalten. Das Ersetzen von `SPEC.md` umfasst noch keine Implementierung des Agenten oder Erstellung echter Zugangsdaten.
