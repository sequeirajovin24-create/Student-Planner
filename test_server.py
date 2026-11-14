import http.client
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import date

import planner
import server

HERE = os.path.dirname(os.path.abspath(__file__))
TODAY = "2026-11-03"
NEW_TASK = {"title": "Essay", "subject": "English", "deadline": "2026-11-09",
            "hours": 3, "priority": 4}
PASSWORD = "correct horse"


class ApiTestCase(unittest.TestCase):
    AUTO_LOGIN = True  # sign up "alice" before each test

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.dir, "data")
        self.cookie = None
        with open(os.path.join(self.dir, "index.html"), "w") as f:
            f.write("<h1>test page</h1>")
        self.srv = server.make_server(self.data_dir, port=0, static_dir=self.dir,
                                      today=date(2026, 11, 3), verbose=False)
        self.port = self.srv.server_address[1]
        self.codes = []  # reset codes the server would print: (username, code)
        self.srv.announce = lambda user, code, secs: self.codes.append((user, code))
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        if self.AUTO_LOGIN:
            status, _ = self.signup("alice")
            assert status == 201, status

    def signup(self, username, password=PASSWORD):
        return self.call("POST", "/api/auth/signup",
                         {"username": username, "password": password})

    def login(self, username, password=PASSWORD):
        return self.call("POST", "/api/auth/login",
                         {"username": username, "password": password})

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.dir)

    def load_sample(self, user_id=1):
        tasks = planner.load_tasks(os.path.join(HERE, "sample_tasks.json"))
        server.import_tasks(self.srv.db, user_id, tasks)

    def call(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = dict(headers or {})
        if self.cookie and "Cookie" not in hdrs:
            hdrs["Cookie"] = self.cookie
        data = raw
        if body is not None:
            data = json.dumps(body)
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        self.last_headers = resp.getheaders()
        for name, value in self.last_headers:
            if name.lower() == "set-cookie":
                pair = value.split(";", 1)[0]
                self.cookie = pair if not pair.endswith("=") else None
        conn.close()
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = payload.decode("utf-8", "replace")
        return resp.status, parsed


class TestCrud(ApiTestCase):
    def test_health(self):
        status, body = self.call("GET", "/api/health")
        self.assertEqual((status, body["status"]), (200, "ok"))

    def test_empty_list_when_no_file(self):
        self.assertEqual(self.call("GET", "/api/tasks"), (200, []))

    def test_create_and_list(self):
        status, task = self.call("POST", "/api/tasks", NEW_TASK)
        self.assertEqual(status, 201)
        self.assertEqual(task["id"], 1)
        self.assertEqual(task["days_left"], 6)
        self.assertAlmostEqual(task["urgency"], 4 * (3 / 6), places=2)
        status, tasks = self.call("GET", "/api/tasks")
        self.assertEqual([t["title"] for t in tasks], ["Essay"])

    def test_create_strips_text_and_assigns_increasing_ids(self):
        self.call("POST", "/api/tasks", dict(NEW_TASK, title="  A  "))
        _, second = self.call("POST", "/api/tasks", dict(NEW_TASK, title="B"))
        self.assertEqual(second["id"], 2)
        _, first = self.call("GET", "/api/tasks/1")
        self.assertEqual(first["title"], "A")

    def test_update_is_partial(self):
        self.call("POST", "/api/tasks", NEW_TASK)
        status, task = self.call("PUT", "/api/tasks/1", {"hours": 6, "priority": 5})
        self.assertEqual(status, 200)
        self.assertEqual((task["hours"], task["priority"], task["title"]), (6, 5, "Essay"))

    def test_update_validates_merged_result(self):
        self.call("POST", "/api/tasks", NEW_TASK)
        status, body = self.call("PUT", "/api/tasks/1", {"deadline": "soon"})
        self.assertEqual(status, 400)
        self.assertIn("date", body["error"].lower())
        self.assertEqual(self.call("GET", "/api/tasks/1")[1]["deadline"], "2026-11-09")

    def test_update_rejects_unknown_and_protected_fields(self):
        self.call("POST", "/api/tasks", NEW_TASK)
        for field in ("done", "id", "nonsense"):
            status, _ = self.call("PUT", "/api/tasks/1", {field: 1})
            self.assertEqual(status, 400, field)

    def test_delete(self):
        self.call("POST", "/api/tasks", NEW_TASK)
        self.assertEqual(self.call("DELETE", "/api/tasks/1"), (200, {"deleted": 1}))
        self.assertEqual(self.call("GET", "/api/tasks/1")[0], 404)

    def test_missing_task_returns_404(self):
        for method in ("GET", "DELETE"):
            self.assertEqual(self.call(method, "/api/tasks/99")[0], 404)
        self.assertEqual(self.call("PUT", "/api/tasks/99", {"hours": 2})[0], 404)
        self.assertEqual(self.call("POST", "/api/tasks/99/done")[0], 404)


class TestValidation(ApiTestCase):
    def test_bad_create_bodies(self):
        bad = {
            "missing title": {k: v for k, v in NEW_TASK.items() if k != "title"},
            "empty title": dict(NEW_TASK, title="   "),
            "empty subject": dict(NEW_TASK, subject=""),
            "bad date": dict(NEW_TASK, deadline="09-11-2026"),
            "impossible date": dict(NEW_TASK, deadline="2026-02-30"),
            "zero hours": dict(NEW_TASK, hours=0),
            "negative hours": dict(NEW_TASK, hours=-2),
            "text hours": dict(NEW_TASK, hours="3"),
            "bool hours": dict(NEW_TASK, hours=True),
            "priority 6": dict(NEW_TASK, priority=6),
            "priority 0": dict(NEW_TASK, priority=0),
            "float priority": dict(NEW_TASK, priority=3.5),
            "bool priority": dict(NEW_TASK, priority=True),
            "list title": dict(NEW_TASK, title=["x"]),
            "long title": dict(NEW_TASK, title="x" * 201),
            "long subject": dict(NEW_TASK, subject="x" * 81),
        }
        for name, body in bad.items():
            with self.subTest(name):
                status, payload = self.call("POST", "/api/tasks", body)
                self.assertEqual(status, 400)
                self.assertIn("error", payload)
        self.assertEqual(self.call("GET", "/api/tasks"), (200, []))

    def test_invalid_json_and_wrong_shapes(self):
        h = {"Content-Type": "application/json"}
        self.assertEqual(self.call("POST", "/api/tasks", raw="{nope", headers=h)[0], 400)
        self.assertEqual(self.call("POST", "/api/tasks", raw="[1,2]", headers=h)[0], 400)

    def test_wrong_content_type_rejected(self):
        status, _ = self.call("POST", "/api/tasks", raw=json.dumps(NEW_TASK),
                              headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_oversized_body_rejected(self):
        big = json.dumps(dict(NEW_TASK, title="x" * (70 * 1024)))
        status, _ = self.call("POST", "/api/tasks", raw=big,
                              headers={"Content-Type": "application/json"})
        self.assertEqual(status, 413)

    def test_bad_today_parameter(self):
        self.assertEqual(self.call("GET", "/api/today?today=yesterday")[0], 400)


class TestDone(ApiTestCase):
    def test_done_and_undo(self):
        self.call("POST", "/api/tasks", NEW_TASK)
        status, task = self.call("POST", f"/api/tasks/1/done?today={TODAY}")
        self.assertEqual(status, 200)
        self.assertTrue(task["done"])
        self.assertEqual(task["completed_on"], TODAY)
        self.assertIsNone(task["urgency"])
        _, task = self.call("POST", "/api/tasks/1/undo")
        self.assertFalse(task["done"])
        self.assertIsNone(task["completed_on"])

    def test_done_tasks_listed_after_pending(self):
        self.load_sample()
        _, tasks = self.call("GET", f"/api/tasks?today={TODAY}")
        flags = [t["done"] for t in tasks]
        self.assertEqual(flags, sorted(flags))  # all False first, then True


class TestViews(ApiTestCase):
    def test_tasks_ranked_by_urgency(self):
        self.load_sample()
        _, tasks = self.call("GET", f"/api/tasks?today={TODAY}")
        pending = [t for t in tasks if not t["done"]]
        self.assertEqual([t["id"] for t in pending], [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual([t["urgency"] for t in pending[:3]], [64.0, 12.0, 10.0])

    def test_today_matches_cli_demo(self):
        self.load_sample()
        status, body = self.call("GET", f"/api/today?today={TODAY}")
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in body["focus"]], [1, 2, 3])
        self.assertEqual([t["id"] for t in body["overdue"]], [1])
        self.assertEqual(body["overloaded"], [{"date": "2026-11-09", "hours": 9.0}])

    def test_summary(self):
        self.load_sample()
        status, body = self.call("GET", f"/api/summary?today={TODAY}")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(t["id"] for t in body["completed"]), [8, 9])
        self.assertEqual([t["id"] for t in body["missed"]], [1])
        self.assertEqual(body["hours_completed"], 4)
        self.assertEqual(body["hours_planned"], 18)

    def test_server_today_override_used_without_query(self):
        self.load_sample()
        _, body = self.call("GET", "/api/today")
        self.assertEqual(body["date"], TODAY)


class TestDatabase(ApiTestCase):
    def test_concurrent_creates_get_unique_ids(self):
        results = []

        def make():
            results.append(self.call("POST", "/api/tasks", NEW_TASK)[1]["id"])

        threads = [threading.Thread(target=make) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), list(range(1, 13)))
        self.assertEqual(len(self.call("GET", "/api/tasks")[1]), 12)

    def test_deleted_ids_are_not_reused(self):
        self.call("POST", "/api/tasks", NEW_TASK)
        self.call("DELETE", "/api/tasks/1")
        _, second = self.call("POST", "/api/tasks", NEW_TASK)
        self.assertEqual(second["id"], 2)

    def test_quotes_and_sql_text_are_stored_as_plain_text(self):
        evil = "x'); DROP TABLE tasks;--"
        status, task = self.call("POST", "/api/tasks", dict(NEW_TASK, title=evil, subject="O'Brien"))
        self.assertEqual(status, 201)
        _, tasks = self.call("GET", "/api/tasks")
        self.assertEqual((tasks[0]["title"], tasks[0]["subject"]), (evil, "O'Brien"))
        self.assertEqual(self.call("GET", "/api/tasks/" + str(task["id"]) + "?today=2026-11-03")[0], 200)

    def test_unicode_text_round_trips(self):
        self.call("POST", "/api/tasks", dict(NEW_TASK, title="Física: ley de Ohm ⚡", subject="Ciencias"))
        self.assertEqual(self.call("GET", "/api/tasks")[1][0]["title"], "Física: ley de Ohm ⚡")

    def test_data_survives_a_server_restart(self):
        self.call("POST", "/api/tasks", NEW_TASK)
        self.srv.shutdown()
        self.srv.server_close()
        self.srv = server.make_server(self.data_dir, port=0, static_dir=self.dir,
                                      today=date(2026, 11, 3), verbose=False)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()
        self.cookie = None  # sessions are in memory, so log in again
        self.assertEqual(self.call("GET", "/api/tasks")[0], 401)
        self.assertEqual(self.login("alice")[0], 200)
        self.assertEqual([t["title"] for t in self.call("GET", "/api/tasks")[1]], ["Essay"])

    def test_database_rejects_bad_rows_even_without_the_api(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.srv.db.tx() as c:
                c.execute("INSERT INTO tasks (user_id, title, subject, deadline, hours, priority)"
                          " VALUES (1, 'a', 'b', '2026-11-10', 2, 9)")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.srv.db.tx() as c:
                c.execute("INSERT INTO tasks (user_id, title, subject, deadline, hours, priority)"
                          " VALUES (999, 'a', 'b', '2026-11-10', 2, 3)")  # no such user

    def test_deleting_a_user_removes_their_tasks(self):
        self.call("POST", "/api/tasks", NEW_TASK)
        with self.srv.db.tx() as c:
            c.execute("DELETE FROM users WHERE id = 1")
        with self.srv.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)

    def test_corrupt_database_file_gives_a_clear_error(self):
        bad_dir = os.path.join(self.dir, "bad")
        os.makedirs(bad_dir)
        with open(os.path.join(bad_dir, "studplan.db"), "wb") as f:
            f.write(b"this is not a sqlite database " * 20)
        with self.assertRaises(planner.ValidationError) as ctx:
            server.make_server(bad_dir, port=0, verbose=False)
        self.assertIn("studplan.db", str(ctx.exception))


class TestExportImport(ApiTestCase):
    def test_export_matches_the_planner_file_format(self):
        self.load_sample()
        out = os.path.join(self.dir, "out.json")
        self.assertEqual(server.main(["--data-dir", self.data_dir, "export", "alice", out]), 0)
        tasks = planner.load_tasks(out)
        self.assertEqual([t.id for t in tasks], list(range(1, 10)))
        self.assertEqual(tasks[0].title, "Physics lab report 3")
        self.assertTrue(tasks[7].done)

    def test_exported_file_works_with_the_cli(self):
        self.load_sample()
        out = os.path.join(self.dir, "out.json")
        server.main(["--data-dir", self.data_dir, "export", "alice", out])
        self.assertEqual(planner.main(["--file", out, "--today", TODAY, "warnings"]), 0)

    def test_import_adds_cli_tasks_to_the_account(self):
        cli_file = os.path.join(self.dir, "cli.json")
        planner.main(["--file", cli_file, "add", "Lab", "-s", "Physics",
                      "-d", "2026-11-12", "-H", "4", "-p", "5"])
        self.assertEqual(server.main(["--data-dir", self.data_dir, "import", "ALICE", cli_file]), 0)
        _, tasks = self.call("GET", "/api/tasks")
        self.assertEqual([(t["title"], t["priority"]) for t in tasks], [("Lab", 5)])

    def test_import_is_all_or_nothing(self):
        bad = os.path.join(self.dir, "bad.json")
        with open(bad, "w") as f:
            json.dump([
                {"id": 1, "title": "Good", "subject": "A", "deadline": "2026-11-10",
                 "hours": 2, "priority": 3, "done": False, "completed_on": None},
                {"id": 2, "title": "Bad", "subject": "A", "deadline": "2026-11-10",
                 "hours": 2, "priority": 9, "done": False, "completed_on": None}], f)
        self.assertEqual(server.main(["--data-dir", self.data_dir, "import", "alice", bad]), 1)
        self.assertEqual(self.call("GET", "/api/tasks"), (200, []))

    def test_unknown_user_is_reported(self):
        out = os.path.join(self.dir, "x.json")
        self.assertEqual(server.main(["--data-dir", self.data_dir, "export", "nobody", out]), 1)
        self.assertFalse(os.path.exists(out))

    def test_import_keeps_done_state(self):
        self.load_sample()
        _, tasks = self.call("GET", f"/api/tasks?today={TODAY}")
        done = [t for t in tasks if t["done"]]
        self.assertEqual(sorted(t["id"] for t in done), [8, 9])
        self.assertEqual(done[0]["completed_on"] is not None, True)


class TestMigrationFromJson(unittest.TestCase):
    """Data created by the previous (JSON file) version moves into the database."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.data = os.path.join(self.dir, "data")
        os.makedirs(self.data)
        users = [
            {"id": 1, "username": "alice", "password_hash": server.hash_password(PASSWORD),
             "created": "2026-11-01"},
            {"id": 2, "username": "bob", "password_hash": server.hash_password("bob password"),
             "created": "2026-11-02"},
        ]
        with open(os.path.join(self.data, "users.json"), "w") as f:
            json.dump(users, f)
        shutil.copy(os.path.join(HERE, "sample_tasks.json"), os.path.join(self.data, "tasks_1.json"))
        # bob has no tasks file at all

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_accounts_passwords_and_tasks_are_moved(self):
        db, message = server.open_database(self.data)
        self.assertIn("2 account", message)
        users = server.Users(db)
        self.assertIsNotNone(users.authenticate("alice", PASSWORD))
        self.assertIsNotNone(users.authenticate("BOB", "bob password"))
        self.assertEqual(len(server.Store(db, 1).all()), 9)
        self.assertEqual(server.Store(db, 2).all(), [])

    def test_old_files_are_kept_as_backups(self):
        server.open_database(self.data)
        names = sorted(os.listdir(self.data))
        self.assertIn("users.json.migrated", names)
        self.assertIn("tasks_1.json.migrated", names)
        self.assertNotIn("users.json", names)

    def test_migration_runs_only_once(self):
        server.open_database(self.data)
        _, message = server.open_database(self.data)
        self.assertIsNone(message)

    def test_migrated_user_can_log_in_over_http(self):
        srv = server.make_server(self.data, port=0, verbose=False)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request("POST", "/api/auth/login",
                         json.dumps({"username": "alice", "password": PASSWORD}),
                         {"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            cookie = resp.getheader("Set-Cookie").split(";")[0]
            resp.read()
            conn.request("GET", "/api/tasks", headers={"Cookie": cookie})
            self.assertEqual(len(json.loads(conn.getresponse().read())), 9)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_bad_legacy_data_changes_nothing(self):
        with open(os.path.join(self.data, "tasks_1.json"), "w") as f:
            f.write("{broken")
        with self.assertRaises(planner.ValidationError):
            server.open_database(self.data)
        self.assertTrue(os.path.exists(os.path.join(self.data, "users.json")))
        self.assertTrue(os.path.exists(os.path.join(self.data, "tasks_1.json")))
        with server.Database(os.path.join(self.data, "studplan.db")).read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_existing_database_is_not_overwritten_by_old_files(self):
        db, _ = server.open_database(os.path.join(self.dir, "other"))
        server.Users(db).create("carol", "carol password")
        shutil.copy(os.path.join(self.data, "users.json"), os.path.join(self.dir, "other", "users.json"))
        _, message = server.open_database(os.path.join(self.dir, "other"))
        self.assertIsNone(message)
        self.assertEqual(server.Users(db).by_username("alice"), None)


class TestDueTime(ApiTestCase):
    def test_create_with_and_without_time(self):
        _, with_time = self.call("POST", "/api/tasks", dict(NEW_TASK, due_time="17:30"))
        _, without = self.call("POST", "/api/tasks", NEW_TASK)
        _, empty = self.call("POST", "/api/tasks", dict(NEW_TASK, due_time=""))
        self.assertEqual(with_time["due_time"], "17:30")
        self.assertIsNone(without["due_time"])
        self.assertIsNone(empty["due_time"])

    def test_bad_times_rejected(self):
        for value in ("24:00", "7:30", "17:60", "noon", "17:30:00", 1730, ["17:30"], True):
            with self.subTest(value=value):
                status, body = self.call("POST", "/api/tasks", dict(NEW_TASK, due_time=value))
                self.assertEqual(status, 400)
        self.assertEqual(self.call("GET", "/api/tasks"), (200, []))

    def test_update_sets_keeps_and_clears_time(self):
        self.call("POST", "/api/tasks", dict(NEW_TASK, due_time="17:30"))
        _, t = self.call("PUT", "/api/tasks/1", {"title": "Renamed"})
        self.assertEqual(t["due_time"], "17:30")                      # untouched
        _, t = self.call("PUT", "/api/tasks/1", {"due_time": "08:00"})
        self.assertEqual(t["due_time"], "08:00")
        _, t = self.call("PUT", "/api/tasks/1", {"due_time": None})
        self.assertIsNone(t["due_time"])                              # cleared
        self.assertEqual(self.call("PUT", "/api/tasks/1", {"due_time": "99:99"})[0], 400)

    def test_time_survives_export_and_import(self):
        self.call("POST", "/api/tasks", dict(NEW_TASK, due_time="17:30"))
        out = os.path.join(self.dir, "out.json")
        server.main(["--data-dir", self.data_dir, "export", "alice", out])
        self.assertEqual(planner.load_tasks(out)[0].due_time, "17:30")
        self.call("DELETE", "/api/tasks/1")
        server.main(["--data-dir", self.data_dir, "import", "alice", out])
        self.assertEqual(self.call("GET", "/api/tasks")[1][0]["due_time"], "17:30")

    def test_database_from_before_due_times_is_upgraded_in_place(self):
        old_dir = os.path.join(self.dir, "old")
        os.makedirs(old_dir)
        path = os.path.join(old_dir, "studplan.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL,
                password_hash TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
                title TEXT NOT NULL, subject TEXT NOT NULL, deadline TEXT NOT NULL,
                hours REAL NOT NULL, priority INTEGER NOT NULL,
                done INTEGER NOT NULL DEFAULT 0, completed_on TEXT);
            INSERT INTO users (username, password_hash, created) VALUES ('old', 'x', '2026-01-01');
            INSERT INTO tasks (user_id, title, subject, deadline, hours, priority)
                VALUES (1, 'Kept', 'S', '2026-11-10', 2, 3);
        """)
        conn.commit()
        conn.close()
        for _ in range(2):  # opening twice must be harmless
            db = server.Database(path)
        task = server.Store(db, 1).all()[0]
        self.assertEqual((task.title, task.due_time), ("Kept", None))
        server.Store(db, 1).update(task.id, {"due_time": "10:00"})
        self.assertEqual(server.Store(db, 1).get(task.id).due_time, "10:00")
        self.assertEqual(server.Settings(db, 1).get(), server.DEFAULT_GOALS)


class TestSettings(ApiTestCase):
    def test_defaults(self):
        self.assertEqual(self.call("GET", "/api/settings"), (200, {"daily_goal": 2, "weekly_goal": 8}))

    def test_partial_and_full_updates_persist(self):
        self.assertEqual(self.call("PUT", "/api/settings", {"daily_goal": 5})[1],
                         {"daily_goal": 5, "weekly_goal": 8})
        self.assertEqual(self.call("PUT", "/api/settings", {"weekly_goal": 20, "daily_goal": 3})[1],
                         {"daily_goal": 3, "weekly_goal": 20})
        self.assertEqual(self.call("GET", "/api/settings")[1], {"daily_goal": 3, "weekly_goal": 20})

    def test_validation(self):
        for body in ({"daily_goal": 0}, {"daily_goal": 21}, {"weekly_goal": 101},
                     {"weekly_goal": 0}, {"daily_goal": "3"}, {"daily_goal": 3.5},
                     {"daily_goal": True}, {"daily_goal": None}, {"colour": "red"}):
            with self.subTest(body=body):
                self.assertEqual(self.call("PUT", "/api/settings", body)[0], 400)
        self.assertEqual(self.call("GET", "/api/settings")[1], {"daily_goal": 2, "weekly_goal": 8})

    def test_goal_limits_are_inclusive(self):
        self.assertEqual(self.call("PUT", "/api/settings", {"daily_goal": 1, "weekly_goal": 100})[0], 200)
        self.assertEqual(self.call("PUT", "/api/settings", {"daily_goal": 20, "weekly_goal": 1})[0], 200)

    def test_each_student_has_their_own_goals(self):
        self.call("PUT", "/api/settings", {"daily_goal": 9})
        self.cookie = None
        self.signup("bob")
        self.assertEqual(self.call("GET", "/api/settings")[1]["daily_goal"], 2)
        self.call("PUT", "/api/settings", {"daily_goal": 4})
        self.assertEqual(server.Settings(self.srv.db, 1).get()["daily_goal"], 9)

    def test_needs_login_and_correct_methods(self):
        self.assertEqual(self.call("POST", "/api/settings", {})[0], 405)
        self.assertEqual(self.call("DELETE", "/api/settings")[0], 405)
        self.cookie = None
        self.assertEqual(self.call("GET", "/api/settings")[0], 401)
        self.assertEqual(self.call("PUT", "/api/settings", {"daily_goal": 3})[0], 401)

    def test_settings_removed_with_their_user(self):
        self.call("PUT", "/api/settings", {"daily_goal": 7})
        with self.srv.db.tx() as c:
            c.execute("DELETE FROM users WHERE id = 1")
        with self.srv.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM user_settings").fetchone()[0], 0)


class TestRoutingAndSafety(ApiTestCase):
    def test_serves_index_page(self):
        self.cookie = None  # the page itself is public
        status, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("test page", body)

    def test_unknown_paths_404(self):
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)
        self.assertEqual(self.call("GET", "/../server.py")[0], 404)

    def test_wrong_methods_405(self):
        self.assertEqual(self.call("POST", "/api/today")[0], 405)
        self.assertEqual(self.call("GET", "/api/tasks/1/done")[0], 405)
        self.assertEqual(self.call("DELETE", "/api/tasks")[0], 405)

    def test_foreign_host_header_blocked(self):
        for host in ("evil.example.com", "evil.example.com:8000", "127.0.0.1.evil.com"):
            with self.subTest(host=host):
                status, _ = self.call("GET", "/api/tasks", headers={"Host": host})
                self.assertEqual(status, 403)

    def test_localhost_host_headers_allowed(self):
        for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000", "localhost"):
            with self.subTest(host=host):
                status, _ = self.call("GET", "/api/health", headers={"Host": host})
                self.assertEqual(status, 200)

    def test_server_binds_to_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")


class TestAccounts(ApiTestCase):
    AUTO_LOGIN = False

    def stored_hash(self, username):
        with self.srv.db.read() as c:
            return c.execute("SELECT password_hash FROM users WHERE username = ?",
                             (username,)).fetchone()[0]

    def test_signup_logs_in_and_sets_a_safe_cookie(self):
        status, body = self.signup("alice")
        self.assertEqual(status, 201)
        self.assertEqual(body["user"]["username"], "alice")
        cookie = dict(self.last_headers)["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.call("GET", "/api/auth/me")[1]["user"]["username"], "alice")

    def test_signup_validation(self):
        bad = {
            "short username": ("ab", PASSWORD),
            "long username": ("a" * 31, PASSWORD),
            "spaces in username": ("al ice", PASSWORD),
            "symbols in username": ("al<ice>", PASSWORD),
            "short password": ("alice", "1234567"),
            "long password": ("alice", "x" * 129),
            "password equals username": ("alice.smith", "ALICE.SMITH"),
        }
        for name, (user, pw) in bad.items():
            with self.subTest(name):
                self.assertEqual(self.signup(user, pw)[0], 400)
        for body in ({"username": 123, "password": PASSWORD}, {"username": "alice"}, {}):
            self.assertEqual(self.call("POST", "/api/auth/signup", body)[0], 400)
        with self.srv.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_duplicate_username_is_rejected_ignoring_case(self):
        self.signup("alice")
        self.assertEqual(self.signup("ALICE")[0], 409)

    def test_password_is_stored_hashed_not_in_plain_text(self):
        self.signup("alice")
        with open(os.path.join(self.data_dir, "studplan.db"), "rb") as f:
            raw = f.read()
        wal = os.path.join(self.data_dir, "studplan.db-wal")
        if os.path.exists(wal):
            with open(wal, "rb") as f:
                raw += f.read()
        self.assertNotIn(PASSWORD.encode(), raw)
        stored = self.stored_hash("alice")
        self.assertTrue(stored.startswith(("scrypt$", "pbkdf2$")))
        self.assertTrue(server.verify_password(PASSWORD, stored))
        self.assertFalse(server.verify_password("wrong password", stored))

    def test_same_password_gets_different_hashes(self):
        self.assertNotEqual(server.hash_password("same password"),
                            server.hash_password("same password"))

    def test_malformed_stored_hashes_never_verify(self):
        for bad in ("", "plain", "scrypt$1$2", "scrypt$x$y$z$zz$zz", "md5$aa$bb", None):
            self.assertFalse(server.verify_password("pw", bad), bad)

    def test_login_success_and_failure(self):
        self.signup("alice")
        self.call("POST", "/api/auth/logout")
        status, body = self.login("Alice")  # case-insensitive username
        self.assertEqual((status, body["user"]["username"]), (200, "alice"))
        self.call("POST", "/api/auth/logout")
        wrong = self.login("alice", "not the password")
        unknown = self.login("nobody", PASSWORD)
        self.assertEqual(wrong, unknown)  # same answer, so usernames cannot be probed
        self.assertEqual(wrong[0], 401)

    def test_login_with_non_text_values(self):
        self.signup("alice")
        for body in ({"username": ["alice"], "password": PASSWORD},
                     {"username": "alice", "password": 12345678}, {}):
            self.assertEqual(self.call("POST", "/api/auth/login", body)[0], 401)

    def test_logout_ends_the_session_for_good(self):
        self.signup("alice")
        old_cookie = self.cookie
        self.assertEqual(self.call("POST", "/api/auth/logout")[0], 200)
        self.assertIsNone(self.cookie)  # cookie cleared
        self.assertEqual(self.call("GET", "/api/tasks")[0], 401)
        # replaying the old token must fail too
        status, _ = self.call("GET", "/api/tasks", headers={"Cookie": old_cookie})
        self.assertEqual(status, 401)

    def test_protected_routes_need_login(self):
        self.signup("alice")
        self.call("POST", "/api/tasks", NEW_TASK)
        self.cookie = None
        for method, path in [("GET", "/api/tasks"), ("POST", "/api/tasks"),
                             ("GET", "/api/tasks/1"), ("PUT", "/api/tasks/1"),
                             ("DELETE", "/api/tasks/1"), ("POST", "/api/tasks/1/done"),
                             ("GET", "/api/today"), ("GET", "/api/summary"),
                             ("GET", "/api/auth/me")]:
            with self.subTest(path=path, method=method):
                body = NEW_TASK if method in ("POST", "PUT") else None
                self.assertEqual(self.call(method, path, body)[0], 401)
        self.assertEqual(self.call("GET", "/api/health")[0], 200)

    def test_garbage_and_forged_cookies_are_rejected(self):
        for cookie in ("studplan_session=abc", "studplan_session=", "nonsense", ";;;==",
                       "studplan_session=" + "A" * 43):
            status, _ = self.call("GET", "/api/tasks", headers={"Cookie": cookie})
            self.assertEqual(status, 401, cookie)

    def test_expired_session_is_rejected(self):
        self.signup("alice")
        for token, (uid, epoch, _) in list(self.srv.sessions._data.items()):
            self.srv.sessions._data[token] = (uid, epoch, 0)
        self.assertEqual(self.call("GET", "/api/tasks")[0], 401)

    def test_each_student_only_sees_their_own_tasks(self):
        self.signup("alice")
        self.call("POST", "/api/tasks", NEW_TASK)
        alice = self.cookie
        self.cookie = None
        self.signup("bob")
        self.assertEqual(self.call("GET", "/api/tasks"), (200, []))
        self.assertEqual(self.call("GET", "/api/tasks/1")[0], 404)
        self.assertEqual(self.call("PUT", "/api/tasks/1", {"hours": 9})[0], 404)
        self.assertEqual(self.call("DELETE", "/api/tasks/1")[0], 404)
        self.assertEqual(self.call("POST", "/api/tasks/1/done")[0], 404)
        self.call("POST", "/api/tasks", dict(NEW_TASK, title="Bob's task"))
        self.assertEqual([t["title"] for t in self.call("GET", "/api/today")[1]["focus"]],
                         ["Bob's task"])
        # alice's data is untouched
        self.cookie = alice
        _, tasks = self.call("GET", "/api/tasks")
        self.assertEqual([(t["title"], t["hours"]) for t in tasks], [("Essay", 3)])
        self.assertEqual([t.title for t in server.Store(self.srv.db, 1).all()], ["Essay"])
        self.assertEqual([t.title for t in server.Store(self.srv.db, 2).all()], ["Bob's task"])

    def test_user_cannot_pick_another_users_file_via_input(self):
        self.signup("alice")
        status, _ = self.call("POST", "/api/tasks", dict(NEW_TASK, user_id=2, id=7))
        self.assertEqual(status, 201)
        self.assertEqual(self.call("GET", "/api/tasks")[1][0]["id"], 1)

    def test_repeated_wrong_passwords_lock_the_username_out(self):
        self.signup("alice")
        self.call("POST", "/api/auth/logout")
        for _ in range(5):
            self.assertEqual(self.login("alice", "wrong password")[0], 401)
        status, body = self.login("alice")  # even the right password is refused
        self.assertEqual(status, 429)
        self.assertIn("Try again", body["error"])
        self.signup("bob")  # other people are not affected
        self.assertEqual(self.call("GET", "/api/auth/me")[0], 200)

    def test_successful_login_resets_the_failure_count(self):
        self.signup("alice")
        self.call("POST", "/api/auth/logout")
        for _ in range(4):
            self.login("alice", "wrong password")
        self.assertEqual(self.login("alice")[0], 200)
        self.call("POST", "/api/auth/logout")
        for _ in range(4):
            self.assertEqual(self.login("alice", "wrong password")[0], 401)

    def test_login_endpoints_require_post(self):
        self.assertEqual(self.call("GET", "/api/auth/login")[0], 405)
        self.assertEqual(self.call("GET", "/api/auth/signup")[0], 405)
        self.assertEqual(self.call("GET", "/api/auth/logout")[0], 405)
        self.assertEqual(self.call("POST", "/api/auth/me")[0], 405)
        self.assertEqual(self.call("POST", "/api/auth/unknown", {})[0], 404)

    def test_security_headers_present(self):
        self.signup("alice")
        names = {k.lower() for k, _ in self.last_headers}
        self.assertTrue({"x-frame-options", "x-content-type-options", "cache-control"} <= names)

    def test_simultaneous_signups_for_one_username_create_one_account(self):
        results = []

        def go():
            results.append(self.signup("carol", "carol password")[0])

        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [201] + [409] * 7)

    def test_sql_in_credentials_is_harmless(self):
        self.signup("alice")
        self.call("POST", "/api/auth/logout")
        self.assertEqual(self.login("alice' OR '1'='1", "x' OR '1'='1")[0], 401)
        self.assertEqual(self.signup("a'; DROP TABLE users;--", PASSWORD)[0], 400)
        self.assertEqual(self.login("alice")[0], 200)


class TestPasswords(ApiTestCase):
    NEW = "brand new pass"

    def change(self, current=PASSWORD, new=NEW):
        return self.call("POST", "/api/auth/change-password",
                         {"current_password": current, "new_password": new})

    def forgot(self, username="alice"):
        return self.call("POST", "/api/auth/forgot", {"username": username})

    def reset(self, code, password=NEW, username="alice"):
        return self.call("POST", "/api/auth/reset",
                         {"username": username, "code": code, "password": password})

    def fresh_client(self):
        """Forget this browser's cookie (a different device)."""
        old, self.cookie = self.cookie, None
        return old

    # change password
    def test_change_password_works_and_keeps_this_session(self):
        status, _ = self.change()
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/auth/me")[0], 200)
        self.fresh_client()
        self.assertEqual(self.login("alice", PASSWORD)[0], 401)
        self.assertEqual(self.login("alice", self.NEW)[0], 200)

    def test_change_password_logs_out_other_devices(self):
        first = self.fresh_client()
        self.login("alice")                     # second device
        second = self.cookie
        self.cookie = first
        self.assertEqual(self.change()[0], 200)
        self.cookie = second
        self.assertEqual(self.call("GET", "/api/auth/me")[0], 401)
        self.assertEqual(self.call("GET", "/api/tasks")[0], 401)

    def test_change_password_needs_login(self):
        self.fresh_client()
        self.assertEqual(self.change()[0], 401)

    def test_change_password_wrong_current(self):
        status, body = self.change(current="not my password")
        self.assertEqual(status, 400)
        self.assertIn("current password", body["error"])
        self.fresh_client()
        self.assertEqual(self.login("alice")[0], 200)   # nothing changed

    def test_change_password_validation(self):
        self.assertEqual(self.change(new="short")[0], 400)
        self.assertEqual(self.change(new="alice")[0], 400)     # same as username
        self.assertEqual(self.change(new=PASSWORD)[0], 400)    # same as current
        self.assertEqual(self.change(new=12345678)[0], 400)    # not text
        self.assertEqual(self.call("POST", "/api/auth/change-password", {})[0], 400)

    def test_change_password_wrong_guesses_are_limited(self):
        for _ in range(5):
            self.assertEqual(self.change(current="nope nope nope")[0], 400)
        self.assertEqual(self.change(current="nope nope nope")[0], 429)
        self.assertEqual(self.change()[0], 429)   # even the right one waits

    def test_password_is_stored_hashed(self):
        self.change()
        with self.srv.db.read() as c:
            stored = c.execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertNotIn(self.NEW, stored)
        self.assertTrue(server.verify_password(self.NEW, stored))

    # forgot / reset
    def test_forgot_prints_a_code_and_reset_works(self):
        self.fresh_client()
        status, body = self.forgot()
        self.assertEqual(status, 200)
        self.assertEqual(len(self.codes), 1)
        user, code = self.codes[0]
        self.assertEqual(user, "alice")
        self.assertRegex(code, r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertNotIn(code, json.dumps(body))      # never sent to the browser
        status, body = self.reset(code)
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["username"], "alice")
        self.assertEqual(self.call("GET", "/api/auth/me")[0], 200)   # logged in
        self.fresh_client()
        self.assertEqual(self.login("alice", PASSWORD)[0], 401)
        self.assertEqual(self.login("alice", self.NEW)[0], 200)

    def test_code_accepts_lowercase_and_no_dash(self):
        self.forgot()
        code = self.codes[0][1]
        self.assertEqual(self.reset(code.lower().replace("-", " "))[0], 200)

    def test_reset_logs_out_old_sessions(self):
        old = self.cookie
        self.fresh_client()
        self.forgot()
        self.reset(self.codes[0][1])
        self.cookie = old
        self.assertEqual(self.call("GET", "/api/auth/me")[0], 401)

    def test_code_works_once_only(self):
        self.forgot()
        code = self.codes[0][1]
        self.assertEqual(self.reset(code)[0], 200)
        self.assertEqual(self.reset(code, password="another pass 1")[0], 400)

    def test_wrong_code_is_rejected_and_locks_after_five(self):
        self.forgot()
        code = self.codes[0][1]
        for _ in range(4):
            self.assertEqual(self.reset("AAAA-AAAA")[0], 400)
        self.assertEqual(self.reset("AAAA-AAAA")[0], 429)      # 5th wrong guess
        self.assertEqual(self.reset(code)[0], 400)             # the real one is gone too
        self.forgot()                                          # a new code is allowed
        self.assertEqual(self.reset(self.codes[-1][1])[0], 200)

    def test_new_code_replaces_the_old_one(self):
        self.forgot()
        first = self.codes[0][1]
        self.forgot()
        second = self.codes[1][1]
        self.assertNotEqual(first, second)
        self.assertEqual(self.reset(first)[0], 400)
        self.assertEqual(self.reset(second)[0], 200)

    def test_code_expires(self):
        self.forgot()
        code = self.codes[0][1]
        for entry in self.srv.resets._codes.values():
            entry["expires"] = 0
        self.assertEqual(self.reset(code)[0], 400)

    def test_bad_new_password_does_not_spend_the_code(self):
        self.forgot()
        code = self.codes[0][1]
        self.assertEqual(self.reset(code, password="short")[0], 400)
        self.assertEqual(self.reset(code)[0], 200)

    def test_unknown_user_looks_the_same_and_prints_nothing(self):
        status, body = self.forgot("nobody_here")
        status2, body2 = self.forgot("alice")
        self.assertEqual((status, body), (status2, body2))
        self.assertEqual([u for u, _ in self.codes], ["alice"])
        self.assertEqual(self.reset("ABCD-EFGH", username="nobody_here")[0], 400)

    def test_forgot_is_rate_limited(self):
        for _ in range(3):
            self.forgot()
        self.assertEqual(len(self.codes), 3)
        self.assertEqual(self.forgot()[0], 200)    # same reply, but no 4th code
        self.assertEqual(len(self.codes), 3)

    def test_forgot_and_reset_bad_input(self):
        self.assertEqual(self.call("POST", "/api/auth/forgot", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/auth/forgot", {"username": 5})[0], 400)
        self.assertEqual(self.call("POST", "/api/auth/reset", {"username": "alice"})[0], 400)
        self.assertEqual(self.call("GET", "/api/auth/forgot")[0], 405)

    def test_reset_clears_login_lockout(self):
        self.fresh_client()
        for _ in range(5):
            self.login("alice", "wrong wrong")
        self.assertEqual(self.login("alice")[0], 429)
        self.forgot()
        self.assertEqual(self.reset(self.codes[0][1])[0], 200)
        self.fresh_client()
        self.assertEqual(self.login("alice", self.NEW)[0], 200)

    def test_default_announce_prints_the_code(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            server.StudPlanServer.announce(self.srv, "alice", "ABCD-EFGH", 600)
        self.assertIn("ABCD-EFGH", buf.getvalue())
        self.assertIn("alice", buf.getvalue())

    # database upgrade and CLI
    def test_old_database_gets_session_epoch(self):
        import sqlite3
        path = os.path.join(self.dir, "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "username TEXT NOT NULL, password_hash TEXT NOT NULL, created TEXT NOT NULL)")
        conn.execute("INSERT INTO users (username, password_hash, created) VALUES "
                     "('old', 'x', '2026-01-01')")
        conn.commit()
        conn.close()
        db = server.Database(path)
        self.assertEqual(server.Users(db).by_username("old")["session_epoch"], 0)

    def test_cli_reset_password(self):
        from unittest import mock
        import io
        from contextlib import redirect_stdout
        old = self.cookie
        answers = iter([self.NEW, self.NEW])
        with mock.patch("getpass.getpass", lambda prompt="": next(answers)), \
                redirect_stdout(io.StringIO()):
            code = server.main(["--data-dir", self.data_dir, "reset-password", "alice"])
        self.assertEqual(code, 0)
        self.cookie = old
        self.assertEqual(self.call("GET", "/api/auth/me")[0], 401)   # old session ended
        self.cookie = None
        self.assertEqual(self.login("alice", self.NEW)[0], 200)

    def test_cli_reset_password_mismatch_and_unknown_user(self):
        from unittest import mock
        answers = iter([self.NEW, "different one"])
        with mock.patch("getpass.getpass", lambda prompt="": next(answers)):
            self.assertEqual(server.main(["--data-dir", self.data_dir,
                                          "reset-password", "alice"]), 1)
        self.assertEqual(server.main(["--data-dir", self.data_dir,
                                      "reset-password", "nobody"]), 1)
        self.cookie = None
        self.assertEqual(self.login("alice", PASSWORD)[0], 200)     # unchanged


class TestStaticFiles(ApiTestCase):
    AUTO_LOGIN = False

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.dir, "scene.js"), "w") as f:
            f.write("/* scene */")
        with open(os.path.join(self.dir, "secret.txt"), "w") as f:
            f.write("nope")

    def raw_get(self, path, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read()
        headers = dict(resp.getheaders())
        conn.close()
        return resp.status, headers, body

    def test_scene_script_is_served_without_login(self):
        status, headers, body = self.raw_get("/scene.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertEqual(body, b"/* scene */")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_only_allow_listed_files_are_reachable(self):
        for path in ("/secret.txt", "/models/scene.glb", "/models/", "/scene.js/../secret.txt",
                     "/%2e%2e/etc/passwd", "/server.py", "/data/studplan.db", "/scene.JS"):
            self.assertEqual(self.raw_get(path)[0], 404, path)

    def test_static_files_are_read_only(self):
        self.assertEqual(self.call("POST", "/scene.js", {})[0], 405)
        self.assertEqual(self.call("DELETE", "/scene.js")[0], 405)

    def test_head_request_has_no_body(self):
        status, headers, body = self.raw_get("/scene.js", "HEAD")
        self.assertEqual((status, body), (200, b""))


if __name__ == "__main__":
    unittest.main()
