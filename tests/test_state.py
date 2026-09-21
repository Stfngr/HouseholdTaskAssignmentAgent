"""Persistence and calendar regressions, using only the standard library."""

from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from household_agent.state import StateError, StateStore, new_state, reconcile
from household_agent.telegram import make_message


WEEKDAYS = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
]


def resident(identifier="alice", off_days=(), name=None):
    return {
        "id": identifier, "name": name or identifier.title(), "role": "adult",
        "off_days": list(off_days),
    }


def snapshot(*residents):
    return {
        "settings": {
            "daily_execution_time": "06:00", "timezone": "Europe/Berlin",
            "tasks_per_active_day": 1, "residents": list(residents) or [resident()],
        },
        "tasks": [{
            "id": "dishes", "name": "Dishes", "frequency": "weekly",
            "allowed_roles": ["adult", "child"],
        }],
    }


def assigned_state():
    state = reconcile(new_state(date(2026, 9, 9)), snapshot(), date(2026, 9, 9))
    state["last_execution_date"] = "2026-09-09"
    state["assignment_history"] = [{
        "id": json.dumps(["dishes", "weekly", "2026-09-07"]),
        "task_id": "dishes", "task_name": "Dishes", "frequency": "weekly",
        "period": "2026-09-07", "date": "2026-09-09", "resident_id": "alice",
        "resident_name": "Alice",
    }]
    state["monthly_statistics"]["2026-09"]["alice"]["count"] = 1
    state["outbox"] = [make_message("daily:2026-09-09", "daily", "2026-09-09", "Tasks today")]
    state["planned_assignments"] = [{
        "task_id": "dishes", "frequency": "weekly", "period": "2026-09-14",
        "date": "2026-09-15", "resident_id": "alice",
    }]
    state["plan_context"] = {"date": "2026-09-09", "snapshot": snapshot()}
    return state


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = StateStore(self.root)

    def test_missing_initial_and_populated_roundtrip(self):
        self.assertEqual(self.store.path, self.root / "storage" / "state.json")
        self.assertIsNone(self.store.load())
        for state in (new_state(date(2026, 9, 9)), assigned_state()):
            self.store.save(state)
            self.assertEqual(self.store.load(), state)
        state = new_state(date(2026, 9, 9))
        for field in ("config_history", "plan_context", "unassignable"):
            del state[field]
        self.store.save(state)
        self.assertEqual(self.store.load(), state)

    def test_schema_one_migrates_atomically_to_schema_two(self):
        legacy = new_state(date(2026, 9, 9))
        legacy["schema_version"] = 1
        del legacy["dashboard_pending"]
        del legacy["dashboard_last_updated_at"]
        self.store.path.parent.mkdir()
        self.store.path.write_text(json.dumps(legacy), encoding="utf-8")
        migrated = self.store.load()
        self.assertEqual(migrated["schema_version"], 2)
        self.assertIsNone(migrated["dashboard_pending"])
        self.assertIsNone(migrated["dashboard_last_updated_at"])
        self.assertEqual(self.store.load(), migrated)

    def test_corrupt_json_duplicate_keys_and_unknown_schema(self):
        self.store.path.parent.mkdir()
        for text in (
            "", "{", "null", "[]", '{"schema_version": 1, "schema_version": 1}',
            json.dumps(dict(new_state(date(2026, 9, 9)), schema_version=3)),
            json.dumps(dict(new_state(date(2026, 9, 9)), plan_context={"x": float("nan")})),
            json.dumps(new_state(date(2026, 9, 9))).replace('"availability_by_week": {}', '"availability_by_week": {"x": {}, "x": {}}'),
        ):
            with self.subTest(text=text):
                self.store.path.write_text(text, encoding="utf-8")
                with self.assertRaises(StateError):
                    self.store.load()
        self.store.path.write_bytes(b"\xff")
        with self.assertRaises(StateError):
            self.store.load()

    def test_nested_corruption_rejected_on_load_and_before_save(self):
        original = assigned_state()
        self.store.save(original)
        mutations = [
            lambda s: s.update(schema_version=True),
            lambda s: s.update(started_on="20260909"),
            lambda s: s.pop("last_observed_date"),
            lambda s: s.update(last_observed_date=None),
            lambda s: s.update(last_observed_date="20260909"),
            lambda s: s.update(last_observed_date="2026-09-08"),
            lambda s: s.update(last_execution_date="2026-09-08"),
            lambda s: s.update(config_history={}),
            lambda s: s.update(plan_context=[]),
            lambda s: s.update(unassignable=[{}]),
            lambda s: s.update(unknown=True),
            lambda s: s["config_history"][0].update(effective_date="2026-09-08"),
            lambda s: s["config_history"][0]["snapshot"]["settings"].update(tasks_per_active_day=True),
            lambda s: s["config_history"][0]["snapshot"]["settings"]["residents"].append(resident()),
            lambda s: s["config_history"][0]["snapshot"]["settings"]["residents"][0].update(off_days=["Monday", "Monday"]),
            lambda s: s["config_history"][0]["snapshot"]["tasks"][0].update(allowed_roles=["child"]),
            lambda s: s["availability_by_week"].update({"2026-09-08": {}}),
            lambda s: s["availability_by_week"]["2026-09-07"].update(alice=["2026-09-08"]),
            lambda s: s["availability_by_week"]["2026-09-07"].update(alice=["2026-09-14"]),
            lambda s: s["availability_by_week"]["2026-09-07"].update(alice=["2026-09-09"] * 2),
            lambda s: s["assignment_history"][0].update(id=""),
            lambda s: s["assignment_history"][0].pop("task_name"),
            lambda s: s["assignment_history"].append(deepcopy(s["assignment_history"][0])),
            lambda s: s["assignment_history"][0].update(period="2026-09-14"),
            lambda s: s["assignment_history"][0].update(date="2026-09-10"),
            lambda s: s["planned_assignments"][0].update(frequency="monthly", period="2026-10"),
            lambda s: s["planned_assignments"].append(deepcopy(s["planned_assignments"][0])),
            lambda s: s["monthly_statistics"]["2026-09"]["alice"].update(count=0),
            lambda s: s["monthly_statistics"]["2026-09"]["alice"].update(count=True),
            lambda s: s["monthly_statistics"].clear(),
            lambda s: s["reported_months"].append("2026-09"),
            lambda s: s["outbox"][0].update(attempts=True),
            lambda s: s["outbox"][0].update(next_attempt_at="2026-09-10T06:00:00"),
            lambda s: s["outbox"][0].update(parts=[]),
            lambda s: s["outbox"][0]["parts"][0].update(sent="false"),
            lambda s: s["outbox"][0]["parts"][0].update(text="x" * 4097),
            lambda s: s["outbox"][0]["parts"].append({"text": "second", "sent": True}),
            lambda s: s["outbox"].append(deepcopy(s["outbox"][0])),
            lambda s: s["outbox"].clear(),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                corrupt = deepcopy(original)
                mutate(corrupt)
                with patch("household_agent.state.tempfile.NamedTemporaryFile") as temporary:
                    with self.assertRaises(StateError):
                        self.store.save(corrupt)
                    temporary.assert_not_called()
                self.assertEqual(self.store.load(), original)
                self.store.path.write_text(json.dumps(corrupt), encoding="utf-8")
                with self.assertRaises(StateError):
                    self.store.load()
                self.store.save(original)

    def test_unique_history_ids_across_distinct_occurrences(self):
        state = assigned_state()
        entry = deepcopy(state["assignment_history"][0])
        entry["task_id"] = "other"
        state["assignment_history"].append(entry)
        state["monthly_statistics"]["2026-09"]["alice"]["count"] = 2
        with self.assertRaisesRegex(StateError, "Duplicate assignment ID"):
            self.store.save(state)

    def test_report_requires_all_parts_confirmed(self):
        state = reconcile(assigned_state(), snapshot(), date(2026, 10, 1))
        state["outbox"][0]["parts"][0]["sent"] = True
        message = make_message("month:2026-09", "monthly", "2026-09", "Report")
        message["parts"].append({"text": "Second part", "sent": False})
        message["parts"][0]["sent"] = True
        message["attempts"] = 1
        message["next_attempt_at"] = "2026-10-01T06:00:30+00:00"
        state["outbox"].append(message)
        self.store.save(state)
        state["reported_months"].append("2026-09")
        with self.assertRaises(StateError):
            self.store.save(state)
        message["parts"][1]["sent"] = True
        message["next_attempt_at"] = None
        self.store.save(state)
        self.assertEqual(self.store.load(), state)

    def test_daily_coverage_includes_history_and_empty_last_execution(self):
        state = reconcile(assigned_state(), snapshot(), date(2026, 9, 13))
        state["last_execution_date"] = "2026-09-13"
        with self.assertRaisesRegex(StateError, "missing its daily"):
            self.store.save(state)
        state["outbox"].append(make_message("daily:2026-09-13", "daily", "2026-09-13", "No tasks"))
        self.store.save(state)
        self.assertEqual(self.store.load(), state)
        del state["outbox"][0]
        with self.assertRaisesRegex(StateError, "missing its daily"):
            self.store.save(state)
        state["assignment_history"].clear()
        state["monthly_statistics"]["2026-09"]["alice"]["count"] = 0
        self.store.save(state)
        state["outbox"].clear()
        with self.assertRaisesRegex(StateError, "missing its daily"):
            self.store.save(state)

    def test_daily_message_cannot_exceed_last_execution(self):
        state = reconcile(assigned_state(), snapshot(), date(2026, 9, 13))
        state["outbox"].append(make_message("daily:2026-09-13", "daily", "2026-09-13", "Tasks"))
        with self.assertRaisesRegex(StateError, "outside execution dates"):
            self.store.save(state)

    def test_later_message_cannot_be_confirmed_before_pending_message(self):
        state = reconcile(assigned_state(), snapshot(), date(2026, 9, 13))
        state["last_execution_date"] = "2026-09-13"
        later = make_message("daily:2026-09-13", "daily", "2026-09-13", "No tasks")
        later["parts"][0]["sent"] = True
        later["parts"].append({"text": "More text", "sent": False})
        state["outbox"].append(later)
        with self.assertRaisesRegex(StateError, "Outbox messages confirmed out of order"):
            self.store.save(state)
        state["outbox"][0]["parts"][0]["sent"] = True
        self.store.save(state)

    def test_observation_cannot_predate_execution_or_latest_config(self):
        state = reconcile(assigned_state(), snapshot(resident("bob")), date(2026, 9, 13))
        state["last_observed_date"] = "2026-09-12"
        with self.assertRaisesRegex(StateError, "Observation predates latest config"):
            self.store.save(state)
        state["last_execution_date"] = "2026-09-13"
        state["outbox"].append(make_message("daily:2026-09-13", "daily", "2026-09-13", "No tasks"))
        with self.assertRaisesRegex(StateError, "Observation predates last execution"):
            self.store.save(state)

    def test_reported_month_requires_observation_in_next_month(self):
        for first, end, next_month in (
            (date(2026, 9, 1), date(2026, 9, 30), date(2026, 10, 1)),
            (date(2026, 12, 1), date(2026, 12, 31), date(2027, 1, 1)),
            (date(2024, 2, 1), date(2024, 2, 29), date(2024, 3, 1)),
        ):
            with self.subTest(first=first):
                state = reconcile(new_state(first), snapshot(), end)
                month = first.isoformat()[:7]
                message = make_message("monthly:" + month, "monthly", month, "Report")
                message["parts"][0]["sent"] = True
                state["outbox"].append(message)
                state["reported_months"].append(month)
                with self.assertRaisesRegex(StateError, "Reported month must end"):
                    self.store.save(state)
                self.store.path.parent.mkdir(exist_ok=True)
                self.store.path.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaisesRegex(StateError, "Reported month must end"):
                    self.store.load()
                state["last_observed_date"] = next_month.isoformat()
                self.store.save(state)

    def test_daily_booking_cannot_follow_its_completed_monthly_report(self):
        state = reconcile(assigned_state(), snapshot(), date(2026, 10, 1))
        state["outbox"][0]["parts"][0]["sent"] = True
        message = make_message("monthly:2026-09", "monthly", "2026-09", "Report")
        message["parts"][0]["sent"] = True
        state["outbox"].append(message)
        state["reported_months"].append("2026-09")
        self.store.save(state)
        state["last_execution_date"] = "2026-09-20"
        state["outbox"].append(make_message("daily:2026-09-20", "daily", "2026-09-20", "No tasks"))
        with self.assertRaisesRegex(StateError, "Daily booking follows"):
            self.store.save(state)

    def test_reported_month_cannot_predate_startup(self):
        state = new_state(date(2026, 10, 1))
        state["monthly_statistics"]["2026-09"] = {}
        state["reported_months"].append("2026-09")
        message = make_message("monthly:2026-09", "monthly", "2026-09", "Report")
        message["parts"][0]["sent"] = True
        state["outbox"].append(message)
        with self.assertRaisesRegex(StateError, "Statistics predate startup"):
            self.store.save(state)

    def test_reporting_only_observation_survives_restart_and_clock_rollback(self):
        state = reconcile(assigned_state(), snapshot(), date(2026, 9, 13))
        state["last_execution_date"] = "2026-09-13"
        state["outbox"].append(make_message("daily:2026-09-13", "daily", "2026-09-13", "No tasks"))
        state = reconcile(state, snapshot(), date(2026, 10, 1))
        self.assertEqual(len(state["config_history"]), 1)
        self.assertEqual(state["last_observed_date"], "2026-10-01")
        message = make_message("monthly:2026-09", "monthly", "2026-09", "September report")
        state["outbox"].append(message)
        for item in state["outbox"]:
            item["parts"][0]["sent"] = True
        state["reported_months"].append("2026-09")
        self.store.save(state)
        restored = self.store.load()
        rolled = reconcile(restored, snapshot(resident(name="Changed", off_days=WEEKDAYS)), date(2026, 9, 20))
        self.assertEqual(rolled, state)
        self.assertEqual(restored, state)
        self.assertIsNot(rolled, restored)
        self.assertEqual(rolled["last_execution_date"], "2026-09-13")

    def test_atomic_replace_failure_keeps_previous_file_and_cleans_temp(self):
        old = new_state(date(2026, 9, 9))
        self.store.save(old)
        with patch("household_agent.state.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(StateError):
                self.store.save(assigned_state())
        self.assertEqual(self.store.load(), old)
        self.assertEqual(list(self.store.path.parent.iterdir()), [self.store.path])

    def test_file_fsync_failure_keeps_previous_file(self):
        old = new_state(date(2026, 9, 9))
        self.store.save(old)
        with patch("household_agent.state.os.fsync", side_effect=OSError("sync failed")):
            with self.assertRaises(StateError):
                self.store.save(assigned_state())
        self.assertEqual(self.store.load(), old)
        self.assertEqual(list(self.store.path.parent.iterdir()), [self.store.path])

    def test_atomic_order_and_directory_fsync_failure(self):
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(descriptor):
            events.append("fsync")
            real_fsync(descriptor)

        def replace(source, target):
            events.append("replace")
            self.assertEqual(Path(source).parent, self.store.path.parent)
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")), assigned_state())
            real_replace(source, target)

        with patch("household_agent.state.os.fsync", side_effect=fsync), patch("household_agent.state.os.replace", side_effect=replace):
            self.store.save(assigned_state())
        self.assertEqual(events, ["fsync", "replace", "fsync"])
        with patch("household_agent.state.os.fsync", side_effect=[None, OSError("directory sync failed")]):
            with self.assertRaises(StateError):
                self.store.save(new_state(date(2026, 9, 9)))
        # Replacement has happened: the new complete JSON survives, never a partial file.
        self.assertEqual(self.store.load(), new_state(date(2026, 9, 9)))

    def test_lock_contention_and_release_on_error(self):
        script = (
            "from pathlib import Path\n"
            "import sys\n"
            "from household_agent.state import StateStore, StateError\n"
            "try:\n"
            "    with StateStore(Path(sys.argv[1])).lock(): pass\n"
            "except StateError:\n"
            "    sys.exit(23)\n"
        )
        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with self.store.lock():
                with self.assertRaises(StateError):
                    with StateStore(self.root).lock():
                        self.fail("Conflicting lock acquired")
                result = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 23, result.stderr)
                raise RuntimeError("body failed")
        result = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.store.path.parent / "agent.lock").exists())


class ReconcileTests(unittest.TestCase):
    def test_leap_day_and_year_boundary_horizons(self):
        for today, final_day in (
            (date(2024, 2, 28), "2024-03-03"),
            (date(2026, 12, 31), "2027-01-03"),
        ):
            with self.subTest(today=today):
                state = reconcile(new_state(today), snapshot(), today)
                days = [day for week in state["availability_by_week"].values() for day in week["alice"]]
                self.assertEqual(min(days), today.isoformat())
                self.assertEqual(max(days), final_day)
                if today.year == 2024:
                    self.assertIn("2024-02-29", days)

    def test_start_midweek_horizon_and_deep_copy(self):
        original = new_state(date(2026, 9, 9))
        config = snapshot(resident(off_days=("Sunday",)), resident("zero", WEEKDAYS))
        state = reconcile(original, config, date(2026, 9, 9))
        self.assertEqual(original, new_state(date(2026, 9, 9)))
        self.assertEqual(state["availability_by_week"]["2026-09-07"]["alice"], [
            "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-12",
        ])
        self.assertEqual(max(state["availability_by_week"]), "2026-09-28")
        self.assertEqual(state["availability_by_week"]["2026-09-28"]["alice"][-1], "2026-10-03")
        self.assertEqual(state["monthly_statistics"], {"2026-09": {
            "alice": {"name": "Alice", "count": 0}, "zero": {"name": "Zero", "count": 0},
        }})
        self.assertTrue(all(week["zero"] == [] for week in state["availability_by_week"].values()))
        config["settings"]["residents"][0]["name"] = "Changed"
        self.assertEqual(state["config_history"][0]["snapshot"]["settings"]["residents"][0]["name"], "Alice")

    def test_semantic_reordering_does_not_append_history(self):
        config = snapshot(resident(off_days=("Saturday", "Sunday")), resident("bob"))
        state = reconcile(new_state(date(2026, 9, 9)), config, date(2026, 9, 9))
        config["settings"]["residents"][0]["off_days"].reverse()
        config["settings"]["residents"].reverse()
        config["tasks"][0]["allowed_roles"].reverse()
        same = reconcile(state, config, date(2026, 9, 9))
        self.assertEqual(same, state)
        config["tasks"][0]["name"] = "New task name"
        changed = reconcile(same, config, date(2026, 9, 9))
        self.assertEqual(len(changed["config_history"]), 2)

    def test_future_offdays_change_but_past_does_not(self):
        state = reconcile(new_state(date(2026, 9, 7)), snapshot(resident(off_days=("Tuesday",))), date(2026, 9, 7))
        state = reconcile(state, snapshot(resident(off_days=("Monday", "Thursday"))), date(2026, 9, 9))
        days = state["availability_by_week"]["2026-09-07"]["alice"]
        self.assertIn("2026-09-07", days)
        self.assertNotIn("2026-09-08", days)
        self.assertNotIn("2026-09-10", days)
        self.assertNotIn("2026-09-14", state["availability_by_week"]["2026-09-14"]["alice"])

    def test_offline_months_use_previous_config_and_preserve_names_counts(self):
        state = assigned_state()
        old = snapshot(resident(off_days=("Tuesday",)), resident("zero", WEEKDAYS))
        state = reconcile(state, old, date(2026, 9, 10))
        context = deepcopy(state["plan_context"])
        state = reconcile(state, snapshot(resident("bob")), date(2027, 1, 6))
        self.assertEqual(set(state["monthly_statistics"]), {"2026-09", "2026-10", "2026-11", "2026-12", "2027-01"})
        self.assertEqual(state["monthly_statistics"]["2026-09"]["alice"]["count"], 1)
        for month in ("2026-10", "2026-11", "2026-12"):
            self.assertEqual(state["monthly_statistics"][month], {
                "alice": {"name": "Alice", "count": 0}, "zero": {"name": "Zero", "count": 0},
            })
        self.assertEqual(set(state["monthly_statistics"]["2027-01"]), {"alice", "zero", "bob"})
        self.assertNotIn("2026-11-03", state["availability_by_week"]["2026-11-02"]["alice"])
        self.assertIn("2026-11-04", state["availability_by_week"]["2026-11-02"]["alice"])
        self.assertEqual(state["availability_by_week"]["2027-01-04"]["alice"], ["2027-01-04"])
        self.assertEqual(state["availability_by_week"]["2027-01-04"]["bob"][0], "2027-01-06")
        self.assertNotIn("alice", state["availability_by_week"]["2027-01-11"])
        self.assertEqual(state["plan_context"], context)

    def test_same_day_membership_events_survive_removal_and_downtime(self):
        state = new_state(date(2026, 9, 9))
        for config in (snapshot(resident()), snapshot(resident("bob")), snapshot(resident("charlie"))):
            state = reconcile(state, config, date(2026, 9, 9))
        self.assertEqual(len(state["config_history"]), 3)
        self.assertEqual(set(state["monthly_statistics"]["2026-09"]), {"alice", "bob", "charlie"})
        state = reconcile(state, snapshot(resident("charlie")), date(2026, 11, 2))
        self.assertEqual(set(state["monthly_statistics"]["2026-09"]), {"alice", "bob", "charlie"})
        self.assertEqual(set(state["monthly_statistics"]["2026-10"]), {"charlie"})
        self.assertNotIn("alice", state["availability_by_week"]["2026-10-05"])

    def test_removed_resident_past_preserved_and_reentry_not_backdated(self):
        state = reconcile(new_state(date(2026, 9, 7)), snapshot(resident(), resident("bob")), date(2026, 9, 7))
        state = reconcile(state, snapshot(resident("bob")), date(2026, 9, 9))
        self.assertEqual(state["availability_by_week"]["2026-09-07"]["alice"], ["2026-09-07", "2026-09-08"])
        state = reconcile(state, snapshot(resident(), resident("bob")), date(2026, 9, 11))
        self.assertEqual(state["availability_by_week"]["2026-09-07"]["alice"], [
            "2026-09-07", "2026-09-08", "2026-09-11", "2026-09-12", "2026-09-13",
        ])

    def test_historical_names_stable_current_name_updated(self):
        state = reconcile(new_state(date(2026, 8, 31)), snapshot(), date(2026, 8, 31))
        state = reconcile(state, snapshot(resident(name="New Name")), date(2026, 10, 7))
        for month in ("2026-08", "2026-09"):
            self.assertEqual(state["monthly_statistics"][month]["alice"]["name"], "Alice")
        self.assertEqual(state["monthly_statistics"]["2026-10"]["alice"]["name"], "New Name")

    def test_rollback_does_not_rewrite_history_or_calendar(self):
        state = reconcile(new_state(date(2026, 9, 9)), snapshot(), date(2026, 9, 9))
        state = reconcile(state, snapshot(resident("bob")), date(2026, 10, 9))
        rolled = reconcile(state, snapshot(resident("charlie")), date(2026, 9, 10))
        self.assertEqual(rolled, state)
        self.assertIsNot(rolled, state)
        state["last_execution_date"] = "2026-10-11"
        state["last_observed_date"] = "2026-10-11"
        state["outbox"].append(make_message("daily:2026-10-11", "daily", "2026-10-11", "No tasks"))
        self.assertEqual(reconcile(state, snapshot(), date(2026, 10, 10)), state)

    def test_observation_advances_without_config_change_or_execution(self):
        state = reconcile(new_state(date(2026, 9, 9)), snapshot(), date(2026, 9, 9))
        observed = reconcile(state, snapshot(), date(2026, 10, 1))
        self.assertEqual(observed["last_observed_date"], "2026-10-01")
        self.assertEqual(observed["config_history"], state["config_history"])
        self.assertIsNone(observed["last_execution_date"])
        self.assertEqual(reconcile(observed, snapshot(resident("bob")), date(2026, 9, 20)), observed)

    def test_rollback_does_not_add_optional_fields(self):
        state = new_state(date(2026, 9, 9))
        del state["config_history"]
        self.assertEqual(reconcile(state, snapshot(), date(2026, 9, 8)), state)

    def test_initial_history_is_effective_today_not_startup(self):
        state = reconcile(new_state(date(2026, 8, 1)), snapshot(), date(2026, 9, 9))
        self.assertEqual(state["config_history"][0]["effective_date"], "2026-09-09")
        self.assertEqual(state["monthly_statistics"]["2026-08"], {})
        self.assertEqual(min(state["availability_by_week"]), "2026-09-07")
        self.assertEqual(state["availability_by_week"]["2026-09-07"]["alice"][0], "2026-09-09")

    def test_missing_past_weeks_backfilled_without_changing_recorded_offdays(self):
        state = reconcile(new_state(date(2026, 2, 2)), snapshot(resident(off_days=("Monday",))), date(2026, 2, 2))
        # Missing data is reconstructed, while existing empty lists mean no active days.
        del state["availability_by_week"]["2026-02-09"]
        state["availability_by_week"]["2026-02-02"]["alice"] = []
        state = reconcile(state, snapshot(), date(2026, 3, 4))
        self.assertEqual(state["availability_by_week"]["2026-02-02"]["alice"], [])
        self.assertNotIn("2026-02-09", state["availability_by_week"]["2026-02-09"]["alice"])
        self.assertIn("2026-02-10", state["availability_by_week"]["2026-02-09"]["alice"])


if __name__ == "__main__":
    unittest.main()
