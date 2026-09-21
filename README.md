# Household Task Agent

[English below](#english)

Python-Dienst zur automatischen Verteilung von Haushaltsaufgaben. Er sendet
tägliche Zuteilungen und Monatsstatistiken auf Deutsch an Telegram. Bewohner,
Rollen, freie Wochentage und Aufgaben stehen in JSON; Zustand und Versandwarteschlange
bleiben lokal erhalten.

**Gezählt werden zugewiesene Aufgaben, nicht bestätigte Erledigungen.**
Erledigungsbuttons, Umbuchungen, Weboberfläche und mehrere Haushalte sind nicht
implementiert. `SPEC.md` beschreibt Zielverhalten und Abnahmekriterien.

## Überblick

- Wochen- und Monatsaufgaben werden unter Rollen- und Verfügbarkeitsregeln verteilt.
- Min-Cost-Flow priorisiert wenig Überlast, faire Wochenlast und verteilte Tageslast.
- Tageszuweisungen, Historie, Monatszähler und Outbox werden atomar gespeichert.
- Monatliche Berichte und fehlgeschlagene Telegram-Zustellung werden wiederholt.

## Voraussetzungen

Unterstützt wird ein Linux-Host mit ARM64-Architektur. Veröffentlicht werden
`linux/arm64`-Images; Docker ist der offizielle Betriebsweg. Für lokale
Entwicklung werden Python 3.11+, `tzdata` und Telegram-Zugangsdaten benötigt.

Direkte Laufzeitabhängigkeiten stehen in `requirements.txt`:
`python-telegram-bot` und `python-dotenv`.

## Lokale Entwicklung und Tests

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python main.py --check-config
python main.py --preview 2026-09-10 --seed 0
python3 -m unittest discover -s tests -v
```

`--check-config` prüft JSON ohne `.env`, Zustand oder Netzwerk. `--preview`
zeigt einen hypothetischen Plan und schreibt keinen Zustand. `python main.py`
ist kein Testlauf: Es kann echte Nachrichten senden und darf nicht parallel zum
Container laufen.

## Konfiguration

`config/Settings.json` enthält Ausführungszeit, IANA-Zeitzone, Tageskapazität
und Bewohner. `config/tasks.json` enthält Wochen- und Monatsaufgaben. JSON muss
UTF-8 sein; Kommentare, nachgestellte Kommata, unbekannte Felder und doppelte
Schlüssel werden abgewiesen.

`role` ist `adult` oder `child`; freie Tage verhindern Zuweisungen. Aufgaben
haben stabile IDs, erlaubte Rollen und `weekly` oder `monthly` als Frequenz.
Bei laufendem Container Konfigurationen zusammen ändern: Container stoppen,
Kopien prüfen und Dateien atomar ersetzen; danach Container starten.

## Telegram einrichten

1. Bot bei BotFather erstellen und Token abrufen.
2. Bot im Zielchat hinzufügen und Senden erlauben.
3. Numerische Chat-ID über Telegram `getUpdates` ermitteln.
4. `.env` aus `.env.example` anlegen; Token niemals in Logs oder Versionsverwaltung speichern.

## Deployment mit Docker

### Veröffentlichte Images

Jeder Push nach `main` testet und veröffentlicht:

```text
ghcr.io/stfngr/householdtaskassignmentagent:latest
ghcr.io/stfngr/householdtaskassignmentagent:main
ghcr.io/stfngr/householdtaskassignmentagent:sha-<commit>
```

GitHub **Package** nach erstem Workflow-Lauf auf **Public** setzen. SHA-Tags
sind unveränderlich. Die Docker-Pipeline veröffentlicht aktuell `latest` und
`main` direkt neben dem SHA-Tag.

### Erstinstallation

Docker Engine mit Compose-Plugin auf Linux ARM64 installieren. Aus dem
Projektverzeichnis Konfiguration und persistenten Zustand anlegen:

```bash
sudo install -d -o 10001 -g 10001 -m 0700 /etc/household-agent/config /var/lib/household-agent
sudo install -o 10001 -g 10001 -m 0600 .env.example /etc/household-agent/.env
sudo install -o 10001 -g 10001 -m 0600 config/Settings.json config/tasks.json /etc/household-agent/config/
sudoedit /etc/household-agent/.env
sudoedit /etc/household-agent/config/Settings.json /etc/household-agent/config/tasks.json
```

Docker liest `.env` als Prozessumgebung; die Datei wird nicht eingebunden.
Container läuft als UID/GID `10001`, daher gehören Konfigurations- und
Zustandspfade diesem Benutzer.

### Starten und Status prüfen

```bash
sudo docker pull ghcr.io/stfngr/householdtaskassignmentagent:latest
sudo docker run -d \
  --name household-agent \
  --restart unless-stopped \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --env-file /etc/household-agent/.env \
  --mount type=bind,src=/etc/household-agent/config,dst=/app/config,readonly \
  --mount type=bind,src=/var/lib/household-agent,dst=/app/storage \
  ghcr.io/stfngr/householdtaskassignmentagent:latest
sudo docker logs -f household-agent
sudo docker ps --filter name=household-agent
```

### Aktualisieren und Rollback

```bash
sudo docker pull ghcr.io/stfngr/householdtaskassignmentagent:latest
sudo docker rm -f household-agent
# Then rerun the preceding docker-run command with identical mounts and options.
```

Für Rollback `latest` durch veröffentlichten `sha-<commit>`-Tag ersetzen. Der
State-Bind-Mount bleibt erhalten.

## Betrieb und Daten

`storage/state.json` enthält Historie, Sperren, Pläne, Statistiken,
Konfigurationsverlauf und Outbox. Zustand nicht löschen oder manuell bearbeiten:
ein Erststart kann Aufgaben neu vergeben und Nachrichten duplizieren.

Für Backups Container stoppen, den gesamten State-Bind-Mount bzw. mindestens
`state.json` konsistent sichern und Container starten. Ein älteres Backup verliert
seitdem erfolgte Buchungen und Versandbestätigungen. Tests brauchen keine echten
Telegram-Zugangsdaten; ein erfolgreicher Lauf ersetzt keinen Betriebs-Smoke-Test.

## Alternative: systemd

`deploy/household-agent.service` und die systemd-spezifischen Installationsdateien
bleiben als Alternative im Repository. Docker und systemd nicht gleichzeitig mit
denselben Telegram-Zugangsdaten ausführen. Docker ist der gepflegte Betriebsweg.

## English

Python service for automatic household-task allocation. It sends daily assignments
and monthly statistics in German to Telegram. Residents, roles, days off, and
tasks are JSON configuration; state and delivery queue persist locally.

**The agent counts assigned tasks, not confirmed completion.** Completion buttons,
reassignments, web UI, and multiple households are not implemented. `SPEC.md`
contains target behavior and acceptance criteria.

## Overview

- Weekly and monthly tasks follow role and availability constraints.
- Min-cost flow prioritizes low overflow, fair weekly load, and distributed daily load.
- Assignments, history, monthly counters, and outbox persist atomically.
- Monthly reports and failed Telegram delivery retry.

## Requirements

Supported deployment target: Linux ARM64 host. Published images are
`linux/arm64`; Docker is the supported runtime. Local development needs Python
3.11+, `tzdata`, and Telegram credentials.

## Local Development And Tests

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python main.py --check-config
python main.py --preview 2026-09-10 --seed 0
python3 -m unittest discover -s tests -v
```

`--check-config` validates JSON without `.env`, state, or network. `--preview`
is hypothetical and writes no state. `python main.py` can send real messages;
never run it alongside the container.

## Configuration

`config/Settings.json` defines execution time, timezone, capacity, and residents.
`config/tasks.json` defines weekly and monthly tasks. JSON must be UTF-8 with no
comments, trailing commas, unknown fields, or duplicate keys. Stop the container,
validate related changes together, replace files atomically, then restart it.

## Telegram Setup

Create bot with BotFather, add it to the target chat, permit sending, derive the
numeric chat ID using `getUpdates`, and create `.env` from `.env.example`. Never
expose the token in logs or version control.

## Deployment With Docker

### Published Images

Every `main` push tests and publishes:

```text
ghcr.io/stfngr/householdtaskassignmentagent:latest
ghcr.io/stfngr/householdtaskassignmentagent:main
ghcr.io/stfngr/householdtaskassignmentagent:sha-<commit>
```

Make GitHub Package public after first publication. SHA tags are immutable. The
current Docker pipeline pushes `latest` and `main` directly with the SHA tag.

### First Installation

Install Docker Engine with Compose plugin on Linux ARM64. Create
`/etc/household-agent/.env`, `/etc/household-agent/config`, and
`/var/lib/household-agent` exactly as in the German commands above.

### Start And Inspect

Use the locale-independent `docker pull` and `docker run` command above. Inspect
with `sudo docker logs -f household-agent` and `sudo docker ps --filter name=household-agent`.

### Update And Roll Back

Pull `latest`, remove the container, and rerun it with the same mounts/options.
Replace `latest` with published `sha-<commit>` for rollback.

## Operations And Data

`storage/state.json` holds history, cooldowns, plans, statistics, configuration
history, and outbox. Never delete or edit it manually. Stop the container for a
consistent backup of state. An older backup loses bookings and delivery
acknowledgements made afterward. Tests require no real Telegram credentials but
do not replace operational smoke testing.

## Alternative: systemd

`deploy/household-agent.service` remains an alternative. Never operate systemd
and Docker concurrently with the same Telegram credentials. Docker is maintained.
