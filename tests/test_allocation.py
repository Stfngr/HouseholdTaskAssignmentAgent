import calendar
import copy
import itertools
import random
import time
import unittest
from collections import Counter
from datetime import date, timedelta
from fractions import Fraction

from household_agent.allocation import monday, month_key, plan


WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
)


def resident(identifier, role="adult", off_days=()):
    return {"id": identifier, "name": identifier.title(), "role": role, "off_days": list(off_days)}


def task(identifier, frequency="weekly", adult=False):
    return {
        "id": identifier, "name": identifier.title(), "frequency": frequency,
        "allowed_roles": ["adult"] if adult else ["adult", "child"],
    }


def config(residents, tasks, capacity=1):
    return {
        "settings": {
            "daily_execution_time": "06:00", "timezone": "Europe/Berlin",
            "tasks_per_active_day": capacity, "residents": residents,
        },
        "tasks": tasks,
    }


def history(identifier, resident_id, day, frequency="weekly"):
    return {
        "id": identifier + ":" + day, "task_id": identifier, "task_name": identifier,
        "date": day, "frequency": frequency,
        "period": monday(date.fromisoformat(day)).isoformat() if frequency == "weekly" else day[:7],
        "resident_id": resident_id, "resident_name": resident_id,
    }


def objective(assignments, state, capacity):
    """Independent exact objective, including constant committed baseline costs."""
    daily = Counter()
    weekly = Counter()
    for entry in state.get("assignment_history", []) + assignments:
        key = (entry["resident_id"], entry["date"])
        daily[key] += 1
        weekly[entry["resident_id"], monday(date.fromisoformat(entry["date"])).isoformat()] += 1
    fairness = Fraction(0)
    for (resident_id, week), count in weekly.items():
        active = len(state["availability_by_week"][week][resident_id])
        if active:
            fairness += Fraction(count * count, active)
    return (
        sum(max(0, count - capacity) for count in daily.values()),
        fairness,
        sum(count * count for count in daily.values()),
    )


class AllocationTests(unittest.TestCase):
    def assert_valid(self, snapshot, assignments, today):
        tasks = {item["id"]: item for item in snapshot["tasks"]}
        residents = {item["id"]: item for item in snapshot["settings"]["residents"]}
        seen = set()
        month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
        horizon_end = monday(month_end) + timedelta(days=6)
        for entry in assignments:
            self.assertEqual(set(entry), {"task_id", "frequency", "period", "date", "resident_id"})
            day = date.fromisoformat(entry["date"])
            self.assertLessEqual(today, day)
            self.assertLessEqual(day, horizon_end)
            item = tasks[entry["task_id"]]
            person = residents[entry["resident_id"]]
            self.assertIn(person["role"], item["allowed_roles"])
            self.assertNotIn(WEEKDAYS[day.weekday()], person["off_days"])
            self.assertEqual(entry["frequency"], item["frequency"])
            period = monday(day).isoformat() if item["frequency"] == "weekly" else month_key(day)
            self.assertEqual(entry["period"], period)
            if item["frequency"] == "monthly":
                self.assertEqual(period, month_key(today))
            key = (item["id"], period)
            self.assertNotIn(key, seen)
            seen.add(key)

    def test_twenty_one_tasks_split_ten_eleven_with_seven_overflow(self):
        today = date(2026, 5, 25)
        snapshot = config([resident("a"), resident("b")], [task(str(n)) for n in range(21)])
        assignments, blocked = plan(snapshot, {}, today, random.Random(1))
        self.assertEqual(blocked, [])
        self.assertEqual(len(assignments), 21)
        self.assertEqual(sorted(Counter(item["resident_id"] for item in assignments).values()), [10, 11])
        daily = Counter((item["resident_id"], item["date"]) for item in assignments)
        self.assertEqual(sum(max(0, count - 1) for count in daily.values()), 7)
        for person in ("a", "b"):
            counts = [daily[person, (today + timedelta(days=n)).isoformat()] for n in range(7)]
            self.assertLessEqual(max(counts) - min(counts), 1)
        self.assert_valid(snapshot, assignments, today)

    def test_five_and_seven_active_days_receive_five_and_seven(self):
        snapshot = config(
            [resident("a", off_days=("Saturday", "Sunday")), resident("b")],
            [task(str(n)) for n in range(12)],
        )
        assignments, blocked = plan(snapshot, {}, date(2026, 5, 25), random.Random(2))
        self.assertEqual(blocked, [])
        self.assertEqual(Counter(item["resident_id"] for item in assignments), {"a": 5, "b": 7})
        self.assertEqual(len({(item["resident_id"], item["date"]) for item in assignments}), 12)

    def test_roles_off_days_and_unassignable_current_period_only(self):
        today = date(2026, 8, 30)
        snapshot = config(
            [resident("adult", off_days=WEEKDAYS[1:]), resident("child", "child"),
             resident("inactive", off_days=WEEKDAYS)],
            [task("restricted", adult=True), task("general"), task("monthly", "monthly", True)],
        )
        assignments, blocked = plan(snapshot, {}, today, random.Random(3))
        self.assertEqual(blocked, ["Restricted"])
        self.assert_valid(snapshot, assignments, today)
        self.assertEqual(len(assignments), 4)
        self.assertNotIn("inactive", {entry["resident_id"] for entry in assignments})
        restricted = [entry for entry in assignments if entry["task_id"] != "general"]
        self.assertTrue(all(entry["date"] == "2026-08-31" for entry in restricted))

    def test_future_unassignable_occurrences_do_not_repeat_warning(self):
        snapshot = config([resident("child", "child")], [task("oven", adult=True)])
        self.assertEqual(plan(snapshot, {}, date(2026, 9, 1), random.Random(0)), ([], ["Oven"]))
        state = {"assignment_history": [history("oven", "removed", "2026-08-31")]}
        self.assertEqual(plan(snapshot, state, date(2026, 9, 1), random.Random(0)), ([], []))

    def test_empty_pool_and_no_active_residents(self):
        self.assertEqual(plan(config([resident("a")], []), {}, date(2026, 9, 1), random.Random(0)), ([], []))
        snapshot = config([resident("a", off_days=WEEKDAYS)], [task("weekly"), task("monthly", "monthly")])
        self.assertEqual(plan(snapshot, {}, date(2026, 9, 1), random.Random(0)), ([], ["Monthly", "Weekly"]))

    def test_calendar_horizon_and_monthly_deadlines(self):
        for today in (date(2024, 2, 27), date(2025, 2, 27), date(2025, 12, 30), date(2026, 1, 1)):
            with self.subTest(today=today):
                snapshot = config([resident("a")], [task("weekly"), task("monthly", "monthly")])
                assignments, blocked = plan(snapshot, {}, today, random.Random(4))
                self.assertEqual(blocked, [])
                self.assert_valid(snapshot, assignments, today)
                month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
                weeks = (monday(month_end) - monday(today)).days // 7 + 1
                self.assertEqual(len(assignments), weeks + 1)
        self.assertEqual(monday(date(2026, 1, 1)), date(2025, 12, 29))
        self.assertEqual(month_key(date(2024, 2, 29)), "2024-02")

    def test_frequency_changes_lock_by_date_not_stored_period(self):
        today = date(2026, 9, 1)
        snapshot = config([resident("a")], [task("now_weekly"), task("now_monthly", "monthly")])
        state = {"assignment_history": [
            history("now_weekly", "a", "2026-08-31", "monthly"),
            history("now_monthly", "a", "2026-09-01", "weekly"),
        ]}
        assignments, blocked = plan(snapshot, state, today, random.Random(5))
        self.assertEqual(blocked, [])
        self.assertEqual(len(assignments), 4)
        self.assertTrue(all(entry["task_id"] == "now_weekly" for entry in assignments))
        self.assertTrue(all(entry["period"] != "2026-08-31" for entry in assignments))
        self.assert_valid(snapshot, assignments, today)

    def test_expired_month_lock_does_not_lock_new_month(self):
        snapshot = config([resident("a")], [task("monthly", "monthly")])
        state = {"assignment_history": [history("monthly", "a", "2025-12-31")]}
        assignments, blocked = plan(snapshot, state, date(2026, 1, 1), random.Random(0))
        self.assertEqual(blocked, [])
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0]["period"], "2026-01")

    def test_baseline_consumes_daily_capacity_shared_by_frequencies(self):
        today = date(2026, 5, 30)
        snapshot = config([resident("a")], [task("weekly"), task("monthly", "monthly")], capacity=2)
        state = {"assignment_history": [history("old1", "a", "2026-05-30"), history("old2", "a", "2026-05-30")]}
        assignments, blocked = plan(snapshot, state, today, random.Random(6))
        self.assertEqual(blocked, [])
        self.assertEqual([entry["date"] for entry in assignments], ["2026-05-31", "2026-05-31"])

    def test_past_week_load_and_full_active_days_survive_month_boundary(self):
        today = date(2026, 9, 1)
        snapshot = config([resident("a"), resident("b")], [task("monthly", "monthly")])
        state = {
            "assignment_history": [history("old", "a", "2026-08-31")],
            "availability_by_week": {
                "2026-08-31": {"a": ["2026-08-31", "2026-09-01"], "b": ["2026-08-31", "2026-09-01"]},
                "2026-09-07": {"a": [], "b": []},
                "2026-09-14": {"a": [], "b": []},
                "2026-09-21": {"a": [], "b": []},
                "2026-09-28": {"a": [], "b": []},
            },
        }
        assignments, _ = plan(snapshot, state, today, random.Random(7))
        self.assertEqual(assignments[0]["resident_id"], "b")

    def test_full_week_denominator_includes_past_days(self):
        today = date(2026, 5, 31)
        snapshot = config([resident("a"), resident("b")], [task("new")], capacity=10)
        state = {
            "assignment_history": [history("old", "a", "2026-05-25")],
            "availability_by_week": {"2026-05-25": {
                "a": [(date(2026, 5, 25) + timedelta(days=n)).isoformat() for n in range(7)],
                "b": ["2026-05-31"],
            }},
        }
        assignments, _ = plan(snapshot, state, today, random.Random(8))
        # A's marginal fairness cost is 3/7, B's is 1/1.
        self.assertEqual(assignments[0]["resident_id"], "a")

    def test_fallback_starts_today_and_future_weeks_start_monday(self):
        snapshot = config([resident("a")], [task("weekly")])
        today = date(2026, 8, 30)
        assignments, _ = plan(snapshot, {}, today, random.Random(9))
        self.assertEqual(len(assignments), 2)
        self.assertEqual(assignments[0]["date"], "2026-08-30")
        self.assertGreaterEqual(assignments[1]["date"], "2026-08-31")
        self.assertLessEqual(assignments[1]["date"], "2026-09-06")

    def test_recorded_empty_availability_is_not_filled_by_fallback(self):
        snapshot = config([resident("a")], [task("weekly")])
        state = {"availability_by_week": {"2026-05-25": {"a": []}}}
        self.assertEqual(plan(snapshot, state, date(2026, 5, 25), random.Random(0)), ([], ["Weekly"]))

    def test_past_availability_does_not_override_current_off_days(self):
        snapshot = config([resident("a", off_days=("Sunday",))], [task("weekly")])
        state = {"availability_by_week": {"2026-05-25": {"a": ["2026-05-25", "2026-05-31"]}}}
        self.assertEqual(plan(snapshot, state, date(2026, 5, 31), random.Random(0)), ([], ["Weekly"]))

    def test_mixed_period_repair_trap(self):
        # Monthly deadline is Monday. Next week's adult weekly occurrence can
        # move to Tuesday, freeing Monday for the month without any overflow.
        today = date(2026, 8, 30)
        snapshot = config(
            [resident("a", off_days=("Wednesday", "Thursday", "Friday", "Saturday"))],
            [task("a_month", "monthly", True), task("z_week", adult=True)],
        )
        for seed in range(20):
            with self.subTest(seed=seed):
                assignments, blocked = plan(snapshot, {}, today, random.Random(seed))
                self.assertEqual(blocked, [])
                self.assertEqual(len(assignments), 3)
                self.assertEqual(len({entry["date"] for entry in assignments}), 3)
                self.assert_valid(snapshot, assignments, today)

    def test_role_and_deadline_repair_trap(self):
        # Adult monthly work can use Sunday or Monday; general current-week
        # work can only use adult Sunday. Child is first available Monday.
        today = date(2026, 8, 30)
        snapshot = config(
            [resident("a"), resident("b", "child", off_days=("Sunday",))],
            [task("restricted", "monthly", True), task("general")],
        )
        for seed in range(20):
            assignments, blocked = plan(snapshot, {}, today, random.Random(seed))
            self.assertEqual(blocked, [])
            self.assertEqual(len(assignments), 3)
            self.assertEqual(len({(entry["resident_id"], entry["date"]) for entry in assignments}), 3)
            monthly = next(entry for entry in assignments if entry["frequency"] == "monthly")
            self.assertEqual(monthly["date"], "2026-08-31")

    def test_capacity_is_not_inflated_by_roles_or_periods(self):
        snapshot = config(
            [resident("a"), resident("b", "child")],
            [task("adult_week", adult=True), task("adult_month", "monthly", True),
             task("general_week"), task("general_month", "monthly")],
        )
        assignments, _ = plan(snapshot, {}, date(2026, 5, 31), random.Random(0))
        counts = Counter(entry["resident_id"] for entry in assignments)
        self.assertEqual(counts, {"a": 2, "b": 2})
        self.assertEqual(sum(max(0, count - 1) for count in counts.values()), 2)

    def test_overflow_beats_weekly_fairness(self):
        snapshot = config([resident("a"), resident("b")], [task("new")])
        state = {
            "assignment_history": [history("old", "a", "2026-05-31")],
            "availability_by_week": {"2026-05-25": {
                "a": [(date(2026, 5, 25) + timedelta(days=n)).isoformat() for n in range(7)],
                "b": ["2026-05-31"],
            }},
        }
        assignments, _ = plan(snapshot, state, date(2026, 5, 31), random.Random(0))
        self.assertEqual(assignments[0]["resident_id"], "b")

    def test_seed_variation_stable_order_and_input_purity(self):
        today = date(2026, 5, 25)
        snapshot = config([resident("a"), resident("b")], [task("one"), task("two")])
        state = {"assignment_history": [], "availability_by_week": {}, "planned_assignments": [{"unused": True}]}
        original = copy.deepcopy((snapshot, state))
        reversed_snapshot = copy.deepcopy(snapshot)
        reversed_snapshot["tasks"].reverse()
        reversed_snapshot["settings"]["residents"].reverse()
        for item in reversed_snapshot["tasks"]:
            item["allowed_roles"].reverse()
        variants = set()
        for seed in range(20):
            result = plan(snapshot, state, today, random.Random(seed))
            self.assertEqual(result, plan(reversed_snapshot, state, today, random.Random(seed)))
            variants.add(tuple((entry["task_id"], entry["date"], entry["resident_id"]) for entry in result[0]))
        self.assertGreater(len(variants), 5)
        self.assertEqual((snapshot, state), original)
        single = config([resident("a"), resident("b")], [task("one")])
        winners = {plan(single, {}, today, random.Random(seed))[0][0]["resident_id"] for seed in range(20)}
        self.assertEqual(winners, {"a", "b"})

    def test_randomized_tiny_instances_match_exhaustive_objective(self):
        generator = random.Random(1789)
        for case in range(60):
            with self.subTest(case=case):
                today = date(2026, 8, 30) if case % 2 else date(2026, 5, 29)
                month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
                weeks = [monday(today)]
                if monday(month_end) != weeks[0]:
                    weeks.append(monday(month_end))
                people = [resident("a"), resident("b", generator.choice(("adult", "child")))]
                tasks = [task("weekly", adult=generator.choice((True, False))),
                         task("monthly", "monthly", generator.choice((True, False)))]
                if case % 3 == 0:
                    tasks.append(task("extra", "monthly", generator.choice((True, False))))
                snapshot = config(people, tasks, capacity=generator.choice((1, 2)))
                state = {"assignment_history": [], "availability_by_week": {}}
                for week in weeks:
                    recorded = state["availability_by_week"][week.isoformat()] = {}
                    for person in people:
                        candidates = [week, max(today, week), max(today, week) + timedelta(days=1)]
                        recorded[person["id"]] = sorted({
                            day.isoformat() for day in candidates
                            if monday(day) == week and generator.randrange(4) != 0
                        })
                        if recorded[person["id"]] and generator.randrange(2):
                            day = generator.choice(recorded[person["id"]])
                            state["assignment_history"].append(history("old" + person["id"], person["id"], day))
                choices = []
                expected_blocked = set()
                for item in tasks:
                    periods = weeks if item["frequency"] == "weekly" else [today.replace(day=1)]
                    for start in periods:
                        end = start + timedelta(days=6) if item["frequency"] == "weekly" else month_end
                        candidates = []
                        for person in people:
                            if person["role"] not in item["allowed_roles"]:
                                continue
                            for recorded in state["availability_by_week"].values():
                                for value in recorded[person["id"]]:
                                    day = date.fromisoformat(value)
                                    if max(today, start) <= day <= end:
                                        candidates.append({"resident_id": person["id"], "date": value})
                        if candidates:
                            choices.append(candidates)
                        elif item["frequency"] == "monthly" or start == monday(today):
                            expected_blocked.add(item["name"])
                best = min(
                    objective(list(combination), state, snapshot["settings"]["tasks_per_active_day"])
                    for combination in itertools.product(*choices)
                )
                assignments, blocked = plan(snapshot, state, today, random.Random(case))
                self.assertEqual(len(assignments), len(choices))
                self.assertEqual(set(blocked), expected_blocked)
                self.assertEqual(objective(assignments, state, snapshot["settings"]["tasks_per_active_day"]), best)
                self.assert_valid(snapshot, assignments, today)

    def test_hundred_plus_tasks_full_month_performance(self):
        today = date(2026, 9, 1)
        snapshot = config(
            [resident("a", off_days=("Saturday", "Sunday")), resident("b"), resident("c", "child")],
            [task("weekly%03d" % n, adult=n < 30) for n in range(100)]
            + [task("monthly%03d" % n, "monthly", n < 10) for n in range(20)],
        )
        started = time.monotonic()
        assignments, blocked = plan(snapshot, {}, today, random.Random(17))
        elapsed = time.monotonic() - started
        self.assertEqual(blocked, [])
        self.assertEqual(len(assignments), 520)
        self.assert_valid(snapshot, assignments, today)
        # Generous regression guard, not a Raspberry Pi wall-clock guarantee.
        self.assertLess(elapsed, 30.0)


if __name__ == "__main__":
    unittest.main()
