"""German plain-text messages and durable, ordered Telegram delivery."""

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import logging
import time
from typing import Awaitable, Callable


LOGGER = logging.getLogger(__name__)
TELEGRAM_LIMIT = 4096
SEND_TIMEOUT = 30
WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
)
MONTHS = (
    "Januar", "Februar", "M\u00e4rz", "April", "Mai", "Juni", "Juli", "August",
    "September", "Oktober", "November", "Dezember",
)


def daily_text(
    day: date,
    residents: list[dict],
    assignments: list[dict],
    unassignable: list[str],
) -> str:
    """Describe assignments, fixed days off, and idle active residents."""
    heading = (
        "Aufgaben f\u00fcr den {}:" if assignments
        else "F\u00fcr den {} sind heute keine Aufgaben eingeplant."
    ).format(day.strftime("%d.%m.%Y"))
    lines = ["Guten Morgen!", heading, ""]
    by_resident = {}
    for assignment in assignments:
        by_resident.setdefault(assignment["resident_id"], []).append(
            assignment["task_name"]
        )
    for resident in residents:
        name = resident["name"]
        if WEEKDAYS[day.weekday()] in resident["off_days"]:
            lines.append(f"{name} hat heute einen festen freien Tag.")
        elif by_resident.get(resident["id"]):
            lines.append(f"{name}: {'; '.join(by_resident[resident['id']])}")
        else:
            lines.append(f"{name} hat heute keine Aufgabe erhalten.")
    if unassignable:
        lines.append("")
        for task in unassignable:
            lines.append(
                f"Hinweis: {task} kann im verbleibenden Zeitraum nicht zugeteilt "
                "werden, da kein geeigneter Bewohner an einem verbleibenden "
                "Tag verf\u00fcgbar ist."
            )
    return "\n".join(lines)


def monthly_text(month: str, statistics: dict) -> str:
    """Render historical names and counts without relying on system locales."""
    first = date.fromisoformat(month + "-01")
    lines = [f"Monatsstatistik f\u00fcr {MONTHS[first.month - 1]} {first.year}:", ""]
    for entry in statistics.values():
        noun = "Aufgabe" if entry["count"] == 1 else "Aufgaben"
        lines.append(f"{entry['name']}: {entry['count']} {noun} zugewiesen")
    if not statistics:
        lines.append("Keine Aufgaben zugewiesen.")
    lines.extend(["", "F\u00fcr den neuen Monat beginnen neue Z\u00e4hler."])
    return "\n".join(lines)


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split at UTF-16 limits, preferring lines.

    Text is preserved except whitespace-only parts, which Telegram cannot accept.
    """
    if not text or not text.strip():
        raise ValueError("Telegram message text must not be empty")
    if limit < 2 or limit > TELEGRAM_LIMIT:
        raise ValueError("Telegram text limit must be between 2 and 4096")
    parts = []
    start = 0
    while start < len(text):
        end = start
        units = 0
        while end < len(text):
            value = ord(text[end])
            if 0xD800 <= value <= 0xDFFF:
                raise ValueError("Telegram text contains an unpaired surrogate")
            width = 2 if value > 0xFFFF else 1
            if units + width > limit:
                break
            units += width
            end += 1
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline >= start and text[start:newline + 1].strip():
                end = newline + 1
        part = text[start:end]
        if part.strip():
            parts.append(part)
        start = end
    return parts


def make_message(message_id: str, kind: str, reference: str, text: str) -> dict:
    """Build a JSON-serializable outbox entry without accessing other modules."""
    return {
        "id": message_id,
        "kind": kind,
        "reference": reference,
        "parts": [{"text": part, "sent": False} for part in split_text(text)],
        "attempts": 0,
        "next_attempt_at": None,
    }


async def deliver_one(
    state: dict,
    send: Callable[[str], Awaitable[None]],
    save: Callable[[dict], None],
    now: datetime,
    *,
    local_day: date = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Attempt only the first unfinished message; return whether any part ACKed.

    Each save receives a detached snapshot. Save errors and cancellation escape;
    only send errors schedule retries. A lost ACK can still cause duplicates.
    Retry deadlines include monotonic elapsed time since this call began.
    Delayed headers use local_day when supplied, otherwise the UTC calendar day.
    Text freezes before the first attempt: messages first attempted on time do
    not gain a delayed header if subsequent retries cross a calendar boundary.
    """
    started = monotonic()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be an aware UTC datetime")
    index = next(
        (i for i, item in enumerate(state.get("outbox", []))
         if any(not part["sent"] for part in item["parts"])),
        None,
    )
    if index is None:
        return False
    message = state["outbox"][index]
    if message["next_attempt_at"] is not None:
        due = datetime.fromisoformat(message["next_attempt_at"].replace("Z", "+00:00"))
        if now < due:
            return False

    candidate = deepcopy(state)
    message = candidate["outbox"][index]
    if (message["attempts"] == 0 and message["kind"] == "daily"
            and not any(part["sent"] for part in message["parts"])):
        original_day = date.fromisoformat(message["reference"])
        if original_day < (local_day if local_day is not None else now.date()):
            text = "Nachtr\u00e4gliche Mitteilung f\u00fcr den {}:\n\n{}".format(
                original_day.strftime("%d.%m.%Y"),
                "".join(part["text"] for part in message["parts"]),
            )
            message["parts"] = [
                {"text": part, "sent": False} for part in split_text(text)
            ]
    # Persist the attempt before sending, freezing text even if the ACK is lost.
    message["attempts"] += 1
    message["next_attempt_at"] = None
    save(candidate)
    state.clear()
    state.update(candidate)

    progress = False
    for part_index in range(len(state["outbox"][index]["parts"])):
        part = state["outbox"][index]["parts"][part_index]
        if part["sent"]:
            continue
        try:
            await asyncio.wait_for(send(part["text"]), timeout=SEND_TIMEOUT)
        except Exception as error:
            failed_at = now.astimezone(timezone.utc) + timedelta(
                seconds=max(0, monotonic() - started)
            )
            candidate = deepcopy(state)
            message = candidate["outbox"][index]
            delay = min(30 * 2 ** min(message["attempts"] - 1, 7), 3600)
            permanent = any(
                cls.__name__ in {"Forbidden", "InvalidToken", "BadRequest", "Unauthorized"}
                for cls in type(error).__mro__
            )
            if permanent:
                delay = max(delay, 3600)
            retry_after = getattr(error, "retry_after", None)
            if isinstance(retry_after, timedelta):
                retry_after = retry_after.total_seconds()
            if isinstance(retry_after, (int, float)) and retry_after > delay:
                delay = retry_after
            message["next_attempt_at"] = (
                failed_at + timedelta(seconds=delay)
            ).isoformat()
            # Never include exception text, URLs, message contents, or credentials.
            LOGGER.warning(
                "Telegram-Versand fehlgeschlagen (%s); neuer Versuch in %s Sekunden.",
                "dauerhafter Fehler" if permanent else "vor\u00fcbergehender Fehler",
                delay,
            )
            save(candidate)
            state.clear()
            state.update(candidate)
            return progress

        candidate = deepcopy(state)
        message = candidate["outbox"][index]
        message["parts"][part_index]["sent"] = True
        if all(part["sent"] for part in message["parts"]):
            message["next_attempt_at"] = None
            if message["kind"] == "monthly":
                reported = candidate.setdefault("reported_months", [])
                if message["reference"] not in reported:
                    reported.append(message["reference"])
        save(candidate)
        state.clear()
        state.update(candidate)
        progress = True
    return progress


class TelegramSender:
    """Initialize on first send so offline startup cannot block local booking."""

    def __init__(self, token: str, chat_id: str):
        self._token = token
        self._chat_id = chat_id
        self._bot = None
        self._initialized = False

    async def __aenter__(self):
        from telegram import Bot

        # HTTP request logs can expose the bot token embedded in Telegram URLs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._bot = Bot(token=self._token)
        self._initialized = False
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        try:
            if self._bot is not None:
                shutdown = asyncio.create_task(self._bot.shutdown())
                try:
                    await asyncio.shield(shutdown)
                except asyncio.CancelledError:
                    # Finish closing resources before propagating cancellation.
                    await asyncio.gather(shutdown, return_exceptions=True)
                    raise
                except Exception:
                    LOGGER.warning("Telegram-Verbindung konnte nicht sauber geschlossen werden.")
        finally:
            self._bot = None
            self._initialized = False

    async def send(self, text: str) -> None:
        if self._bot is None:
            raise RuntimeError("TelegramSender must be used as an async context manager")
        if not self._initialized:
            await self._bot.initialize()
            self._initialized = True
        await self._bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode=None,
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
            pool_timeout=10,
        )
