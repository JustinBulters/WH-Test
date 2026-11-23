"""Hike checkpoint tracking website.

Lightweight Flask + SQLite app with server-rendered pages and only a little plain
JavaScript, so it stays usable on phones with poor signal. UI text is Dutch.
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

import io

import click
import segno
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from markupsafe import Markup
from openpyxl import Workbook
from openpyxl.styles import Font
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

# Hiker categories and their group number ranges.
CATEGORIES = {
    "junioren": (101, 199),
    "senioren": (201, 299),
    "veteranen": (301, 399),
}
# How many checkpoints each of the two days has, unless an admin changed it.
DEFAULT_PER_DAY = {1: 7, 2: 5}
MAX_PER_DAY = 25
# Hikers per group. A merge may go above this; the admin then gets a warning.
MAX_HIKERS = 3
# A group counts as late this many minutes after the expected walking time.
LATE_GRACE = 30


class _Checkpoints(Mapping):
    """Checkpoint number -> day, read from the settings for the current request.

    Numbering runs on: day 1 is CP 1..n1, day 2 is CP n1+1..n1+n2.
    """
    def _data(self):
        if "checkpoints" not in g:
            n1, n2 = checkpoints_per_day()
            g.checkpoints = {n: (1 if n <= n1 else 2) for n in range(1, n1 + n2 + 1)}
        return g.checkpoints

    def __getitem__(self, cp):
        return self._data()[cp]

    def __iter__(self):
        return iter(self._data())

    def __len__(self):
        return len(self._data())


CHECKPOINTS = _Checkpoints()
ROLES = {"admin": "Beheerder", "staff": "Checkpointpost", "viewer": "Meekijker"}

app = Flask(__name__)
# Behind a reverse proxy (e.g. the Synology one) every visitor seems to come from the proxy.
# WH_PROXIES=1 tells the site to trust one proxy's X-Forwarded-For header for the real address.
# Leave it unset when visitors reach the site directly, or anyone could fake their address.
_proxies = int(os.environ.get("WH_PROXIES", "0") or 0)
if _proxies > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_proxies, x_proto=_proxies)
app.config["DATABASE"] = os.environ.get("WH_DATABASE", os.path.join(BASE_DIR, "wh.sqlite3"))


def _secret_key():
    if os.environ.get("WH_SECRET_KEY"):
        return os.environ["WH_SECRET_KEY"]
    # Keep the generated key next to the database, so it survives container updates.
    path = os.path.join(os.path.dirname(app.config["DATABASE"]), ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  # Behind HTTPS (set WH_HTTPS=1) only send the cookie over HTTPS.
                  SESSION_COOKIE_SECURE=bool(os.environ.get("WH_HTTPS")),
                  # A login ends after this long; covers the two hike days with room to spare.
                  PERMANENT_SESSION_LIFETIME=timedelta(days=4))


@app.after_request
def security_headers(response):
    """Tell browsers to protect the pages: no showing them inside another site, no guessing types."""
    h = response.headers
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    if app.config["SESSION_COOKIE_SECURE"]:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")  # always use https:// from now on
    return response


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'staff', 'viewer')),
    checkpoint INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    theme TEXT NOT NULL DEFAULT 'auto',
    is_super INTEGER NOT NULL DEFAULT 0,  -- superadmin: an admin who may also change settings
    can_map INTEGER NOT NULL DEFAULT 1    -- may see the Kaart tab (set by a superadmin)
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,  -- the group number, e.g. '101'; assigned automatically
    category TEXT NOT NULL CHECK (category IN ('junioren', 'senioren', 'veteranen')),
    retired_at TEXT,            -- set when the group dropped out ("uitgevallen")
    retired_cp INTEGER,
    phone TEXT NOT NULL DEFAULT '',  -- emergency contact, only shown to admins
    retired_note TEXT NOT NULL DEFAULT '',  -- e.g. 'samengevoegd met 101'
    retired_reason TEXT NOT NULL DEFAULT ''  -- why it dropped out, e.g. 'Blessure: knie'
);
CREATE TABLE IF NOT EXISTS hikers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    checkpoint INTEGER NOT NULL,
    checkin_at TEXT,
    checkin_by INTEGER REFERENCES users(id),
    checkout_at TEXT,
    checkout_by INTEGER REFERENCES users(id),
    UNIQUE (group_id, checkpoint)
);
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    checkpoint INTEGER NOT NULL,
    quiz INTEGER CHECK (quiz BETWEEN 1 AND 10),
    practical INTEGER CHECK (practical BETWEEN 1 AND 10),
    updated_at TEXT NOT NULL,
    updated_by INTEGER REFERENCES users(id),
    UNIQUE (group_id, checkpoint)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    user_id INTEGER,
    username TEXT,
    method TEXT,
    path TEXT,
    action TEXT,
    ip TEXT
);
CREATE INDEX IF NOT EXISTS audit_at ON audit_log(at);
-- Checkpoint staff enter a score once; changing it needs an admin's approval.
CREATE TABLE IF NOT EXISTS score_requests (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    checkpoint INTEGER NOT NULL,
    old_quiz INTEGER, old_practical INTEGER,
    quiz INTEGER, practical INTEGER,
    reason TEXT NOT NULL,
    requested_by INTEGER REFERENCES users(id),
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'approved', 'declined')),
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT,
    decision_note TEXT NOT NULL DEFAULT ''
);
-- Messages shown inside the site (the bell in the header).
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    text TEXT NOT NULL,
    link TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id, read_at);
CREATE TABLE IF NOT EXISTS checkpoint_locations (
    checkpoint INTEGER PRIMARY KEY,
    lat REAL NOT NULL,
    lng REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Which "group is late" alerts were already sent: one per group per leg (per check-out).
CREATE TABLE IF NOT EXISTS late_alerts (
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    checkpoint INTEGER NOT NULL,  -- the checkpoint the group left
    since TEXT NOT NULL,          -- its check-out time there
    at TEXT NOT NULL,
    PRIMARY KEY (group_id, checkpoint, since)
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    # Small migrations for databases created by an older version.
    cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
    if "theme" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'auto'")
    if "can_map" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN can_map INTEGER NOT NULL DEFAULT 1")
    if "is_super" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN is_super INTEGER NOT NULL DEFAULT 0")
        # The first admin account becomes superadmin.
        db.execute("UPDATE users SET is_super = 1 WHERE id = "
                   "(SELECT MIN(id) FROM users WHERE role = 'admin' AND active = 1)")
    gcols = {r["name"] for r in db.execute("PRAGMA table_info(groups)")}
    if "retired_at" not in gcols:
        db.execute("ALTER TABLE groups ADD COLUMN retired_at TEXT")
        db.execute("ALTER TABLE groups ADD COLUMN retired_cp INTEGER")
    if "phone" not in gcols:
        db.execute("ALTER TABLE groups ADD COLUMN phone TEXT NOT NULL DEFAULT ''")
    if "retired_note" not in gcols:
        db.execute("ALTER TABLE groups ADD COLUMN retired_note TEXT NOT NULL DEFAULT ''")
    if "retired_reason" not in gcols:
        db.execute("ALTER TABLE groups ADD COLUMN retired_reason TEXT NOT NULL DEFAULT ''")
    # Hikers no longer have their own number: the group has it.
    if "number" in {r["name"] for r in db.execute("PRAGMA table_info(hikers)")}:
        db.commit()
        db.execute("PRAGMA foreign_keys = OFF")
        db.executescript("""
            BEGIN;
            CREATE TABLE hikers_new (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL);
            INSERT INTO hikers_new (id, name, comment, group_id)
                SELECT id, name, comment, group_id FROM hikers;
            DROP TABLE hikers;
            ALTER TABLE hikers_new RENAME TO hikers;
            COMMIT;""")
        db.execute("PRAGMA foreign_keys = ON")
    # Groups with an old free-text name get the next number in their category.
    for g in db.execute("SELECT id, category FROM groups WHERE name GLOB '*[^0-9]*' OR name = '' "
                        "ORDER BY id").fetchall():
        number = next_group_number(db, g["category"])
        if number is not None:  # category full: keep the old name rather than writing "None"
            db.execute("UPDATE groups SET name = ? WHERE id = ?", (str(number), g["id"]))
    # At most one open change request per group per checkpoint, also when two taps arrive together.
    try:
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS score_requests_one_open "
                   "ON score_requests (group_id, checkpoint) WHERE status = 'open'")
    except sqlite3.IntegrityError:
        pass  # an old database already has two open requests; the check in the route still applies
    db.commit()


def next_group_number(db, category):
    """The next free group number in a category: one above the highest, else the first gap."""
    lo, hi = CATEGORIES[category]
    used = {int(r[0]) for r in db.execute(
        "SELECT name FROM groups WHERE category = ?", (category,)) if r[0].isdecimal()}
    if not used:
        return lo
    if max(used) < hi:
        return max(used) + 1
    free = [n for n in range(lo, hi + 1) if n not in used]
    return free[0] if free else None


@app.cli.command("init-db")
@click.option("--admin", default="admin", help="Gebruikersnaam van de eerste beheerder.")
def init_db_command(admin):
    """Maak de database aan en een eerste beheerder."""
    init_db()
    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
        click.echo("Database bijgewerkt. Er bestaat al een beheerder.")
        return
    # On a hosting service, set WH_ADMIN_PASSWORD so the password is not printed in logs.
    password = os.environ.get("WH_ADMIN_PASSWORD") or secrets.token_urlsafe(9)
    db.execute("INSERT INTO users (username, password_hash, role, is_super) VALUES (?, ?, 'admin', 1)",
               (admin, generate_password_hash(password)))
    db.commit()
    if os.environ.get("WH_ADMIN_PASSWORD"):
        click.echo(f"Beheerder aangemaakt: {admin} (wachtwoord uit WH_ADMIN_PASSWORD)")
    else:
        click.echo(f"Beheerder aangemaakt: {admin} / {password}")
    click.echo("Wijzig dit wachtwoord na de eerste keer inloggen.")


# ----------------------------------------------------------------- helpers

def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


MONTHS = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus",
          "september", "oktober", "november", "december"]


@app.template_filter("dutch_date")
def dutch_date(d):
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}" if d else ""


@app.template_filter("localtime")
def localtime(value, fmt="%d-%m %H:%M"):
    """Show a stored UTC timestamp in Dutch local time."""
    if not value:
        return ""
    return datetime.fromisoformat(value).astimezone(LOCAL_TZ).strftime(fmt)


def current_user():
    if "user" not in g:
        g.user = None
        uid = session.get("user_id")
        if uid:
            g.user = get_db().execute(
                "SELECT * FROM users WHERE id = ? AND active = 1", (uid,)).fetchone()
            if g.user is not None:
                # A password change logs out every other phone with that account.
                mark = password_mark(g.user)
                if "pw" not in session:
                    session["pw"] = mark  # logged in before this check existed
                elif session["pw"] != mark:
                    g.user = None
    return g.user


def password_mark(user):
    """A fingerprint of the password, kept in the session to notice password changes.

    The session cookie is signed but readable, so it holds a keyed hash of the password
    hash, never a piece of the password hash itself.
    """
    return hmac.new(app.secret_key.encode(), user["password_hash"].encode(), hashlib.sha256).hexdigest()[:32]


def audit(action):
    """Attach a description of what this request did; written in after_request."""
    g.audit_action = action


@app.before_request
def csrf_protect():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(16)
    if request.method == "POST":
        if request.form.get("csrf") != session["csrf"]:
            abort(400)


@app.after_request
def write_audit(response):
    if request.endpoint in ("static", "service_worker", "notifications_poll", "csrf_token", "map_groups"):
        return response
    user = current_user()  # cached in g, so still known right after logout
    action = g.get("audit_action")
    if user is None and action is None:
        return response
    db = get_db()
    db.execute(
        "INSERT INTO audit_log (at, user_id, username, method, path, action, ip) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now_utc(), user["id"] if user else None,
         user["username"] if user else g.get("audit_username"),
         request.method, request.full_path.rstrip("?"), action or "bekeken",
         request.remote_addr))
    db.commit()
    return response


@app.context_processor
def inject_globals():
    me = current_user()
    unread = get_db().execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL",
                              (me["id"],)).fetchone()[0] if me else 0
    return {"me": me, "ROLES": ROLES, "CATEGORIES": CATEGORIES, "unread": unread,
            "CHECKPOINTS": CHECKPOINTS, "csrf": session.get("csrf", ""),
            "role_label": role_label, "can_see_map": can_see_map, "cp_label": cp_label, "cp_names": cp_names, "cp_details": cp_details,
            "day1_last": checkpoints_per_day()[0], "last_cp": max(CHECKPOINTS), "start_date": start_date() if me else None,
            "today": lambda: datetime.now(LOCAL_TZ).date()}


def notify(db, user_ids, text, link):
    db.executemany("INSERT INTO notifications (user_id, at, text, link) VALUES (?, ?, ?, ?)",
                   [(uid, now_utc(), text, link) for uid in user_ids])


def can_see_map(user):
    return bool(user["is_super"] or user["can_map"])


def role_label(user):
    return "Superbeheerder" if user["is_super"] else ROLES[user["role"]]


def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(db, key, value):
    if value is None:
        db.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))


def checkpoints_per_day():
    out = []
    for day in (1, 2):
        raw = get_setting(f"cp_day{day}", "")
        out.append(int(raw) if raw.isdecimal() and 1 <= int(raw) <= MAX_PER_DAY else DEFAULT_PER_DAY[day])
    return tuple(out)


def cp_names():
    if "cp_names" not in g:
        g.cp_names = {int(r[0][8:]): r[1] for r in get_db().execute(
            "SELECT key, value FROM settings WHERE key LIKE 'cp_name_%'") if r[0][8:].isdecimal()}
    return g.cp_names


def cp_details(cp):
    """Free-text notes per checkpoint: theme and who is manning it."""
    if "cp_details" not in g:
        g.cp_details = {}
        for key, value in get_db().execute(
                "SELECT key, value FROM settings WHERE key LIKE 'cp_theme_%' OR key LIKE 'cp_crew_%'"):
            field, num = key[3:].split("_", 1)
            if num.isdecimal():
                g.cp_details.setdefault(int(num), {})[field] = value
    return g.cp_details.get(cp, {})


def cp_label(cp):
    """'CP 3 · Molen' when the checkpoint has a name, else 'CP 3'."""
    name = cp_names().get(cp)
    return f"CP {cp} · {name}" if name else f"CP {cp}"


def start_date():
    """The first day of the hike as a date, or None when not set."""
    raw = get_setting("start_date")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date() if raw else None
    except ValueError:
        return None


def hike_started():
    sd = start_date()
    return sd is None or datetime.now(LOCAL_TZ).date() >= sd


def login_required(*roles):
    """Only logged-in users with one of these roles. "superadmin" means an admin with is_super."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                if wants_json():
                    # Scripts must see "not logged in", not a login page that looks like success.
                    return jsonify(ok=False, login=True, message="Log opnieuw in."), 401
                return redirect(url_for("login", next=request.path))
            if roles and user["role"] not in roles:
                abort(403)
            if "superadmin" in roles and not user["is_super"]:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def is_local_path(path):
    """A link to a page on this site. Browsers read '\\' as '/', so '/\\evil.example' is not local."""
    return path.startswith("/") and not path.startswith("//") and "\\" not in path


def safe_back(default):
    """Only follow a local 'back' link from a form."""
    back = request.form.get("back", "")
    return back if is_local_path(back) else default


def wants_json():
    """Requests made by the site's own scripts (offline check-ins, notification poll)."""
    return bool(request.form.get("ajax") or request.args.get("ajax"))


def can_view_checkpoint(cp):
    user = current_user()
    return user["role"] in ("admin", "viewer") or (
        user["role"] == "staff" and user["checkpoint"] == cp)


def can_edit_checkpoint(cp):
    user = current_user()
    return user["role"] == "admin" or (user["role"] == "staff" and user["checkpoint"] == cp)


def check_cp(cp, edit=False):
    if cp not in CHECKPOINTS:
        abort(404)
    if not (can_edit_checkpoint(cp) if edit else can_view_checkpoint(cp)):
        abort(403)


# ------------------------------------------------------------------- auth

@app.route("/inloggen", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if too_many_login_failures(username):
            # Not written to the logbook: a flood of blocked tries would only fill the database.
            flash(f"Te veel mislukte pogingen. Probeer het over {LOGIN_LOCK_MINUTES} minuten opnieuw.", "error")
            return render_template("login.html"), 429
        user = get_db().execute(
            # Usernames are not case sensitive; an exact match wins if two only differ in case.
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE AND active = 1 "
            "ORDER BY username = ? DESC LIMIT 1", (username, username)).fetchone()
        # Always check a password, also for an unknown name, so the answer takes equally long
        # and nobody can tell from the timing which usernames exist.
        ok = check_password_hash(user["password_hash"] if user else DUMMY_HASH,
                                 request.form.get("password", ""))
        if user and ok:
            session.clear()
            session.permanent = True  # survives closing the browser, so offline taps can still be sent
            session["user_id"] = user["id"]
            session["pw"] = password_mark(user)
            g.user = user
            audit("ingelogd")
            nxt = request.args.get("next", "")
            return redirect(nxt if is_local_path(nxt) else url_for("index"))
        g.audit_username = username
        audit("mislukte inlogpoging")
        flash("Onjuiste gebruikersnaam of wachtwoord.", "error")
    return render_template("login.html")


# Failed logins: after this many in LOGIN_LOCK_MINUTES, logging in is paused.
LOGIN_MAX_PER_USER = 10  # per username *from one address*
LOGIN_MAX_PER_IP = 30    # per address, all usernames together (only with real addresses)
LOGIN_LOCK_MINUTES = 15
DUMMY_HASH = generate_password_hash("geen-echt-wachtwoord")


def too_many_login_failures(username):
    """Too many wrong passwords from this address.

    Counted per username *and* address, so someone elsewhere cannot lock a staff member
    out by typing wrong passwords for their name. The per-address limit only works when
    the site sees real addresses (WH_PROXIES behind HAProxy); otherwise every visitor
    seems to come from the proxy, and that limit would block everybody at once.
    """
    since = (datetime.now(timezone.utc) - timedelta(minutes=LOGIN_LOCK_MINUTES)).isoformat(timespec="seconds")
    db = get_db()
    per_user = db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'mislukte inlogpoging' AND at > ? "
                          "AND username = ? COLLATE NOCASE AND ip IS ?",
                          (since, username, request.remote_addr)).fetchone()[0]
    if per_user >= LOGIN_MAX_PER_USER:
        return True
    if not _proxies:
        return False
    per_ip = db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'mislukte inlogpoging' AND at > ? "
                        "AND ip = ?", (since, request.remote_addr)).fetchone()[0]
    return per_ip >= LOGIN_MAX_PER_IP


@app.route("/uitloggen", methods=["POST"])
def logout():
    audit("uitgelogd")
    current_user()
    session.clear()
    return redirect(url_for("login"))


THEMES = {"auto": "Zoals mijn apparaat", "light": "Licht", "dark": "Donker"}


@app.route("/profiel", methods=["GET", "POST"])
@login_required()
def profile():
    user = current_user()
    db = get_db()
    if request.method == "POST":
        if request.form.get("op") == "theme":
            theme = request.form.get("theme")
            if theme in THEMES:
                db.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, user["id"]))
                db.commit()
                audit(f"weergave ingesteld op {theme}")
                flash("Weergave opgeslagen.", "ok")
        else:
            new = request.form.get("new", "")
            if not check_password_hash(user["password_hash"], request.form.get("old", "")):
                flash("Huidig wachtwoord klopt niet.", "error")
            elif len(new) < 8:
                flash("Nieuw wachtwoord moet minstens 8 tekens hebben.", "error")
            else:
                new_hash = generate_password_hash(new)
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user["id"]))
                db.commit()
                session["pw"] = password_mark({"password_hash": new_hash})  # stay logged in here only
                audit("eigen wachtwoord gewijzigd")
                flash("Wachtwoord gewijzigd.", "ok")
        return redirect(url_for("profile"))
    return render_template("profile.html", THEMES=THEMES)


@app.route("/wachtwoord")
def change_password():
    return redirect(url_for("profile"))


# ---------------------------------------------------------------- progress

def group_progress(db, group):
    """Per checkpoint: visit, score and a state for the timeline.

    States: done (checked out), here (checked in), next (where the group heads),
    retired (dropped out there), todo.
    """
    visits = {r["checkpoint"]: r for r in db.execute(
        "SELECT * FROM checkins WHERE group_id = ?", (group["id"],))}
    scores = {r["checkpoint"]: r for r in db.execute(
        "SELECT * FROM scores WHERE group_id = ?", (group["id"],))}
    latest = max(visits.values(), key=lambda v: (v["checkout_at"] or v["checkin_at"] or "", v["id"]),
                 default=None)
    nxt = None
    if not group["retired_at"]:
        if latest is None:
            nxt = 1
        elif latest["checkout_at"] and latest["checkpoint"] < max(CHECKPOINTS):
            nxt = latest["checkpoint"] + 1
    rows = []
    for cp in CHECKPOINTS:
        v = visits.get(cp)
        if group["retired_at"] and group["retired_cp"] == cp:
            state = "retired"
        elif v and v["checkout_at"]:
            state = "done"
        elif v and v["checkin_at"]:
            state = "here"
        elif cp == nxt:
            state = "next"
        else:
            state = "todo"
        rows.append({"cp": cp, "day": CHECKPOINTS[cp], "state": state, "visit": v, "score": scores.get(cp)})
    return rows


def form_scores(cp):
    vals = []
    for field in ("quiz", "practical"):
        raw = request.form.get(f"{field}_{cp}", "")
        vals.append(int(raw) if raw.isdecimal() and 1 <= int(raw) <= 10 else None)
    return vals


def save_group_scores(db, group, cps, write_once=False):
    """Store quiz/practical from the form for the given checkpoints; returns changes for the log.

    With write_once (checkpoint staff), a score that is already filled in is kept:
    changing it goes through a change request instead.
    """
    changed = []
    for cp in cps:
        vals = form_scores(cp)
        old = db.execute("SELECT quiz, practical FROM scores WHERE group_id = ? AND checkpoint = ?",
                         (group["id"], cp)).fetchone()
        old = list(old or (None, None))
        if write_once:
            vals = [o if o is not None else v for o, v in zip(old, vals)]
        if old == vals:
            continue
        # With write_once, a score saved by someone else in the meantime is never overwritten.
        keep = "COALESCE(scores.{0}, excluded.{0})" if write_once else "excluded.{0}"
        db.execute(
            "INSERT INTO scores (group_id, checkpoint, quiz, practical, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (group_id, checkpoint) DO UPDATE SET "
            f"quiz = {keep.format('quiz')}, practical = {keep.format('practical')}, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (group["id"], cp, vals[0], vals[1], now_utc(), current_user()["id"]))
        changed.append(f"CP{cp} quiz={vals[0]} praktijk={vals[1]}")
    db.commit()
    return changed


# ---------------------------------------------------------------- overview

def checkpoint_stats(db):
    """Per checkpoint: groups present (in), left (out) and still expected (todo); plus active groups."""
    stats = {cp: {"in": 0, "out": 0} for cp in CHECKPOINTS}
    for row in db.execute(
            # A group that dropped out while checked in is no longer "present".
            "SELECT c.checkpoint, SUM(c.checkin_at IS NOT NULL AND c.checkout_at IS NULL "
            "AND g.retired_at IS NULL) AS present, SUM(c.checkout_at IS NOT NULL) AS done "
            "FROM checkins c JOIN groups g ON g.id = c.group_id GROUP BY c.checkpoint"):
        stats[row["checkpoint"]] = {"in": row["present"], "out": row["done"]}
    # Still expected at a checkpoint: groups that have not dropped out, have not been
    # there yet and have not been seen at a later checkpoint either.
    furthest = {g["id"]: g["furthest"] for g in db.execute(
        "SELECT g.id, g.retired_at, MAX(c.checkpoint) AS furthest FROM groups g "
        "LEFT JOIN checkins c ON c.group_id = g.id GROUP BY g.id HAVING g.retired_at IS NULL")}
    for cp in CHECKPOINTS:
        stats[cp]["todo"] = sum(1 for f in furthest.values() if f is None or f < cp)
    return stats, len(furthest)


def legs():
    """Checkpoints that have a next checkpoint on the same day: cp -> next cp."""
    return {cp: cp + 1 for cp, day in CHECKPOINTS.items() if CHECKPOINTS.get(cp + 1) == day}


def leg_times(db):
    """Per leg (from cp): expected minutes (set by an admin) and the real average so far."""
    out = {cp: {"to": nxt, "expected": None, "avg": None, "count": 0} for cp, nxt in legs().items()}
    for cp in out:
        raw = get_setting(f"leg_{cp}", "")
        out[cp]["expected"] = int(raw) if raw.isdecimal() else None
    walked = {}
    for r in db.execute("SELECT a.checkpoint AS cp, a.checkout_at AS start, b.checkin_at AS end FROM checkins a "
                        "JOIN checkins b ON b.group_id = a.group_id AND b.checkpoint = a.checkpoint + 1 "
                        "WHERE a.checkout_at IS NOT NULL AND b.checkin_at IS NOT NULL"):
        m = minutes_between(r["start"], r["end"])
        if r["cp"] in out and m is not None and m >= 0:
            walked.setdefault(r["cp"], []).append(m)
    for cp, ms in walked.items():
        out[cp]["avg"], out[cp]["count"] = round(sum(ms) / len(ms)), len(ms)
    return out


def late_groups(db):
    """Groups that left a checkpoint and have been walking longer than expected + LATE_GRACE."""
    times = leg_times(db)
    now = datetime.now(timezone.utc)
    late = []
    for r in db.execute(
            "SELECT g.id, g.name, g.category, c.checkpoint, c.checkout_at FROM groups g JOIN ("
            "  SELECT *, ROW_NUMBER() OVER (PARTITION BY group_id "
            "    ORDER BY COALESCE(checkout_at, checkin_at) DESC, id DESC) AS rn FROM checkins"
            ") c ON c.group_id = g.id AND c.rn = 1 "
            "WHERE g.retired_at IS NULL AND c.checkout_at IS NOT NULL"):
        leg = times.get(r["checkpoint"])
        if not leg or leg["expected"] is None:
            continue
        walking = round((now - datetime.fromisoformat(r["checkout_at"])).total_seconds() / 60)
        if walking > leg["expected"] + LATE_GRACE:
            late.append({"id": r["id"], "name": r["name"], "category": r["category"], "from": r["checkpoint"],
                         "to": leg["to"], "since": r["checkout_at"], "walking": walking,
                         "expected": leg["expected"], "avg": leg["avg"]})
    return sorted(late, key=lambda x: x["walking"] - x["expected"], reverse=True)


def send_late_alerts(db):
    """Notify all admins once when a group becomes late on a leg.

    Runs on every notification poll, so it needs no background job: as long as
    anyone has the site open, new late groups are noticed within ~15 seconds.
    """
    late = late_groups(db)
    if not late:
        return
    admins = [r["id"] for r in db.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1")]
    for x in late:
        # INSERT OR IGNORE only adds a row the first time, so each alert goes out once,
        # even when two phones poll at the same moment.
        cur = db.execute("INSERT OR IGNORE INTO late_alerts (group_id, checkpoint, since, at) VALUES (?, ?, ?, ?)",
                         (x["id"], x["from"], x["since"], now_utc()))
        if cur.rowcount:
            notify(db, admins, f"Groep {x['name']} is te laat: al {x['walking']} min onderweg van "
                               f"{cp_label(x['from'])} naar {cp_label(x['to'])} (verwacht {x['expected']} min)",
                   url_for("group_link", gid=x["id"]))
    db.commit()


@app.route("/")
@login_required()
def index():
    user = current_user()
    if user["role"] == "staff":
        if user["checkpoint"]:
            return redirect(url_for("checkpoint", cp=user["checkpoint"]))
        return render_template("message.html", text="Je bent nog niet aan een checkpoint gekoppeld.")
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    stats, active = checkpoint_stats(db)
    # Where is each group now: its most recent check-in or check-out.
    where = db.execute(
        "SELECT g.id, g.name, g.category, g.retired_at, g.retired_cp, g.retired_note, "
        "c.checkpoint, c.checkin_at, c.checkout_at, "
        "(SELECT GROUP_CONCAT(h.name, ', ') FROM hikers h WHERE h.group_id = g.id) AS members "
        "FROM groups g LEFT JOIN ("
        "  SELECT *, ROW_NUMBER() OVER (PARTITION BY group_id "
        "    ORDER BY COALESCE(checkout_at, checkin_at) DESC, id DESC) AS rn FROM checkins"
        ") c ON c.group_id = g.id AND c.rn = 1 "
        "ORDER BY g.category, CAST(g.name AS INTEGER)").fetchall()
    return render_template("index.html", stats=stats, total=total, active=active, where=where,
                           late=late_groups(db), grace=LATE_GRACE)


@app.route("/checkpoint/<int:cp>")
@login_required()
def checkpoint(cp):
    check_cp(cp)
    db = get_db()
    q = request.args.get("q", "").strip()
    if q:
        found = db.execute("SELECT id FROM groups WHERE name = ?", (q,)).fetchone()
        if found:
            return redirect(url_for("checkpoint_group", cp=cp, gid=found["id"]))
        flash(f"Groep {q} bestaat niet.", "error")
    groups = db.execute(
        "SELECT g.*, c.checkin_at, c.checkout_at, "
        "(SELECT COUNT(*) FROM hikers h WHERE h.group_id = g.id) AS size, "
        "(SELECT GROUP_CONCAT(h.name, ', ') FROM hikers h WHERE h.group_id = g.id) AS members "
        "FROM groups g LEFT JOIN checkins c ON c.group_id = g.id AND c.checkpoint = ? "
        "ORDER BY g.category, CAST(g.name AS INTEGER)", (cp,)).fetchall()
    late = {x["id"]: x for x in late_groups(db) if x["to"] == cp}
    return render_template("checkpoint.html", cp=cp, groups=groups, late=late,
                           can_edit=can_edit_checkpoint(cp))


@app.route("/checkpoint/<int:cp>/groep/<int:gid>", methods=["GET", "POST"])
@login_required()
def checkpoint_group(cp, gid):
    check_cp(cp)
    db = get_db()
    group = db.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
    if group is None:
        abort(404)
    hikers = db.execute("SELECT * FROM hikers WHERE group_id = ? ORDER BY id",
                        (gid,)).fetchall()
    score = db.execute("SELECT * FROM scores WHERE group_id = ? AND checkpoint = ?",
                       (gid, cp)).fetchone()

    if request.method == "POST":
        check_cp(cp, edit=True)
        changed = save_group_scores(db, group, [cp], write_once=current_user()["role"] != "admin")
        audit(f"scores groep {group['name']}: " + ("; ".join(changed) or "geen wijziging"))
        flash("Scores opgeslagen.", "ok")
        return redirect(url_for("checkpoint_group", cp=cp, gid=gid))

    visit = db.execute("SELECT * FROM checkins WHERE group_id = ? AND checkpoint = ?",
                       (gid, cp)).fetchone()
    pending = db.execute("SELECT * FROM score_requests WHERE group_id = ? AND checkpoint = ? "
                         "AND status = 'open'", (gid, cp)).fetchone()
    return render_template("group_scores.html", pending=pending, cp=cp, group=group, hikers=hikers, score=score,
                           progress=group_progress(db, group),
                           visit=visit, can_edit=can_edit_checkpoint(cp))


@app.route("/checkpoint/<int:cp>/groep/<int:gid>/wijziging", methods=["POST"])
@login_required("staff")
def score_request_new(cp, gid):
    """Staff ask an admin to change a score that was already entered."""
    check_cp(cp, edit=True)
    db = get_db()
    group = db.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
    if group is None:
        abort(404)
    back = url_for("checkpoint_group", cp=cp, gid=gid)
    old = db.execute("SELECT quiz, practical FROM scores WHERE group_id = ? AND checkpoint = ?",
                     (gid, cp)).fetchone()
    old = list(old or (None, None))
    # A field left on "–" means "keep this score", never "erase it".
    new = [n if n is not None else o for n, o in zip(form_scores(cp), old)]
    reason = request.form.get("reason", "").strip()[:500]
    if db.execute("SELECT 1 FROM score_requests WHERE group_id = ? AND checkpoint = ? AND status = 'open'",
                  (gid, cp)).fetchone():
        flash("Er staat al een wijzigingsverzoek open voor deze groep.", "error")
    elif not reason:
        flash("Vul een reden in voor de wijziging.", "error")
    elif new == old:
        flash("Er is niets gewijzigd.", "error")
    else:
        me = current_user()
        try:
            cur = db.execute(
                "INSERT INTO score_requests (group_id, checkpoint, old_quiz, old_practical, quiz, practical, "
                "reason, requested_by, requested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (gid, cp, old[0], old[1], new[0], new[1], reason, me["id"], now_utc()))
        except sqlite3.IntegrityError:  # a double tap: the first one already made the request
            flash("Er staat al een wijzigingsverzoek open voor deze groep.", "error")
            return redirect(back)
        admins = [r[0] for r in db.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1")]
        notify(db, admins, f"{me['username']} vraagt een scorewijziging voor groep {group['name']} op CP {cp}",
               url_for("score_request", rid=cur.lastrowid))
        db.commit()
        audit(f"wijzigingsverzoek scores groep {group['name']} CP{cp}: {old} -> {new}, reden: {reason}")
        flash("Wijzigingsverzoek verstuurd. Een beheerder keurt het goed of af.", "ok")
    return redirect(back)


@app.route("/verzoeken")
@login_required("admin", "staff")
def score_requests():
    db = get_db()
    me = current_user()
    where, params = ("", []) if me["role"] == "admin" else ("WHERE r.requested_by = ?", [me["id"]])
    rows = db.execute(
        "SELECT r.*, g.name AS group_name, u.username AS requester FROM score_requests r "
        "JOIN groups g ON g.id = r.group_id LEFT JOIN users u ON u.id = r.requested_by "
        f"{where} ORDER BY r.status != 'open', r.id DESC LIMIT 200", params).fetchall()
    return render_template("requests.html", rows=rows)


@app.route("/verzoeken/<int:rid>", methods=["GET", "POST"])
@login_required("admin", "staff")
def score_request(rid):
    db = get_db()
    me = current_user()
    req = db.execute(
        "SELECT r.*, g.name AS group_name, g.category, u.username AS requester, d.username AS decider "
        "FROM score_requests r JOIN groups g ON g.id = r.group_id "
        "LEFT JOIN users u ON u.id = r.requested_by LEFT JOIN users d ON d.id = r.decided_by "
        "WHERE r.id = ?", (rid,)).fetchone()
    if req is None:
        abort(404)
    if me["role"] != "admin" and req["requested_by"] != me["id"]:
        abort(403)
    if request.method == "POST":
        if me["role"] != "admin":
            abort(403)
        decision = request.form.get("decision")
        note = request.form.get("note", "").strip()[:500]
        if req["status"] != "open":
            flash("Over dit verzoek is al beslist.", "error")
        elif decision in ("approved", "declined"):
            db.execute("UPDATE score_requests SET status = ?, decided_by = ?, decided_at = ?, "
                       "decision_note = ? WHERE id = ?", (decision, me["id"], now_utc(), note, rid))
            if decision == "approved":
                # An empty value in the request keeps the current score (older requests could have one).
                db.execute(
                    "INSERT INTO scores (group_id, checkpoint, quiz, practical, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (group_id, checkpoint) DO UPDATE SET "
                    "quiz = COALESCE(excluded.quiz, scores.quiz), "
                    "practical = COALESCE(excluded.practical, scores.practical), "
                    "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                    (req["group_id"], req["checkpoint"], req["quiz"], req["practical"], now_utc(), me["id"]))
            word = "goedgekeurd" if decision == "approved" else "afgewezen"
            if req["requested_by"]:
                notify(db, [req["requested_by"]],
                       f"Je scorewijziging voor groep {req['group_name']} op CP {req['checkpoint']} is {word}",
                       url_for("score_request", rid=rid))
            db.commit()
            audit(f"wijzigingsverzoek {rid} (groep {req['group_name']} CP{req['checkpoint']}) {word}"
                  + (f": {note}" if note else ""))
            flash(f"Verzoek {word}.", "ok")
        return redirect(url_for("score_request", rid=rid))
    current = db.execute("SELECT quiz, practical FROM scores WHERE group_id = ? AND checkpoint = ?",
                         (req["group_id"], req["checkpoint"])).fetchone()
    return render_template("request.html", req=req, current=current)


@app.route("/meldingen")
@login_required()
def notifications():
    rows = get_db().execute("SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 100",
                            (current_user()["id"],)).fetchall()
    return render_template("notifications.html", rows=rows)


POLL_LIMIT = 5  # pop-ups per poll; the rest follow in the next poll


@app.route("/meldingen/nieuw")
@login_required()
def notifications_poll():
    """Small JSON for the pop-up: unread notifications newer than ?na=<id>, and the unread count."""
    after = request.args.get("na", "0")
    after = int(after) if after.isdecimal() else 0
    db = get_db()
    send_late_alerts(db)
    uid = current_user()["id"]
    rows = db.execute("SELECT id, text FROM notifications WHERE user_id = ? AND read_at IS NULL AND id > ? "
                      "ORDER BY id LIMIT ?", (uid, after, POLL_LIMIT)).fetchall()
    unread = db.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL",
                        (uid,)).fetchone()[0]
    if len(rows) == POLL_LIMIT:
        last = rows[-1]["id"]  # more are waiting: the next poll continues after these
    else:
        last = db.execute("SELECT COALESCE(MAX(id), 0) FROM notifications WHERE user_id = ?",
                          (uid,)).fetchone()[0]
    resp = jsonify(unread=unread, last=last,
                   items=[{"id": r["id"], "text": r["text"], "url": url_for("notification_open", nid=r["id"])}
                          for r in rows])
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/meldingen/<int:nid>")
@login_required()
def notification_open(nid):
    """Mark a notification as read and go to what it is about."""
    db = get_db()
    n = db.execute("SELECT * FROM notifications WHERE id = ? AND user_id = ?",
                   (nid, current_user()["id"])).fetchone()
    if n is None:
        abort(404)
    if not n["read_at"]:
        db.execute("UPDATE notifications SET read_at = ? WHERE id = ?", (now_utc(), nid))
        db.commit()
    return redirect(n["link"] if is_local_path(n["link"]) else url_for("index"))


@app.route("/meldingen/gelezen", methods=["POST"])
@login_required()
def notifications_read():
    db = get_db()
    db.execute("UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
               (now_utc(), current_user()["id"]))
    db.commit()
    return redirect(url_for("notifications"))


@app.route("/checkpoint/<int:cp>/groep/<int:gid>/<action>", methods=["POST"])
@login_required("admin", "staff")
def checkpoint_stamp(cp, gid, action):
    """Check a group in or out, or (admin) clear it.

    The offline script sends `at` (the moment the button was tapped) and `ajax=1`;
    it then gets JSON back. A repeated tap that was already stored counts as done.
    """
    check_cp(cp, edit=True)
    db = get_db()
    group = db.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
    if group is None:
        abort(404)
    ajax = bool(request.form.get("ajax"))
    visit = db.execute("SELECT * FROM checkins WHERE group_id = ? AND checkpoint = ?",
                       (gid, cp)).fetchone()
    uid, ts = current_user()["id"], now_utc()
    raw_at = request.form.get("at", "")
    tapped = tap_time(raw_at)
    late_note = ""
    if tapped and tapped < ts:
        late_note = f" (offline getikt om {localtime(tapped, '%H:%M')}, later verstuurd)"
        ts = tapped
    elif raw_at and not tapped:
        # Too old (or the phone's clock is wrong): the time of sending is used. Say so in the log.
        late_note = f" (tiktijd {raw_at[:25]} niet geloofwaardig, verzendtijd gebruikt)"

    def done(ok, msg, already=False):
        if ajax:
            return jsonify(ok=ok, already=already, message=msg), (200 if ok else 409)
        flash(msg, "ok" if ok else "error")
        return redirect(safe_back(url_for("checkpoint", cp=cp)))

    if action in ("in", "uit") and not hike_started():
        return done(False, f"De hike begint op {dutch_date(start_date())}. Inchecken kan vanaf die dag.")
    if group["retired_at"] and action in ("in", "uit"):
        return done(False, f"Groep {group['name']} is uitgevallen.")
    if action == "in":
        # ON CONFLICT: two taps at the same moment give "already checked in", not an error.
        cur = None if visit and visit["checkin_at"] else db.execute(
            "INSERT INTO checkins (group_id, checkpoint, checkin_at, checkin_by) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (group_id, checkpoint) DO NOTHING", (gid, cp, ts, uid))
        if cur is None or not cur.rowcount:
            return done(ajax, "Deze groep is hier al ingecheckt.", already=True)
        audit(f"groep {group['name']} ingecheckt op CP{cp}" + late_note)
        msg = f"Groep {group['name']} ingecheckt om {localtime(ts, '%H:%M')}."
    elif action == "uit":
        if not visit or not visit["checkin_at"]:
            return done(False, "Groep moet eerst inchecken.")
        if visit["checkout_at"]:
            return done(ajax, "Deze groep is hier al uitgecheckt.", already=True)
        if ts < visit["checkin_at"]:
            ts = visit["checkin_at"]
        cur = db.execute("UPDATE checkins SET checkout_at = ?, checkout_by = ? "
                         "WHERE id = ? AND checkout_at IS NULL", (ts, uid, visit["id"]))
        if not cur.rowcount:
            return done(ajax, "Deze groep is hier al uitgecheckt.", already=True)
        audit(f"groep {group['name']} uitgecheckt op CP{cp}" + late_note)
        msg = f"Groep {group['name']} uitgecheckt om {localtime(ts, '%H:%M')}."
    elif action == "herstel" and current_user()["role"] == "admin":
        db.execute("DELETE FROM checkins WHERE group_id = ? AND checkpoint = ?", (gid, cp))
        audit(f"in/uitcheck van groep {group['name']} op CP{cp} gewist")
        msg = "In- en uitcheck gewist."
    else:
        abort(400)
    db.commit()
    return done(True, msg)


# Offline taps older than this are not trusted; the server time is used instead.
MAX_OFFLINE_HOURS = 12


def tap_time(raw):
    """The time an offline tap happened, as UTC ISO text, if it is believable."""
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    now = datetime.now(timezone.utc)
    if not (now - timedelta(hours=MAX_OFFLINE_HOURS) <= t <= now + timedelta(minutes=2)):
        return None
    return min(t, now).astimezone(timezone.utc).isoformat(timespec="seconds")


@app.route("/csrf")
@login_required()
def csrf_token():
    """The current form token, for offline check-ins sent after a new login."""
    resp = jsonify(csrf=session["csrf"])
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/sw.js")
def service_worker():
    """The service worker must be served from the site root to cover every page."""
    resp = app.send_static_file("sw.js")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.route("/groep/<int:gid>/uitgevallen", methods=["POST"])
@login_required("admin")
def group_retire(gid):
    """Mark a group as dropped out, or undo that. Admin only."""
    db = get_db()
    group = db.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
    if group is None:
        abort(404)
    raw = request.form.get("cp", "")
    cp = int(raw) if raw.isdecimal() and int(raw) in CHECKPOINTS else None
    if cp is None:
        # Not given: the checkpoint the group was last seen at.
        last = db.execute("SELECT checkpoint FROM checkins WHERE group_id = ? "
                          "ORDER BY COALESCE(checkout_at, checkin_at) DESC, id DESC LIMIT 1",
                          (gid,)).fetchone()
        cp = last[0] if last else None
    if request.form.get("undo") and group["retired_note"] and not db.execute(
            "SELECT 1 FROM hikers WHERE group_id = ?", (gid,)).fetchone():
        # A merged group has no hikers left: bringing it back would give an empty group.
        flash(f"Groep {group['name']} is samengevoegd en heeft geen wandelaars meer. "
              "Voeg eerst een wandelaar toe.", "error")
        return redirect(safe_back(url_for("index")))
    if request.form.get("undo"):
        db.execute("UPDATE groups SET retired_at = NULL, retired_cp = NULL, retired_note = '', retired_reason = '' "
                   "WHERE id = ?", (gid,))
        audit(f"groep {group['name']} weer actief gemaakt")
        flash(f"Groep {group['name']} doet weer mee.", "ok")
    elif not group["retired_at"]:
        reason = retire_reason_from_form()
        db.execute("UPDATE groups SET retired_at = ?, retired_cp = ?, retired_reason = ? WHERE id = ?",
                   (now_utc(), cp, reason, gid))
        audit(f"groep {group['name']} uitgevallen" + (f" bij CP{cp}" if cp else "")
              + (f", reden: {reason}" if reason else ""))
        flash(f"Groep {group['name']} staat nu als uitgevallen.", "ok")
    db.commit()
    return redirect(safe_back(url_for("index")))


@app.route("/uitslag")
@login_required("admin", "viewer")
def results():
    return render_template("results.html", by_cat=ranking(get_db()))


@app.route("/scorebord")
@login_required("admin", "viewer")
def scoreboard():
    """Big read-only ranking for a screen; the page reloads itself every 30 seconds."""
    return render_template("scoreboard.html", by_cat=ranking(get_db()), bare=True,
                           now=localtime(now_utc(), "%H:%M"))


def ranking(db):
    """Groups per category, best total score first. Equal totals share a place (1, 2, 2, 4)."""
    rows = [dict(r) for r in db.execute(
        "SELECT g.id, g.name, g.category, g.retired_at, "
        "(SELECT GROUP_CONCAT(h.name, ', ') FROM hikers h WHERE h.group_id = g.id) AS members, "
        "COALESCE(SUM(s.quiz), 0) AS quiz, COALESCE(SUM(s.practical), 0) AS practical, "
        # CP's: checkpoints where the group checked in or got a score.
        "(SELECT COUNT(*) FROM (SELECT checkpoint FROM checkins WHERE group_id = g.id UNION "
        "                       SELECT checkpoint FROM scores WHERE group_id = g.id)) AS visits "
        "FROM groups g LEFT JOIN scores s ON s.group_id = g.id "
        "GROUP BY g.id ORDER BY (COALESCE(SUM(s.quiz), 0) + COALESCE(SUM(s.practical), 0)) DESC, CAST(g.name AS INTEGER)"
    )]
    by_cat = {cat: [r for r in rows if r["category"] == cat] for cat in CATEGORIES}
    for cat_rows in by_cat.values():
        for i, r in enumerate(cat_rows):
            prev = cat_rows[i - 1] if i else None
            same = prev and prev["quiz"] + prev["practical"] == r["quiz"] + r["practical"]
            r["place"] = prev["place"] if same else i + 1
    return by_cat


@app.route("/export.xlsx")
@login_required("admin")
def export_xlsx():
    """Everything in one Excel file: ranking, groups and every checkpoint visit."""
    db = get_db()
    wb = Workbook()

    def sheet(title, header, rows, first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title
        ws.append(header)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append(list(r))
            for cell in ws[ws.max_row]:
                # Text like "=1+1" in a name or comment stays text, not an Excel formula.
                if cell.data_type == "f":
                    cell.data_type = "s"
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = max(
                8, min(40, max(len(str(c.value or "")) for c in col) + 2))
        ws.freeze_panes = "A2"

    by_cat = ranking(db)
    sheet("Uitslag", ["Categorie", "Plaats", "Groep", "Wandelaars", "Quiz", "Praktijk", "Totaal",
                      "CP's", "Uitgevallen"],
          [(cat.capitalize(), r["place"], int(r["name"]), r["members"] or "", r["quiz"], r["practical"],
            r["quiz"] + r["practical"], r["visits"], "ja" if r["retired_at"] else "")
           for cat, rows in by_cat.items() for r in rows], first=True)
    hikers = {}
    for h in db.execute("SELECT * FROM hikers ORDER BY id"):
        hikers.setdefault(h["group_id"], []).append(h)
    groups = db.execute("SELECT * FROM groups ORDER BY category, CAST(name AS INTEGER)").fetchall()
    sheet("Groepen", ["Groep", "Categorie", "Wandelaars", "Opmerkingen", "Noodnummer",
                      "Uitgevallen", "Uitgevallen bij CP", "Reden uitvallen"],
          [(int(gr["name"]), gr["category"].capitalize(),
            ", ".join(h["name"] for h in hikers.get(gr["id"], [])),
            "; ".join(f"{h['name']}: {h['comment']}" for h in hikers.get(gr["id"], []) if h["comment"]),
            gr["phone"], localtime(gr["retired_at"]) if gr["retired_at"] else "", gr["retired_cp"] or "",
            gr["retired_reason"])
           for gr in groups])
    visits = db.execute(
        # k.checkpoint, not c.checkpoint: a score at a CP without a check-in must not drop out.
        "SELECT g.name, g.category, k.checkpoint, c.checkin_at, c.checkout_at, s.quiz, s.practical "
        "FROM groups g JOIN (SELECT group_id, checkpoint FROM checkins UNION "
        "                    SELECT group_id, checkpoint FROM scores) k ON k.group_id = g.id "
        "LEFT JOIN checkins c ON c.group_id = g.id AND c.checkpoint = k.checkpoint "
        "LEFT JOIN scores s ON s.group_id = g.id AND s.checkpoint = k.checkpoint "
        "ORDER BY g.category, CAST(g.name AS INTEGER), k.checkpoint").fetchall()
    sheet("Tijden en scores", ["Groep", "Categorie", "CP", "Dag", "Ingecheckt", "Uitgecheckt",
                               "Minuten op CP", "Quiz", "Praktijk"],
          [(int(v["name"]), v["category"].capitalize(), v["checkpoint"], CHECKPOINTS.get(v["checkpoint"]),
            localtime(v["checkin_at"]) if v["checkin_at"] else "",
            localtime(v["checkout_at"]) if v["checkout_at"] else "",
            ("" if minutes_between(v["checkin_at"], v["checkout_at"]) is None
             else minutes_between(v["checkin_at"], v["checkout_at"])),
            v["quiz"] or "", v["practical"] or "")
           for v in visits if v["checkpoint"] is not None])
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    audit("export naar Excel gedownload")
    stamp = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d_%H%M")
    return send_file(out, as_attachment=True, download_name=f"hike-{stamp}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def minutes_between(start, end):
    if not start or not end:
        return None
    return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60)


# ------------------------------------------------------- lookup, QR cards

def group_page_for(user, gid):
    """Where a group link takes this user: staff to their checkpoint, others to the group page."""
    if user["role"] == "staff":
        if not user["checkpoint"]:
            abort(403)
        return url_for("checkpoint_group", cp=user["checkpoint"], gid=gid)
    return url_for("group_edit", gid=gid)


@app.route("/g/<int:gid>")
@login_required()
def group_link(gid):
    """Target of the QR code on a group card."""
    if get_db().execute("SELECT 1 FROM groups WHERE id = ?", (gid,)).fetchone() is None:
        abort(404)
    return redirect(group_page_for(current_user(), gid))


@app.route("/q/<int:number>")
@login_required()
def group_qr(number):
    """Target of the QR code on a printed card: the group *number*, as printed on the card.

    The internal id can be given out again after a group is deleted or all data is wiped,
    so a card holding the id could open a different group.
    """
    found = get_db().execute("SELECT id FROM groups WHERE name = ?", (str(number),)).fetchone()
    if found is None:
        abort(404)
    return redirect(group_page_for(current_user(), found["id"]))


@app.route("/zoek")
@login_required()
def lookup():
    q = request.args.get("q", "").strip()
    found = get_db().execute("SELECT id FROM groups WHERE name = ?", (q,)).fetchone() if q else None
    if found:
        return redirect(group_page_for(current_user(), found["id"]))
    if q:
        flash(f"Groep {q} bestaat niet.", "error")
    back = request.args.get("back", "")
    return redirect(back if is_local_path(back) else url_for("index"))


@app.route("/groepen/kaarten")
@login_required("admin")
def group_cards():
    """Printable cards: group number, names and a QR code that opens the group."""
    db = get_db()
    base = (os.environ.get("WH_BASE_URL") or request.url_root).rstrip("/")
    cards = []
    for gr in db.execute("SELECT * FROM groups WHERE retired_at IS NULL "
                         "ORDER BY category, CAST(name AS INTEGER)"):
        names = [h["name"] for h in db.execute(
            "SELECT name FROM hikers WHERE group_id = ? ORDER BY id", (gr["id"],))]
        qr = segno.make(base + url_for("group_qr", number=int(gr["name"])), error="m")
        cards.append({"group": gr, "names": names,
                      "qr": Markup(qr.svg_inline(scale=4, border=2, omitsize=True))})
    return render_template("cards.html", cards=cards, base=base)


# --------------------------------------------------------- groups & hikers

@app.route("/groepen", methods=["GET", "POST"])
@login_required("admin", "viewer")
def groups():
    db = get_db()
    if request.method == "POST":
        if current_user()["role"] != "admin":
            abort(403)
        cat = request.form.get("category")
        people = [(n.strip(), c.strip()) for n, c in zip(request.form.getlist("hiker_name"),
                                                         request.form.getlist("hiker_comment"))]
        people = [(n, c) for n, c in people if n]
        if cat not in CATEGORIES:
            flash("Kies een categorie.", "error")
        elif not people:
            flash("Vul minstens één naam in.", "error")
        elif len(people) > MAX_HIKERS:
            flash(f"Een groep heeft hoogstens {MAX_HIKERS} wandelaars.", "error")
        else:
            number = next_group_number(db, cat)
            if number is None:
                lo, hi = CATEGORIES[cat]
                flash(f"Alle nummers van {lo} tot {hi} zijn al in gebruik.", "error")
            else:
                try:
                    cur = db.execute("INSERT INTO groups (name, category, phone) VALUES (?, ?, ?)",
                                     (str(number), cat, request.form.get("phone", "").strip()[:40]))
                except sqlite3.IntegrityError:  # another admin took this number a moment ago
                    flash("Dat groepsnummer werd net door iemand anders gebruikt. Probeer het opnieuw.", "error")
                    return redirect(url_for("groups"))
                db.executemany("INSERT INTO hikers (name, comment, group_id) VALUES (?, ?, ?)",
                               [(n, c, cur.lastrowid) for n, c in people])
                db.commit()
                audit(f"groep {number} ({cat}) aangemaakt met {', '.join(n for n, _ in people)}")
                flash(f"Groep {number} aangemaakt.", "ok")
        return redirect(url_for("groups"))
    groups = db.execute("SELECT * FROM groups ORDER BY category, CAST(name AS INTEGER)").fetchall()
    hikers = {}
    for h in db.execute("SELECT * FROM hikers ORDER BY id"):
        hikers.setdefault(h["group_id"], []).append(h)
    return render_template("groups.html", groups=groups, hikers=hikers)


@app.route("/groepen/<int:gid>", methods=["GET", "POST"])
@login_required("admin", "viewer")
def group_edit(gid):
    db = get_db()
    group = db.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
    if group is None:
        abort(404)
    if request.method == "POST":
        if current_user()["role"] != "admin":
            abort(403)
        form = request.form
        op = form.get("op")
        if op == "phone":
            phone = form.get("phone", "").strip()[:40]
            db.execute("UPDATE groups SET phone = ? WHERE id = ?", (phone, gid))
            db.commit()
            audit(f"noodnummer groep {group['name']} gewijzigd")
            flash("Noodnummer opgeslagen.", "ok")
        elif op == "scores":
            changed = save_group_scores(db, group, list(CHECKPOINTS))
            audit(f"scores groep {group['name']}: " + ("; ".join(changed) or "geen wijziging"))
            flash("Scores opgeslagen.", "ok")
        elif op == "hiker_save":
            hid = form.get("hiker_id")
            name = form.get("name", "").strip()
            comment = form.get("comment", "").strip()
            if not name:
                flash("Vul een naam in.", "error")
            elif hid:
                db.execute("UPDATE hikers SET name = ?, comment = ? WHERE id = ? AND group_id = ?",
                           (name, comment, hid, gid))
                db.commit()
                audit(f"wandelaar {name} in groep {group['name']} gewijzigd")
                flash("Wandelaar opgeslagen.", "ok")
            elif group["retired_at"]:
                flash("Groep is uitgevallen of samengevoegd. Maak de groep eerst weer actief.", "error")
            elif db.execute("SELECT COUNT(*) FROM hikers WHERE group_id = ?", (gid,)).fetchone()[0] >= MAX_HIKERS:
                flash(f"Een groep heeft hoogstens {MAX_HIKERS} wandelaars.", "error")
            else:
                db.execute("INSERT INTO hikers (name, comment, group_id) VALUES (?, ?, ?)",
                           (name, comment, gid))
                db.commit()
                audit(f"wandelaar {name} toegevoegd aan groep {group['name']}")
                flash("Wandelaar toegevoegd.", "ok")
        elif op == "hiker_delete":
            h = db.execute("SELECT name FROM hikers WHERE id = ? AND group_id = ?",
                           (form.get("hiker_id"), gid)).fetchone()
            if h:
                db.execute("DELETE FROM hikers WHERE id = ?", (form.get("hiker_id"),))
                db.commit()
                audit(f"wandelaar {h['name']} uit groep {group['name']} verwijderd")
                flash("Wandelaar verwijderd.", "ok")
        elif op == "merge":
            target = db.execute("SELECT * FROM groups WHERE id = ? AND id != ? AND retired_at IS NULL",
                                (form.get("target", ""), gid)).fetchone()
            ids = [int(i) for i in form.getlist("hiker") if i.isdecimal()]
            moving = db.execute(
                f"SELECT * FROM hikers WHERE group_id = ? AND id IN ({','.join('?' * len(ids))})",
                [gid] + ids).fetchall() if ids else []
            if target is None:
                flash("Kies een groep om bij aan te sluiten.", "error")
            elif not moving:
                flash("Kies minstens één wandelaar die overstapt.", "error")
            else:
                origin = f"oorspronkelijk groep {group['name']}"
                db.executemany("UPDATE hikers SET group_id = ?, comment = ? WHERE id = ?",
                               [(target["id"], f"{h['comment']}; {origin}" if h["comment"] else origin.capitalize(),
                                 h["id"]) for h in moving])
                last = db.execute("SELECT checkpoint FROM checkins WHERE group_id = ? "
                                  "ORDER BY COALESCE(checkout_at, checkin_at) DESC, id DESC LIMIT 1",
                                  (gid,)).fetchone()
                note = f"samengevoegd met {target['name']}"
                if group["retired_at"]:
                    db.execute("UPDATE groups SET retired_note = ? WHERE id = ?", (note, gid))
                else:
                    db.execute("UPDATE groups SET retired_at = ?, retired_cp = ?, retired_note = ?, retired_reason = ? "
                               "WHERE id = ?", (now_utc(), last[0] if last else None, note,
                                                f"Samengevoegd met {target['name']}", gid))
                db.commit()
                names = ", ".join(h["name"] for h in moving)
                audit(f"groep {group['name']} samengevoegd met {target['name']}: {names} overgestapt")
                size = db.execute("SELECT COUNT(*) FROM hikers WHERE group_id = ?", (target["id"],)).fetchone()[0]
                flash(f"{names} {'loopt' if len(moving) == 1 else 'lopen'} nu mee met groep {target['name']}. "
                      f"Groep {group['name']} staat als uitgevallen.", "ok")
                if size > 3:
                    flash(f"Let op: groep {target['name']} heeft nu {size} wandelaars.", "error")
                return redirect(url_for("group_edit", gid=target["id"]))
        elif op == "group_delete":
            db.execute("DELETE FROM hikers WHERE group_id = ?", (gid,))
            db.execute("DELETE FROM groups WHERE id = ?", (gid,))
            db.commit()
            audit(f"groep {group['name']} verwijderd")
            flash("Groep verwijderd.", "ok")
            return redirect(url_for("groups"))
        return redirect(url_for("group_edit", gid=gid))
    hikers = db.execute("SELECT * FROM hikers WHERE group_id = ? ORDER BY id", (gid,)).fetchall()
    others = db.execute(
        "SELECT g.id, g.name, g.category, (SELECT COUNT(*) FROM hikers h WHERE h.group_id = g.id) AS size "
        "FROM groups g WHERE g.id != ? AND g.retired_at IS NULL ORDER BY g.category != ?, "
        "CAST(g.name AS INTEGER)", (gid, group["category"])).fetchall()
    return render_template("group_edit.html", group=group, hikers=hikers, others=others,
                           progress=group_progress(db, group))


# ------------------------------------------------ map, dropout reason, paper sheets (agent C)

# Choices for "why did this group drop out"; the admin may add a few words of their own.
RETIRE_REASONS = ["Blessure", "Ziek of niet lekker", "Te moe", "Opgehaald door ouders", "Anders"]


def retire_reason_from_form():
    """'Blessure: knie' from the retire form; '' when nothing was filled in."""
    choice = request.form.get("reason", "")
    choice = choice if choice in RETIRE_REASONS else ""
    text = " ".join(request.form.get("reason_text", "").split())[:200]
    if choice and text:
        return f"{choice}: {text}"
    return choice or text


@app.context_processor
def inject_retire_reasons():
    return {"RETIRE_REASONS": RETIRE_REASONS}


def group_positions(db):
    """Where each active group is on the map, by its most recent check-in or check-out.

    Only the group number, category and state: no names, comments or phone numbers,
    so the map shows nothing personal.
    """
    locs = {r["checkpoint"]: (r["lat"], r["lng"]) for r in db.execute("SELECT * FROM checkpoint_locations")}
    nxt = legs()
    late = {x["id"] for x in late_groups(db)}
    out = []
    for r in db.execute(
            "SELECT g.id, g.name, g.category, c.checkpoint, c.checkin_at, c.checkout_at FROM groups g JOIN ("
            "  SELECT *, ROW_NUMBER() OVER (PARTITION BY group_id "
            "    ORDER BY COALESCE(checkout_at, checkin_at) DESC, id DESC) AS rn FROM checkins"
            ") c ON c.group_id = g.id AND c.rn = 1 "
            "WHERE g.retired_at IS NULL ORDER BY g.category, CAST(g.name AS INTEGER)"):
        cp = r["checkpoint"]
        if cp not in CHECKPOINTS or cp not in locs:
            continue
        (lat, lng), at_cp = locs[cp], True
        if not r["checkout_at"]:
            state = f"op {cp_label(cp)}"
        elif cp in nxt:
            to = nxt[cp]
            state = f"onderweg van CP {cp} naar CP {to} sinds {localtime(r['checkout_at'], '%H:%M')}"
            if to in locs:  # halfway along the leg
                lat, lng = round((lat + locs[to][0]) / 2, 6), round((lng + locs[to][1]) / 2, 6)
                at_cp = False
        else:
            state = "dag 1 klaar" if CHECKPOINTS[cp] == 1 else "binnen"
        out.append({"number": r["name"], "category": r["category"], "state": state,
                    "lat": lat, "lng": lng, "at_cp": at_cp, "late": r["id"] in late})
    return out


@app.route("/kaart/groepen.json")
@login_required()
def map_groups():
    """The groups on the map; the map page fetches this every minute."""
    if not can_see_map(current_user()):
        abort(403)
    resp = jsonify(groups=group_positions(get_db()))
    resp.headers["Cache-Control"] = "no-store"
    return resp


def paper_sheet(cp):
    """Header details for one checkpoint's paper list."""
    day = CHECKPOINTS[cp]
    sd = start_date()
    return {"cp": cp, "label": cp_label(cp), "day": day,
            "date": sd + timedelta(days=day - 1) if sd else None,
            "theme": cp_details(cp).get("theme", ""), "crew": cp_details(cp).get("crew", "")}


def paper_groups(db):
    """All groups still walking, with hiker count and comments (e.g. allergies)."""
    hikers = {}
    for h in db.execute("SELECT * FROM hikers ORDER BY id"):
        hikers.setdefault(h["group_id"], []).append(h)
    return [{"name": gr["name"], "category": gr["category"], "size": len(hikers.get(gr["id"], [])),
             "comments": "; ".join(f"{h['name']}: {h['comment']}" for h in hikers.get(gr["id"], [])
                                   if h["comment"])}
            for gr in db.execute("SELECT * FROM groups WHERE retired_at IS NULL "
                                 "ORDER BY category, CAST(name AS INTEGER)")]


@app.route("/checkpoint/<int:cp>/papier")
@login_required()
def paper_checkpoint(cp):
    """Printable backup list for one checkpoint, to fill in by hand when phones fail."""
    check_cp(cp)
    return render_template("paper.html", sheets=[paper_sheet(cp)], groups=paper_groups(get_db()),
                           bare=True, back=url_for("checkpoint", cp=cp))


@app.route("/papier")
@login_required("admin")
def paper_all():
    """The paper lists of every checkpoint in one document, one checkpoint per page."""
    return render_template("paper.html", sheets=[paper_sheet(cp) for cp in CHECKPOINTS],
                           groups=paper_groups(get_db()), bare=True, back=url_for("checkpoint_setup"))


# ------------------------------------------------------------------- users

@app.route("/checkpoints", methods=["GET", "POST"])
@login_required("admin")
def checkpoint_setup():
    """Admins set how many checkpoints each day has and give them names."""
    db = get_db()
    if request.method == "POST":
        counts = []
        for day in (1, 2):
            raw = request.form.get(f"day{day}", "")
            counts.append(int(raw) if raw.isdecimal() and 1 <= int(raw) <= MAX_PER_DAY else None)
        if None in counts:
            flash(f"Kies per dag 1 tot {MAX_PER_DAY} checkpoints.", "error")
            return redirect(url_for("checkpoint_setup"))
        total = sum(counts)
        # Checkpoint numbers run on across both days, so a new count for day 1 shifts which
        # day a number belongs to. A checkpoint that is in use must stay on the same day.
        old_n1 = checkpoints_per_day()[0]
        in_use = {r[0] for r in db.execute(
            "SELECT checkpoint FROM checkins UNION SELECT checkpoint FROM scores UNION "
            "SELECT checkpoint FROM score_requests UNION "
            "SELECT checkpoint FROM users WHERE checkpoint IS NOT NULL")}
        gone = sorted(c for c in in_use if c > total)
        moved = sorted(c for c in in_use if c <= total and (c <= old_n1) != (c <= counts[0]))
        if gone:
            flash("Dat kan niet: " + ", ".join(f"CP {c}" for c in gone) + " heeft al in- of uitchecks, "
                  "scores of een checkpointpost. Haal die eerst weg.", "error")
            return redirect(url_for("checkpoint_setup"))
        if moved:
            flash("Dat kan niet: " + ", ".join(f"CP {c}" for c in moved) + " zou naar een andere dag "
                  "verschuiven, maar heeft al in- of uitchecks, scores of een checkpointpost. "
                  "Pas alleen het aantal checkpoints van dag 2 aan, of haal die gegevens eerst weg.", "error")
            return redirect(url_for("checkpoint_setup"))
        for day, n in zip((1, 2), counts):
            set_setting(db, f"cp_day{day}", str(n))
        for cp in range(1, MAX_PER_DAY * 2 + 1):
            name = request.form.get(f"name_{cp}", "").strip()[:40] if cp <= total else ""
            set_setting(db, f"cp_name_{cp}", name or None)
            for field, limit in (("theme", 200), ("crew", 500)):
                value = request.form.get(f"{field}_{cp}", "").strip()[:limit] if cp <= total else ""
                set_setting(db, f"cp_{field}_{cp}", value or None)
            raw = request.form.get(f"leg_{cp}", "").strip() if cp < total else ""
            set_setting(db, f"leg_{cp}", str(int(raw)) if raw.isdecimal() and 0 < int(raw) <= 24 * 60 else None)
        db.execute("DELETE FROM checkpoint_locations WHERE checkpoint > ?", (total,))
        db.commit()
        audit(f"checkpoints ingesteld: dag 1 = {counts[0]}, dag 2 = {counts[1]}")
        flash("Checkpoints opgeslagen.", "ok")
        return redirect(url_for("checkpoint_setup"))
    return render_template("checkpoints.html", per_day=checkpoints_per_day(), max_per_day=MAX_PER_DAY,
                           legs=leg_times(db), grace=LATE_GRACE)


@app.route("/kaart", methods=["GET", "POST"])
@login_required()
def map_view():
    """OpenStreetMap with the checkpoints. Admins place a checkpoint by clicking or dragging."""
    if not can_see_map(current_user()):
        abort(403)
    db = get_db()
    if request.method == "POST":
        if current_user()["role"] != "admin":
            abort(403)
        raw = request.form.get("cp", "")
        cp = int(raw) if raw.isdecimal() and int(raw) in CHECKPOINTS else None
        if cp is None:
            abort(400)
        if request.form.get("op") == "remove":
            db.execute("DELETE FROM checkpoint_locations WHERE checkpoint = ?", (cp,))
            audit(f"CP{cp} van de kaart gehaald")
        else:
            try:
                lat, lng = float(request.form["lat"]), float(request.form["lng"])
            except (KeyError, ValueError):
                abort(400)
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                abort(400)
            db.execute("INSERT INTO checkpoint_locations (checkpoint, lat, lng) VALUES (?, ?, ?) "
                       "ON CONFLICT (checkpoint) DO UPDATE SET lat = excluded.lat, lng = excluded.lng",
                       (cp, round(lat, 6), round(lng, 6)))
            audit(f"CP{cp} op de kaart gezet op {lat:.5f}, {lng:.5f}")
        db.commit()
        if request.form.get("ajax"):
            return jsonify(ok=True)
        return redirect(url_for("map_view"))
    stats, _ = checkpoint_stats(db)
    locs = {r["checkpoint"]: r for r in db.execute("SELECT * FROM checkpoint_locations")}
    points = [{"cp": cp, "day": day, "label": cp_label(cp), "theme": cp_details(cp).get("theme", ""),
               "crew": cp_details(cp).get("crew", ""), "lat": locs[cp]["lat"] if cp in locs else None,
               "lng": locs[cp]["lng"] if cp in locs else None, **stats[cp],
               "url": url_for("checkpoint", cp=cp) if can_view_checkpoint(cp) else None}
              for cp, day in CHECKPOINTS.items()]
    return render_template("map.html", points=points)


@app.route("/instellingen", methods=["GET", "POST"])
@login_required("admin", "superadmin")
def settings():
    """Superadmin only: start date of the hike and wiping all group data."""
    db = get_db()
    if request.method == "POST":
        op = request.form.get("op")
        if op == "start_date":
            raw = request.form.get("start_date", "").strip()
            try:
                value = datetime.strptime(raw, "%Y-%m-%d").date().isoformat() if raw else None
            except ValueError:
                flash("Ongeldige datum.", "error")
                return redirect(url_for("settings"))
            set_setting(db, "start_date", value)
            db.commit()
            audit(f"startdatum gezet op {value or 'geen'}")
            flash("Startdatum opgeslagen." if value else "Startdatum verwijderd.", "ok")
        elif op == "wipe":
            if request.form.get("confirm", "").strip().upper() != "WISSEN":
                flash("Typ WISSEN om te bevestigen. Er is niets gewist.", "error")
            else:
                counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                          for t in ("groups", "hikers", "checkins", "scores")}
                for table in ("late_alerts", "score_requests", "scores", "checkins", "hikers", "groups"):
                    db.execute(f"DELETE FROM {table}")
                db.execute("DELETE FROM notifications WHERE link LIKE '/verzoeken%' OR link LIKE '/g/%'")
                db.commit()
                audit("alle groepsdata gewist: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
                flash(f"Alle groepsdata is gewist ({counts['groups']} groepen, {counts['hikers']} wandelaars). "
                      "Accounts en instellingen zijn bewaard.", "ok")
        return redirect(url_for("settings"))
    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("groups", "hikers", "checkins", "scores")}
    return render_template("settings.html", counts=counts, start=start_date())


@app.route("/gebruikers", methods=["GET", "POST"])
@login_required("admin")
def users():
    db = get_db()
    if request.method == "POST":
        form = request.form
        op = form.get("op")
        role = form.get("role")
        me = current_user()
        make_super = role == "superadmin"
        if make_super:
            if not me["is_super"]:
                abort(403)
            role = "admin"
        cp = form.get("checkpoint", "")
        cp = int(cp) if cp.isdecimal() and int(cp) in CHECKPOINTS else None
        if role == "staff" and cp is None:
            flash("Een checkpointpost moet een checkpoint hebben.", "error")
        elif op == "create":
            username = form.get("username", "").strip()
            password = form.get("password", "")
            if not username or len(password) < 8 or role not in ROLES:
                flash("Vul gebruikersnaam, rol en een wachtwoord van minstens 8 tekens in.", "error")
            elif db.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone():
                flash("Die gebruikersnaam bestaat al (hoofdletters tellen niet mee).", "error")
            else:
                try:
                    # Kaart zien is off for a new account; only a superadmin turns it on later.
                    db.execute("INSERT INTO users (username, password_hash, role, checkpoint, is_super, can_map) "
                               "VALUES (?, ?, ?, ?, ?, 0)", (username, generate_password_hash(password),
                                                             role, cp if role == "staff" else None,
                                                             int(make_super)))
                    db.commit()
                    audit(f"gebruiker {username} aangemaakt als {'superadmin' if make_super else role}")
                    flash("Gebruiker aangemaakt.", "ok")
                except sqlite3.IntegrityError:
                    flash("Die gebruikersnaam bestaat al.", "error")
        elif op == "update":
            raw = form.get("user_id", "")
            uid = int(raw) if raw.isdecimal() else 0
            target = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if target and role in ROLES:
                active = 1 if form.get("active") else 0
                if target["is_super"] and not me["is_super"]:
                    abort(403)
                if uid == me["id"] and (role != "admin" or not active or make_super != bool(me["is_super"])):
                    flash("Je kunt je eigen beheerdersrechten niet afnemen.", "error")
                else:
                    db.execute("UPDATE users SET role = ?, checkpoint = ?, active = ?, is_super = ? WHERE id = ?",
                               (role, cp if role == "staff" else None, active, int(make_super), uid))
                    if me["is_super"]:
                        db.execute("UPDATE users SET can_map = ? WHERE id = ?",
                                   (1 if form.get("can_map") else 0, uid))
                    if form.get("password"):
                        if len(form["password"]) < 8:
                            flash("Wachtwoord moet minstens 8 tekens hebben.", "error")
                        else:
                            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                                       (generate_password_hash(form["password"]), uid))
                    db.commit()
                    audit(f"gebruiker {target['username']} gewijzigd: rol={'superadmin' if make_super else role} cp={cp} actief={active}"
                          + (f" kaart={1 if form.get('can_map') else 0}" if me["is_super"] else "")
                          + (" + nieuw wachtwoord" if form.get("password") else ""))
                    flash("Gebruiker bijgewerkt.", "ok")
        return redirect(url_for("users"))
    rows = db.execute("SELECT * FROM users ORDER BY role, username").fetchall()
    return render_template("users.html", users=rows)


@app.route("/logboek")
@login_required("admin")
def audit_log():
    db = get_db()
    who = request.args.get("wie", "").strip()
    ip = request.args.get("ip", "").strip()
    raw = request.args.get("p", "1")
    page = max(int(raw), 1) if raw.isdecimal() else 1
    per = 100
    conds, params = [], []
    if who:
        conds.append("username = ?")
        params.append(who)
    if ip:  # the start of an address is enough, e.g. 192.168.1.
        conds.append("ip LIKE ? ESCAPE '\\'")
        params.append(ip.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = db.execute(f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                      params + [per + 1, (page - 1) * per]).fetchall()
    names = [r[0] for r in db.execute(
        "SELECT DISTINCT username FROM audit_log WHERE username IS NOT NULL ORDER BY username")]
    return render_template("audit.html", rows=rows[:per], more=len(rows) > per,
                           page=page, who=who, ip=ip, names=names)


@app.errorhandler(403)
def forbidden(e):
    audit("toegang geweigerd")
    return render_template("message.html", text="Je hebt geen toegang tot deze pagina."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("message.html", text="Pagina niet gevonden."), 404


if __name__ == "__main__":
    app.run(debug=True)
