"""Dashboard payload and durable retry behavior without HTTP access."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock

import main
from household_agent.dashboard import current_tasks_payload, deliver_pending, make_pending
from household_agent.state import new_state, reconcile


def snapshot():
    return {"settings": {
        "daily_execution_time": "06:00", "timezone": "Europe/Berlin", "tasks_per_active_day": 1,
        "residents": [
        {"id": "alice", "name": "Alice", "role": "adult", "off_days": []},
        {"id": "bob", "name": "Bob", "role": "adult", "off_days": ["Sunday"]},
        {"id": "cara", "name": "Cara", "role": "child", "off_days": []},
    ]}, "tasks": []}


class DashboardPayloadTests(unittest.TestCase):
    def test_every_resident_has_current_booked_tasks_or_off_day(self):
        payload = current_tasks_payload(snapshot(), [
            {"resident_id": "alice", "task_name": "Dishes"},
            {"resident_id": "alice", "task_name": "Trash"},
            {"resident_id": "bob", "task_name": "Ignored off day task"},
        ], date(2026, 9, 13))
        self.assertEqual(payload, {"date": "2026-09-13", "residents": [
            {"id": "alice", "name": "Alice", "off_day": False, "tasks": ["Dishes", "Trash"]},
            {"id": "bob", "name": "Bob", "off_day": True, "tasks": []},
            {"id": "cara", "name": "Cara", "off_day": False, "tasks": []},
        ]})

    def test_booking_persists_only_todays_assignments_not_future_plan(self):
        day = date(2026, 9, 7)
        config = snapshot()
        config["tasks"] = [{
            "id": "task-" + str(index), "name": "Task " + str(index),
            "frequency": "weekly", "allowed_roles": ["adult", "child"],
        } for index in range(8)]
        state = reconcile(new_state(day), config, day)
        booked = main.book_day(
            state, config, day, __import__("random").Random(1),
            dashboard_updated_at=datetime(2026, 9, 7, 6, tzinfo=timezone.utc),
        )
        pending = booked["dashboard_pending"]
        actual = {}
        for entry in booked["assignment_history"]:
            if entry["date"] == day.isoformat():
                actual.setdefault(entry["resident_id"], []).append(entry["task_name"])
        self.assertEqual({item["id"]: item["tasks"] for item in pending["payload"]["residents"]}, {
            resident["id"]: actual.get(resident["id"], [])
            for resident in config["settings"]["residents"]
        })
        self.assertTrue(any(entry["date"] > day.isoformat() for entry in booked["planned_assignments"]))

    def test_clock_rollback_keeps_dashboard_updates_monotonic(self):
        first = make_pending(snapshot(), [], date(2026, 9, 13),
                             datetime(2026, 9, 13, 6, tzinfo=timezone.utc))
        second = make_pending(snapshot(), [], date(2026, 9, 14),
                              datetime(2026, 9, 12, 6, tzinfo=timezone.utc), first["updated_at"])
        self.assertGreater(second["updated_at"], first["updated_at"])


class DashboardDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 13, 6, tzinfo=timezone.utc)
        self.state = {"dashboard_pending": make_pending(snapshot(), [
            {"resident_id": "alice", "task_name": "Dishes"},
        ], date(2026, 9, 13), self.now)}
        self.saved = []
        self.send = AsyncMock()

    def save(self, candidate):
        self.assertIsNot(candidate, self.state)
        self.saved.append(deepcopy(candidate))

    async def test_failure_survives_restart_then_delivers_same_document(self):
        self.send.side_effect = [OSError("network"), None]
        with self.assertLogs("household_agent.dashboard", "WARNING"):
            self.assertFalse(await deliver_pending(self.state, self.send, self.save, self.now, monotonic=lambda: 0))
        pending = self.state["dashboard_pending"]
        self.assertEqual(pending["attempts"], 1)
        self.assertEqual(pending["next_attempt_at"], (self.now + timedelta(seconds=30)).isoformat())
        self.state = deepcopy(self.saved[-1])
        self.assertTrue(await deliver_pending(
            self.state, self.send, self.save, self.now + timedelta(seconds=30), monotonic=lambda: 0,
        ))
        self.assertIsNone(self.state["dashboard_pending"])
        document = self.send.await_args_list[0].args[0]
        self.assertEqual(self.send.await_args_list[1].args[0], document)
        self.assertEqual(document["updated_at"], self.now.isoformat())

    async def test_newer_pending_is_never_cleared_by_old_ack(self):
        old = deepcopy(self.state["dashboard_pending"])

        async def send(_document):
            self.state["dashboard_pending"] = make_pending(snapshot(), [], date(2026, 9, 14), self.now + timedelta(days=1))

        self.assertFalse(await deliver_pending(self.state, send, self.save, self.now, monotonic=lambda: 0))
        self.assertNotEqual(self.state["dashboard_pending"]["updated_at"], old["updated_at"])
