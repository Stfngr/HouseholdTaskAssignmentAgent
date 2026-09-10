"""Household agent entry point and restart-safe daily orchestration."""

import argparse
import asyncio
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import random
import signal
from zoneinfo import ZoneInfo

from household_agent.allocation import monday, month_key, plan
from household_agent.config import ConfigError, config_snapshot, load_config, load_credentials
from household_agent.state import StateError, StateStore, new_state, reconcile
from household_agent.telegram import (
    TelegramSender, daily_text, deliver_one, make_message, monthly_text,
)


LOGGER = logging.getLogger("household_agent")
ROOT = Path(__file__).resolve().parent


def execution_instant(day: date, execution_time: str, zone: ZoneInfo) -> datetime:
    """Resolve DST gaps to the next valid minute and folds to the first instant."""
    wall = datetime.combine(day, time.fromisoformat(execution_time))
    for _ in range(24 * 60 + 1):
        candidates = []
        for fold in (0, 1):
            instant = wall.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            if instant.astimezone(zone).replace(tzinfo=None) == wall:
                candidates.append(instant)
        if candidates:
            return min(candidates)
        wall += timedelta(minutes=1)
    raise ValueError("No valid execution instant found")


def _persist(state, candidate, save):
    if candidate != state:
        save(candidate)
        state.clear()
        state.update(candidate)


def _reports(state, today):
    candidate = deepcopy(state)
    queued = {item["reference"] for item in candidate["outbox"] if item["kind"] == "monthly"}
    for month, statistics in sorted(candidate["monthly_statistics"].items()):
        if month < month_key(today) and month not in queued:
            candidate["outbox"].append(make_message(
                "monthly:" + month, "monthly", month, monthly_text(month, statistics),
            ))
    return candidate


def book_day(state: dict, snapshot: dict, today: date, rng: random.Random) -> dict:
    """Build one atomic daily transaction, without I/O or Telegram delivery."""
    day_key = today.isoformat()
    if (day_key <= (state["last_execution_date"] or "")
            or day_key < state["last_observed_date"]):
        return deepcopy(state)
    if month_key(today) in state["reported_months"]:
        raise StateError("A reported month cannot be reopened")
    if any(month < month_key(today) and month not in state["reported_months"]
           for month in state["monthly_statistics"]):
        raise StateError("Previous monthly reports must be delivered before allocation")
    candidate = deepcopy(state)
    settings = snapshot["settings"]
    planning_input = {
        "residents": settings["residents"], "capacity": settings["tasks_per_active_day"],
        "tasks": snapshot["tasks"],
    }
    context = {
        "week": monday(today).isoformat(), "month": month_key(today),
        "fingerprint": hashlib.sha256(json.dumps(planning_input, sort_keys=True).encode()).hexdigest(),
    }
    missed = any(item["date"] < day_key for item in candidate["planned_assignments"])
    if context != candidate["plan_context"] or missed:
        if missed:
            LOGGER.warning("Verpasste vorlaeufige Zuteilungen werden neu geplant; keine Rueckdatierung.")
        candidate["planned_assignments"], candidate["unassignable"] = plan(snapshot, candidate, today, rng)
        candidate["plan_context"] = context

    residents = {item["id"]: item for item in settings["residents"]}
    tasks = {item["id"]: item for item in snapshot["tasks"]}
    assignments = []
    weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    for entry in candidate["planned_assignments"]:
        if entry["date"] != day_key:
            continue
        task, resident = tasks.get(entry["task_id"]), residents.get(entry["resident_id"])
        if (task is None or resident is None or resident["role"] not in task["allowed_roles"]
                or weekdays[today.weekday()] in resident["off_days"]
                or task["frequency"] != entry["frequency"]):
            raise StateError("Planned assignment is no longer eligible")
        period = monday(today).isoformat() if task["frequency"] == "weekly" else month_key(today)
        if entry["period"] != period:
            raise StateError("Planned assignment has an invalid period")
        for past in candidate["assignment_history"] + assignments:
            assigned_date = date.fromisoformat(past["date"])
            past_period = (monday(assigned_date).isoformat() if task["frequency"] == "weekly"
                           else month_key(assigned_date))
            if past["task_id"] == task["id"] and past_period == period:
                raise StateError("Planned assignment violates a period cooldown")
        assignment = dict(entry, id=json.dumps([task["id"], task["frequency"], period]),
                          task_name=task["name"], resident_name=resident["name"])
        assignments.append(assignment)
        candidate["monthly_statistics"][month_key(today)][resident["id"]]["count"] += 1
    candidate["assignment_history"].extend(assignments)
    candidate["planned_assignments"] = [
        entry for entry in candidate["planned_assignments"] if entry["date"] > day_key
    ]
    candidate["last_execution_date"] = day_key
    candidate["outbox"].append(make_message(
        "daily:" + day_key, "daily", day_key,
        daily_text(today, settings["residents"], assignments, candidate["unassignable"]),
    ))
    return candidate


async def run_cycle(state, snapshot, save, send, clock, rng):
    """Reconcile configuration, deliver reports, then commit at most one day."""
    zone = ZoneInfo(snapshot["settings"]["timezone"])
    now = clock()
    today = now.astimezone(zone).date()
    _persist(state, reconcile(state, snapshot, today), save)
    due = now >= execution_instant(today, snapshot["settings"]["daily_execution_time"], zone)
    watermark = max(state["last_observed_date"], state["last_execution_date"] or "",
                    state["config_history"][-1]["effective_date"] if state["config_history"] else "")
    if due and today.isoformat() >= watermark:
        _persist(state, _reports(state, today), save)

    await deliver_one(state, send, save, now, local_day=today)
    # A send may span midnight. Never commit with a stale date/config snapshot.
    current = clock()
    if current.astimezone(zone).date() != today:
        return
    if current < execution_instant(today, snapshot["settings"]["daily_execution_time"], zone):
        return
    if (not due or today.isoformat() < watermark
            or today.isoformat() <= (state["last_execution_date"] or "")):
        return
    if any(month < month_key(today) and month not in state["reported_months"]
           for month in state["monthly_statistics"]):
        return
    _persist(state, book_day(state, snapshot, today, rng), save)
    await deliver_one(state, send, save, clock(), local_day=today)


async def serve(root: Path):
    store = StateStore(root)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    clock = lambda: datetime.now(timezone.utc)
    rng = random.Random()
    with store.lock():
        state = store.load()
        token, chat_id = load_credentials(root)
        last_error = None
        async with TelegramSender(token, chat_id) as sender:
            while not stop.is_set():
                try:
                    snapshot = config_snapshot(load_config(root))
                except ConfigError as error:
                    if str(error) != last_error:
                        LOGGER.error("Konfiguration ungueltig: %s", error)
                        last_error = str(error)
                    if state is not None:
                        local_day = clock().date()
                        if state["config_history"]:
                            zone = ZoneInfo(state["config_history"][-1]["snapshot"]["settings"]["timezone"])
                            local_day = clock().astimezone(zone).date()
                        await deliver_one(state, sender.send, store.save, clock(), local_day=local_day)
                else:
                    if last_error:
                        LOGGER.info("Konfiguration wieder gueltig.")
                        last_error = None
                    if state is None:
                        today = clock().astimezone(ZoneInfo(snapshot["settings"]["timezone"])).date()
                        LOGGER.warning("Erststart ohne Zustand: neue Historie wird angelegt.")
                        candidate = reconcile(new_state(today), snapshot, today)
                        store.save(candidate)
                        state = candidate
                    await run_cycle(state, snapshot, store.save, sender.send, clock, rng)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Haushaltsaufgaben automatisch verteilen")
    parser.add_argument("--check-config", action="store_true", help="JSON pruefen; kein Versand, keine Zustandsaenderung")
    parser.add_argument("--preview", metavar="YYYY-MM-DD", help="Unverbindliche Planung ohne Zustand oder Telegram")
    parser.add_argument("--seed", type=int, default=0, help="Zufallsstartwert fuer --preview")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.check_config or args.preview:
            snapshot = config_snapshot(load_config(ROOT))
            if args.preview:
                today = date.fromisoformat(args.preview)
                state = reconcile(new_state(today), snapshot, today)
                assignments, unassignable = plan(snapshot, state, today, random.Random(args.seed))
                print(json.dumps({"planned_assignments": assignments, "unassignable": unassignable},
                                 ensure_ascii=False, indent=2))
            else:
                print("Konfiguration gueltig.")
            return 0
        asyncio.run(serve(ROOT))
        return 0
    except (ConfigError, StateError, ValueError) as error:
        LOGGER.error("Agent gestoppt: %s", error)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Third-party exceptions may contain credential-bearing request URLs.
        LOGGER.error("Agent wegen unerwartetem Fehler gestoppt; Zustand bleibt erhalten.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
