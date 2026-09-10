"""Pure, jointly optimized weekly/monthly household task planning."""

import calendar
import heapq
import random
from collections import Counter
from datetime import date, timedelta


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def month_key(day: date) -> str:
    return day.isoformat()[:7]


class _Edge:
    __slots__ = ("target", "reverse", "capacity", "cost")

    def __init__(self, target, reverse, capacity, cost):
        self.target = target
        self.reverse = reverse
        self.capacity = capacity
        self.cost = cost


def plan(
    config_snapshot: dict, state: dict, today: date, rng: random.Random
) -> tuple[list[dict], list[str]]:
    """Return tentative assignments and unassignable current-period task names.

    Inputs are validated snapshots and are never modified. The supplied RNG is
    consumed only for final tie breaking. Callers handle persistence, reuse of
    valid plans, and the guard against executing an already committed day.

    Costs lexicographically minimize new overflow, weekly sum(n*n/A), daily
    sum(n*n), then seeded ties. 420 is divisible by every possible A (1..7),
    so all fairness arithmetic is exact. Residual paths can repair earlier
    placements across both roles and periods without sacrificing any objective.
    """
    settings = config_snapshot["settings"]
    residents = sorted(settings["residents"], key=lambda resident: resident["id"])
    tasks = sorted(
        config_snapshot["tasks"],
        key=lambda task: (len(task["allowed_roles"]), task["id"]),
    )
    normal_capacity = settings["tasks_per_active_day"]
    first_week = monday(today)
    month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    last_week = monday(month_end)
    weeks = [
        first_week + timedelta(days=offset)
        for offset in range(0, (last_week - first_week).days + 1, 7)
    ]
    weekly_locks = set()
    monthly_locks = set()
    daily_baseline = Counter()
    weekly_baseline = Counter()
    for entry in state.get("assignment_history", []):
        assigned = date.fromisoformat(entry["date"])
        week = monday(assigned).isoformat()
        # Stored frequency/period may predate a configuration change.
        weekly_locks.add((entry["task_id"], week))
        monthly_locks.add((entry["task_id"], month_key(assigned)))
        daily_baseline[entry["resident_id"], entry["date"]] += 1
        weekly_baseline[entry["resident_id"], week] += 1

    weekdays = (
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
    )
    availability = state.get("availability_by_week", {})
    active_counts = {}
    days = []
    for resident in residents:
        off_days = set(resident["off_days"])
        for week in weeks:
            week_key = week.isoformat()
            recorded = availability.get(week_key, {}).get(resident["id"])
            if recorded is None:
                active = {
                    week + timedelta(days=offset)
                    for offset in range(7)
                    if week + timedelta(days=offset) >= today
                    and weekdays[offset] not in off_days
                }
            else:
                active = {
                    date.fromisoformat(value) for value in recorded
                    if week_key <= value <= (week + timedelta(days=6)).isoformat()
                }
            resident_week = (resident["id"], week_key)
            # Past participation remains in A, even after off-day changes.
            active_counts[resident_week] = len(active)
            for day in sorted(active):
                if day >= today and weekdays[day.weekday()] not in off_days:
                    days.append((resident["id"], day.isoformat(), week_key, resident["role"]))

    occurrences = []
    unassignable = []
    day_limits = Counter()
    week_limits = Counter()
    for task in tasks:
        if task["frequency"] == "weekly":
            periods = [
                (week.isoformat(), max(today, week), week + timedelta(days=6))
                for week in weeks
            ]
            locks = weekly_locks
            current_period = first_week.isoformat()
        else:
            periods = [(month_key(today), today, month_end)]
            locks = monthly_locks
            current_period = month_key(today)
        for period, start, end in periods:
            if (task["id"], period) in locks:
                continue
            start_key, end_key = start.isoformat(), end.isoformat()
            eligible = [
                index for index, (_, day, _, role) in enumerate(days)
                if role in task["allowed_roles"] and start_key <= day <= end_key
            ]
            if not eligible:
                if period == current_period:
                    unassignable.append(task["name"])
                continue
            occurrences.append((task, period, eligible))
            day_limits.update(eligible)
            # One occurrence can use at most one slot in any resident-week.
            week_limits.update({(days[index][0], days[index][2]) for index in eligible})

    if not occurrences:
        return [], unassignable

    source = 0
    day_nodes = {index: 1 + len(occurrences) + n for n, index in enumerate(sorted(day_limits))}
    week_nodes = {
        key: 1 + len(occurrences) + len(day_nodes) + n
        for n, key in enumerate(sorted(week_limits))
    }
    sink = 1 + len(occurrences) + len(day_nodes) + len(week_nodes)
    graph = [[] for _ in range(sink + 1)]
    zero = (0, 0, 0, 0)

    def add_edge(origin, target, cost):
        forward = _Edge(target, len(graph[target]), 1, cost)
        backward = _Edge(origin, len(graph[origin]), 0, tuple(-part for part in cost))
        graph[origin].append(forward)
        graph[target].append(backward)
        return forward

    choices = []
    for node, (_, _, eligible) in enumerate(occurrences, 1):
        add_edge(source, node, zero)
        choices.append([
            (index, add_edge(node, day_nodes[index], (0, 0, 0, rng.getrandbits(32))))
            for index in eligible
        ])
    for index, node in day_nodes.items():
        resident_id, day, week, _ = days[index]
        baseline = daily_baseline[resident_id, day]
        for slot in range(1, day_limits[index] + 1):
            add_edge(
                node, week_nodes[resident_id, week],
                (int(baseline + slot > normal_capacity), 0, 2 * (baseline + slot) - 1, 0),
            )
    for resident_week, node in week_nodes.items():
        baseline = weekly_baseline[resident_week]
        active = active_counts[resident_week]
        for slot in range(1, week_limits[resident_week] + 1):
            add_edge(node, sink, (0, (2 * (baseline + slot) - 1) * 420 // active, 0, 0))

    # Nonnegative initial forward costs allow zero initial potentials. Each
    # augmentation has unit flow; reverse edges permit arbitrary assignment swaps.
    potentials = [zero] * len(graph)
    for _ in occurrences:
        distances = [None] * len(graph)
        previous = [None] * len(graph)
        distances[source] = zero
        queue = [(zero, source)]
        while queue:
            distance, node = heapq.heappop(queue)
            if distances[node] != distance:
                continue
            if node == sink:
                break
            p = potentials[node]
            for edge_index, edge in enumerate(graph[node]):
                if not edge.capacity:
                    continue
                q = potentials[edge.target]
                cost = edge.cost
                candidate = (
                    distance[0] + cost[0] + p[0] - q[0],
                    distance[1] + cost[1] + p[1] - q[1],
                    distance[2] + cost[2] + p[2] - q[2],
                    distance[3] + cost[3] + p[3] - q[3],
                )
                old = distances[edge.target]
                if old is None or candidate < old:
                    distances[edge.target] = candidate
                    previous[edge.target] = (node, edge_index)
                    heapq.heappush(queue, (candidate, edge.target))
        limit = distances[sink]
        if limit is None:
            raise RuntimeError("A schedulable occurrence has no residual assignment path")
        # Clip distances at the settled sink distance. This permits early exit
        # from Dijkstra while preserving feasible potentials for every edge.
        for node, distance in enumerate(distances):
            increment = distance if distance is not None and distance < limit else limit
            p = potentials[node]
            potentials[node] = (
                p[0] + increment[0], p[1] + increment[1],
                p[2] + increment[2], p[3] + increment[3],
            )
        node = sink
        while node != source:
            origin, edge_index = previous[node]
            edge = graph[origin][edge_index]
            edge.capacity -= 1
            graph[node][edge.reverse].capacity += 1
            node = origin

    assignments = []
    for (task, period, _), candidates in zip(occurrences, choices):
        for index, edge in candidates:
            if edge.capacity == 0:
                resident_id, day, _, _ = days[index]
                assignments.append({
                    "task_id": task["id"], "frequency": task["frequency"],
                    "period": period, "date": day, "resident_id": resident_id,
                })
                break
    assignments.sort(key=lambda entry: (entry["date"], entry["resident_id"], entry["task_id"]))
    return assignments, unassignable
