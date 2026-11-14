# StudentPlanner

**A study planner and deadline tracker that tells you what to work on first.**
It runs as a website with accounts, as an installable **offline phone app**, and
as a command-line tool. Python standard library only, no frameworks, no build step.

![StudentPlanner on desktop](screenshots/desktop-light.png)

| Dark mode | Books open when you tap them | On a phone |
|---|---|---|
| ![dark](screenshots/desktop-dark.png) | ![book](screenshots/book-opening.png) | ![phone](screenshots/phone.png) |

## Why

Students juggle assignments, labs and exams with different deadlines and
workloads, and it is hard to know what to start on. StudentPlanner scores every
task by urgency (days left, hours needed, priority), lists the top three to focus
on today, and warns about overdue work and days with more hours due than you can
realistically do.

## Features

- **Smart ranking** of tasks, a "Work on these first" list, an all-tasks view and a weekly summary
- **Reminders:** an alert 1 hour before a deadline, and a notification when you hit your daily or weekly goal
- **Accounts** (server version): sign up, log in, change or reset your password; every student's tasks are private
- **Offline phone app (PWA):** install it on Android or iPhone, works with no internet, tasks stay on the device
- **3D study background** written in plain WebGL: books, notebooks, pencils, rulers, set squares, a protractor and a compass float behind the page. Tap a book to open it; other items spin, hop or wobble
- Light and dark mode, scroll animations, shining icons, layout that fits phones down to 320px
- **Command-line tool** (`planner.py`) with the same ranking logic

## Quick start

Needs Python 3.8 or newer. There is nothing to install.

```bash
git clone https://github.com/<sequeirajovin24-create>/StudentPlanner.git
cd StudentPlanner
python server.py
```

Open http://127.0.0.1:8000, create an account and add a task. Data is stored in
`data/studplan.db` (SQLite).

Forgot your password? The reset code is printed in the terminal where the server
runs, or run `python server.py reset-password <username>`.

### Use it as an offline phone app

The app files in this repository work without the server. Host them on any
static host over HTTPS, for example **GitHub Pages**:

1. Repository **Settings > Pages**, deploy from branch `main`, folder `/ (root)`.
2. Open `https://<sequeirajovin24-create>.github.io/StudentPlanner/` on your phone.
3. Android (Chrome): menu > **Install app**. iPhone (Safari): Share > **Add to Home Screen**.

When there is no server, the app saves tasks in the phone's own storage and
keeps working in airplane mode. Tasks are per device. Deadline reminders appear
while the app is open.

### Command-line tool

```bash
python planner.py add "Lab report 3" -s Physics -d 2026-11-20 -H 4 -p 4
python planner.py today
```

See [docs/USAGE.md](docs/USAGE.md) for every command, the API, accounts and security notes.

## Tests

```bash
python -m unittest -v
```

## How it works

| Part | Notes |
|---|---|
| `planner.py` | Ranking logic and CLI ([DESIGN.md](DESIGN.md) explains the urgency formula) |
| `server.py` | `http.server` backend: SQLite, scrypt password hashing, cookie sessions, login throttling, per-user data isolation |
| `index.html` | Single-file frontend in vanilla JavaScript; uses the API when a server is present, browser storage otherwise |
| `scene.js` | Dependency-free WebGL renderer for the 3D study items |
| `sw.js`, `manifest.webmanifest` | Service worker and manifest that make it an installable offline app |

## Limitations

- The server is meant for your own computer. Before hosting it publicly you would need HTTPS, a way to deliver password-reset codes, and database backups.
- Offline phone data lives only on that phone; there is no sync between devices.

## License

MIT, see [LICENSE](LICENSE).
