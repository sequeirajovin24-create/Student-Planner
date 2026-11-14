#!/usr/bin/env python3
"""StudentPlanner backend: a small JSON API plus the web page, standard library only.

It reuses the scoring and validation code in planner.py. Every student has an
account and their own task list. Everything lives in one SQLite file,
data/studplan.db (passwords are stored only as salted hashes).

Run:
    python server.py                      # http://127.0.0.1:8000
    python server.py --port 5000 --data-dir my_data

Move tasks between the database and the command-line tool (planner.py):
    python server.py export alice tasks.json    # database -> planner.py file
    python server.py import alice tasks.json    # planner.py file -> database

Set a new password for an account from this computer (no old password needed):
    python server.py reset-password alice

Data from the earlier JSON version (users.json and tasks_<id>.json) is moved
into the database automatically the first time the server starts; the old files
are renamed to *.migrated and kept as a backup.

Also served: /scene.js (the 3D background). Nothing else on disk is reachable.

API (all JSON). Everything except /api/health and /api/auth/* needs a login:
    GET    /api/health
    POST   /api/auth/signup               {"username", "password"}
    POST   /api/auth/login                {"username", "password"}
    POST   /api/auth/logout
    GET    /api/auth/me                   who is logged in
    POST   /api/auth/change-password      {"current_password", "new_password"} (logged in)
    POST   /api/auth/forgot               {"username"}: prints a reset code in this window
    POST   /api/auth/reset                {"username", "code", "password"}
    GET    /api/tasks                     all tasks, with days_left and urgency
    POST   /api/tasks                     create a task (due_time "HH:MM" is optional)
    GET    /api/tasks/<id>
    PUT    /api/tasks/<id>                change any fields (partial update)
    DELETE /api/tasks/<id>
    POST   /api/tasks/<id>/done           mark done (records today's date)
    POST   /api/tasks/<id>/undo           mark not done
    GET    /api/today                     top tasks, overdue tasks, overloaded days
    GET    /api/summary                   weekly summary
    GET    /api/settings                  goals: {"daily_goal", "weekly_goal"}
    PUT    /api/settings                  change either goal

Add ?today=YYYY-MM-DD to any GET or POST to pretend it is another date.

By default the server only listens on this computer (127.0.0.1) and sends the session
cookie over plain HTTP. To host it on the internet (see DEPLOY.md) run it with
--host 0.0.0.0 behind HTTPS and set STUDPLAN_ALLOWED_HOSTS to your site's address
(Render, Railway and Fly.io are detected automatically). PORT, STUDPLAN_HOST,
STUDPLAN_DATA_DIR and STUDPLAN_SECURE_COOKIE=1 are also read from the environment.

Also served for the installable app: /manifest.webmanifest, /sw.js and the icons.
"""

import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import planner
from planner import ValidationError

MAX_BODY_BYTES = 64 * 1024
MAX_TITLE = 200
MAX_SUBJECT = 80
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
REQUIRED_FIELDS = ("title", "subject", "deadline", "hours", "priority")
TASK_FIELDS = REQUIRED_FIELDS + ("due_time",)   # due_time is optional: "HH:MM" or null
DEFAULT_GOALS = {"daily_goal": 2, "weekly_goal": 8}
GOAL_LIMITS = {"daily_goal": (1, 20), "weekly_goal": (1, 100)}
COOKIE_NAME = "studplan_session"
SESSION_SECONDS = 7 * 24 * 3600
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")
PASSWORD_MIN, PASSWORD_MAX = 8, 128
LOGIN_MAX_FAILS, LOGIN_WINDOW_SECONDS = 5, 300
RESET_SECONDS = 600            # a reset code works for 10 minutes
RESET_MAX_ATTEMPTS = 5         # wrong guesses before the code is thrown away
RESET_MAX_ISSUES = 3           # codes per username per 10 minutes
RESET_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I
RESET_LENGTH = 8
# The only files served besides index.html and the API (nothing else on disk is reachable).
STATIC_FILES = {
    "/scene.js": ("scene.js", "text/javascript; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/icon-maskable-512.png": ("icon-maskable-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}


def hosting_settings(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Settings for running on the internet, read from environment variables.
    Hosting sites that tell us their address (Render, Railway, Fly.io) need no setup."""
    env = os.environ if env is None else env
    hosts = {h.strip().lower() for h in (env.get("STUDPLAN_ALLOWED_HOSTS") or "").split(",") if h.strip()}
    for key in ("RENDER_EXTERNAL_HOSTNAME", "RAILWAY_PUBLIC_DOMAIN"):
        if env.get(key):
            hosts.add(env[key].strip().lower())
    if env.get("FLY_APP_NAME"):
        hosts.add(env["FLY_APP_NAME"].strip().lower() + ".fly.dev")
    online = bool(hosts) or any(env.get(k) for k in ("RENDER", "RAILWAY_ENVIRONMENT", "FLY_APP_NAME"))
    secure = (env.get("STUDPLAN_SECURE_COOKIE") or "").strip().lower() in ("1", "true", "yes") or online
    if (env.get("STUDPLAN_SECURE_COOKIE") or "").strip().lower() in ("0", "false", "no"):
        secure = False
    return {"allowed_hosts": ALLOWED_HOSTS | hosts, "secure_cookie": secure, "online": online}


class ApiError(Exception):
    """An error that maps to an HTTP status code and a JSON message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------
# Storage and business rules
# --------------------------------------------------------------------------

def _clean_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """Type and length checks that planner.validate_fields does not cover."""
    for name in ("title", "subject", "deadline"):
        if not isinstance(data[name], str):
            raise ValidationError(f"{name.capitalize()} must be text.")
    if isinstance(data["hours"], bool) or not isinstance(data["hours"], (int, float)):
        raise ValidationError("Estimated hours must be a number.")
    if isinstance(data["priority"], bool) or not isinstance(data["priority"], int):
        raise ValidationError("Priority must be a whole number from 1 to 5.")
    if data.get("due_time") == "":
        data["due_time"] = None            # an empty box in the form means "no time"
    if data.get("due_time") is not None and not isinstance(data["due_time"], str):
        raise ValidationError("Due time must be text like 17:30.")
    if len(data["title"].strip()) > MAX_TITLE:
        raise ValidationError(f"Title must be {MAX_TITLE} characters or fewer.")
    if len(data["subject"].strip()) > MAX_SUBJECT:
        raise ValidationError(f"Subject must be {MAX_SUBJECT} characters or fewer.")
    planner.validate_fields(data["title"], data["subject"], data["deadline"],
                            data["hours"], data["priority"], data.get("due_time"))
    return data


# --------------------------------------------------------------------------
# SQLite storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created       TEXT NOT NULL,
    session_epoch INTEGER NOT NULL DEFAULT 0,
    email         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS users_username_nocase ON users (username COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    subject      TEXT NOT NULL,
    deadline     TEXT NOT NULL,
    hours        REAL NOT NULL CHECK (hours > 0),
    priority     INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 5),
    done         INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
    completed_on TEXT,
    due_time     TEXT
);
CREATE INDEX IF NOT EXISTS tasks_by_user ON tasks (user_id);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id     INTEGER PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    daily_goal  INTEGER NOT NULL DEFAULT 2 CHECK (daily_goal BETWEEN 1 AND 20),
    weekly_goal INTEGER NOT NULL DEFAULT 8 CHECK (weekly_goal BETWEEN 1 AND 100)
);
"""


class Database:
    """One SQLite file. Every operation uses its own short-lived connection, so
    the threads that serve requests never share one."""

    def __init__(self, path: str):
        self.path = path
        try:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(SCHEMA)
                # Databases made by earlier versions have no due_time column yet.
                columns = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)")]
                if "due_time" not in columns:
                    conn.execute("ALTER TABLE tasks ADD COLUMN due_time TEXT")
                # session_epoch rises on every password change, which logs out old sessions.
                ucols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
                if "session_epoch" not in ucols:
                    conn.execute(
                        "ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0")
                if "email" not in ucols:
                    conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise ValidationError(
                f"Could not open the database {os.path.basename(path)}: {exc}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def read(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self):
        """A transaction: everything inside succeeds together or not at all."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()


def row_to_task(row: sqlite3.Row) -> planner.Task:
    return planner.Task(
        id=row["id"], title=row["title"], subject=row["subject"],
        deadline=row["deadline"], hours=float(row["hours"]),
        priority=row["priority"], done=bool(row["done"]),
        completed_on=row["completed_on"], due_time=row["due_time"])


def _not_found(task_id: int) -> ApiError:
    return ApiError(404, f"No task with id {task_id}.")


class Store:
    """One student's tasks. Every query is limited to that student's user_id."""

    def __init__(self, db: Database, user_id: int):
        self.db = db
        self.user_id = user_id

    def all(self) -> List[planner.Task]:
        with self.db.read() as c:
            rows = c.execute("SELECT * FROM tasks WHERE user_id = ? ORDER BY id",
                             (self.user_id,)).fetchall()
        return [row_to_task(r) for r in rows]

    def get(self, task_id: int) -> planner.Task:
        with self.db.read() as c:
            row = c.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                            (task_id, self.user_id)).fetchone()
        if row is None:
            raise _not_found(task_id)
        return row_to_task(row)

    def create(self, data: Dict[str, Any]) -> planner.Task:
        missing = [f for f in REQUIRED_FIELDS if f not in data]
        if missing:
            raise ValidationError("Missing field: " + ", ".join(missing))
        fields = {f: data.get(f) for f in TASK_FIELDS}
        _clean_fields(fields)
        task = planner.make_task(0, **fields)  # trims text, converts hours to float
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO tasks (user_id, title, subject, deadline, hours, priority, due_time)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.user_id, task.title, task.subject, task.deadline,
                 task.hours, task.priority, task.due_time))
            task.id = cur.lastrowid
        return task

    def update(self, task_id: int, data: Dict[str, Any]) -> planner.Task:
        unknown = [k for k in data if k not in TASK_FIELDS]
        if unknown:
            raise ValidationError("Unknown field: " + ", ".join(unknown))
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                            (task_id, self.user_id)).fetchone()
            if row is None:
                raise _not_found(task_id)
            task = row_to_task(row)
            merged = {f: data.get(f, getattr(task, f)) for f in TASK_FIELDS}
            _clean_fields(merged)
            task.title = merged["title"].strip()
            task.subject = merged["subject"].strip()
            task.deadline = merged["deadline"]
            task.hours = float(merged["hours"])
            task.priority = merged["priority"]
            task.due_time = merged["due_time"]
            c.execute("UPDATE tasks SET title=?, subject=?, deadline=?, hours=?, priority=?,"
                      " due_time=? WHERE id = ? AND user_id = ?",
                      (task.title, task.subject, task.deadline, task.hours,
                       task.priority, task.due_time, task_id, self.user_id))
        return task

    def delete(self, task_id: int) -> None:
        with self.db.tx() as c:
            cur = c.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?",
                            (task_id, self.user_id))
            if cur.rowcount == 0:
                raise _not_found(task_id)

    def set_done(self, task_id: int, done: bool, today: date) -> planner.Task:
        with self.db.tx() as c:
            cur = c.execute("UPDATE tasks SET done = ?, completed_on = ?"
                            " WHERE id = ? AND user_id = ?",
                            (int(done), today.isoformat() if done else None,
                             task_id, self.user_id))
            if cur.rowcount == 0:
                raise _not_found(task_id)
        return self.get(task_id)


class Settings:
    """One student's goals (tasks to finish per day and per week)."""

    def __init__(self, db: Database, user_id: int):
        self.db = db
        self.user_id = user_id

    def get(self) -> Dict[str, int]:
        with self.db.read() as c:
            row = c.execute("SELECT daily_goal, weekly_goal FROM user_settings"
                            " WHERE user_id = ?", (self.user_id,)).fetchone()
        return dict(row) if row else dict(DEFAULT_GOALS)

    def update(self, data: Dict[str, Any]) -> Dict[str, int]:
        unknown = [k for k in data if k not in GOAL_LIMITS]
        if unknown:
            raise ValidationError("Unknown setting: " + ", ".join(unknown))
        merged = self.get()
        for name, value in data.items():
            low, high = GOAL_LIMITS[name]
            label = name.replace("_", " ")
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValidationError(f"The {label} must be a whole number from {low} to {high}.")
            merged[name] = value
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO user_settings (user_id, daily_goal, weekly_goal) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET daily_goal = excluded.daily_goal,"
                " weekly_goal = excluded.weekly_goal",
                (self.user_id, merged["daily_goal"], merged["weekly_goal"]))
        return merged


def _insert_task(conn: sqlite3.Connection, user_id: int, task: planner.Task) -> None:
    """Validate and insert a task that came from a planner.py JSON file."""
    fields = {f: getattr(task, f) for f in TASK_FIELDS}
    _clean_fields(fields)
    if not isinstance(task.done, bool):
        raise ValidationError(f"Task '{task.title}': done must be true or false.")
    if task.completed_on is not None:
        planner.parse_date(task.completed_on)
    conn.execute(
        "INSERT INTO tasks (user_id, title, subject, deadline, hours, priority, done,"
        " completed_on, due_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, task.title.strip(), task.subject.strip(), task.deadline,
         float(task.hours), task.priority, int(task.done), task.completed_on,
         fields["due_time"]))


def import_tasks(db: Database, user_id: int, tasks: List[planner.Task]) -> int:
    """Add tasks from a planner.py file to an account. All of them or none."""
    with db.tx() as c:
        for task in tasks:
            _insert_task(c, user_id, task)
    return len(tasks)


def export_tasks(db: Database, user_id: int) -> List[planner.Task]:
    """A student's tasks in planner.py's format, numbered 1, 2, 3..."""
    return [replace(t, id=i) for i, t in enumerate(Store(db, user_id).all(), start=1)]


def migrate_legacy_json(db: Database, data_dir: str) -> Optional[str]:
    """Move users.json and tasks_<id>.json (the earlier version) into the database.

    Runs only when the database has no accounts yet. The old files are renamed
    to *.migrated, never deleted."""
    users_path = os.path.join(data_dir, "users.json")
    if not os.path.exists(users_path):
        return None
    with db.read() as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return None
    try:
        with open(users_path, "r", encoding="utf-8") as f:
            users = json.load(f)
        if not isinstance(users, list):
            raise ValueError("expected a list of accounts")
        moved = [users_path]
        with db.tx() as c:
            for u in users:
                c.execute("INSERT INTO users (id, username, password_hash, created)"
                          " VALUES (?, ?, ?, ?)",
                          (u["id"], u["username"], u["password_hash"], u["created"]))
                tasks_path = os.path.join(data_dir, f"tasks_{u['id']}.json")
                if os.path.exists(tasks_path):
                    for task in planner.load_tasks(tasks_path):
                        _insert_task(c, u["id"], task)
                    moved.append(tasks_path)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        raise ValidationError(f"Could not move the old JSON data into the database: {exc}")
    for path in moved:
        os.replace(path, path + ".migrated")
    return (f"Moved {len(users)} account(s) from the old JSON files into the database "
            "(originals renamed to *.migrated).")


# --------------------------------------------------------------------------
# Accounts: password hashing, users file, sessions, login throttling
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Salted, slow hash. scrypt when available, otherwise PBKDF2."""
    salt = os.urandom(16)
    if hasattr(hashlib, "scrypt"):
        n, r, p = 2 ** 14, 8, 1
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
        return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"
    iterations = 310_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        pw = password.encode("utf-8")
        if parts[0] == "scrypt":
            _, n, r, p, salt, digest = parts
            expected = bytes.fromhex(digest)
            actual = hashlib.scrypt(pw, salt=bytes.fromhex(salt), n=int(n), r=int(r),
                                    p=int(p), dklen=len(expected))
        elif parts[0] == "pbkdf2":
            _, iterations, salt, digest = parts
            expected = bytes.fromhex(digest)
            actual = hashlib.pbkdf2_hmac("sha256", pw, bytes.fromhex(salt),
                                         int(iterations), dklen=len(expected))
        else:
            return False
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False


_dummy_hash: Optional[str] = None


def _dummy() -> str:
    """Hash checked for unknown usernames so timing does not reveal them."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("not-a-real-password")
    return _dummy_hash


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": user["id"], "username": user["username"]}


class Users:
    """Accounts in the database. Passwords are never stored, only hashes."""

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def check_credentials(username: Any, password: Any) -> str:
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValidationError("Username and password must be text.")
        username = username.strip()
        if not USERNAME_RE.match(username):
            raise ValidationError(
                "Username must be 3 to 30 characters: letters, numbers, dot, dash or underscore.")
        if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
            raise ValidationError(
                f"Password must be {PASSWORD_MIN} to {PASSWORD_MAX} characters.")
        if password.lower() == username.lower():
            raise ValidationError("Password cannot be the same as the username.")
        return username

    def create(self, username: Any, password: Any) -> Dict[str, Any]:
        username = self.check_credentials(username, password)
        password_hash = hash_password(password)  # slow, so done before the transaction
        created = date.today().isoformat()
        try:
            with self.db.tx() as c:
                cur = c.execute(
                    "INSERT INTO users (username, password_hash, created) VALUES (?, ?, ?)",
                    (username, password_hash, created))
                user_id = cur.lastrowid
        except sqlite3.IntegrityError:  # the unique, case-insensitive index
            raise ApiError(409, "That username is already taken.")
        return {"id": user_id, "username": username,
                "password_hash": password_hash, "created": created, "session_epoch": 0}

    def by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self.db.read() as c:
            row = c.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                            (username.strip(),)).fetchone()
        return dict(row) if row else None

    def authenticate(self, username: Any, password: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(username, str) or not isinstance(password, str):
            verify_password("x", _dummy())
            return None
        match = self.by_username(username)
        ok = verify_password(password, match["password_hash"] if match else _dummy())
        return match if (match and ok) else None

    def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self.db.read() as c:
            row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def set_password(self, user_id: int, new_password: Any) -> Dict[str, Any]:
        """Replace a password and log out every existing session of that user."""
        user = self.get(user_id)
        if user is None:
            raise ApiError(404, "No such account.")
        self.check_credentials(user["username"], new_password)
        password_hash = hash_password(new_password)  # slow, so before the transaction
        with self.db.tx() as c:
            c.execute("UPDATE users SET password_hash = ?, session_epoch = session_epoch + 1 "
                      "WHERE id = ?", (password_hash, user_id))
        return self.get(user_id)


class Sessions:
    """Random session tokens kept in memory (everyone logs in again after a restart).

    Each session remembers the user's session_epoch at login. A password change
    raises the epoch in the database, so older sessions stop working."""

    def __init__(self, ttl: int = SESSION_SECONDS):
        self.ttl = ttl
        self._data: Dict[str, Tuple[int, int, float]] = {}  # token -> (user, epoch, expiry)
        self._lock = threading.Lock()

    def create(self, user_id: int, epoch: int = 0) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._data = {t: v for t, v in self._data.items() if v[2] > now}
            self._data[token] = (user_id, epoch, now + self.ttl)
        return token

    def lookup(self, token: Optional[str]) -> Optional[Tuple[int, int]]:
        """(user_id, epoch) for a live token, else None."""
        if not token:
            return None
        with self._lock:
            entry = self._data.get(token)
            if entry and entry[2] > time.time():
                return entry[0], entry[1]
            self._data.pop(token, None)
            return None

    def user_id(self, token: Optional[str]) -> Optional[int]:
        found = self.lookup(token)
        return found[0] if found else None

    def destroy(self, token: str) -> None:
        with self._lock:
            self._data.pop(token, None)


class ResetCodes:
    """One-time password reset codes, kept in memory and handed out through the
    server terminal (there is no email). A code lasts 10 minutes, allows a few
    wrong guesses, and is destroyed once used."""

    def __init__(self, ttl: int = RESET_SECONDS, max_attempts: int = RESET_MAX_ATTEMPTS,
                 max_issues: int = RESET_MAX_ISSUES):
        self.ttl, self.max_attempts, self.max_issues = ttl, max_attempts, max_issues
        self._codes: Dict[str, Dict[str, Any]] = {}
        self._issued: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def normalise(code: Any) -> str:
        if not isinstance(code, str):
            return ""
        return re.sub(r"[\s-]", "", code).upper()

    @staticmethod
    def pretty(code: str) -> str:
        half = len(code) // 2
        return f"{code[:half]}-{code[half:]}"

    def issue(self, key: str) -> Optional[str]:
        """A fresh code, or None when too many were requested recently."""
        now = time.time()
        with self._lock:
            recent = [t for t in self._issued.get(key, []) if now - t < self.ttl]
            if len(recent) >= self.max_issues:
                self._issued[key] = recent
                return None
            code = "".join(secrets.choice(RESET_ALPHABET) for _ in range(RESET_LENGTH))
            recent.append(now)
            self._issued[key] = recent
            self._codes[key] = {"code": code, "expires": now + self.ttl, "attempts": 0}
            return code

    def check(self, key: str, code: Any) -> str:
        """'ok' (code is used up), 'bad', 'locked' (too many guesses) or 'none'."""
        guess = self.normalise(code)
        now = time.time()
        with self._lock:
            entry = self._codes.get(key)
            if entry is None or entry["expires"] <= now:
                self._codes.pop(key, None)
                return "none"
            if guess and hmac.compare_digest(guess.encode(), entry["code"].encode()):
                del self._codes[key]
                return "ok"
            entry["attempts"] += 1
            if entry["attempts"] >= self.max_attempts:
                del self._codes[key]
                return "locked"
            return "bad"

    def restore(self, key: str, code: Any) -> None:
        """Put back a code that check() just used up (the new password was rejected)."""
        with self._lock:
            self._codes[key] = {"code": self.normalise(code),
                                "expires": time.time() + self.ttl, "attempts": 0}

    def discard(self, key: str) -> None:
        with self._lock:
            self._codes.pop(key, None)


class LoginThrottle:
    """Locks a username out after repeated wrong passwords."""

    def __init__(self, max_fails: int = LOGIN_MAX_FAILS, window: int = LOGIN_WINDOW_SECONDS):
        self.max_fails, self.window = max_fails, window
        self._fails: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def blocked_for(self, key: str) -> int:
        """Seconds left on a lockout, or 0 when the user may try."""
        now = time.time()
        with self._lock:
            recent = [t for t in self._fails.get(key, []) if now - t < self.window]
            self._fails[key] = recent
            if len(recent) >= self.max_fails:
                return int(recent[0] + self.window - now) + 1
            return 0

    def fail(self, key: str) -> None:
        with self._lock:
            self._fails.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


def task_json(task: planner.Task, today: date) -> Dict[str, Any]:
    out = asdict(task)
    out["days_left"] = planner.days_left(task, today)
    out["urgency"] = None if task.done else round(planner.urgency(task, today), 2)
    return out


def today_payload(tasks: List[planner.Task], today: date) -> Dict[str, Any]:
    return {
        "date": today.isoformat(),
        "focus": [task_json(t, today) for t in planner.today_view(tasks, today)],
        "overdue": [task_json(t, today) for t in planner.overdue_tasks(tasks, today)],
        "overloaded": [
            {"date": d.isoformat(), "hours": h}
            for d, h in planner.overloaded_days(tasks, today).items()
        ],
        "daily_limit_hours": planner.DAILY_LIMIT_HOURS,
    }


def summary_payload(tasks: List[planner.Task], today: date) -> Dict[str, Any]:
    s = planner.weekly_summary(tasks, today)
    return {
        "start": s["start"].isoformat(),
        "end": s["end"].isoformat(),
        "completed": [asdict(t) for t in s["completed"]],
        "missed": [asdict(t) for t in s["missed"]],
        "hours_completed": s["hours_completed"],
        "hours_planned": s["hours_planned"],
    }


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

TASK_ROUTE = re.compile(r"^/api/tasks/(\d+)(?:/(done|undo))?$")


class Handler(BaseHTTPRequestHandler):
    server_version = "StudentPlanner/1.0"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "verbose", True):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str,
              extra: Optional[List[Tuple[str, str]]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in extra or []:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any,
              extra: Optional[List[Tuple[str, str]]] = None) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _check_host(self, path: str = "") -> None:
        if path == "/api/health":      # reveals nothing; hosting health checks use odd Host values
            return
        # Blocks DNS-rebinding: a web page on another site cannot reach this
        # server by pointing its own hostname at 127.0.0.1.
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            name = host.split("]", 1)[0] + "]"
        else:
            name = host.split(":", 1)[0]
        if name not in self.server.allowed_hosts:
            raise ApiError(403, "Requests must use localhost, 127.0.0.1 or this server's own address "
                                "(set STUDPLAN_ALLOWED_HOSTS when hosting).")

    def _read_json(self) -> Dict[str, Any]:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "Send the body as application/json.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Invalid Content-Length.")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "Request body is too large.")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "Body is not valid JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "Body must be a JSON object.")
        return data

    def _today(self, query: Dict[str, List[str]]) -> date:
        raw = (query.get("today") or [None])[0]
        if raw:
            return planner.parse_date(raw)  # ValidationError -> 400
        return self.server.today_override or date.today()

    # -- routing -----------------------------------------------------------
    def _handle(self, method: str) -> None:
        try:
            url = urlparse(self.path)
            self._check_host(url.path.rstrip("/"))
            path, query = url.path.rstrip("/") or "/", parse_qs(url.query)

            if path in ("/", "/index.html"):
                if method not in ("GET", "HEAD"):
                    raise ApiError(405, "Method not allowed.")
                return self._serve_index()
            asset = STATIC_FILES.get(path)
            if asset:
                if method not in ("GET", "HEAD"):
                    raise ApiError(405, "Method not allowed.")
                return self._serve_file(*asset)
            if not path.startswith("/api/"):
                raise ApiError(404, "Not found.")
            self._api(method, path, query)
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # keep the server alive on unexpected bugs
            sys.stderr.write("Unexpected error: %r\n" % (exc,))
            self._json(500, {"error": "Internal server error."})

    def _serve_file(self, relpath: str, content_type: str) -> None:
        """Serve one allow-listed file (scene.js)."""
        full = os.path.join(self.server.static_dir, *relpath.split("/"))
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            raise ApiError(404, "Not found.")
        self._send(200, body, content_type)

    def _serve_index(self) -> None:
        index = os.path.join(self.server.static_dir, "index.html")
        try:
            with open(index, "rb") as f:
                body = f.read()
        except OSError:
            raise ApiError(404, "index.html not found next to server.py.")
        self._send(200, body, "text/html; charset=utf-8")

    # -- accounts ----------------------------------------------------------
    def _session_token(self) -> Optional[str]:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except CookieError:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _user_payload(self, user: Dict[str, Any]) -> Dict[str, Any]:
        return {"user": public_user(user)}

    def _current_user(self) -> Optional[Dict[str, Any]]:
        found = self.server.sessions.lookup(self._session_token())
        if found is None:
            return None
        user = self.server.users.get(found[0])
        # A changed password raises session_epoch, which ends older sessions.
        if user is None or user["session_epoch"] != found[1]:
            return None
        return user

    def _cookie(self, value: str, max_age: int) -> Tuple[str, str]:
        secure = "; Secure" if self.server.secure_cookie else ""
        return ("Set-Cookie",
                f"{COOKIE_NAME}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}{secure}")

    def _login(self, username: Any, password: Any) -> Dict[str, Any]:
        key = username.strip().lower() if isinstance(username, str) else ""
        wait = self.server.throttle.blocked_for(key)
        if wait:
            raise ApiError(429, f"Too many failed attempts. Try again in {wait} seconds.")
        user = self.server.users.authenticate(username, password)
        if user is None:
            self.server.throttle.fail(key)
            raise ApiError(401, "Incorrect username or password.")
        self.server.throttle.clear(key)
        return user

    def _auth(self, method: str, path: str) -> None:
        if path == "/api/auth/me":
            self._expect(method, "GET")
            user = self._current_user()
            if user is None:
                raise ApiError(401, "Please log in.")
            return self._json(200, self._user_payload(user))
        if path == "/api/auth/logout":
            self._expect(method, "POST")
            token = self._session_token()
            if token:
                self.server.sessions.destroy(token)
            return self._json(200, {"ok": True}, [self._cookie("", 0)])
        if path in ("/api/auth/signup", "/api/auth/login"):
            self._expect(method, "POST")
            data = self._read_json()
            username, password = data.get("username"), data.get("password")
            if path.endswith("signup"):
                user, status = self.server.users.create(username, password), 201
            else:
                user, status = self._login(username, password), 200
            token = self.server.sessions.create(user["id"], user["session_epoch"])
            return self._json(status, self._user_payload(user),
                              [self._cookie(token, SESSION_SECONDS)])
        if path == "/api/auth/change-password":
            self._expect(method, "POST")
            user = self._current_user()
            if user is None:
                raise ApiError(401, "Please log in.")
            data = self._read_json()
            current, new = data.get("current_password"), data.get("new_password")
            key = f"change:{user['id']}"
            wait = self.server.throttle.blocked_for(key)
            if wait:
                raise ApiError(429, f"Too many wrong passwords. Try again in {wait} seconds.")
            if not isinstance(current, str) or not verify_password(current, user["password_hash"]):
                self.server.throttle.fail(key)
                raise ApiError(400, "Your current password is not right.")
            self.server.throttle.clear(key)
            if isinstance(new, str) and new == current:
                raise ApiError(400, "The new password must be different from the current one.")
            updated = self.server.users.set_password(user["id"], new)
            # This browser stays logged in; every other session ends.
            old = self._session_token()
            if old:
                self.server.sessions.destroy(old)
            token = self.server.sessions.create(updated["id"], updated["session_epoch"])
            return self._json(200, {"ok": True}, [self._cookie(token, SESSION_SECONDS)])
        if path == "/api/auth/forgot":
            self._expect(method, "POST")
            data = self._read_json()
            username = data.get("username")
            if not isinstance(username, str) or not username.strip():
                raise ApiError(400, "Enter your username.")
            user = self.server.users.by_username(username)
            if user is not None:
                code = self.server.resets.issue(user["username"].lower())
                if code is not None:
                    self.server.announce(user["username"], self.server.resets.pretty(code),
                                         self.server.resets.ttl)
            # Same answer whether or not the account exists, so nobody can probe for names.
            message = ("If that account exists, a reset code was printed in the window "
                       "where the StudentPlanner server is running. It works for 10 minutes.")
            return self._json(200, {"ok": True, "message": message})
        if path == "/api/auth/reset":
            self._expect(method, "POST")
            data = self._read_json()
            username, code, password = data.get("username"), data.get("code"), data.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                raise ApiError(400, "Username, code and new password are required.")
            user = self.server.users.by_username(username)
            key = user["username"].lower() if user else username.strip().lower()
            result = self.server.resets.check(key, code) if user else "none"
            if result == "locked":
                raise ApiError(429, "Too many wrong codes. Request a new code.")
            if result != "ok":
                raise ApiError(400, "That code is not right or has expired.")
            try:
                updated = self.server.users.set_password(user["id"], password)
            except (ValidationError, ApiError):
                # The code was spent by check(); issue it again so a typo in the new
                # password does not force a new code.
                self.server.resets.restore(key, code)
                raise
            self.server.throttle.clear(user["username"].lower())
            token = self.server.sessions.create(updated["id"], updated["session_epoch"])
            return self._json(200, self._user_payload(updated),
                              [self._cookie(token, SESSION_SECONDS)])
        raise ApiError(404, "Not found.")

    def _api(self, method: str, path: str, query: Dict[str, List[str]]) -> None:
        if path == "/api/health":
            self._expect(method, "GET")
            return self._json(200, {"status": "ok"})
        if path.startswith("/api/auth/"):
            return self._auth(method, path)

        user = self._current_user()
        if user is None:
            raise ApiError(401, "Please log in.")
        store: Store = self.server.store_for(user["id"])

        if path == "/api/tasks":
            if method == "GET":
                today = self._today(query)
                everything = store.all()
                tasks = planner.rank_tasks(everything, today)
                tasks += sorted((t for t in everything if t.done), key=lambda t: t.id)
                return self._json(200, [task_json(t, today) for t in tasks])
            if method == "POST":
                task = store.create(self._read_json())
                return self._json(201, task_json(task, self._today(query)))
            raise ApiError(405, "Method not allowed.")

        if path == "/api/today":
            self._expect(method, "GET")
            return self._json(200, today_payload(store.all(), self._today(query)))

        if path == "/api/summary":
            self._expect(method, "GET")
            return self._json(200, summary_payload(store.all(), self._today(query)))

        if path == "/api/settings":
            settings = Settings(self.server.db, user["id"])
            if method == "GET":
                return self._json(200, settings.get())
            if method in ("PUT", "PATCH"):
                return self._json(200, settings.update(self._read_json()))
            raise ApiError(405, "Method not allowed.")

        match = TASK_ROUTE.match(path)
        if not match:
            raise ApiError(404, "Not found.")
        task_id, action = int(match.group(1)), match.group(2)
        today = self._today(query)

        if action in ("done", "undo"):
            self._expect(method, "POST")
            task = store.set_done(task_id, action == "done", today)
            return self._json(200, task_json(task, today))
        if method == "GET":
            return self._json(200, task_json(store.get(task_id), today))
        if method in ("PUT", "PATCH"):
            task = store.update(task_id, self._read_json())
            return self._json(200, task_json(task, today))
        if method == "DELETE":
            store.delete(task_id)
            return self._json(200, {"deleted": task_id})
        raise ApiError(405, "Method not allowed.")

    @staticmethod
    def _expect(method: str, allowed: str) -> None:
        if method != allowed:
            raise ApiError(405, "Method not allowed.")

    def do_GET(self): self._handle("GET")
    def do_HEAD(self): self._handle("HEAD")
    def do_POST(self): self._handle("POST")
    def do_PUT(self): self._handle("PUT")
    def do_PATCH(self): self._handle("PATCH")
    def do_DELETE(self): self._handle("DELETE")


def open_database(data_dir: str) -> Tuple[Database, Optional[str]]:
    """Create the data folder and database, moving old JSON data in if needed."""
    os.makedirs(data_dir, exist_ok=True)
    db = Database(os.path.join(data_dir, "studplan.db"))
    return db, migrate_legacy_json(db, data_dir)


class StudPlanServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, data_dir: str, port: int, static_dir: Optional[str],
                 today: Optional[date], verbose: bool, host: str = "127.0.0.1"):
        # Open the database first so a bad file fails before the port is taken.
        self.db, self.migration_message = open_database(data_dir)
        super().__init__((host, port), Handler)
        hosting = hosting_settings()
        self.allowed_hosts = hosting["allowed_hosts"]
        self.secure_cookie = hosting["secure_cookie"]
        self.online = hosting["online"]
        self.data_dir = data_dir
        self.users = Users(self.db)
        self.sessions = Sessions()
        self.throttle = LoginThrottle()
        self.resets = ResetCodes()
        self.static_dir = static_dir or os.path.dirname(os.path.abspath(__file__))
        self.today_override = today
        self.verbose = verbose

    def announce(self, username: str, code: str, seconds: int) -> None:
        """Show a reset code in the terminal running the server. Tests replace this method to capture the code."""
        print(f"Password reset code for '{username}': {code}  "
              f"(valid {seconds // 60} minutes)", flush=True)

    def store_for(self, user_id: int) -> Store:
        """A Store limited to one student, so nobody sees anyone else's tasks."""
        return Store(self.db, user_id)


def make_server(data_dir: str = "data", port: int = 8000,
                static_dir: Optional[str] = None, today: Optional[date] = None,
                verbose: bool = True, host: str = "127.0.0.1") -> StudPlanServer:
    """Create (but do not start) a server (127.0.0.1 unless a host is given)."""
    return StudPlanServer(data_dir, port, static_dir, today, verbose, host)


def _find_user(db: Database, username: str) -> Dict[str, Any]:
    user = Users(db).by_username(username)
    if user is None:
        raise ValidationError(f"No account named '{username}'.")
    return user


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="StudentPlanner backend server.")
    p.add_argument("--data-dir", default=os.environ.get("STUDPLAN_DATA_DIR") or "data",
                   help="folder for the database (default: data, or STUDPLAN_DATA_DIR)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT") or os.environ.get("STUDPLAN_PORT") or 8000))
    p.add_argument("--host", default=os.environ.get("STUDPLAN_HOST") or "127.0.0.1",
                   help="address to listen on (default 127.0.0.1; use 0.0.0.0 when hosting)")
    sub = p.add_subparsers(dest="command")
    ex = sub.add_parser("export", help="write an account's tasks to a planner.py JSON file")
    ex.add_argument("username")
    ex.add_argument("file")
    im = sub.add_parser("import", help="add tasks from a planner.py JSON file to an account")
    im.add_argument("username")
    im.add_argument("file")
    rp = sub.add_parser("reset-password", help="set a new password for an account")
    rp.add_argument("username")
    args = p.parse_args(argv)

    try:
        if args.command in ("export", "import"):
            db, message = open_database(args.data_dir)
            if message:
                print(message)
            user = _find_user(db, args.username)
            if args.command == "export":
                tasks = export_tasks(db, user["id"])
                planner.save_tasks(args.file, tasks)
                print(f"Wrote {len(tasks)} task(s) from '{user['username']}' to {args.file}")
            else:
                tasks = planner.load_tasks(args.file)
                count = import_tasks(db, user["id"], tasks)
                print(f"Added {count} task(s) from {args.file} to '{user['username']}'")
            return 0

        if args.command == "reset-password":
            db, message = open_database(args.data_dir)
            if message:
                print(message)
            user = _find_user(db, args.username)
            first = getpass.getpass(f"New password for '{user['username']}': ")
            if first != getpass.getpass("Type it again: "):
                raise ValidationError("The two passwords do not match.")
            Users(db).set_password(user["id"], first)
            print("Password changed. Everyone logged in as that account was logged out.")
            return 0

        server = make_server(args.data_dir, args.port, host=args.host)
    except (ValidationError, ApiError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if server.migration_message:
        print(server.migration_message)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") and not server.online else args.host
    print(f"StudentPlanner running at http://{shown}:{args.port}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        if not server.online:
            print("Note: listening on a public address, but no site name is set. Set STUDPLAN_ALLOWED_HOSTS "
                  "to your site's address (for example myapp.onrender.com) or requests will be refused.")
        if not server.secure_cookie:
            print("Warning: serve this over HTTPS and set STUDPLAN_SECURE_COOKIE=1.")
    print(f"Database: {os.path.abspath(server.db.path)}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
