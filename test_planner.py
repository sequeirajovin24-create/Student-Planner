import json
import os
import tempfile
import unittest
from datetime import date

import planner
from planner import (ValidationError, make_task, urgency, rank_tasks,
                     today_view, overdue_tasks, overloaded_days,
                     weekly_summary, load_tasks, save_tasks, next_id)

TODAY = date(2026, 11, 3)


def T(i, deadline, hours=2, priority=3, done=False, completed_on=None):
    t = make_task(i, f"Task {i}", "Math", deadline, hours, priority)
    t.done = done
    t.completed_on = completed_on
    return t


class TestUrgency(unittest.TestCase):
    def test_formula(self):
        # priority 4 x (6h / 3 days) = 8.0
        self.assertAlmostEqual(urgency(T(1, "2026-11-06", 6, 4), TODAY), 8.0)

    def test_due_today_uses_minimum_days(self):
        # priority 2 x (3h / 0.5) = 12.0, no division by zero
        self.assertAlmostEqual(urgency(T(1, "2026-11-03", 3, 2), TODAY), 12.0)

    def test_overdue_gets_multiplier(self):
        overdue = T(1, "2026-11-01", 3, 2)
        due_today = T(2, "2026-11-03", 3, 2)
        self.assertAlmostEqual(urgency(overdue, TODAY),
                               urgency(due_today, TODAY) * 2)


class TestRanking(unittest.TestCase):
    def test_higher_urgency_first(self):
        far = T(1, "2026-11-30", 2, 3)
        near = T(2, "2026-11-05", 2, 3)
        self.assertEqual([t.id for t in rank_tasks([far, near], TODAY)], [2, 1])

    def test_priority_breaks_otherwise_equal_tasks(self):
        low = T(1, "2026-11-10", 4, 1)
        high = T(2, "2026-11-10", 4, 5)
        self.assertEqual(rank_tasks([low, high], TODAY)[0].id, 2)

    def test_done_tasks_excluded(self):
        done = T(1, "2026-11-04", 5, 5, done=True, completed_on="2026-11-02")
        open_ = T(2, "2026-11-20", 1, 1)
        self.assertEqual([t.id for t in rank_tasks([done, open_], TODAY)], [2])

    def test_tie_broken_by_deadline_then_id(self):
        # Same score (hours/days ratio), different deadlines.
        a = T(1, "2026-11-05", 2, 3)   # 3 x (2/2) = 3.0
        b = T(2, "2026-11-07", 4, 3)   # 3 x (4/4) = 3.0
        self.assertEqual([t.id for t in rank_tasks([b, a], TODAY)], [1, 2])

    def test_today_view_limits_to_top_three(self):
        tasks = [T(i, f"2026-11-{4 + i:02d}", 3, 3) for i in range(1, 8)]
        self.assertEqual(len(today_view(tasks, TODAY)), 3)

    def test_overdue_ranks_above_identical_task_due_today(self):
        due_today = T(1, "2026-11-03", 2, 3)
        overdue = T(2, "2026-11-01", 2, 3)
        self.assertEqual(rank_tasks([due_today, overdue], TODAY)[0].id, 2)


class TestWarnings(unittest.TestCase):
    def test_overdue_detection(self):
        tasks = [T(1, "2026-11-01"), T(2, "2026-11-03"), T(3, "2026-11-09")]
        self.assertEqual([t.id for t in overdue_tasks(tasks, TODAY)], [1])

    def test_completed_task_is_not_overdue(self):
        done = T(1, "2026-11-01", done=True, completed_on="2026-10-30")
        self.assertEqual(overdue_tasks([done], TODAY), [])

    def test_overloaded_day_detected(self):
        tasks = [T(1, "2026-11-10", 5), T(2, "2026-11-10", 4)]
        result = overloaded_days(tasks, TODAY)
        self.assertEqual(result, {date(2026, 11, 10): 9.0})

    def test_exactly_at_limit_is_not_overloaded(self):
        tasks = [T(1, "2026-11-10", 5), T(2, "2026-11-10", 3)]
        self.assertEqual(overloaded_days(tasks, TODAY), {})

    def test_overload_ignores_done_and_past_tasks(self):
        tasks = [
            T(1, "2026-11-10", 6),
            T(2, "2026-11-10", 6, done=True, completed_on="2026-11-02"),
            T(3, "2026-10-30", 9),
        ]
        self.assertEqual(overloaded_days(tasks, TODAY), {})


class TestValidation(unittest.TestCase):
    def test_bad_inputs_rejected(self):
        bad = [
            ("", "Math", "2026-11-10", 2, 3),
            ("A", "", "2026-11-10", 2, 3),
            ("A", "Math", "10-11-2026", 2, 3),
            ("A", "Math", "2026-11-10", 0, 3),
            ("A", "Math", "2026-11-10", 2, 6),
            ("A", "Math", "2026-11-10", 2, 0),
        ]
        for args in bad:
            with self.subTest(args=args):
                with self.assertRaises(ValidationError):
                    make_task(1, *args)


class TestSummary(unittest.TestCase):
    def test_weekly_summary(self):
        tasks = [
            T(1, "2026-11-02", 3, done=True, completed_on="2026-11-02"),
            T(2, "2026-10-01", 2, done=True, completed_on="2026-10-01"),
            T(3, "2026-11-01", 4),                  # missed, in window
            T(4, "2026-10-01", 1),                  # missed but long ago
            T(5, "2026-11-06", 5),                  # planned
            T(6, "2026-12-30", 9),                  # beyond next 7 days
        ]
        s = weekly_summary(tasks, TODAY)
        self.assertEqual([t.id for t in s["completed"]], [1])
        self.assertEqual([t.id for t in s["missed"]], [3])
        self.assertEqual(s["hours_completed"], 3)
        self.assertEqual(s["hours_planned"], 5)


class TestStorage(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tasks.json")
            tasks = [T(1, "2026-11-10"), T(2, "2026-11-12", 3, 5)]
            save_tasks(path, tasks)
            self.assertEqual(load_tasks(path), tasks)
            self.assertEqual(next_id(tasks), 3)

    def test_missing_file_gives_empty_list(self):
        self.assertEqual(load_tasks("/nonexistent/tasks.json"), [])

    def test_corrupt_file_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tasks.json")
            with open(path, "w") as f:
                f.write("{not json")
            with self.assertRaises(ValidationError):
                load_tasks(path)


class TestDueTime(unittest.TestCase):
    def test_valid_times_accepted(self):
        for value in ("00:00", "09:05", "17:30", "23:59"):
            self.assertEqual(make_task(1, "A", "B", "2026-11-10", 2, 3, value).due_time, value)

    def test_time_is_optional(self):
        self.assertIsNone(make_task(1, "A", "B", "2026-11-10", 2, 3).due_time)

    def test_bad_times_rejected(self):
        for value in ("24:00", "7:30", "17:60", "noon", "17:30:00", "", 1730):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    make_task(1, "A", "B", "2026-11-10", 2, 3, value)

    def test_old_json_without_due_time_still_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.json")
            with open(path, "w") as f:
                json.dump([{"id": 1, "title": "Old", "subject": "S", "deadline": "2026-11-10",
                            "hours": 2, "priority": 3, "done": False, "completed_on": None}], f)
            self.assertIsNone(load_tasks(path)[0].due_time)

    def test_time_survives_save_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.json")
            tasks = [make_task(1, "A", "B", "2026-11-10", 2, 3, "08:15")]
            save_tasks(path, tasks)
            self.assertEqual(load_tasks(path), tasks)

    def test_cli_add_edit_and_clear_time(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.json")
            base = ["--file", path]
            self.assertEqual(planner.main(base + ["add", "Quiz", "-s", "M", "-d", "2026-11-10",
                                                  "-H", "1", "-t", "17:30"]), 0)
            self.assertEqual(load_tasks(path)[0].due_time, "17:30")
            self.assertEqual(planner.main(base + ["edit", "1", "--time", "09:00"]), 0)
            self.assertEqual(load_tasks(path)[0].due_time, "09:00")
            self.assertEqual(planner.main(base + ["edit", "1", "-H", "2"]), 0)
            self.assertEqual(load_tasks(path)[0].due_time, "09:00")  # untouched by other edits
            self.assertEqual(planner.main(base + ["edit", "1", "--clear-time"]), 0)
            self.assertIsNone(load_tasks(path)[0].due_time)
            self.assertEqual(planner.main(base + ["edit", "1", "--time", "25:00"]), 1)

    def test_time_does_not_change_the_urgency_score(self):
        a = make_task(1, "A", "B", "2026-11-06", 6, 4)
        b = make_task(2, "A", "B", "2026-11-06", 6, 4, "08:00")
        self.assertEqual(urgency(a, TODAY), urgency(b, TODAY))


class TestCLI(unittest.TestCase):
    def test_add_done_delete_flow(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.json")
            base = ["--file", path, "--today", "2026-11-03"]
            self.assertEqual(planner.main(base + [
                "add", "Essay", "-s", "English", "-d", "2026-11-09",
                "-H", "3", "-p", "4"]), 0)
            self.assertEqual(planner.main(base + ["done", "1"]), 0)
            with open(path) as f:
                data = json.load(f)
            self.assertTrue(data[0]["done"])
            self.assertEqual(data[0]["completed_on"], "2026-11-03")
            self.assertEqual(planner.main(base + ["delete", "1"]), 0)
            self.assertEqual(planner.main(base + ["done", "1"]), 1)


if __name__ == "__main__":
    unittest.main()
