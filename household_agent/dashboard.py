"""Durable current-day task snapshots for an optional home dashboard."""

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta
import logging
import time
from typing import Awaitable, Callable


LOGGER = logging.getLogger(__name__)
SEND_TIMEOUT = 30
PATH = "/api/v1/state/household-agent/current-tasks"
WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
)


def current_tasks_payload(snapshot: dict, assignments: list[dict], today: date) -> dict:
    """Build dashboard payload from actual bookings, never future planning."""
    by_resident = {}
    for assignment in assignments:
        by_resident.setdefault(assignment["resident_id"], []).append(assignment["task_name"])
    weekday = WEEKDAYS[today.weekday()]
    residents = []
    for resident in snapshot["settings"]["residents"]:
        off_day = weekday in resident["off_days"]
        residents.append({
            "id": resident["id"],
            "name": resident["name"],
            "off_day": off_day,
            "tasks": [] if off_day else by_resident.get(resident["id"], []),
        })
    return {"date": today.isoformat(), "residents": residents}


def make_pending(snapshot: dict, assignments: list[dict], today: date, updated_at: datetime,
                 previous_updated_at: str | None = None) -> dict:
    """Create retry metadata and a document that can be atomically booked."""
    if updated_at.tzinfo is None or updated_at.utcoffset() != timedelta(0):
        raise ValueError("updated_at must be an aware UTC datetime")
    if previous_updated_at is not None:
        previous = datetime.fromisoformat(previous_updated_at.replace("Z", "+00:00"))
        updated_at = max(updated_at, previous + timedelta(microseconds=1))
    return {
        "updated_at": updated_at.isoformat(),
        "payload": current_tasks_payload(snapshot, assignments, today),
        "attempts": 0,
        "next_attempt_at": None,
    }


def _same_pending(left: dict | None, right: dict) -> bool:
    return (left is not None and left["updated_at"] == right["updated_at"]
            and left["payload"] == right["payload"])


async def deliver_pending(
    state: dict,
    send: Callable[[dict], Awaitable[None]],
    save: Callable[[dict], None],
    now: datetime,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Deliver current pending snapshot; never let old ACK erase newer snapshot."""
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be an aware UTC datetime")
    pending = state.get("dashboard_pending")
    if pending is None:
        return False
    if pending["next_attempt_at"] is not None:
        due = datetime.fromisoformat(pending["next_attempt_at"].replace("Z", "+00:00"))
        if now < due:
            return False

    started = monotonic()
    attempted = deepcopy(pending)
    candidate = deepcopy(state)
    candidate["dashboard_pending"]["attempts"] += 1
    candidate["dashboard_pending"]["next_attempt_at"] = None
    save(candidate)
    state.clear()
    state.update(candidate)
    try:
        await asyncio.wait_for(send({
            "updated_at": attempted["updated_at"], "payload": attempted["payload"],
        }), timeout=SEND_TIMEOUT)
    except Exception:
        if not _same_pending(state.get("dashboard_pending"), attempted):
            return False
        failed_at = now + timedelta(seconds=max(0, monotonic() - started))
        candidate = deepcopy(state)
        pending = candidate["dashboard_pending"]
        delay = min(30 * 2 ** min(pending["attempts"] - 1, 7), 3600)
        pending["next_attempt_at"] = (failed_at + timedelta(seconds=delay)).isoformat()
        # Exception text can contain authenticated URLs and response bodies.
        LOGGER.warning("Dashboard-Synchronisierung fehlgeschlagen; neuer Versuch in %s Sekunden.", delay)
        save(candidate)
        state.clear()
        state.update(candidate)
        return False

    if not _same_pending(state.get("dashboard_pending"), attempted):
        return False
    candidate = deepcopy(state)
    candidate["dashboard_pending"] = None
    save(candidate)
    state.clear()
    state.update(candidate)
    return True


class DashboardSender:
    """Minimal HTTP adapter, imported only when optional sync is configured."""

    def __init__(self, origin: str, token: str):
        self._url = origin + PATH
        self._token = token
        self._client = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, document: dict) -> None:
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False)
        response = await self._client.put(
            self._url,
            json=document,
            headers={"Authorization": "Bearer " + self._token},
        )
        response.raise_for_status()
