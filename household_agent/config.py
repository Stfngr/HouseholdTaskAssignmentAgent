"""Strict configuration loading without runtime dependencies at import time."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
)


class ConfigError(ValueError):
    """A configuration file or required credential is invalid."""


@dataclass(frozen=True)
class Resident:
    id: str
    name: str
    role: str
    off_days: tuple[str, ...]


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    allowed_roles: tuple[str, ...]
    frequency: str


@dataclass(frozen=True)
class Settings:
    daily_execution_time: str
    timezone: str
    tasks_per_active_day: int
    residents: tuple[Resident, ...]


@dataclass(frozen=True)
class Config:
    settings: Settings
    tasks: tuple[Task, ...]


@dataclass(frozen=True)
class DashboardConfig:
    origin: str
    token: str


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ConfigError(f"Invalid JSON constant: {value}")


def _read_json(path: Path):
    try:
        with path.open(encoding="utf-8") as source:
            return json.load(
                source, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
            )
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    except (json.JSONDecodeError, ConfigError) as exc:
        raise ConfigError(f"Invalid configuration in {path}: {exc}") from exc


def _object(value, required, optional, location):
    if not isinstance(value, dict):
        raise ConfigError(f"{location}: expected an object")
    unknown = value.keys() - required - optional
    if unknown:
        raise ConfigError(f"{location}: unknown fields: {', '.join(sorted(unknown))}")
    missing = required - value.keys()
    if missing:
        raise ConfigError(f"{location}: missing fields: {', '.join(sorted(missing))}")
    return value


def _string(value, location):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{location}: expected a nonempty string")
    return value


def _choices(value, allowed, location):
    if not isinstance(value, list):
        raise ConfigError(f"{location}: expected a list")
    for item in value:
        if not isinstance(item, str) or item not in allowed:
            raise ConfigError(f"{location}: allowed values: {', '.join(allowed)}")
    if len(value) != len(set(value)):
        raise ConfigError(f"{location}: duplicate values are not allowed")
    return tuple(value)


def load_config(root: Path) -> Config:
    """Load both files relative to root, returning only fully validated data."""
    settings_data = _object(
        _read_json(root / "config" / "Settings.json"),
        {"residents"},
        {"daily_execution_time", "timezone", "tasks_per_active_day"},
        "Settings.json",
    )
    execution_time = _string(
        settings_data.get("daily_execution_time", "06:00"), "daily_execution_time"
    )
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", execution_time):
        raise ConfigError("daily_execution_time: expected HH:MM from 00:00 to 23:59")
    timezone = _string(settings_data.get("timezone", "Europe/Berlin"), "timezone")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError("timezone: expected an available IANA timezone") from exc
    capacity = settings_data.get("tasks_per_active_day", 1)
    if type(capacity) is not int or capacity <= 0:
        raise ConfigError("tasks_per_active_day: expected a positive integer")

    resident_data = settings_data["residents"]
    if not isinstance(resident_data, list) or not resident_data:
        raise ConfigError("residents: expected a nonempty list")
    residents = []
    resident_ids = set()
    for index, item in enumerate(resident_data):
        location = f"residents[{index}]"
        item = _object(item, {"id", "name", "role", "off_days"}, set(), location)
        resident_id = _string(item["id"], f"{location}.id")
        if resident_id in resident_ids:
            raise ConfigError(f"{location}.id: duplicate resident ID")
        resident_ids.add(resident_id)
        name = _string(item["name"], f"{location}.name")
        role = _string(item["role"], f"{location}.role")
        if role not in ("adult", "child"):
            raise ConfigError(f"{location}.role: expected adult or child")
        off_days = _choices(item["off_days"], WEEKDAYS, f"{location}.off_days")
        residents.append(Resident(resident_id, name, role, off_days))

    task_data = _object(
        _read_json(root / "config" / "tasks.json"), {"tasks"}, set(), "tasks.json"
    )["tasks"]
    if not isinstance(task_data, list):
        raise ConfigError("tasks: expected a list")
    tasks = []
    task_ids = set()
    for index, item in enumerate(task_data):
        location = f"tasks[{index}]"
        item = _object(
            item, {"id", "name", "allowed_roles", "frequency"}, set(), location
        )
        task_id = _string(item["id"], f"{location}.id")
        if task_id in task_ids:
            raise ConfigError(f"{location}.id: duplicate task ID")
        task_ids.add(task_id)
        name = _string(item["name"], f"{location}.name")
        roles = _choices(item["allowed_roles"], ("adult", "child"), f"{location}.allowed_roles")
        if "adult" not in roles:
            raise ConfigError(f"{location}.allowed_roles: adult must be included")
        frequency = _string(item["frequency"], f"{location}.frequency")
        if frequency not in ("weekly", "monthly"):
            raise ConfigError(f"{location}.frequency: expected weekly or monthly")
        tasks.append(Task(task_id, name, roles, frequency))

    return Config(Settings(execution_time, timezone, capacity, tuple(residents)), tuple(tasks))


def config_snapshot(config: Config) -> dict:
    """Return a fresh JSON-compatible snapshot, ignoring semantic list ordering."""
    return {
        "settings": {
            "daily_execution_time": config.settings.daily_execution_time,
            "timezone": config.settings.timezone,
            "tasks_per_active_day": config.settings.tasks_per_active_day,
            "residents": [
                {
                    "id": resident.id,
                    "name": resident.name,
                    "role": resident.role,
                    "off_days": [day for day in WEEKDAYS if day in resident.off_days],
                }
                for resident in sorted(config.settings.residents, key=lambda item: item.id)
            ],
        },
        "tasks": [
            {
                "id": task.id,
                "name": task.name,
                "allowed_roles": sorted(task.allowed_roles),
                "frequency": task.frequency,
            }
            for task in sorted(config.tasks, key=lambda item: item.id)
        ],
    }


def _load_dotenv(root: Path) -> None:
    """Load dotenv without overriding the environment; never expose values."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        raise ConfigError("python-dotenv is required; install requirements.txt") from None
    try:
        load_dotenv(dotenv_path=root / ".env", override=False)
    except Exception:
        # Third-party exception messages can contain credential values.
        raise ConfigError("Cannot load credentials from .env") from None


def load_credentials(root: Path) -> tuple[str, str]:
    """Load Telegram credentials without exposing secret values.

    Chat IDs are nonzero numeric Telegram identifiers, returned as strings.
    Token validation checks syntax only; Telegram verifies authenticity on use.
    """
    _load_dotenv(root)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]+", token):
        raise ConfigError("TELEGRAM_BOT_TOKEN: required valid bot token (not a placeholder)")
    if not re.fullmatch(r"-?[1-9][0-9]*", chat_id):
        raise ConfigError("TELEGRAM_CHAT_ID: required nonzero integer chat ID")
    return token, chat_id


def load_dashboard_config(root: Path) -> DashboardConfig | None:
    """Load optional dashboard credentials and require an HTTP(S) origin only."""
    _load_dotenv(root)
    url = os.environ.get("DASHBOARD_URL", "")
    token = os.environ.get("DASHBOARD_TOKEN", "")
    if not url and not token:
        return None
    if not url or not token:
        raise ConfigError("DASHBOARD_URL and DASHBOARD_TOKEN must be set together")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ConfigError("DASHBOARD_URL: expected an HTTP(S) origin") from None
    if (parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or port is not None and not 1 <= port <= 65535):
        raise ConfigError("DASHBOARD_URL: expected an HTTP(S) origin")
    if (len(token) < 32 or not token.isascii() or any(character.isspace() for character in token)):
        raise ConfigError("DASHBOARD_TOKEN: expected token with at least 32 ASCII non-whitespace characters")
    return DashboardConfig(urlunsplit((parsed.scheme, parsed.netloc, "", "", "")), token)
