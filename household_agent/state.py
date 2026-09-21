"""Validated durable state and historical household availability."""

import calendar
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
)


class StateError(ValueError):
    """State is invalid, unavailable, or locked by another agent."""


def _require(condition, message):
    if not condition:
        raise StateError(message)


def _object(value, required, location, optional=()):
    _require(type(value) is dict, f"{location}: expected an object")
    _require(set(required) <= value.keys(), f"{location}: missing required fields")
    _require(value.keys() <= set(required) | set(optional), f"{location}: unknown fields")


def _string(value, location):
    _require(type(value) is str and bool(value.strip()), f"{location}: expected nonempty text")


def _date(value, location):
    _string(value, location)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise StateError(f"{location}: invalid ISO date") from exc
    _require(parsed.isoformat() == value, f"{location}: expected YYYY-MM-DD")
    return parsed


def _month(value, location):
    _string(value, location)
    _require(bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}", value)), f"{location}: expected YYYY-MM")
    return _date(value + "-01", location)


def _json_value(value):
    if type(value) is dict:
        for key, item in value.items():
            _require(type(key) is str, "JSON object keys must be strings")
            _json_value(item)
    elif type(value) is list:
        for item in value:
            _json_value(item)
    else:
        _require(value is None or type(value) in (str, int, float, bool), "Invalid JSON value")
        if type(value) is float:
            _require(math.isfinite(value), "Non-finite JSON number")
        if type(value) is str:
            _require(not any(0xD800 <= ord(char) <= 0xDFFF for char in value), "Invalid Unicode text")


def _snapshot(snapshot):
    _object(snapshot, {"settings", "tasks"}, "snapshot")
    settings = snapshot["settings"]
    _object(settings, {"daily_execution_time", "timezone", "tasks_per_active_day", "residents"}, "settings")
    time = settings["daily_execution_time"]
    _require(type(time) is str and bool(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", time)), "Invalid execution time")
    _string(settings["timezone"], "timezone")
    try:
        ZoneInfo(settings["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise StateError("Invalid timezone") from exc
    capacity = settings["tasks_per_active_day"]
    _require(type(capacity) is int and capacity > 0, "Invalid daily capacity")
    _require(type(settings["residents"]) is list and bool(settings["residents"]), "Residents must be a nonempty list")
    _require(type(snapshot["tasks"]) is list, "Tasks must be a list")
    for kind, records in (("resident", settings["residents"]), ("task", snapshot["tasks"])):
        seen = set()
        for record in records:
            fields = {"id", "name", "role", "off_days"} if kind == "resident" else {"id", "name", "frequency", "allowed_roles"}
            _object(record, fields, kind)
            for field in ("id", "name"):
                _string(record[field], f"{kind}.{field}")
            _require(record["id"] not in seen, f"Duplicate {kind} ID")
            seen.add(record["id"])
            choices = record["off_days"] if kind == "resident" else record["allowed_roles"]
            allowed = WEEKDAYS if kind == "resident" else ("adult", "child")
            _require(type(choices) is list, f"Invalid {kind} choices")
            _require(all(type(item) is str and item in allowed for item in choices), f"Invalid {kind} choices")
            _require(len(choices) == len(set(choices)), f"Duplicate {kind} choices")
            if kind == "resident":
                _require(record["role"] in ("adult", "child"), "Invalid resident role")
            else:
                _require("adult" in choices, "Task must allow adults")
                _require(record["frequency"] in ("weekly", "monthly"), "Invalid task frequency")


def _semantic_snapshot(snapshot):
    result = deepcopy(snapshot)
    result["settings"]["residents"].sort(key=lambda item: item["id"])
    result["tasks"].sort(key=lambda item: item["id"])
    for resident in result["settings"]["residents"]:
        resident["off_days"].sort()
    for task in result["tasks"]:
        task["allowed_roles"].sort()
    return result


def new_state(today: date) -> dict:
    """Create an unconfigured first-start state; the caller logs first startup."""
    _require(type(today) is date, "today must be a date")
    return {
        "schema_version": 2,
        "started_on": today.isoformat(),
        "last_observed_date": today.isoformat(),
        "last_execution_date": None,
        "availability_by_week": {},
        "planned_assignments": [],
        "assignment_history": [],
        "monthly_statistics": {},
        "reported_months": [],
        "outbox": [],
        "config_history": [],
        "plan_context": None,
        "unassignable": [],
        "dashboard_pending": None,
        "dashboard_last_updated_at": None,
    }


def _validate(state):
    """Reject malformed records and counters before either loading or saving."""
    _json_value(state)
    required = {
        "schema_version", "started_on", "last_observed_date", "last_execution_date", "availability_by_week",
        "planned_assignments", "assignment_history", "monthly_statistics", "reported_months", "outbox",
        "dashboard_pending", "dashboard_last_updated_at",
    }
    _object(state, required, "state", {"config_history", "plan_context", "unassignable"})
    _require(type(state["schema_version"]) is int and state["schema_version"] == 2, "Unknown state schema")
    started = _date(state["started_on"], "started_on")
    observed = _date(state["last_observed_date"], "last_observed_date")
    _require(observed >= started, "Observation predates startup")
    last = state["last_execution_date"]
    if last is not None:
        last = _date(last, "last_execution_date")
        _require(last >= started, "Execution predates startup")
        _require(observed >= last, "Observation predates last execution")
    for field in ("planned_assignments", "assignment_history", "reported_months", "outbox", "config_history", "unassignable"):
        _require(type(state.get(field, [])) is list, f"{field}: expected a list")
    for field in ("availability_by_week", "monthly_statistics"):
        _require(type(state[field]) is dict, f"{field}: expected an object")
    _require(state.get("plan_context") is None or type(state["plan_context"]) is dict, "Invalid plan_context")
    for name in state.get("unassignable", []):
        _string(name, "unassignable task name")

    previous = started
    for event in state.get("config_history", []):
        _object(event, {"effective_date", "snapshot"}, "config_history entry")
        effective = _date(event["effective_date"], "effective_date")
        _require(effective >= previous, "Config history must be chronological and not predate startup")
        previous = effective
        _snapshot(event["snapshot"])
    _require(observed >= previous, "Observation predates latest config")

    for week, residents in state["availability_by_week"].items():
        beginning = _date(week, "availability week")
        _require(beginning.weekday() == 0, "Availability week must begin on Monday")
        _require((started - beginning).days <= 6, "Availability week predates startup")
        _require(type(residents) is dict, "Availability residents must be an object")
        for resident_id, days in residents.items():
            _string(resident_id, "availability resident ID")
            _require(type(days) is list, "Availability days must be a list")
            parsed = [_date(day, "availability date") for day in days]
            _require(len(parsed) == len(set(parsed)), "Duplicate availability date")
            _require(all(day >= started and 0 <= (day - beginning).days <= 6 for day in parsed), "Availability date outside its week or before startup")

    counts = Counter()
    history_ids = set()
    executed_dates = {last.isoformat()} if last is not None else set()
    for field in ("assignment_history", "planned_assignments"):
        occurrences = set()
        for entry in state[field]:
            fields = {"task_id", "frequency", "period", "date", "resident_id"}
            if field == "assignment_history":
                fields |= {"id", "task_name", "resident_name"}
            _object(entry, fields, field + " entry")
            for key in fields:
                _string(entry[key], f"{field}.{key}")
            day = _date(entry["date"], "assignment date")
            _require(day >= started, "Assignment predates startup")
            if entry["frequency"] == "weekly":
                period = _date(entry["period"], "weekly period")
                _require(period.weekday() == 0 and 0 <= (day - period).days <= 6, "Assignment outside weekly period")
            else:
                _require(entry["frequency"] == "monthly", "Invalid assignment frequency")
                _month(entry["period"], "monthly period")
                _require(entry["period"] == entry["date"][:7], "Assignment outside monthly period")
            occurrence = (entry["task_id"], entry["frequency"], entry["period"])
            _require(occurrence not in occurrences, "Duplicate task occurrence")
            occurrences.add(occurrence)
            if field == "assignment_history":
                _require(last is not None and day <= last, "Assignment after last execution")
                _require(entry["id"] not in history_ids, "Duplicate assignment ID")
                history_ids.add(entry["id"])
                executed_dates.add(entry["date"])
                counts[entry["date"][:7], entry["resident_id"]] += 1

    for month, residents in state["monthly_statistics"].items():
        _month(month, "statistics month")
        _require(month >= state["started_on"][:7], "Statistics predate startup")
        _require(type(residents) is dict, "Monthly statistics must be an object")
        for resident_id, entry in residents.items():
            _string(resident_id, "statistics resident ID")
            _object(entry, {"name", "count"}, "statistics entry")
            _string(entry["name"], "statistics name")
            _require(type(entry["count"]) is int and entry["count"] >= 0, "Invalid statistics count")
            _require(entry["count"] == counts.pop((month, resident_id), 0), "Statistics disagree with assignment history")
    _require(not counts, "Assignment history is missing monthly statistics")

    reported = set()
    for month in state["reported_months"]:
        first = _month(month, "reported month")
        end = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        _require(observed > end, "Reported month must end before last observation")
        _require(month not in reported, "Duplicate reported month")
        _require(month in state["monthly_statistics"], "Reported month has no statistics")
        reported.add(month)
    message_ids = set()
    references = set()
    complete_months = set()
    earlier_pending = False
    for message in state["outbox"]:
        _object(message, {"id", "kind", "reference", "parts", "attempts", "next_attempt_at"}, "outbox message")
        _string(message["id"], "message ID")
        _require(message["id"] not in message_ids, "Duplicate message ID")
        message_ids.add(message["id"])
        kind = message["kind"]
        _require(kind in ("daily", "monthly"), "Invalid message kind")
        reference = message["reference"]
        if kind == "daily":
            day = _date(reference, "daily reference")
            _require(last is not None and started <= day <= last, "Daily message outside execution dates")
            _require(reference[:7] not in complete_months, "Daily booking follows its completed monthly report")
        else:
            _month(reference, "monthly reference")
            _require(reference in state["monthly_statistics"], "Monthly message has no statistics")
        _require((kind, reference) not in references, "Duplicate message reference")
        references.add((kind, reference))
        _require(type(message["attempts"]) is int and message["attempts"] >= 0, "Invalid retry count")
        retry = message["next_attempt_at"]
        if retry is not None:
            _string(retry, "next_attempt_at")
            _require(bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)", retry)), "Retry timestamp must be ISO UTC")
            try:
                datetime.fromisoformat(retry.replace("Z", "+00:00"))
            except ValueError as exc:
                raise StateError("Invalid retry timestamp") from exc
        parts = message["parts"]
        _require(type(parts) is list and bool(parts), "Message parts must be a nonempty list")
        pending = False
        for part in parts:
            _object(part, {"text", "sent"}, "message part")
            _string(part["text"], "message text")
            _require(len(part["text"].encode("utf-16-le")) // 2 <= 4096, "Message part exceeds Telegram limit")
            _require(type(part["sent"]) is bool, "Invalid message delivery status")
            _require(not (earlier_pending and part["sent"]), "Outbox messages confirmed out of order")
            _require(not (pending and part["sent"]), "Message parts confirmed out of order")
            pending |= not part["sent"]
        if not pending:
            _require(retry is None, "Delivered message still has a retry scheduled")
            if kind == "monthly":
                complete_months.add(reference)
        earlier_pending |= pending
    _require(all(("daily", day) in references for day in executed_dates), "Executed date is missing its daily outbox message")
    _require(reported == complete_months, "Reported months disagree with complete monthly outbox messages")

    last_dashboard_update = state["dashboard_last_updated_at"]
    if last_dashboard_update is not None:
        _string(last_dashboard_update, "dashboard_last_updated_at")
        _require(bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\+00:00", last_dashboard_update)), "Dashboard last update timestamp must be canonical UTC")
        try:
            last_dashboard_time = datetime.fromisoformat(last_dashboard_update.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StateError("Invalid dashboard last update timestamp") from exc
        _require(last_dashboard_time.tzinfo is not None and last_dashboard_time.utcoffset() == timedelta(0), "Dashboard last update timestamp must be UTC")
    pending = state["dashboard_pending"]
    if pending is not None:
        _object(pending, {"updated_at", "payload", "attempts", "next_attempt_at"}, "dashboard_pending")
        _string(pending["updated_at"], "dashboard updated_at")
        _require(bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\+00:00", pending["updated_at"])), "Dashboard updated_at must be canonical UTC")
        try:
            updated_at = datetime.fromisoformat(pending["updated_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise StateError("Invalid dashboard updated_at") from exc
        _require(updated_at.tzinfo is not None and updated_at.utcoffset() == timedelta(0), "Dashboard updated_at must be UTC")
        _require(last_dashboard_update == pending["updated_at"], "Dashboard pending timestamp mismatch")
        payload = pending["payload"]
        _object(payload, {"date", "residents"}, "dashboard payload")
        payload_day = _date(payload["date"], "dashboard payload date")
        _require(last is not None and started <= payload_day <= last, "Dashboard payload outside execution dates")
        _require(type(payload["residents"]) is list and bool(payload["residents"]), "Dashboard residents must be a nonempty list")
        resident_ids = set()
        for resident in payload["residents"]:
            _object(resident, {"id", "name", "off_day", "tasks"}, "dashboard resident")
            _string(resident["id"], "dashboard resident ID")
            _require(resident["id"] not in resident_ids, "Duplicate dashboard resident ID")
            resident_ids.add(resident["id"])
            _string(resident["name"], "dashboard resident name")
            _require(type(resident["off_day"]) is bool, "Invalid dashboard off_day")
            _require(type(resident["tasks"]) is list, "Dashboard tasks must be a list")
            _require(all(type(task) is str and task.strip() for task in resident["tasks"]), "Invalid dashboard task")
            _require(not resident["off_day"] or not resident["tasks"], "Dashboard off day has tasks")
        _require(type(pending["attempts"]) is int and pending["attempts"] >= 0, "Invalid dashboard retry count")
        retry = pending["next_attempt_at"]
        if retry is not None:
            _string(retry, "dashboard next_attempt_at")
            try:
                retry_at = datetime.fromisoformat(retry.replace("Z", "+00:00"))
            except ValueError as exc:
                raise StateError("Invalid dashboard retry timestamp") from exc
            _require(retry_at.tzinfo is not None and retry_at.utcoffset() == timedelta(0), "Dashboard retry timestamp must be UTC")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _migrate(state: dict) -> tuple[dict, bool]:
    """Upgrade v1 only by adding an empty optional dashboard delivery slot."""
    if (type(state) is not dict or type(state.get("schema_version")) is not int
            or state["schema_version"] != 1):
        return state, False
    migrated = deepcopy(state)
    migrated["schema_version"] = 2
    migrated["dashboard_pending"] = None
    migrated["dashboard_last_updated_at"] = None
    return migrated, True


class StateStore:
    """A store whose caller holds lock() for the entire agent lifetime."""

    def __init__(self, root: Path):
        self.path = Path(root) / "storage" / "state.json"

    def load(self) -> Optional[dict]:
        try:
            with self.path.open(encoding="utf-8") as source:
                state = json.load(source, object_pairs_hook=_unique_object)
            state, migrated = _migrate(state)
            _validate(state)
            if migrated:
                self.save(state)
            return state
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise StateError(f"Cannot load state: {exc}") from exc

    def save(self, state: dict) -> None:
        temporary = None
        try:
            _validate(state)
            payload = json.dumps(state, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix=".state-", suffix=".tmp", delete=False) as target:
                temporary = target.name
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise StateError(f"Cannot save state: {exc}") from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    @contextmanager
    def lock(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = (self.path.parent / "agent.lock").open("a")
        except OSError as exc:
            raise StateError(f"Cannot open agent lock: {exc}") from exc
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StateError("Agent state is already locked or lock is unavailable") from exc
            yield
        finally:
            # Closing releases flock; never unlink the shared lock inode.
            handle.close()


def reconcile(state: dict, snapshot: dict, today: date) -> dict:
    """Apply today's config without rewriting past availability or membership."""
    _validate(state)
    _json_value(snapshot)
    _snapshot(snapshot)
    _require(type(today) is date, "today must be a date")
    result = deepcopy(state)
    history = result.get("config_history", [])
    latest = max(state["last_observed_date"], state["started_on"], state["last_execution_date"] or state["started_on"], history[-1]["effective_date"] if history else state["started_on"])
    if today.isoformat() < latest:
        return result
    result["last_observed_date"] = max(state["last_observed_date"], today.isoformat())
    history = result.setdefault("config_history", [])
    if not history or _semantic_snapshot(history[-1]["snapshot"]) != _semantic_snapshot(snapshot):
        history.append({"effective_date": today.isoformat(), "snapshot": deepcopy(snapshot)})

    availability = result["availability_by_week"]
    recorded = {(week, resident_id) for week, residents in availability.items() for resident_id in residents}
    for week, residents in list(availability.items()):
        for resident_id, days in list(residents.items()):
            residents[resident_id] = [day for day in days if day < today.isoformat()]
            if week >= today.isoformat():
                del residents[resident_id]
        if not residents:
            del availability[week]

    statistics = result["monthly_statistics"]
    # Even a same-day join/remove event constitutes membership in that month.
    for event in history:
        month = event["effective_date"][:7]
        entries = statistics.setdefault(month, {})
        for resident in event["snapshot"]["settings"]["residents"]:
            entries.setdefault(resident["id"], {"name": resident["name"], "count": 0})

    month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    horizon_end = month_end + timedelta(days=6 - month_end.weekday())
    day = _date(result["started_on"], "started_on")
    event_index = -1
    while day <= horizon_end:
        day_key = day.isoformat()
        while event_index + 1 < len(history) and history[event_index + 1]["effective_date"] <= day_key:
            event_index += 1
        if day <= today:
            entries = statistics.setdefault(day_key[:7], {})
        if event_index >= 0:
            week = (day - timedelta(days=day.weekday())).isoformat()
            for resident in history[event_index]["snapshot"]["settings"]["residents"]:
                resident_id = resident["id"]
                if day <= today:
                    entries.setdefault(resident_id, {"name": resident["name"], "count": 0})
                if day >= today or (week, resident_id) not in recorded:
                    days = availability.setdefault(week, {}).setdefault(resident_id, [])
                    if WEEKDAYS[day.weekday()] not in resident["off_days"]:
                        days.append(day_key)
        day += timedelta(days=1)
    for resident in history[-1]["snapshot"]["settings"]["residents"]:
        statistics[today.isoformat()[:7]][resident["id"]]["name"] = resident["name"]
    _validate(result)
    return result
