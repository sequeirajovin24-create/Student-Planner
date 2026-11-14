#!/usr/bin/env python3
"""Offline-First Study Planner and Deadline Tracker.

Works fully offline, uses only the Python standard library, and stores all
data in a local JSON file.

Usage examples:
    python planner.py add "Lab report 3" -s Physics -d 2026-11-10 -H 4 -p 4
    python planner.py list
    python planner.py today
    python planner.py done 3
    python planner.py warnings
    python planner.py summary --export weekly.md
"""

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional

DEFAULT_FILE = "tasks.json"
DAILY_LIMIT_HOURS = 8.0   # more than this due on one day = overloaded
OVERDUE_MULTIPLIER = 2.0  # overdue tasks get pushed to the top
MIN_DAYS = 0.5            # avoids division by zero for tasks due today
TOP_N = 3                 # size of the "Today" view


class ValidationError(ValueError):
    """Raised when user input is invalid."""


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Task:
    id: int
    title: str
    subject: str
    deadline: str          # ISO date, YYYY-MM-DD
    hours: float           # estimated hours of work
    priority: int          # 1 (low) to 5 (high)
    done: bool = False
    completed_on: Optional[str] = None
    due_time: Optional[str] = None   # HH:MM (24-hour); None means end of the day

    @property
    def deadline_date(self) -> date:
        return parse_date(self.deadline)


def parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        raise ValidationError(f"Invalid date '{text}'. Use YYYY-MM-DD.")


TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def parse_time(text: str) -> str:
    if not isinstance(text, str) or not TIME_RE.match(text):
        raise ValidationError(f"Invalid time '{text}'. Use HH:MM (24-hour), e.g. 17:30.")
    return text


def validate_fields(title: str, subject: str, deadline: str,
                    hours: float, priority: int,
                    due_time: Optional[str] = None) -> None:
    if not str(title).strip():
        raise ValidationError("Title cannot be empty.")
    if not str(subject).strip():
        raise ValidationError("Subject cannot be empty.")
    parse_date(deadline)
    if due_time is not None:
        parse_time(due_time)
    if not isinstance(hours, (int, float)) or hours <= 0:
        raise ValidationError("Estimated hours must be a positive number.")
    if not isinstance(priority, int) or not 1 <= priority <= 5:
        raise ValidationError("Priority must be an integer from 1 to 5.")


def make_task(task_id: int, title: str, subject: str, deadline: str,
              hours: float, priority: int,
              due_time: Optional[str] = None) -> Task:
    validate_fields(title, subject, deadline, hours, priority, due_time)
    return Task(task_id, title.strip(), subject.strip(), deadline,
                float(hours), priority, due_time=due_time)


# --------------------------------------------------------------------------
# Scoring and views
# --------------------------------------------------------------------------

def days_left(task: Task, today: date) -> int:
    return (task.deadline_date - today).days


def urgency(task: Task, today: date) -> float:
    """urgency = priority x (hours / days_left).

    days_left is floored at MIN_DAYS so tasks due today do not divide by
    zero. Overdue tasks are multiplied by OVERDUE_MULTIPLIER so they always
    float to the top.
    """
    d = days_left(task, today)
    effective_days = max(d, MIN_DAYS)
    score = task.priority * (task.hours / effective_days)
    if d < 0:
        score *= OVERDUE_MULTIPLIER
    return score


def pending(tasks: List[Task]) -> List[Task]:
    return [t for t in tasks if not t.done]


def rank_tasks(tasks: List[Task], today: date) -> List[Task]:
    """Pending tasks, most urgent first (ties: earlier deadline, then id)."""
    return sorted(
        pending(tasks),
        key=lambda t: (-urgency(t, today), t.deadline_date, t.id),
    )


def today_view(tasks: List[Task], today: date, n: int = TOP_N) -> List[Task]:
    return rank_tasks(tasks, today)[:n]


def overdue_tasks(tasks: List[Task], today: date) -> List[Task]:
    return sorted(
        (t for t in pending(tasks) if t.deadline_date < today),
        key=lambda t: t.deadline_date,
    )


def overloaded_days(tasks: List[Task], today: date,
                    limit: float = DAILY_LIMIT_HOURS) -> Dict[date, float]:
    """Dates (today or later) where total pending hours due exceed `limit`."""
    totals: Dict[date, float] = {}
    for t in pending(tasks):
        d = t.deadline_date
        if d >= today:
            totals[d] = totals.get(d, 0.0) + t.hours
    return {d: h for d, h in sorted(totals.items()) if h > limit}


def weekly_summary(tasks: List[Task], today: date, days: int = 7) -> dict:
    """Summary of the window of `days` days ending today (inclusive).

    completed:       tasks marked done inside the window
    missed:          pending tasks whose deadline fell inside the window
    hours_planned:   hours of pending tasks due in the next `days` days
    """
    start = today - timedelta(days=days - 1)
    end_next = today + timedelta(days=days)

    completed = [
        t for t in tasks
        if t.done and t.completed_on
        and start <= parse_date(t.completed_on) <= today
    ]
    missed = [
        t for t in pending(tasks)
        if start <= t.deadline_date < today
    ]
    upcoming = [
        t for t in pending(tasks)
        if today <= t.deadline_date <= end_next
    ]
    return {
        "start": start,
        "end": today,
        "completed": completed,
        "missed": missed,
        "hours_completed": sum(t.hours for t in completed),
        "hours_planned": sum(t.hours for t in upcoming),
    }


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def _hours(h: float) -> str:
    return f"{h:g}"


def format_table(tasks: List[Task], today: date) -> str:
    if not tasks:
        return "(no tasks)"
    rows = [("ID", "Title", "Subject", "Due", "Left", "Hrs", "Pri", "Score")]
    for t in tasks:
        d = days_left(t, today)
        left = f"{d}d" if d >= 0 else f"{-d}d late"
        score = "" if t.done else f"{urgency(t, today):.2f}"
        due = t.deadline + (" " + t.due_time if t.due_time else "")
        rows.append((str(t.id), t.title, t.subject, due, left,
                     _hours(t.hours), str(t.priority), score))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for i, r in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[j]) for j, c in enumerate(r)))
        if i == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def format_warnings(tasks: List[Task], today: date,
                    limit: float = DAILY_LIMIT_HOURS) -> str:
    lines = []
    for t in overdue_tasks(tasks, today):
        lines.append(
            f"OVERDUE: #{t.id} {t.title} ({t.subject}) was due {t.deadline}"
        )
    for d, h in overloaded_days(tasks, today, limit).items():
        lines.append(
            f"OVERLOADED: {d.isoformat()} has {_hours(h)}h of work due "
            f"(limit {_hours(limit)}h)"
        )
    return "\n".join(lines) if lines else "No warnings. You are on track."


def format_summary_markdown(summary: dict) -> str:
    def bullets(items: List[Task]) -> str:
        if not items:
            return "- none"
        return "\n".join(
            f"- #{t.id} {t.title} ({t.subject}, {_hours(t.hours)}h)"
            for t in items
        )

    return (
        f"# Weekly Summary: {summary['start']} to {summary['end']}\n\n"
        f"- Tasks completed: {len(summary['completed'])} "
        f"({_hours(summary['hours_completed'])}h)\n"
        f"- Tasks missed: {len(summary['missed'])}\n"
        f"- Hours planned for the next 7 days: "
        f"{_hours(summary['hours_planned'])}h\n\n"
        f"## Completed\n{bullets(summary['completed'])}\n\n"
        f"## Missed\n{bullets(summary['missed'])}\n"
    )


# --------------------------------------------------------------------------
# Storage (local JSON file, written atomically)
# --------------------------------------------------------------------------

def load_tasks(path: str) -> List[Task]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [Task(**item) for item in data]
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValidationError(f"Could not read {path}: {exc}")


def save_tasks(path: str, tasks: List[Task]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump([asdict(t) for t in tasks], f, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def next_id(tasks: List[Task]) -> int:
    return max((t.id for t in tasks), default=0) + 1


def find_task(tasks: List[Task], task_id: int) -> Task:
    for t in tasks:
        if t.id == task_id:
            return t
    raise ValidationError(f"No task with id {task_id}.")


# --------------------------------------------------------------------------
# Command line interface
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="planner",
        description="Offline study planner and deadline tracker.",
    )
    p.add_argument("--file", default=DEFAULT_FILE,
                   help=f"data file (default: {DEFAULT_FILE})")
    p.add_argument("--today", default=None,
                   help="override today's date, YYYY-MM-DD (for demos/tests)")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="add a task")
    a.add_argument("title")
    a.add_argument("-s", "--subject", required=True)
    a.add_argument("-d", "--deadline", required=True, help="YYYY-MM-DD")
    a.add_argument("-H", "--hours", type=float, required=True)
    a.add_argument("-t", "--time", help="due time HH:MM, 24-hour (default: end of day)")
    a.add_argument("-p", "--priority", type=int, default=3,
                   help="1 (low) to 5 (high), default 3")

    e = sub.add_parser("edit", help="edit a task")
    e.add_argument("id", type=int)
    e.add_argument("--title")
    e.add_argument("-s", "--subject")
    e.add_argument("-d", "--deadline")
    e.add_argument("-H", "--hours", type=float)
    e.add_argument("-p", "--priority", type=int)
    e.add_argument("-t", "--time", help="due time HH:MM, 24-hour")
    e.add_argument("--clear-time", action="store_true", help="remove the due time")

    r = sub.add_parser("delete", help="delete a task")
    r.add_argument("id", type=int)

    dn = sub.add_parser("done", help="mark a task as done")
    dn.add_argument("id", type=int)

    ls = sub.add_parser("list", help="list tasks, most urgent first")
    ls.add_argument("--all", action="store_true",
                    help="include completed tasks")

    sub.add_parser("today", help=f"show the top {TOP_N} tasks to focus on")
    sub.add_parser("warnings", help="show overdue and overloaded-day warnings")

    sm = sub.add_parser("summary", help="weekly summary")
    sm.add_argument("--export", metavar="FILE",
                    help="write the summary to a Markdown file")
    return p


def run(args: argparse.Namespace) -> int:
    today = parse_date(args.today) if args.today else date.today()
    tasks = load_tasks(args.file)
    cmd = args.command

    if cmd == "add":
        task = make_task(next_id(tasks), args.title, args.subject,
                         args.deadline, args.hours, args.priority, args.time)
        tasks.append(task)
        save_tasks(args.file, tasks)
        print(f"Added task #{task.id}: {task.title}")

    elif cmd == "edit":
        t = find_task(tasks, args.id)
        new = {
            "title": args.title if args.title is not None else t.title,
            "subject": args.subject if args.subject is not None else t.subject,
            "deadline": args.deadline if args.deadline is not None else t.deadline,
            "hours": args.hours if args.hours is not None else t.hours,
            "priority": args.priority if args.priority is not None else t.priority,
            "due_time": (args.time if args.time is not None
                         else None if args.clear_time else t.due_time),
        }
        validate_fields(**new)
        t.title = new["title"].strip()
        t.subject = new["subject"].strip()
        t.deadline = new["deadline"]
        t.hours = float(new["hours"])
        t.priority = new["priority"]
        t.due_time = new["due_time"]
        save_tasks(args.file, tasks)
        print(f"Updated task #{t.id}")

    elif cmd == "delete":
        t = find_task(tasks, args.id)
        tasks.remove(t)
        save_tasks(args.file, tasks)
        print(f"Deleted task #{t.id}: {t.title}")

    elif cmd == "done":
        t = find_task(tasks, args.id)
        t.done = True
        t.completed_on = today.isoformat()
        save_tasks(args.file, tasks)
        print(f"Marked #{t.id} as done. Nice work!")

    elif cmd == "list":
        shown = rank_tasks(tasks, today)
        if args.all:
            finished = sorted((t for t in tasks if t.done),
                              key=lambda t: t.id)
            shown += finished
        print(format_table(shown, today))

    elif cmd == "today":
        top = today_view(tasks, today)
        print(f"Focus for {today.isoformat()}:")
        print(format_table(top, today))
        warn = format_warnings(tasks, today)
        if not warn.startswith("No warnings"):
            print("\n" + warn)

    elif cmd == "warnings":
        print(format_warnings(tasks, today))

    elif cmd == "summary":
        text = format_summary_markdown(weekly_summary(tasks, today))
        if args.export:
            with open(args.export, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"Summary written to {args.export}")
        else:
            print(text)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except ValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
