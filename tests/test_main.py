"""Orchestration regressions with real durable state and no network dependencies."""

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import io
import json
from pathlib import Path
import random
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch, sentinel
from zoneinfo import ZoneInfo

import main
from household_agent.config import ConfigError
from household_agent.state import StateError, StateStore, new_state, reconcile


WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)
BERLIN = ZoneInfo("Europe/Berlin")


def resident(identifier="alice", role="adult", off_days=()):
    return {
        "id": identifier, "name": identifier.title(), "role": role,
        "off_days": list(off_days),
    }


def task(identifier="dishes", frequency="weekly", adult_only=False):
    return {
        "id": identifier, "name": identifier.title(), "frequency": frequency,
        "allowed_roles": ["adult"] if adult_only else ["adult", "child"],
    }


def snapshot(residents=None, tasks=None):
    return {
        "settings": {
            "daily_execution_time": "06:00", "timezone": "Europe/Berlin",
            "tasks_per_active_day": 1,
            "residents": [resident()] if residents is None else residents,
        },
        "tasks": [task(), task("oven", adult_only=True)] if tasks is None else tasks,
    }


def utc_on(day, hour=6, minute=0, second=0):
    """Construct test instants independently of execution_instant."""
    return datetime.combine(day, time(hour, minute, second), BERLIN).astimezone(timezone.utc)


class MainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = StateStore(Path(directory.name))
        lock = self.store.lock()
        lock.__enter__()
        self.addCleanup(lock.__exit__, None, None, None)
        self.saved = []
        self.sent = []
        self.send_error = None
        self.on_send = None
        self.save = Mock(side_effect=self.persist)
        self.clock = Mock(side_effect=lambda: self.now)
        self.initialize(date(2026, 9, 13))  # Sunday forces weekly work due today.

    def initialize(self, day, config=None):
        self.snapshot = snapshot() if config is None else config
        self.state = reconcile(new_state(day), self.snapshot, day)
        self.store.save(self.state)
        self.assertEqual(self.store.load(), self.state)
        self.now = utc_on(day)
        self.rng = random.Random(17)
        self.saved.clear()
        self.sent.clear()
        self.save.reset_mock()

    def persist(self, candidate):
        self.store.save(candidate)
        self.assertEqual(self.store.load(), candidate)
        self.saved.append(deepcopy(candidate))

    async def send(self, text):
        durable = self.store.load()
        self.assertEqual(durable, self.state, "Sending uncommitted state")
        pending = next(message for message in durable["outbox"]
                       if any(not part["sent"] for part in message["parts"]))
        self.assertIn(text, [part["text"] for part in pending["parts"] if not part["sent"]])
        self.assertGreater(pending["attempts"], 0)
        self.sent.append((text, durable))
        if self.on_send is not None:
            self.on_send(text, durable)
        if self.send_error is not None:
            raise self.send_error

    async def cycle(self, instant=None):
        if instant is not None:
            self.now = instant
        self.assertIsNotNone(self.now.tzinfo)
        self.assertEqual(self.now.utcoffset(), timedelta(0))
        await main.run_cycle(
            self.state, self.snapshot, self.save, self.send, self.clock, self.rng,
        )
        self.assertEqual(self.store.load(), self.state)

    def assert_counts_and_locks(self):
        history = self.state["assignment_history"]
        ids = [entry["id"] for entry in history]
        self.assertEqual(len(ids), len(set(ids)))
        occurrences = [(entry["task_id"], entry["frequency"], entry["period"])
                       for entry in history]
        self.assertEqual(len(occurrences), len(set(occurrences)))
        counts = Counter((entry["date"][:7], entry["resident_id"]) for entry in history)
        for month, residents in self.state["monthly_statistics"].items():
            for identifier, statistics in residents.items():
                self.assertEqual(statistics["count"], counts.pop((month, identifier), 0))
        self.assertFalse(counts, "History missing from monthly statistics")

    async def test_before_six_waits_then_due_books_atomically_before_send(self):
        original = deepcopy(self.state)
        with patch("main.plan", wraps=main.plan) as planner:
            await self.cycle(utc_on(date(2026, 9, 13), 5, 59, 59))
            self.assertEqual(self.state, original)
            self.save.assert_not_called()
            planner.assert_not_called()
            self.assertEqual(self.sent, [])

            await self.cycle(utc_on(date(2026, 9, 13)))
            planner.assert_called_once()
        self.assertEqual(self.state["last_execution_date"], "2026-09-13")
        self.assertEqual({entry["task_id"] for entry in self.state["assignment_history"]},
                         {"dishes", "oven"})
        booked = self.saved[0]
        self.assertEqual(booked["assignment_history"], self.state["assignment_history"])
        self.assertEqual(booked["monthly_statistics"]["2026-09"]["alice"]["count"], 2)
        self.assertEqual(booked["last_execution_date"], "2026-09-13")
        self.assertEqual(booked["outbox"][0]["attempts"], 0)
        self.assertFalse(booked["outbox"][0]["parts"][0]["sent"])
        self.assertEqual(len(self.sent), 1)
        text, at_send = self.sent[0]
        self.assertIn("13.09.2026", text)
        self.assertIn("Alice:", text)
        self.assertEqual(at_send["assignment_history"], booked["assignment_history"])
        self.assertTrue(self.state["outbox"][0]["parts"][0]["sent"])
        self.assert_counts_and_locks()

    async def test_late_start_and_same_day_restart_do_not_duplicate(self):
        await self.cycle(utc_on(date(2026, 9, 13), 18))
        self.assertEqual(len(self.state["assignment_history"]), 2)
        committed = deepcopy(self.state)
        self.save.reset_mock()
        with patch("main.plan", wraps=main.plan) as planner:
            for _ in range(3):
                self.state = self.store.load()
                self.rng = random.Random(999)
                await self.cycle()
                self.assertEqual(self.state, committed)
            planner.assert_not_called()
        self.save.assert_not_called()
        self.assertEqual(len(self.sent), 1)

    async def test_empty_day_is_committed_sent_and_idempotent(self):
        self.initialize(date(2026, 9, 13), snapshot(tasks=[]))
        await self.cycle()
        self.assertEqual(self.state["last_execution_date"], "2026-09-13")
        self.assertEqual(self.state["assignment_history"], [])
        self.assertEqual(self.state["monthly_statistics"]["2026-09"]["alice"]["count"], 0)
        self.assertIn("keine Aufgaben eingeplant", self.sent[0][0])
        self.assertIn("Alice hat heute keine Aufgabe erhalten.", self.sent[0][0])
        original = deepcopy(self.state)
        self.state = self.store.load()
        self.save.reset_mock()
        await self.cycle()
        self.assertEqual(self.state, original)
        self.assertEqual(len(self.sent), 1)
        self.save.assert_not_called()

    async def test_all_residents_off_day_keeps_tasks_open_with_explanation(self):
        self.initialize(date(2026, 9, 13), snapshot([resident(off_days=WEEKDAYS)]))
        await self.cycle()
        self.assertEqual(self.state["assignment_history"], [])
        self.assertEqual(set(self.state["unassignable"]), {"Dishes", "Oven"})
        self.assertIn("festen freien Tag", self.sent[0][0])
        self.assertIn("Hinweis:", self.sent[0][0])
        self.assertNotIn("Alice hat heute keine Aufgabe erhalten", self.sent[0][0])
        self.assert_counts_and_locks()

    async def test_monthly_report_is_sent_and_acknowledged_before_new_day_books(self):
        self.initialize(date(2026, 8, 31), snapshot(tasks=[task("windows", "monthly")]))
        await self.cycle()
        august = deepcopy(self.state["monthly_statistics"]["2026-08"])
        self.assertEqual(august["alice"]["count"], 1)
        self.sent.clear()
        self.saved.clear()
        await self.cycle(utc_on(date(2026, 9, 1)))
        self.assertEqual(len(self.sent), 2)
        report_text, before_ack = self.sent[0]
        self.assertIn("Monatsstatistik", report_text)
        self.assertIn("August 2026", report_text)
        self.assertIn("Alice: 1 Aufgabe zugewiesen", report_text)
        self.assertEqual(before_ack["last_execution_date"], "2026-08-31")
        self.assertEqual(before_ack["reported_months"], [])
        self.assertFalse(any(message["reference"] == "2026-09-01"
                             for message in before_ack["outbox"]))
        daily_text, before_daily_ack = self.sent[1]
        self.assertIn("01.09.2026", daily_text)
        self.assertEqual(before_daily_ack["last_execution_date"], "2026-09-01")
        self.assertEqual(before_daily_ack["reported_months"], ["2026-08"])
        self.assertEqual(self.state["monthly_statistics"]["2026-08"], august)
        for transaction in self.saved:
            if transaction["last_execution_date"] == "2026-09-01":
                self.assertIn("2026-08", transaction["reported_months"])
        self.assert_counts_and_locks()

    async def test_monthly_failure_blocks_booking_and_retries_after_backoff(self):
        self.initialize(date(2026, 8, 31), snapshot(tasks=[task("windows", "monthly")]))
        await self.cycle()
        history = deepcopy(self.state["assignment_history"])
        self.sent.clear()
        self.send_error = OSError("network unavailable")
        due = utc_on(date(2026, 9, 1))
        with patch("main.book_day", wraps=main.book_day) as book:
            with self.assertLogs("household_agent.telegram", level="WARNING"):
                await self.cycle(due)
            book.assert_not_called()
            self.assertEqual(self.state["last_execution_date"], "2026-08-31")
            self.assertEqual(self.state["assignment_history"], history)
            self.assertEqual(self.state["reported_months"], [])
            self.assertEqual(self.state["monthly_statistics"]["2026-09"]["alice"]["count"], 0)
            pending = self.state["outbox"][-1]
            self.assertEqual(pending["kind"], "monthly")
            self.assertEqual(pending["attempts"], 1)
            deadline = datetime.fromisoformat(pending["next_attempt_at"])
            self.assertGreaterEqual(deadline, due + timedelta(seconds=30))
            self.assertLessEqual(deadline, due + timedelta(seconds=31))
            self.state = self.store.load()
            await self.cycle(due + timedelta(seconds=29))
            book.assert_not_called()
            self.assertEqual(len(self.sent), 1)
            self.send_error = None
            await self.cycle(due + timedelta(seconds=31))
            book.assert_called_once()
        self.assertEqual(self.sent[0][0], self.sent[1][0])
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.state["reported_months"], ["2026-08"])
        self.assertEqual(self.state["last_execution_date"], "2026-09-01")
        self.assert_counts_and_locks()

    async def test_daily_failure_keeps_local_commit_and_restart_retries_only_send(self):
        due = self.now
        self.send_error = OSError("network unavailable")
        with self.assertLogs("household_agent.telegram", level="WARNING"):
            await self.cycle()
        self.assertEqual(len(self.state["assignment_history"]), 2)
        history = deepcopy(self.state["assignment_history"])
        statistics = deepcopy(self.state["monthly_statistics"])
        self.assertEqual(self.state["last_execution_date"], "2026-09-13")
        self.assertEqual(self.state["outbox"][0]["attempts"], 1)
        self.assertFalse(self.state["outbox"][0]["parts"][0]["sent"])
        self.state = self.store.load()
        with patch("main.book_day", wraps=main.book_day) as book:
            await self.cycle(due + timedelta(seconds=29))
            self.assertEqual(len(self.sent), 1)
            self.send_error = None
            await self.cycle(due + timedelta(seconds=31))
            book.assert_not_called()
        self.assertEqual(self.state["assignment_history"], history)
        self.assertEqual(self.state["monthly_statistics"], statistics)
        self.assertEqual(self.sent[0][0], self.sent[1][0])
        self.assertEqual(self.state["outbox"][0]["attempts"], 2)
        self.assertIsNone(self.state["outbox"][0]["next_attempt_at"])
        self.assertTrue(self.state["outbox"][0]["parts"][0]["sent"])
        self.assert_counts_and_locks()

    async def test_save_failure_prevents_send_and_leaves_previous_transaction(self):
        original = deepcopy(self.state)
        with patch("household_agent.state.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(StateError):
                await self.cycle()
        self.assertEqual(self.state, original)
        self.assertEqual(self.store.load(), original)
        self.assertEqual(self.sent, [])
        await self.cycle()
        self.assertEqual(len(self.state["assignment_history"]), 2)
        self.assertEqual(len(self.sent), 1)

    async def test_restart_after_booking_before_send_delivers_before_due_without_rebooking(self):
        with patch("main.deliver_one", side_effect=[False, RuntimeError("process stopped")]):
            with self.assertRaisesRegex(RuntimeError, "process stopped"):
                await self.cycle()
        committed = self.store.load()
        self.assertEqual(len(committed["assignment_history"]), 2)
        self.assertEqual(committed["outbox"][0]["attempts"], 0)
        self.assertEqual(self.sent, [])
        self.state = deepcopy(committed)
        with patch("main.book_day", wraps=main.book_day) as book:
            await self.cycle(utc_on(date(2026, 9, 14), 5))
            book.assert_not_called()
        self.assertEqual(self.state["assignment_history"], committed["assignment_history"])
        self.assertEqual(self.state["last_execution_date"], "2026-09-13")
        self.assertIn("Nachtr\u00e4gliche Mitteilung", self.sent[0][0])
        self.assertIn("13.09.2026", self.sent[0][0])
        self.assertTrue(self.state["outbox"][0]["parts"][0]["sent"])

    async def test_multi_month_gap_reports_zeros_and_removed_residents_in_order(self):
        config = snapshot([resident(), resident("retired", off_days=WEEKDAYS)])
        self.initialize(date(2026, 9, 13), config)
        await self.cycle()
        history = deepcopy(self.state["assignment_history"])
        self.snapshot = snapshot()
        await self.cycle(utc_on(date(2026, 9, 14), 5))
        self.snapshot = snapshot([resident(), resident("newcomer", "child", WEEKDAYS)])
        self.snapshot["settings"]["residents"][0]["name"] = "Alice New"
        self.state = self.store.load()
        self.sent.clear()
        for month in ("2026-09", "2026-10", "2026-11", "2026-12"):
            await self.cycle(utc_on(date(2027, 1, 6)))
            self.assertIn(month, self.state["reported_months"])
            if month != "2026-12":
                self.assertEqual(self.state["last_execution_date"], "2026-09-13")
        self.assertEqual(self.state["reported_months"],
                         ["2026-09", "2026-10", "2026-11", "2026-12"])
        self.assertEqual(len(self.sent), 5)
        for (text, at_send), name in zip(self.sent, ("September", "Oktober", "November", "Dezember")):
            self.assertIn(name + " 2026", text)
            self.assertEqual(at_send["last_execution_date"], "2026-09-13")
            self.assertEqual(at_send["assignment_history"], history)
        self.assertIn("Alice: 2 Aufgaben zugewiesen", self.sent[0][0])
        self.assertIn("Retired: 0 Aufgaben zugewiesen", self.sent[0][0])
        for index, month in enumerate(("2026-10", "2026-11", "2026-12"), 1):
            self.assertEqual(self.state["monthly_statistics"][month],
                             {"alice": {"name": "Alice", "count": 0}})
            self.assertIn("Alice: 0 Aufgaben zugewiesen", self.sent[index][0])
            self.assertNotIn("Retired", self.sent[index][0])
            self.assertNotIn("Newcomer", self.sent[index][0])
        self.assertEqual(set(self.state["monthly_statistics"]),
                         {"2026-09", "2026-10", "2026-11", "2026-12", "2027-01"})
        self.assertEqual(self.state["monthly_statistics"]["2027-01"]["alice"]["name"], "Alice New")
        self.assertEqual(self.state["last_execution_date"], "2027-01-06")
        self.assertTrue(all(entry["date"] in {"2026-09-13", "2027-01-06"}
                            for entry in self.state["assignment_history"]))
        self.assert_counts_and_locks()

    async def test_persisted_plan_is_reused_on_next_day_without_reroll(self):
        config = snapshot(tasks=[task("weekly-" + str(i)) for i in range(8)]
                          + [task("windows", "monthly")])
        self.initialize(date(2026, 9, 7), config)
        with patch("main.plan", wraps=main.plan) as planner:
            await self.cycle()
            planner.assert_called_once()
            future = deepcopy(self.state["planned_assignments"])
            self.assertTrue(future)
            expected = [entry for entry in future if entry["date"] == "2026-09-08"]
            self.assertTrue(expected)
            self.state = self.store.load()
            self.rng = random.Random(999)
            await self.cycle(utc_on(date(2026, 9, 8)))
            planner.assert_called_once()
        actual = [{key: entry[key] for key in expected[0]}
                  for entry in self.state["assignment_history"] if entry["date"] == "2026-09-08"]
        self.assertEqual(actual, expected)
        self.assertEqual(self.state["planned_assignments"],
                         [entry for entry in future if entry["date"] > "2026-09-08"])

    async def test_missed_planned_dates_rebuild_without_retroactive_booking(self):
        config = snapshot(tasks=[task("weekly-" + str(i)) for i in range(14)])
        self.initialize(date(2026, 9, 7), config)
        with patch("main.plan", wraps=main.plan) as planner:
            await self.cycle()
            history = deepcopy(self.state["assignment_history"])
            self.assertTrue(any(entry["date"] in {"2026-09-08", "2026-09-09"}
                                for entry in self.state["planned_assignments"]))
            self.state = self.store.load()
            with self.assertLogs("household_agent", level="WARNING") as logs:
                await self.cycle(utc_on(date(2026, 9, 10)))
            self.assertTrue(any("keine Rueckdatierung" in line for line in logs.output))
            self.assertEqual(planner.call_count, 2)
        self.assertEqual(self.state["assignment_history"][:len(history)], history)
        self.assertTrue(all(entry["date"] in {"2026-09-07", "2026-09-10"}
                            for entry in self.state["assignment_history"]))
        self.assertTrue(all(entry["date"] > "2026-09-10"
                            for entry in self.state["planned_assignments"]))
        self.assertEqual([message["reference"] for message in self.state["outbox"]],
                         ["2026-09-07", "2026-09-10"])
        self.assert_counts_and_locks()

    async def test_configuration_changes_replan_only_future_work(self):
        config = snapshot([resident(off_days=("Saturday", "Sunday"))],
                          [task("weekly-" + str(i), adult_only=i % 2 == 0) for i in range(8)])
        self.initialize(date(2026, 9, 7), config)
        await self.cycle()
        history = deepcopy(self.state["assignment_history"])
        self.assertTrue(history)
        old_daily = deepcopy(self.state["outbox"][0])
        locked_id = history[0]["task_id"]
        removed_id = next(entry["task_id"] for entry in self.state["planned_assignments"]
                          if entry["period"] == "2026-09-07")
        self.snapshot = deepcopy(config)
        alice = self.snapshot["settings"]["residents"][0]
        alice.update(name="Alice New", role="child", off_days=["Tuesday", "Saturday", "Sunday"])
        self.snapshot["settings"]["residents"].append(resident("bob"))
        self.snapshot["tasks"] = [item for item in self.snapshot["tasks"] if item["id"] != removed_id]
        changed_task = next(item for item in self.snapshot["tasks"] if item["id"] == locked_id)
        changed_task.update(name="Renamed Task", frequency="monthly")
        with patch("main.plan", wraps=main.plan) as planner:
            await self.cycle(utc_on(date(2026, 9, 8)))
            planner.assert_called_once()
        self.assertEqual(self.state["assignment_history"][:len(history)], history)
        self.assertEqual(self.state["outbox"][0], old_daily)
        self.assertEqual(len(self.state["config_history"]), 2)
        future = self.state["assignment_history"][len(history):] + self.state["planned_assignments"]
        self.assertTrue(future)
        residents = {item["id"]: item for item in self.snapshot["settings"]["residents"]}
        tasks = {item["id"]: item for item in self.snapshot["tasks"]}
        for entry in future:
            self.assertNotIn(entry["task_id"], {removed_id, locked_id})
            self.assertGreaterEqual(entry["date"], "2026-09-08")
            person = residents[entry["resident_id"]]
            self.assertIn(person["role"], tasks[entry["task_id"]]["allowed_roles"])
            self.assertNotIn(WEEKDAYS[date.fromisoformat(entry["date"]).weekday()], person["off_days"])
        self.assertIn("2026-09-07", self.state["availability_by_week"]["2026-09-07"]["alice"])
        self.assertNotIn("2026-09-08", self.state["availability_by_week"]["2026-09-07"]["alice"])
        self.assert_counts_and_locks()

    async def test_clock_rollback_before_start_or_last_execution_is_noop(self):
        for booked in (False, True):
            with self.subTest(booked=booked):
                self.initialize(date(2026, 9, 7) if booked else date(2026, 9, 13))
                if booked:
                    await self.cycle(utc_on(date(2026, 9, 13)))
                original = deepcopy(self.state)
                sent_count = len(self.sent)
                self.save.reset_mock()
                self.snapshot["settings"]["residents"][0]["name"] = "Must Not Apply"
                with patch("main.book_day", wraps=main.book_day) as book:
                    await self.cycle(utc_on(date(2026, 9, 12), 18))
                    book.assert_not_called()
                self.assertEqual(self.state, original)
                self.assertEqual(len(self.sent), sent_count)
                self.save.assert_not_called()

    async def test_clock_rollback_respects_newer_config_even_without_booking(self):
        self.initialize(date(2026, 9, 7))
        self.snapshot["settings"]["residents"][0]["name"] = "Alice New"
        await self.cycle(utc_on(date(2026, 9, 9), 5))
        self.assertIsNone(self.state["last_execution_date"])
        original = deepcopy(self.state)
        self.save.reset_mock()
        self.snapshot["settings"]["residents"][0]["name"] = "Must Not Apply"
        await self.cycle(utc_on(date(2026, 9, 8), 18))
        self.assertEqual(self.state, original)
        self.assertEqual(self.sent, [])
        self.save.assert_not_called()

    async def test_rollback_during_monthly_send_cannot_reopen_reported_month(self):
        await self.cycle(utc_on(date(2026, 9, 13)))
        september = deepcopy(self.state["monthly_statistics"]["2026-09"])
        history = deepcopy(self.state["assignment_history"])
        config_history = deepcopy(self.state["config_history"])
        self.assertEqual(september["alice"]["count"], 2)
        self.assertEqual(self.state["last_observed_date"], "2026-09-13")
        self.sent.clear()

        def rollback_during_send(text, durable):
            self.assertIn("September 2026", text)
            self.assertIn("Alice: 2 Aufgaben zugewiesen", text)
            self.assertEqual(durable["last_observed_date"], "2026-10-01")
            self.assertEqual(durable["last_execution_date"], "2026-09-13")
            self.assertEqual(durable["reported_months"], [])
            self.now = utc_on(date(2026, 9, 20))

        self.on_send = rollback_during_send
        with patch("main.book_day", wraps=main.book_day) as book:
            await self.cycle(utc_on(date(2026, 10, 1)))
            book.assert_not_called()
        self.on_send = None
        committed = self.store.load()
        self.assertEqual(committed["reported_months"], ["2026-09"])
        self.assertEqual(committed["last_observed_date"], "2026-10-01")
        self.assertEqual(committed["last_execution_date"], "2026-09-13")
        self.assertEqual(committed["config_history"], config_history)
        self.assertEqual(committed["monthly_statistics"]["2026-09"], september)
        self.assertEqual(committed["assignment_history"], history)
        self.assertEqual(committed["outbox"][-1]["kind"], "monthly")
        self.assertTrue(all(part["sent"] for part in committed["outbox"][-1]["parts"]))
        self.assertEqual(len(self.sent), 1)

        # September 20 is after the last booking/config but before the durable
        # observation watermark. Neither unchanged nor changed config may run.
        for changed_config in (False, True):
            with self.subTest(changed_config=changed_config):
                self.state = self.store.load()
                self.save.reset_mock()
                if changed_config:
                    self.snapshot["settings"]["residents"][0].update(
                        name="Must Not Apply", off_days=["Monday"],
                    )
                    self.snapshot["tasks"].append(task("must-not-book"))
                with patch("main.book_day", wraps=main.book_day) as book:
                    await self.cycle()
                    book.assert_not_called()
                self.save.assert_not_called()
                self.assertEqual(self.state, committed)
                self.assertEqual(self.state["monthly_statistics"]["2026-09"], september)
                self.assertEqual(self.state["config_history"], config_history)
                self.assertEqual(len(self.sent), 1)
                self.assert_counts_and_locks()

    async def test_monthly_send_crossing_midnight_does_not_book_stale_day(self):
        self.initialize(date(2026, 8, 31), snapshot(tasks=[task("windows", "monthly")]))
        await self.cycle()
        history = deepcopy(self.state["assignment_history"])
        self.sent.clear()

        def cross_midnight(text, durable):
            self.assertIn("Monatsstatistik", text)
            self.now = utc_on(date(2026, 9, 2), 0, 0, 1)

        self.on_send = cross_midnight
        with patch("main.book_day", wraps=main.book_day) as book:
            await self.cycle(utc_on(date(2026, 9, 1), 23, 59, 59))
            book.assert_not_called()
        self.assertEqual(self.state["reported_months"], ["2026-08"])
        self.assertEqual(self.state["last_execution_date"], "2026-08-31")
        self.assertEqual(self.state["assignment_history"], history)
        self.assertEqual(len(self.sent), 1)
        self.on_send = None
        await self.cycle(utc_on(date(2026, 9, 2)))
        self.assertEqual(self.state["last_execution_date"], "2026-09-02")
        self.assertFalse(any(message["reference"] == "2026-09-01" for message in self.state["outbox"]))

    async def test_berlin_dst_gap_and_fold_execute_once_at_correct_instant(self):
        for day, due, second_fold in (
            (date(2026, 3, 29), datetime(2026, 3, 29, 1, tzinfo=timezone.utc), None),
            (date(2026, 10, 25), datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
             datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)),
        ):
            with self.subTest(day=day):
                config = snapshot()
                config["settings"]["daily_execution_time"] = "02:30"
                self.initialize(day, config)
                await self.cycle(due - timedelta(seconds=1))
                self.assertIsNone(self.state["last_execution_date"])
                self.assertEqual(self.sent, [])
                await self.cycle(due)
                self.assertEqual(self.state["last_execution_date"], day.isoformat())
                self.assertEqual(len(self.state["assignment_history"]), 2)
                original = deepcopy(self.state)
                self.state = self.store.load()
                await self.cycle(second_fold or due + timedelta(hours=1))
                self.assertEqual(self.state, original)
                self.assertEqual(len(self.sent), 1)

    def test_book_day_returns_detached_atomic_candidate_and_guards_repetition(self):
        original = deepcopy(self.state)
        candidate = main.book_day(self.state, self.snapshot, date(2026, 9, 13), self.rng)
        self.assertEqual(self.state, original)
        self.assertEqual(self.store.load(), original)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(candidate["assignment_history"]), 2)
        self.store.save(candidate)
        self.assertEqual(self.store.load(), candidate)
        for day in (date(2026, 9, 12), date(2026, 9, 13)):
            with self.subTest(day=day):
                repeated = main.book_day(candidate, self.snapshot, day, self.rng)
                self.assertEqual(repeated, candidate)
                self.assertIsNot(repeated, candidate)
                repeated["assignment_history"][0]["task_name"] = "Detached"
                self.assertNotEqual(repeated, candidate)

    def test_book_day_rejects_unreported_previous_month_without_mutation(self):
        tomorrow = date(2026, 10, 1)
        state = reconcile(self.state, self.snapshot, tomorrow)
        original = deepcopy(state)
        with self.assertRaisesRegex(StateError, "monthly reports"):
            main.book_day(state, self.snapshot, tomorrow, self.rng)
        self.assertEqual(state, original)
        self.assertEqual(self.sent, [])

    def test_book_day_revalidates_eligibility_period_and_duplicate_cooldowns(self):
        entry = {
            "task_id": "oven", "resident_id": "alice", "date": "2026-09-13",
            "frequency": "weekly", "period": "2026-09-07",
        }
        for violation in ("task", "resident", "role", "off_day", "frequency", "period", "duplicate"):
            with self.subTest(violation=violation):
                config = snapshot()
                planned = deepcopy(entry)
                if violation == "task":
                    planned["task_id"] = "missing"
                elif violation == "resident":
                    planned["resident_id"] = "missing"
                elif violation == "role":
                    config["settings"]["residents"][0]["role"] = "child"
                elif violation == "off_day":
                    config["settings"]["residents"][0]["off_days"] = ["Sunday"]
                elif violation == "frequency":
                    planned["frequency"] = "monthly"
                elif violation == "period":
                    planned["period"] = "2026-09-14"
                state = reconcile(new_state(date(2026, 9, 13)), config, date(2026, 9, 13))
                original = deepcopy(state)
                entries = [planned, deepcopy(planned)] if violation == "duplicate" else [planned]
                with patch("main.plan", return_value=(entries, [])):
                    with self.assertRaises(StateError):
                        main.book_day(state, config, date(2026, 9, 13), self.rng)
                self.assertEqual(state, original)

    def test_frequency_changes_keep_locks_from_historical_dates(self):
        for old, new in (("weekly", "monthly"), ("monthly", "weekly")):
            with self.subTest(old=old, new=new):
                day = date(2026, 9, 7)
                config = snapshot(tasks=[task("dishes", old)])
                state = reconcile(new_state(day), config, day)
                entry = {
                    "task_id": "dishes", "resident_id": "alice", "date": day.isoformat(),
                    "frequency": old, "period": "2026-09-07" if old == "weekly" else "2026-09",
                }
                with patch("main.plan", return_value=([entry], [])):
                    state = main.book_day(state, config, day, self.rng)
                self.store.save(state)
                config["tasks"][0]["frequency"] = new
                tomorrow = day + timedelta(days=1)
                state = reconcile(self.store.load(), config, tomorrow)
                candidate = main.book_day(state, config, tomorrow, self.rng)
                self.store.save(candidate)
                self.assertEqual(self.store.load(), candidate)
                self.assertEqual(candidate["assignment_history"], state["assignment_history"])
                forbidden_period = "2026-09" if new == "monthly" else "2026-09-07"
                self.assertFalse(any(item["period"] == forbidden_period
                                     for item in candidate["planned_assignments"]))

    async def test_56_day_simulation_preserves_locks_counters_and_completes_all_due_tasks(self):
        start = date(2026, 12, 7)
        days = [start + timedelta(days=offset) for offset in range(56)]
        config = snapshot(
            [resident(off_days=("Saturday", "Sunday")), resident("bob"),
             resident("charlie", "child", ("Wednesday",)), resident("inactive", off_days=WEEKDAYS)],
            [task("weekly-" + str(i), adult_only=i < 3) for i in range(10)]
            + [task("monthly-" + str(i), "monthly", adult_only=i < 2) for i in range(4)],
        )
        self.initialize(start, config)
        residents = {item["id"]: item for item in config["settings"]["residents"]}
        tasks = {item["id"]: item for item in config["tasks"]}
        with patch("main.plan", wraps=main.plan) as planner:
            for day in days:
                with self.subTest(day=day):
                    previous = deepcopy(self.state["assignment_history"])
                    self.state = self.store.load()
                    await self.cycle(utc_on(day))
                    self.assertEqual(self.state["last_execution_date"], day.isoformat())
                    self.assertEqual(self.state["assignment_history"][:len(previous)], previous)
                    self.assert_counts_and_locks()
                    self.assertEqual(self.state["unassignable"], [])
                    for entry in self.state["assignment_history"][len(previous):]:
                        self.assertEqual(entry["date"], day.isoformat())
                        person, work = residents[entry["resident_id"]], tasks[entry["task_id"]]
                        self.assertIn(person["role"], work["allowed_roles"])
                        self.assertNotIn(WEEKDAYS[day.weekday()], person["off_days"])
                        period = ((day - timedelta(days=day.weekday())).isoformat()
                                  if work["frequency"] == "weekly" else day.strftime("%Y-%m"))
                        self.assertEqual(entry["period"], period)
                    committed = deepcopy(self.state)
                    sent_count = len(self.sent)
                    await self.cycle(utc_on(day, 18))
                    self.assertEqual(self.state, committed)
                    self.assertEqual(len(self.sent), sent_count)
            # Eight Mondays plus January 1 (Friday), not one reroll per day.
            self.assertEqual(planner.call_count, 9)
        expected = Counter()
        for work in config["tasks"]:
            periods = ({(day - timedelta(days=day.weekday())).isoformat() for day in days}
                       if work["frequency"] == "weekly" else {"2026-12", "2027-01"})
            for period in periods:
                expected[work["id"], work["frequency"], period] = 1
        self.assertEqual(Counter((entry["task_id"], entry["frequency"], entry["period"])
                                 for entry in self.state["assignment_history"]), expected)
        self.assertEqual(len(self.state["assignment_history"]), 88)
        self.assertEqual(self.state["reported_months"], ["2026-12"])
        self.assertEqual([message["reference"] for message in self.state["outbox"]
                          if message["kind"] == "daily"], [day.isoformat() for day in days])
        self.assertEqual(len(self.sent), 57)
        self.assertTrue(all(part["sent"] for message in self.state["outbox"] for part in message["parts"]))
        self.assertTrue(all(statistics["inactive"]["count"] == 0
                            for statistics in self.state["monthly_statistics"].values()))
        daily_load = Counter((entry["resident_id"], entry["date"])
                             for entry in self.state["assignment_history"])
        self.assertLessEqual(max(daily_load.values()), 1)


class ExecutionInstantTests(unittest.TestCase):
    def test_berlin_normal_gap_and_first_fold_resolve_to_aware_utc(self):
        for day, wall_time, expected, local_time in (
            (date(2026, 1, 15), "06:00", datetime(2026, 1, 15, 5, tzinfo=timezone.utc), "06:00"),
            (date(2026, 7, 15), "06:00", datetime(2026, 7, 15, 4, tzinfo=timezone.utc), "06:00"),
            (date(2026, 3, 29), "02:30", datetime(2026, 3, 29, 1, tzinfo=timezone.utc), "03:00"),
            (date(2026, 10, 25), "02:30", datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc), "02:30"),
        ):
            with self.subTest(day=day):
                actual = main.execution_instant(day, wall_time, BERLIN)
                self.assertEqual(actual, expected)
                self.assertEqual(actual.utcoffset(), timedelta(0))
                self.assertEqual(actual.astimezone(BERLIN).strftime("%H:%M"), local_time)
                self.assertEqual(actual.astimezone(BERLIN).fold, 0)


class MainCliTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = snapshot()
        self.guards = {}
        logging_setup = patch("main.logging.basicConfig")
        logging_setup.start()
        self.addCleanup(logging_setup.stop)
        for name in ("StateStore", "TelegramSender", "load_credentials", "serve"):
            guard = patch("main." + name, side_effect=AssertionError("Unexpected runtime access: " + name))
            self.guards[name] = guard.start()
            self.addCleanup(guard.stop)
        root = patch("main.ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)
        loader = patch("main.load_config", return_value=sentinel.config)
        self.loader = loader.start()
        self.addCleanup(loader.stop)
        converter = patch("main.config_snapshot", return_value=self.config)
        self.converter = converter.start()
        self.addCleanup(converter.stop)

    def tearDown(self):
        for guard in self.guards.values():
            guard.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_check_config_validates_without_planning_storage_or_credentials(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output, patch("main.plan") as planner:
            self.assertEqual(main.main(["--check-config"]), 0)
            self.assertIn("Konfiguration gueltig", output.getvalue())
            planner.assert_not_called()
        self.loader.assert_called_once_with(self.root)
        self.converter.assert_called_once_with(sentinel.config)

    def test_preview_is_seeded_advisory_and_uses_no_runtime_services(self):
        day = date(2026, 9, 9)
        expected, unassignable = main.plan(
            self.config, reconcile(new_state(day), self.config, day), day, random.Random(73),
        )
        original = deepcopy(self.config)
        outputs = []
        with patch("main.plan", wraps=main.plan) as planner:
            for _ in range(2):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(main.main(["--preview", "2026-09-09", "--seed", "73"]), 0)
                    outputs.append(json.loads(output.getvalue()))
            self.assertEqual(planner.call_count, 2)
        self.assertEqual(outputs, [{"planned_assignments": expected, "unassignable": unassignable}] * 2)
        self.assertTrue(expected)
        self.assertEqual(self.config, original)

    def test_invalid_config_and_preview_date_return_error_without_writes(self):
        for argv in (["--check-config"], ["--preview", "2026-09-09"]):
            with self.subTest(argv=argv):
                self.loader.side_effect = ConfigError("invalid configuration")
                with self.assertLogs("household_agent", level="ERROR"), patch("main.plan") as planner:
                    self.assertEqual(main.main(argv), 1)
                    planner.assert_not_called()
        self.loader.side_effect = None
        with self.assertLogs("household_agent", level="ERROR"), patch("main.plan") as planner:
            self.assertEqual(main.main(["--preview", "2026-02-30"]), 1)
            planner.assert_not_called()

    def test_fresh_import_and_cli_modes_do_not_import_optional_dependencies(self):
        # -S removes site-packages; the import guard also catches eager imports
        # that application code might swallow. A subprocess avoids cached modules.
        script = """
import builtins
import io
import json
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

original_import = builtins.__import__
attempted = []
def guarded_import(name, *args, **kwargs):
    if name.split('.')[0] in {'telegram', 'dotenv'}:
        attempted.append(name)
        raise ImportError('Optional dependency deliberately unavailable')
    return original_import(name, *args, **kwargs)

with patch('builtins.__import__', side_effect=guarded_import):
    import main
    config = json.loads(sys.argv[1])
    with patch.object(main, 'load_config', return_value=None), \\
         patch.object(main, 'config_snapshot', return_value=config), \\
         patch.object(main, 'StateStore') as store, \\
         patch.object(main, 'TelegramSender') as sender, \\
         patch.object(main, 'load_credentials') as credentials, \\
         patch.object(main, 'serve') as serve:
        with redirect_stdout(io.StringIO()):
            assert main.main(['--check-config']) == 0
            assert main.main(['--preview', '2026-09-09']) == 0
        for guard in (store, sender, credentials, serve):
            guard.assert_not_called()
    assert not attempted, attempted
    assert 'telegram' not in sys.modules
    assert 'dotenv' not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-B", "-S", "-c", script, json.dumps(self.config)],
            cwd=Path(main.__file__).resolve().parent, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
