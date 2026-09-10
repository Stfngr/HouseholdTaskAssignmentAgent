# Household Task Agent

[English below](#english)

Python-Hintergrunddienst zur automatischen Verteilung von Haushaltsaufgaben.
Er sendet taegliche Zuteilungen und Monatsstatistiken auf Deutsch an einen
Telegram-Chat. Bewohner, Rollen, freie Wochentage und Aufgaben stehen in JSON;
Zustand und Versandwarteschlange bleiben lokal erhalten. Zielplattform ist ein
Raspberry Pi 3B mit gepflegtem Raspberry Pi OS Lite.

**Gezaehlt werden zugewiesene Aufgaben, nicht bestaetigte Erledigungen.**
Erledigungsbuttons, Umbuchungen, Weboberflaeche und mehrere Haushalte sind nicht
implementiert. `SPEC.md` beschreibt das Zielverhalten, nicht einen vollstaendig
abgenommenen Produktionsbetrieb.

## Voraussetzungen und lokale Befehle

Der Anwendungscode verwendet Python-3.9-kompatible Sprachmittel. Fuer den Betrieb
eine noch sicherheitsgepflegte Python-Version verwenden, empfohlen Python 3.11+
aus einem gepflegten Raspberry Pi OS. Betrieb und Dateisperre setzen Linux voraus.
Die OS-Pakete `python3`, `python3-venv` und `tzdata` werden benoetigt.

Direkte Laufzeitabhaengigkeiten sind in `requirements.txt` festgelegt:

- `python-telegram-bot==21.11.1` (asynchrone Telegram-API).
- `python-dotenv==1.0.1` (Zugangsdaten aus `.env`).

Im Projektverzeichnis:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python main.py --check-config
python main.py --preview 2026-09-10 --seed 0
python3 -m unittest discover -s tests -v
```

`--check-config` prueft beide JSON-Dateien, benoetigt keine `.env` und prueft keine
Telegram-Zugangsdaten. `--preview YYYY-MM-DD --seed 0` zeigt einen **frischen,
hypothetischen Plan** ab dem angegebenen Datum samt nicht zuweisbaren Aufgaben.
Die Vorschau ignoriert den gespeicherten Zustand, also auch Historie, Sperren und
bestehende Plaene. Sie ist keine Vorschau der naechsten echten Buchung. Beide
Modi schreiben keinen Zustand und greifen nicht auf das Netzwerk zu. Der Seed
steuert nur die Vorschau; der Dienst verwendet eine eigene Zufallsquelle.

Der normale Start lautet nach Einrichtung der Zugangsdaten:

```bash
python main.py
```

Dieser Start ist **kein Testlauf**: Er schreibt Zustand und kann echte Nachrichten
senden. Nicht parallel zum systemd-Dienst betreiben. Pfade werden relativ zu
`main.py` aufgeloest, unabhaengig vom aktuellen Arbeitsverzeichnis.

## Installation unter /opt

Die folgenden Befehle sind fuer eine Erstinstallation gedacht. Aus dem
heruntergeladenen Projektverzeichnis ausfuehren; vorhandene Installation nicht
blind ueberschreiben. Fuer Updates zuerst Dienst stoppen und Zustand sichern.

```bash
sudo apt update
sudo apt install python3 python3-venv tzdata
sudo useradd --system --user-group --home-dir /opt/household-agent --no-create-home --shell /usr/sbin/nologin household-agent
sudo install -d -o root -g household-agent -m 0750 /opt/household-agent
sudo install -d -o root -g household-agent -m 0750 /opt/household-agent/config /opt/household-agent/household_agent /opt/household-agent/tests /opt/household-agent/deploy
sudo install -o root -g household-agent -m 0640 main.py requirements.txt README.md SPEC.md .env.example /opt/household-agent/
sudo install -o root -g household-agent -m 0640 household_agent/*.py /opt/household-agent/household_agent/
sudo install -o root -g household-agent -m 0640 config/Settings.json config/tasks.json /opt/household-agent/config/
sudo install -o root -g household-agent -m 0640 tests/*.py /opt/household-agent/tests/
sudo install -o root -g household-agent -m 0640 deploy/household-agent.service /opt/household-agent/deploy/
sudo python3 -m venv /opt/household-agent/.venv
sudo /opt/household-agent/.venv/bin/python -m pip install -r /opt/household-agent/requirements.txt
sudo chown -R root:household-agent /opt/household-agent/.venv
sudo chmod -R g+rX,g-w,o-rwx /opt/household-agent/.venv
sudo install -d -o household-agent -g household-agent -m 0700 /opt/household-agent/storage
```

Anwendung, virtuelle Umgebung und Konfiguration gehoeren `root`; die
Dienstgruppe darf lesen beziehungsweise Verzeichnisse betreten, aber nichts
aendern. Keine weiteren Benutzer zur Gruppe `household-agent` hinzufuegen.
Nur `storage/` gehoert dem Dienstbenutzer und hat Modus `0700`.

**`storage/` muss vor dem Dienststart existieren**, weil die Unit diesen Pfad als
einzige beschreibbare Ausnahme im Anwendungsverzeichnis freigibt. `state.json`
nicht manuell anlegen, auch nicht als leere Datei oder nach dem SPEC-Beispiel:
Der Agent initialisiert den vollstaendigen Zustand beim ersten erfolgreichen
Start automatisch und protokolliert den Erststart.

### Telegram einrichten

1. Mit dem offiziellen Telegram-Bot `@BotFather` einen Bot erstellen und Token beziehen.
2. Fuer einen privaten Zielchat zuerst dem Bot schreiben; fuer eine Gruppe den Bot hinzufuegen und Senden erlauben.
3. Numerische Chat-ID ermitteln, beispielsweise lokal ueber die Telegram-Bot-API-Methode `getUpdates` nach einer Nachricht an den Bot. Gruppen-IDs koennen negativ sein. Keine fremden Webseiten mit dem Token beauftragen.
4. Beispieldatei kopieren, Rechte setzen und beide Platzhalter lokal ersetzen:

```bash
sudo cp /opt/household-agent/.env.example /opt/household-agent/.env
sudo chown root:household-agent /opt/household-agent/.env
sudo chmod 0640 /opt/household-agent/.env
sudoedit /opt/household-agent/.env
```

Die Datei enthaelt `TELEGRAM_BOT_TOKEN` und `TELEGRAM_CHAT_ID`; niemals echte
Zugangsdaten in Dokumentation, Versionsverwaltung, Shell-History oder Logs
ablegen. `.env` ist mit `root:household-agent` und `0640` ausser fuer root nur
fuer die Dienstgruppe lesbar. Bereits gesetzte Prozess-Umgebungsvariablen haben
Vorrang vor `.env`. Die Anwendung laedt `.env` selbst; die Unit benoetigt kein
`EnvironmentFile`. Eingehende Nachrichten verarbeitet der Agent nicht.

## JSON-Konfiguration

Vor dem ersten Start die mitgelieferten Beispielbewohner und Aufgaben anpassen:

```bash
sudoedit /opt/household-agent/config/Settings.json /opt/household-agent/config/tasks.json
sudo -u household-agent /opt/household-agent/.venv/bin/python /opt/household-agent/main.py --check-config
sudo -u household-agent /opt/household-agent/.venv/bin/python /opt/household-agent/main.py --preview 2026-09-10 --seed 0
```

`config/Settings.json` ist unter Linux gross-/kleinschreibungssensitiv.
JSON muss UTF-8 sein; Kommentare und nachgestellte Kommata sind nicht erlaubt.

| Feld in Settings.json | Bedeutung |
| --- | --- |
| `daily_execution_time` | Lokales `HH:MM`, Standard `06:00`. |
| `timezone` | IANA-Zeitzone, Standard `Europe/Berlin`; benoetigt Zeitzonendaten. |
| `tasks_per_active_day` | Positive ganze Zahl, Standard `1`; normale Kapazitaet, keine harte Obergrenze. |
| `residents` | Nicht leere Liste mit `id`, `name`, `role`, `off_days` je Person. |
| `role` | Ausschliesslich `adult` oder `child`. |
| `off_days` | Liste ohne Duplikate: `Monday`, `Tuesday`, `Wednesday`, `Thursday`, `Friday`, `Saturday`, `Sunday`. `[]` bedeutet keine festen freien Tage. |

Sieben freie Tage sind erlaubt; diese Person erhaelt keine Aufgaben.
`config/tasks.json` enthaelt ein Objekt mit der Liste `tasks`, die leer sein darf.
Jede Aufgabe braucht `id`, `name`, `allowed_roles` und `frequency`:

- `allowed_roles`: entweder `["adult"]` oder beide Rollen `["child", "adult"]`, ohne Duplikate. Nur Kindern vorbehaltene Aufgaben sind nicht vorgesehen.
- `frequency`: `weekly` oder `monthly`. Jede Aufgabe wird einmal pro Zeitraum im gesamten Haushalt vergeben, nicht einmal pro Bewohner.
- IDs und Namen duerfen nicht leer sein; IDs muessen innerhalb ihrer Liste eindeutig und dauerhaft stabil bleiben. IDs nie fuer andere Personen oder Aufgaben wiederverwenden.
- Unbekannte Felder, doppelte JSON-Schluessel, falsche Typen und ungueltige Werte werden abgewiesen.

### Aenderungen im Betrieb

Der Scheduler wartet nach jedem Durchlauf 15 Sekunden und laedt beide JSON-Dateien
im naechsten Zyklus erneut. Eine geaenderte Uhrzeit wird dann wirksam; liegt sie
bereits in der Vergangenheit und ist heute noch nicht gebucht, kann sofort eine
Zuteilung folgen. Planung und Versand koennen den Zyklus verlaengern, daher ist
dies keine sekundengenaue Zeitgarantie. Zugangsdaten werden nur beim Prozessstart
geladen: Nach Aenderung von `.env` den Dienst neu starten.

Ungueltige JSON-Konfiguration pausiert neue Zuteilungen; gespeicherte Nachrichten
duerfen weiter zugestellt werden. Gueltige Aenderungen betreffen offene Planungen,
nicht bereits gebuchte Aufgaben. Frequenzwechsel pruefen die Historie nach der
neuen Frequenz und setzen bestehende Wochen-/Monatssperren nicht einfach zurueck.

Fuer zusammengehoerende Aenderungen an beiden Dateien den Dienst stoppen, Kopien
bearbeiten, vollstaendig validieren und erst dann auf demselben Dateisystem
atomar ersetzen. Zwei Dateiumbenennungen sind zusammen keine Transaktion; der
gestoppte Dienst verhindert das Laden einer gemischten Konfiguration:

```bash
sudo systemctl stop household-agent.service
sudo install -d -o root -g household-agent -m 0750 /opt/household-agent/config-staging /opt/household-agent/config-staging/config
sudo install -o root -g household-agent -m 0640 /opt/household-agent/config/Settings.json /opt/household-agent/config/tasks.json /opt/household-agent/config-staging/config/
sudoedit /opt/household-agent/config-staging/config/Settings.json /opt/household-agent/config-staging/config/tasks.json
sudo chown root:household-agent /opt/household-agent/config-staging/config/Settings.json /opt/household-agent/config-staging/config/tasks.json
sudo chmod 0640 /opt/household-agent/config-staging/config/Settings.json /opt/household-agent/config-staging/config/tasks.json
sudo -u household-agent env PYTHONPATH=/opt/household-agent /opt/household-agent/.venv/bin/python -c 'from pathlib import Path; from household_agent.config import load_config; load_config(Path("/opt/household-agent/config-staging")); print("Konfiguration gueltig.")'
```

Nur nach erfolgreicher Validierung fortfahren; bei Fehlern korrigieren und erneut
pruefen. Vorherige JSON-Dateien bei Bedarf separat sichern.

```bash
sudo mv /opt/household-agent/config-staging/config/Settings.json /opt/household-agent/config/Settings.json
sudo mv /opt/household-agent/config-staging/config/tasks.json /opt/household-agent/config/tasks.json
sudo -u household-agent /opt/household-agent/.venv/bin/python /opt/household-agent/main.py --check-config
sudo systemctl start household-agent.service
```

Auch hier nur nach erfolgreicher Pruefung starten. `--check-config` allein
uebernimmt keine Konfiguration in den persistenten Verlauf.

Statistik und Verfuegbarkeit verwenden das lokale Datum, an dem der laufende
Agent eine Aenderung erfolgreich laedt und speichert (`effective_date`). Aus
Dateizeitstempeln oder waehrend eines Ausfalls bearbeiteten JSON-Dateien werden
keine vergangenen Ein-/Austritte oder freien Tage erraten. Bis zur naechsten
uebernommenen Aenderung gilt der letzte gespeicherte Stand. Historische
Zuteilungsnamen und abgeschlossene Monatsdaten bleiben erhalten; auch entfernte
Bewohner und Teilnehmende mit null Aufgaben bleiben im jeweiligen Bericht.

## Planung und Fairness

Wochen laufen Montag bis Sonntag, Monate nach lokalem Kalender. Ein Monatswechsel
hebt keine laufende Wochensperre auf. Freie Tage und Rollen sind harte Grenzen:
Kinder bekommen keine Erwachsenenaufgaben, freie Tage keine Zusatzaufgaben.
Ohne geeigneten Bewohner-Tag bleibt eine Aufgabe offen und erscheint als Engpass.

Der Plan reicht von heute bis zum Sonntag der Woche, die den letzten Tag des
aktuellen Monats enthaelt, also hoechstens 37 Kalendertage. Er umfasst offene
Monatsaufgaben dieses Monats und Wochenaufgaben aller enthaltenen Wochen.
Monatsaufgaben des Folgemonats kommen erst beim Monatswechsel hinzu.

Die Implementierung verwendet einen gemeinsamen **Min-Cost-Flow-Plan** ohne
externen Optimierungsdienst. Erwachsenenaufgaben werden zuerst beruecksichtigt;
Rueckwaertskanten erlauben das Umordnen vorlaeufiger Platzierungen ueber Rollen
und Fristen hinweg. Wochen- und Monatsaufgaben teilen dieselbe Tageskapazitaet.
Die Kosten optimieren der Reihe nach:

1. Anzahl notwendiger Zusatzplaetze oberhalb der normalen Tageskapazitaet minimieren.
2. Wochenlast angleichen: Summe `n*n/A` minimieren, mit bereits zugeteilten plus geplanten Aufgaben `n` und beruecksichtigten aktiven Wochentagen `A`.
3. Tageslast verteilen: Summe der quadrierten Tageszahlen minimieren.
4. Verbleibende Gleichstaende zufaellig entscheiden.

Teilnahmebeginn mitten in der Woche zaehlt erst ab diesem Datum. Feste freie Tage
erzeugen keine Nachholpflicht. Gleiche Last pro aktivem Tag ist das Ziel, nicht
gleiche absolute Zahlen trotz unterschiedlicher Verfuegbarkeit. Rollenengpaesse
und ganzzahlige Aufgaben koennen ungleiche Ergebnisse erzwingen.

Gueltige vorlaeufige Plaene werden gespeichert und wiederverwendet, statt jeden
Morgen neu ausgelost zu werden. Relevante Konfigurations-/Periodenwechsel und
verpasste Platzierungen loesen Neuplanung aus; verbindliche Buchungen bleiben
erhalten. Die Planung findet im faelligen Tageslauf statt, nicht als staendige
Optimierung im Hintergrund.

Der begrenzte Horizont haelt die Planung fuer Haushalte ueberschaubar. Aufwand
und Speicher wachsen mit Aufgabenvorkommen und geeigneten Bewohner-Tagen; pro
Vorkommen wird ein kuerzester augmentierender Pfad gesucht. Sehr grosse Pools
koennen merklich CPU und RAM benoetigen. Historie und Outbox werden nicht
automatisch geloescht; Zustandspruefung und -abgleich verursachen auch zwischen
Neuplanungen Aufwand. Es gibt noch keine belastbare Laufzeit-/RAM-Messung auf
einem Raspberry Pi 3B mit 1 GB RAM.

## Zeitsteuerung, Zustand und Versand

- Start vor der Ausfuehrungszeit wartet auf den heutigen Termin; ausstehende Nachrichten koennen vorher versandt werden. Start danach holt nur den heutigen, noch nicht gebuchten Tag nach.
- Vergangene Tage werden nicht rueckwirkend zugeteilt. Offene Plaene werden innerhalb ihrer Frist umgelegt; abgelaufene Aufgaben werden nicht als Nachholstapel gesammelt.
- Auch ein Tag ohne Aufgaben gilt als ausgefuehrt. Neustarts und eine rueckwaerts springende Uhr erzeugen keine erneute Buchung bereits verarbeiteter Tage.
- Bei einer Sommerzeit-Luecke gilt die erste gueltige lokale Minute danach; bei doppelter Uhrzeit wird nur einmal pro lokalem Datum gebucht.
- Zuteilungen, Historie, Monatszaehler und Tagesnachricht werden gemeinsam atomar gespeichert, bevor der Versand beginnt. Temporaere Datei und Verzeichnis werden synchronisiert; `storage/agent.lock` verhindert parallele schreibende Instanzen. Sperrdatei nicht im Betrieb loeschen.
- Fehlender Zustand bedeutet protokollierten Erststart. Beschaedigter oder unbekannter Zustand stoppt den Agenten statt Historie leer zu ersetzen. `--check-config` ist keine Zustandspruefung.
- Ausstehende Monatsberichte werden seit dem ersten Betriebsmonat chronologisch nachgeholt, auch fuer Monate ohne Zuteilungen. Vor diesem Betriebsmonat werden keine Berichte erfunden.
- **Ein nicht zugestellter Monatsbericht blockiert neue Zuteilungen**, bis alle Berichtsteile bestaetigt sind. Danach wird fuer den dann aktuellen Tag gebucht, nicht rueckdatiert.
- **Ein fehlgeschlagener Tagesversand rollt Buchungen nicht zurueck und blockiert fuer sich keine weiteren Tagesbuchungen.** Die geordnete Outbox kann dadurch wachsen. Am Monatswechsel kann ein davor wartender Tagesversand allerdings auch den Monatsbericht und damit neue Buchungen aufhalten.
- Versand verwendet Zeitlimits und exponentielle Wiederholungsabstaende von 30 Sekunden bis einer Stunde; dauerhafte Fehler mindestens eine Stunde, Telegram-`retry_after` gegebenenfalls laenger. Bestaetigte Nachrichtenteile werden regulaer nicht erneut versandt.
- Tagesnachrichten behalten ihr urspruengliches Datum. Ein erst an einem spaeteren Tag begonnener Versand erhaelt einen Nachtragshinweis; bereits begonnene Nachrichten behalten ihren eingefrorenen Text.

**Genau einmalige Telegram-Zustellung ist nicht garantiert.** Akzeptiert Telegram
eine Nachricht, aber Antwort oder lokale Bestaetigung gehen verloren, kann ein
Wiederholungsversuch ein Duplikat senden. Lokale Zuteilungen und Statistiken bleiben
davon getrennt. Nachrichten werden als Klartext gesendet und bei Bedarf geteilt.

## systemd-Betrieb

Nach Einrichtung von Konfiguration, `.env` und `storage/`:

```bash
sudo install -o root -g root -m 0644 /opt/household-agent/deploy/household-agent.service /etc/systemd/system/household-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now household-agent.service
sudo systemctl status household-agent.service
sudo journalctl -u household-agent.service -n 100 --no-pager
sudo journalctl -u household-agent.service -f
```

`enable --now` startet sofort und aktiviert den Start beim Booten. Die Unit nutzt
den eigenen Benutzer und die eigene Gruppe `household-agent`, `UMask=0077`,
Neustart bei Fehlern nach 30 Sekunden und `network-online.target` als Startordnung.
Das garantiert keine Erreichbarkeit von Telegram. `NoNewPrivileges`, `PrivateTmp`,
`ProtectHome=true` und `ProtectSystem=strict` begrenzen den Dienst; nur
`/opt/household-agent/storage` ist als persistenter Schreibpfad freigegeben.
Programm und Konfiguration unter `/home` sind deshalb kein Ersatz fuer diese
Installation. Logs gehen ins Journal, nicht in Anwendungsdateien.

```bash
sudo systemctl stop household-agent.service
sudo systemctl start household-agent.service
sudo systemctl restart household-agent.service
sudo systemctl disable --now household-agent.service
```

Diese Befehle sind einzelne Verwaltungsoptionen, keine gemeinsam auszufuehrende
Sequenz. Nach Unit-Aenderungen `daemon-reload` und `restart` ausfuehren; nach
Zugangsdaten-Aenderungen genuegt `restart`. Bei Fehlern JSON, Dateirechte,
Zeitzonendaten, Token, Chat-ID und Sendeberechtigung pruefen. Token und komplette
Telegram-Anfrage-URLs nicht in Diagnoseausgaben veroeffentlichen.

## Sicherung und Wiederherstellung

`storage/state.json` enthaelt Historie, Sperren, Plaene, Statistiken,
Konfigurationsverlauf und Outbox. `.gitignore` schliesst `.env`, `storage/`,
virtuelle Umgebungen und Python-Caches aus; Versionsverwaltung ist kein Backup.
Konfiguration und Zugangsdaten separat mit vergleichbarem Zugriffsschutz sichern.

Fuer eine konsistente Sicherung nach mindestens einem erfolgreichen Erststart:

```bash
sudo systemctl stop household-agent.service
sudo install -d -o root -g root -m 0700 /var/backups/household-agent
BACKUP="/var/backups/household-agent/state-$(date -u +%Y%m%dT%H%M%SZ).json"
sudo cp -p /opt/household-agent/storage/state.json "$BACKUP"
sudo stat -c '%U:%G %a %n' "$BACKUP"
sudo systemctl start household-agent.service
```

Nur nach erfolgreicher Kopie fortfahren. `cp -p` erhaelt Eigentuemer und Rechte;
die Zustandsdatei sollte `household-agent:household-agent`, Modus `600`, haben.
Das Backupverzeichnis ist ausschliesslich fuer root zugaenglich. Sicherungen
zusaetzlich auf ein geschuetztes anderes Medium kopieren; eine zweite Datei auf
derselben SD-Karte schuetzt nicht vor deren Ausfall.

Fuer die Wiederherstellung zuerst Dienst stoppen und vorhandenen Zustand separat
sichern. `BACKUP` auf eine bewusst ausgewaehlte Sicherung setzen:

```bash
sudo systemctl stop household-agent.service
BACKUP=/var/backups/household-agent/state-YYYYMMDDTHHMMSSZ.json
sudo install -o household-agent -g household-agent -m 0600 "$BACKUP" /opt/household-agent/storage/.state-restore.tmp
sudo mv /opt/household-agent/storage/.state-restore.tmp /opt/household-agent/storage/state.json
sudo chown household-agent:household-agent /opt/household-agent/storage
sudo chmod 0700 /opt/household-agent/storage
sudo systemctl start household-agent.service
sudo journalctl -u household-agent.service -n 100 --no-pager
```

Jeden Schritt auf Erfolg pruefen, bevor der naechste erfolgt. Der Agent validiert
den Zustand beim Start. Ein aelteres Backup verliert Buchungen und
Versandbestaetigungen seit dessen Erstellung; erneute Zuteilungen oder Nachrichten
sind dann moeglich. Ein Backup deshalb nicht zum beliebigen Zuruecksetzen nutzen.

**`state.json` zu loeschen verliert Historie, Periodensperren, Zaehler und
Versandstatus. Der folgende Erststart kann Aufgaben erneut vergeben und
Nachrichten duplizieren.** Fehler nicht durch Loeschen oder manuelles Bearbeiten
des Zustands umgehen; bewusst eine intakte Sicherung wiederherstellen.

## Tests und offene Abnahme

Automatisierte Tests verwenden `unittest` und benoetigen keine echten
Telegram-Zugangsdaten:

```bash
python3 -m unittest discover -s tests -v
```

Ein erfolgreicher Testlauf belegt nur die vorhandenen Tests, nicht automatisch
alle Abnahmekriterien aus `SPEC.md`. Installation auf einem echten Raspberry Pi
und Live-Versand an Telegram sind bislang nicht verifiziert.

Die Tests umfassen auch eine 56-Tage-Simulation und kleine Planungsfaelle mit
vollstaendig durchgerechneter Vergleichsloesung. Mit installierten Abhaengigkeiten
pruefen weitere Tests die echte Telegram-Bibliothek gegen einen vollstaendig
simulierten HTTP-Transport; ohne diese Abhaengigkeiten werden sie uebersprungen.
Auch diese Tests senden keine echten Nachrichten.

Noch auszufuehrende Smoke-Checkliste, mit eigenem Testchat und getrenntem Zustand:

- [ ] Auf gepflegtem Raspberry Pi OS installieren; Python-Version, Abhaengigkeiten, Zeitzone und Dateirechte pruefen.
- [ ] `--check-config`, deterministische Vorschau und Tests ausfuehren; Planungszeit und RAM mit realistischem Aufgabenpool messen.
- [ ] Dienst aktivieren und Bootstart pruefen; keine Schreibrechte auf Anwendung, JSON oder `.env`, Schreibzugriff auf `storage/` vorhanden.
- [ ] Tageszeit fuer den Test einstellen; echte deutsche Tagesnachricht und freie/aufgabenlose Bewohner kontrollieren. Es gibt keinen separaten Testnachrichten-Schalter.
- [ ] Am selben Tag neu starten; Zustand bleibt erhalten, keine zweite Tagesbuchung oder doppelte Statistik.
- [ ] Kontrollierten Netzwerkausfall pruefen, ohne den administrativen Zugang zum Pi zu verlieren; Tagesbuchung bleibt erhalten, Outbox wird nach Rueckkehr abgearbeitet.
- [ ] Monatswechsel im isolierten Testbetrieb pruefen: fehlgeschlagener Bericht blockiert Buchung, erfolgreicher Bericht geht ihr voraus. Produktionsuhr oder Produktionszustand nicht dafuer manipulieren.
- [ ] JSON-Aenderung, ungueltige Konfiguration und Zugangsdaten-Neustart pruefen; Sicherung und Wiederherstellung mit Testzustand erproben.

## English

Python background service for automatic household-task allocation. It sends daily
assignments and monthly statistics in German to a Telegram chat. Residents, roles,
weekly days off, and tasks are stored in JSON; state and the delivery queue are
persisted locally. Target platform: Raspberry Pi 3B running maintained Raspberry
Pi OS Lite.

**The agent counts assigned tasks, not confirmed completions.** Completion buttons,
reassignments, a web UI, and multiple households are not implemented. `SPEC.md`
describes intended behavior, not a fully production-approved deployment.

### Requirements and local commands

Application code supports Python 3.9. Use a currently security-supported Python
release in production; Python 3.11+ from maintained Raspberry Pi OS is recommended.
Operation and file locking require Linux. Install `python3`, `python3-venv`, and
`tzdata` from the operating system.

Direct runtime dependencies are pinned in `requirements.txt`:

- `python-telegram-bot==21.11.1` for the asynchronous Telegram API.
- `python-dotenv==1.0.1` for credentials from `.env`.

From the project directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python main.py --check-config
python main.py --preview 2026-09-10 --seed 0
python3 -m unittest discover -s tests -v
```

`--check-config` validates both JSON files, needs no `.env`, and does not validate
Telegram credentials. `--preview YYYY-MM-DD --seed 0` displays a **new,
hypothetical plan** starting on that date, including unassignable tasks. It ignores
persisted state, including history, cooldowns, and existing plans, so it is not a
preview of the next real booking. Neither mode writes state or accesses the
network. The seed controls only the preview; the service uses its own random source.

After configuring credentials, start normally with:

```bash
python main.py
```

This is **not a dry run**: it writes state and can send real messages. Do not run it
alongside the systemd service. Paths are resolved relative to `main.py`, regardless
of the current working directory.

### Installation in /opt

These commands are for a first installation. Run them from the downloaded project
directory; do not blindly overwrite an existing installation. Before updates, stop
the service and back up state.

```bash
sudo apt update
sudo apt install python3 python3-venv tzdata
sudo useradd --system --user-group --home-dir /opt/household-agent --no-create-home --shell /usr/sbin/nologin household-agent
sudo install -d -o root -g household-agent -m 0750 /opt/household-agent
sudo install -d -o root -g household-agent -m 0750 /opt/household-agent/config /opt/household-agent/household_agent /opt/household-agent/tests /opt/household-agent/deploy
sudo install -o root -g household-agent -m 0640 main.py requirements.txt README.md SPEC.md .env.example /opt/household-agent/
sudo install -o root -g household-agent -m 0640 household_agent/*.py /opt/household-agent/household_agent/
sudo install -o root -g household-agent -m 0640 config/Settings.json config/tasks.json /opt/household-agent/config/
sudo install -o root -g household-agent -m 0640 tests/*.py /opt/household-agent/tests/
sudo install -o root -g household-agent -m 0640 deploy/household-agent.service /opt/household-agent/deploy/
sudo python3 -m venv /opt/household-agent/.venv
sudo /opt/household-agent/.venv/bin/python -m pip install -r /opt/household-agent/requirements.txt
sudo chown -R root:household-agent /opt/household-agent/.venv
sudo chmod -R g+rX,g-w,o-rwx /opt/household-agent/.venv
sudo install -d -o household-agent -g household-agent -m 0700 /opt/household-agent/storage
```

Application, virtual environment, and configuration belong to `root`. The service
group may read and traverse directories but must not modify them. Do not add other
users to the `household-agent` group. Only `storage/` belongs to the service user
and uses mode `0700`.

**`storage/` must exist before starting the service**, because the unit grants it as
the only writable exception below the application directory. Do not create
`state.json` manually, not even as an empty file or from the SPEC example. The
agent initializes a complete state at its first successful startup and logs that
first start.

### Configure Telegram

1. Create a bot and obtain its token from the official Telegram bot `@BotFather`.
2. For a private chat, message the bot first. For a group, add the bot and allow it to send messages.
3. Determine the numeric chat ID, for example through Telegram Bot API `getUpdates` after sending the bot a message. Group IDs can be negative. Do not give the token to third-party websites.
4. Copy the example file, set permissions, and replace both local placeholders:

```bash
sudo cp /opt/household-agent/.env.example /opt/household-agent/.env
sudo chown root:household-agent /opt/household-agent/.env
sudo chmod 0640 /opt/household-agent/.env
sudoedit /opt/household-agent/.env
```

The file contains `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Never put real
credentials into documentation, version control, shell history, or logs. With
owner/group `root:household-agent` and mode `0640`, `.env` is readable only by root
and the service group. Process environment variables take precedence over `.env`.
The application loads `.env` itself; the unit needs no `EnvironmentFile`. Incoming
messages are not processed.

### JSON configuration

Before first start, update the supplied residents and tasks:

```bash
sudoedit /opt/household-agent/config/Settings.json /opt/household-agent/config/tasks.json
sudo -u household-agent /opt/household-agent/.venv/bin/python /opt/household-agent/main.py --check-config
sudo -u household-agent /opt/household-agent/.venv/bin/python /opt/household-agent/main.py --preview 2026-09-10 --seed 0
```

`config/Settings.json` is case-sensitive under Linux. JSON must be UTF-8; comments
and trailing commas are not allowed.

| Field in Settings.json | Meaning |
| --- | --- |
| `daily_execution_time` | Local `HH:MM`; default `06:00`. |
| `timezone` | IANA timezone; default `Europe/Berlin`; requires timezone data. |
| `tasks_per_active_day` | Positive integer; default `1`; normal capacity, not a hard limit. |
| `residents` | Non-empty list; every person has `id`, `name`, `role`, and `off_days`. |
| `role` | Only `adult` or `child`. |
| `off_days` | List without duplicates: `Monday`, `Tuesday`, `Wednesday`, `Thursday`, `Friday`, `Saturday`, `Sunday`. `[]` means no fixed days off. |

Seven days off are valid; that resident receives no tasks. `config/tasks.json`
contains an object with a `tasks` list, which may be empty. Every task requires
`id`, `name`, `allowed_roles`, and `frequency`:

- `allowed_roles`: either `['adult']` or both `['child', 'adult']`, without duplicates. Child-only tasks are not supported.
- `frequency`: `weekly` or `monthly`. Every task is assigned once per period for the whole household, not once per resident.
- IDs and names must not be empty. IDs must be unique and permanently stable within their respective list. Never reuse an ID for another person or task.
- Unknown fields, duplicate JSON keys, incorrect types, and invalid values are rejected.

### Changes while running

The scheduler waits 15 seconds after every cycle and reloads both JSON files during
the next cycle. A changed execution time takes effect then; if it is already in the
past and today has not been booked, an assignment can happen immediately. Planning
and delivery can lengthen a cycle, so this is not a second-accurate guarantee.
Credentials load only at process start: restart the service after changing `.env`.

Invalid JSON pauses new assignments while persisted messages may still be delivered.
Valid changes affect only open plans, never already booked tasks. Frequency changes
evaluate history under the new frequency and do not simply reset existing weekly or
monthly cooldowns.

For related changes to both files, stop the service, edit copies, fully validate
them, and then replace files on the same filesystem. Two file renames are not a
transaction; stopping the service prevents it from loading a mixed configuration:

```bash
sudo systemctl stop household-agent.service
sudo install -d -o root -g household-agent -m 0750 /opt/household-agent/config-staging /opt/household-agent/config-staging/config
sudo install -o root -g household-agent -m 0640 /opt/household-agent/config/Settings.json /opt/household-agent/config/tasks.json /opt/household-agent/config-staging/config/
sudoedit /opt/household-agent/config-staging/config/Settings.json /opt/household-agent/config-staging/config/tasks.json
sudo chown root:household-agent /opt/household-agent/config-staging/config/Settings.json /opt/household-agent/config-staging/config/tasks.json
sudo chmod 0640 /opt/household-agent/config-staging/config/Settings.json /opt/household-agent/config-staging/config/tasks.json
sudo -u household-agent env PYTHONPATH=/opt/household-agent /opt/household-agent/.venv/bin/python -c 'from pathlib import Path; from household_agent.config import load_config; load_config(Path("/opt/household-agent/config-staging")); print("Configuration valid.")'
```

Continue only after successful validation; otherwise correct the files and test
again. Back up the previous JSON files separately if required.

```bash
sudo mv /opt/household-agent/config-staging/config/Settings.json /opt/household-agent/config/Settings.json
sudo mv /opt/household-agent/config-staging/config/tasks.json /opt/household-agent/config/tasks.json
sudo -u household-agent /opt/household-agent/.venv/bin/python /opt/household-agent/main.py --check-config
sudo systemctl start household-agent.service
```

Start only after this check succeeds. `--check-config` alone does not add a
configuration to persistent history.

Statistics and availability use the local date when the running agent successfully
loads and persists a change (`effective_date`). It does not guess past joins,
departures, or days off from file timestamps or JSON files edited while it was
offline. The last persisted configuration remains effective until the next accepted
change. Historical assignment names and completed monthly data remain unchanged;
removed residents and participants with zero tasks remain in the respective report.

### Planning and fairness

Weeks run Monday through Sunday; months use the local calendar. A month change does
not lift an ongoing weekly cooldown. Roles and days off are hard constraints:
children never receive adult-only tasks and no tasks, including overflow tasks, are
assigned on a fixed day off. A task with no suitable resident-day remains open and
is reported as a bottleneck.

The plan runs from today to the Sunday of the week containing the final day of the
current month, at most 37 calendar days. It includes open monthly tasks for this
month and weekly tasks for all included weeks. Next-month tasks appear only on the
month change.

Implementation uses a shared **min-cost-flow plan** without an external optimizer.
Adult tasks are considered first; reverse edges allow temporary assignments to be
rearranged across roles and deadlines. Weekly and monthly tasks share daily capacity.
Costs optimize, in order:

1. The number of required overflow slots above normal daily capacity.
2. Weekly load balance: minimize `n*n/A`, where `n` is committed plus planned tasks and `A` is considered active days in the week.
3. Daily load distribution: minimize the sum of squared daily task counts.
4. Random remaining ties.

Starting in the middle of a week counts only days from the start date. Fixed days
off create no catch-up obligation. Equal load per active day is the objective, not
equal absolute totals despite different availability. Role bottlenecks and integer
task counts can force unequal results.

Valid tentative plans are persisted and reused instead of being randomized again
every morning. Relevant configuration or period changes and missed placements cause
replanning; committed bookings remain unchanged. Planning occurs during the due
daily cycle, not as continuous background optimization.

The limited horizon keeps planning manageable for households. CPU and memory grow
with task occurrences and suitable resident-days; one shortest augmenting path is
found per occurrence. Very large pools can noticeably consume CPU and RAM. History
and outbox are never automatically deleted; state validation and reconciliation also
have overhead between replans. No reliable runtime or RAM measurement has yet been
made on a Raspberry Pi 3B with 1 GB RAM.

### Scheduling, state, and delivery

- Starting before execution time waits for today's schedule; pending messages may be delivered first. Starting afterward catches up only today if it has not been booked.
- Past days are never assigned retroactively. Open plans are moved within their allowed period; expired tasks are not accumulated as backlog.
- A day with no tasks is still considered executed. Restarts and a backward-moving clock cannot create another booking for an already processed day.
- During a daylight-saving gap, the first valid local minute afterward is used. During a repeated local hour, booking occurs once per local date.
- Assignments, history, monthly counters, and daily message are stored atomically before delivery starts. A temporary file and directory entry are synchronized; `storage/agent.lock` prevents concurrent writers. Do not remove the lock file while operating.
- Missing state means a logged first start. Corrupt or unknown state stops the agent rather than replacing history with an empty state. `--check-config` does not validate state.
- Pending monthly reports are caught up chronologically from the first operating month, including months without assignments. No reports are invented before that month.
- **An undelivered monthly report blocks new assignments** until all report parts are acknowledged. The current day is booked afterward, never backdated.
- **A failed daily delivery does not roll back bookings and does not itself block later daily bookings.** The ordered outbox can therefore grow. At a month change, a waiting daily message can also delay the monthly report and thus new bookings.
- Delivery uses timeouts and exponential retry intervals from 30 seconds to one hour; permanent errors wait at least one hour, and Telegram `retry_after` can extend that. Confirmed message parts are normally not resent.
- Daily messages retain their original date. A delivery first attempted on a later day gets a delayed-notice prefix; already attempted messages keep their frozen text.

**Exactly-once Telegram delivery is not guaranteed.** If Telegram accepts a message
but its response or local acknowledgement is lost, a retry can send a duplicate.
Local assignments and statistics remain separate from that behavior. Messages are
plain text and are split when needed.

### systemd operation

After configuration, `.env`, and `storage/` are ready:

```bash
sudo install -o root -g root -m 0644 /opt/household-agent/deploy/household-agent.service /etc/systemd/system/household-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now household-agent.service
sudo systemctl status household-agent.service
sudo journalctl -u household-agent.service -n 100 --no-pager
sudo journalctl -u household-agent.service -f
```

`enable --now` starts the service immediately and enables startup at boot. The unit
uses dedicated user/group `household-agent`, `UMask=0077`, restart after failure in
30 seconds, and `network-online.target` as start ordering. This does not guarantee
Telegram is reachable. `NoNewPrivileges`, `PrivateTmp`, `ProtectHome=true`, and
`ProtectSystem=strict` restrict the service; only `/opt/household-agent/storage` is
available as persistent write path. Application and configuration below `/home` are
therefore not replacements for this installation. Logs go to the journal, not to
application files.

```bash
sudo systemctl stop household-agent.service
sudo systemctl start household-agent.service
sudo systemctl restart household-agent.service
sudo systemctl disable --now household-agent.service
```

These are individual administration options, not a sequence to run together. After
changing the unit, run `daemon-reload` and `restart`; after changing credentials, a
`restart` is enough. On failure, check JSON, permissions, timezone data, token, chat
ID, and send permission. Do not publish tokens or complete Telegram request URLs in
diagnostic output.

### Backup and recovery

`storage/state.json` contains history, cooldowns, plans, statistics, configuration
history, and outbox. `.gitignore` excludes `.env`, `storage/`, virtual environments,
and Python caches; version control is not a backup. Back up configuration and
credentials separately with comparable access protection.

For a consistent backup after at least one successful first start:

```bash
sudo systemctl stop household-agent.service
sudo install -d -o root -g root -m 0700 /var/backups/household-agent
BACKUP="/var/backups/household-agent/state-$(date -u +%Y%m%dT%H%M%SZ).json"
sudo cp -p /opt/household-agent/storage/state.json "$BACKUP"
sudo stat -c '%U:%G %a %n' "$BACKUP"
sudo systemctl start household-agent.service
```

Continue only after the copy succeeded. `cp -p` retains owner and permissions; the
state file should be owned by `household-agent:household-agent` with mode `600`. The
backup directory is accessible only to root. Also copy backups to protected separate
media; a second file on the same SD card does not protect against card failure.

For recovery, stop the service first and back up current state separately. Set
`BACKUP` to a deliberately chosen backup:

```bash
sudo systemctl stop household-agent.service
BACKUP=/var/backups/household-agent/state-YYYYMMDDTHHMMSSZ.json
sudo install -o household-agent -g household-agent -m 0600 "$BACKUP" /opt/household-agent/storage/.state-restore.tmp
sudo mv /opt/household-agent/storage/.state-restore.tmp /opt/household-agent/storage/state.json
sudo chown household-agent:household-agent /opt/household-agent/storage
sudo chmod 0700 /opt/household-agent/storage
sudo systemctl start household-agent.service
sudo journalctl -u household-agent.service -n 100 --no-pager
```

Check every step before proceeding. The agent validates state at startup. An older
backup loses bookings and delivery acknowledgements made since that backup; renewed
assignments or messages can occur. Do not use a backup for arbitrary resets.

**Deleting `state.json` loses history, period cooldowns, counters, and delivery
status.** The next first start can assign tasks again and duplicate messages. Do not
work around errors by deleting or manually editing state; deliberately restore a
known-good backup instead.

### Tests and remaining acceptance work

Automated tests use `unittest` and need no real Telegram credentials:

```bash
python3 -m unittest discover -s tests -v
```

A successful test run proves only the tests that exist; it does not automatically
prove all acceptance criteria in `SPEC.md`. Installation on an actual Raspberry Pi
and live Telegram delivery have not yet been verified.

Tests include a 56-day simulation and small planning cases checked against a fully
enumerated reference solution. With dependencies installed, further tests exercise
the real Telegram library against a fully simulated HTTP transport; without those
dependencies they are skipped. None of these tests sends real messages.

Remaining smoke-test checklist, using a dedicated test chat and separate state:

- [ ] Install on maintained Raspberry Pi OS; verify Python version, dependencies, timezone, and file permissions.
- [ ] Run `--check-config`, deterministic preview, and tests; measure planning time and RAM with a realistic task pool.
- [ ] Enable the service and verify boot startup; application, JSON, and `.env` must not be writable, while `storage/` must be writable.
- [ ] Set an execution time for testing; check a real German daily message and residents with days off or no assignment. There is no separate test-message switch.
- [ ] Restart on the same day; state must persist without a second daily booking or duplicate statistic.
- [ ] Test a controlled network outage without losing administrative Pi access; daily booking remains and the outbox is delivered after connectivity returns.
- [ ] Test a month change in isolated test operation: failed report blocks booking and successful report precedes it. Do not manipulate production time or production state for this.
- [ ] Test JSON changes, invalid configuration, and credential restart; practice backup and recovery with test state.
