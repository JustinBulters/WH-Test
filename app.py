"""Hike checkpoint tracking website.

Lightweight Flask + SQLite app with server-rendered pages and no JavaScript,
so it stays usable on phones with poor signal. UI text is Dutch.
"""
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo

import click
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

# Hiker categories and their group number ranges.
CATEGORIES = {
    "junioren": (101, 199),
    "senioren": (201, 299),
    "veteranen": (301, 399),
}
# Checkpoint number -> day.
CHECKPOINTS = {n: (1 if n <= 7 else 2) for n in range(1, 13)}
ROLES = {"admin": "Beheerder", "staff": "Checkpointpost", "viewer": "Meekijker"}

app = Flask(__name__)
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
                  # Behind HTTPS (Render sets RENDER; elsewhere set WH_HTTPS=1) only send the cookie over HTTPS.
                  SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER") or os.environ.get("WH_HTTPS")))


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'staff', 'viewer')),
    checkpoint INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    theme TEXT NOT NULL DEFAULT 'auto'
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,  -- the group number, e.g. '101'; assigned automatically
    category TEXT NOT NULL CHECK (category IN ('junioren', 'senioren', 'veteranen')),
    retired_at TEXT,            -- set when the group dropped out ("uitgevallen")
    retired_cp INTEGER
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
    gcols = {r["name"] for r in db.execute("PRAGMA table_info(groups)")}
    if "retired_at" not in gcols:
        db.execute("ALTER TABLE groups ADD COLUMN retired_at TEXT")
        db.execute("ALTER TABLE groups ADD COLUMN retired_cp INTEGER")
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
        db.execute("UPDATE groups SET name = ? WHERE id = ?",
                   (str(next_group_number(db, g["category"])), g["id"]))
    db.commit()


def next_group_number(db, category):
    """The next free group number in a category: one above the highest, else the first gap."""
    lo, hi = CATEGORIES[category]
    used = {int(r[0]) for r in db.execute(
        "SELECT name FROM groups WHERE category = ?", (category,)) if r[0].isdigit()}
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
    db.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
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
    return g.user


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
    if request.endpoint == "static":
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
         request.headers.get("X-Forwarded-For", request.remote_addr)))
    db.commit()
    return response


@app.context_processor
def inject_globals():
    return {"me": current_user(), "ROLES": ROLES, "CATEGORIES": CATEGORIES,
            "CHECKPOINTS": CHECKPOINTS, "csrf": session.get("csrf", "")}


def login_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("login", next=request.path))
            if roles and user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def safe_back(default):
    """Only follow a local 'back' link from a form."""
    back = request.form.get("back", "")
    return back if back.startswith("/") and not back.startswith("//") else default


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
        user = get_db().execute(
            "SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["user_id"] = user["id"]
            g.user = user
            audit("ingelogd")
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
        g.audit_username = username
        audit("mislukte inlogpoging")
        flash("Onjuiste gebruikersnaam of wachtwoord.", "error")
    return render_template("login.html")


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
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                           (generate_password_hash(new), user["id"]))
                db.commit()
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


def save_group_scores(db, group, cps):
    """Store quiz/practical from the form for the given checkpoints; returns changes for the log."""
    changed = []
    for cp in cps:
        vals = []
        for field in ("quiz", "practical"):
            raw = request.form.get(f"{field}_{cp}", "")
            vals.append(int(raw) if raw.isdigit() and 1 <= int(raw) <= 10 else None)
        old = db.execute("SELECT quiz, practical FROM scores WHERE group_id = ? AND checkpoint = ?",
                         (group["id"], cp)).fetchone()
        if list(old or (None, None)) == vals:
            continue
        db.execute(
            "INSERT INTO scores (group_id, checkpoint, quiz, practical, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (group_id, checkpoint) DO UPDATE SET "
            "quiz = excluded.quiz, practical = excluded.practical, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (group["id"], cp, vals[0], vals[1], now_utc(), current_user()["id"]))
        changed.append(f"CP{cp} quiz={vals[0]} praktijk={vals[1]}")
    db.commit()
    return changed


# ---------------------------------------------------------------- overview

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
    stats = {cp: {"in": 0, "out": 0} for cp in CHECKPOINTS}
    for row in db.execute(
            "SELECT checkpoint, SUM(checkin_at IS NOT NULL AND checkout_at IS NULL) AS present, "
            "SUM(checkout_at IS NOT NULL) AS done FROM checkins GROUP BY checkpoint"):
        stats[row["checkpoint"]] = {"in": row["present"], "out": row["done"]}
    # Still expected at a checkpoint: groups that have not dropped out, have not been
    # there yet and have not been seen at a later checkpoint either.
    furthest = {g["id"]: g["furthest"] for g in db.execute(
        "SELECT g.id, g.retired_at, MAX(c.checkpoint) AS furthest FROM groups g "
        "LEFT JOIN checkins c ON c.group_id = g.id GROUP BY g.id HAVING g.retired_at IS NULL")}
    for cp in CHECKPOINTS:
        stats[cp]["todo"] = sum(1 for f in furthest.values() if f is None or f < cp)
    active = len(furthest)
    # Where is each group now: its most recent check-in or check-out.
    where = db.execute(
        "SELECT g.id, g.name, g.category, g.retired_at, g.retired_cp, "
        "c.checkpoint, c.checkin_at, c.checkout_at, "
        "(SELECT GROUP_CONCAT(h.name, ', ') FROM hikers h WHERE h.group_id = g.id) AS members "
        "FROM groups g LEFT JOIN ("
        "  SELECT *, ROW_NUMBER() OVER (PARTITION BY group_id "
        "    ORDER BY COALESCE(checkout_at, checkin_at) DESC, id DESC) AS rn FROM checkins"
        ") c ON c.group_id = g.id AND c.rn = 1 "
        "ORDER BY g.category, CAST(g.name AS INTEGER)").fetchall()
    return render_template("index.html", stats=stats, total=total, active=active, where=where)


@app.route("/checkpoint/<int:cp>")
@login_required()
def checkpoint(cp):
    check_cp(cp)
    db = get_db()
    groups = db.execute(
        "SELECT g.*, c.checkin_at, c.checkout_at, "
        "(SELECT COUNT(*) FROM hikers h WHERE h.group_id = g.id) AS size, "
        "(SELECT GROUP_CONCAT(h.name, ', ') FROM hikers h WHERE h.group_id = g.id) AS members "
        "FROM groups g LEFT JOIN checkins c ON c.group_id = g.id AND c.checkpoint = ? "
        "ORDER BY g.category, CAST(g.name AS INTEGER)", (cp,)).fetchall()
    return render_template("checkpoint.html", cp=cp, groups=groups,
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
        changed = save_group_scores(db, group, [cp])
        audit(f"scores groep {group['name']}: " + ("; ".join(changed) or "geen wijziging"))
        flash("Scores opgeslagen.", "ok")
        return redirect(url_for("checkpoint_group", cp=cp, gid=gid))

    visit = db.execute("SELECT * FROM checkins WHERE group_id = ? AND checkpoint = ?",
                       (gid, cp)).fetchone()
    return render_template("group_scores.html", cp=cp, group=group, hikers=hikers, score=score,
                           progress=group_progress(db, group),
                           visit=visit, can_edit=can_edit_checkpoint(cp))


@app.route("/checkpoint/<int:cp>/groep/<int:gid>/<action>", methods=["POST"])
@login_required("admin", "staff")
def checkpoint_stamp(cp, gid, action):
    check_cp(cp, edit=True)
    db = get_db()
    group = db.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
    if group is None:
        abort(404)
    visit = db.execute("SELECT * FROM checkins WHERE group_id = ? AND checkpoint = ?",
                       (gid, cp)).fetchone()
    uid, ts = current_user()["id"], now_utc()
    if group["retired_at"] and action in ("in", "uit"):
        flash(f"Groep {group['name']} is uitgevallen.", "error")
        return redirect(safe_back(url_for("checkpoint", cp=cp)))
    if action == "in":
        if visit and visit["checkin_at"]:
            flash("Deze groep is hier al ingecheckt.", "error")
        else:
            db.execute("INSERT INTO checkins (group_id, checkpoint, checkin_at, checkin_by) "
                       "VALUES (?, ?, ?, ?)", (gid, cp, ts, uid))
            audit(f"groep {group['name']} ingecheckt op CP{cp}")
            flash(f"Groep {group['name']} ingecheckt om {localtime(ts, '%H:%M')}.", "ok")
    elif action == "uit":
        if not visit or not visit["checkin_at"]:
            flash("Groep moet eerst inchecken.", "error")
        elif visit["checkout_at"]:
            flash("Deze groep is hier al uitgecheckt.", "error")
        else:
            db.execute("UPDATE checkins SET checkout_at = ?, checkout_by = ? WHERE id = ?",
                       (ts, uid, visit["id"]))
            audit(f"groep {group['name']} uitgecheckt op CP{cp}")
            flash(f"Groep {group['name']} uitgecheckt om {localtime(ts, '%H:%M')}.", "ok")
    elif action == "herstel" and current_user()["role"] == "admin":
        db.execute("DELETE FROM checkins WHERE group_id = ? AND checkpoint = ?", (gid, cp))
        audit(f"in/uitcheck van groep {group['name']} op CP{cp} gewist")
        flash("In- en uitcheck gewist.", "ok")
    else:
        abort(400)
    db.commit()
    return redirect(safe_back(url_for("checkpoint", cp=cp)))


@app.route("/groep/<int:gid>/uitgevallen", methods=["POST"])
@login_required("admin")
def group_retire(gid):
    """Mark a group as dropped out, or undo that. Admin only."""
    db = get_db()
    group = db.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
    if group is None:
        abort(404)
    raw = request.form.get("cp", "")
    cp = int(raw) if raw.isdigit() and int(raw) in CHECKPOINTS else None
    if cp is None:
        # Not given: the checkpoint the group was last seen at.
        last = db.execute("SELECT checkpoint FROM checkins WHERE group_id = ? "
                          "ORDER BY COALESCE(checkout_at, checkin_at) DESC, id DESC LIMIT 1",
                          (gid,)).fetchone()
        cp = last[0] if last else None
    if request.form.get("undo"):
        db.execute("UPDATE groups SET retired_at = NULL, retired_cp = NULL WHERE id = ?", (gid,))
        audit(f"groep {group['name']} weer actief gemaakt")
        flash(f"Groep {group['name']} doet weer mee.", "ok")
    elif not group["retired_at"]:
        db.execute("UPDATE groups SET retired_at = ?, retired_cp = ? WHERE id = ?", (now_utc(), cp, gid))
        audit(f"groep {group['name']} uitgevallen" + (f" bij CP{cp}" if cp else ""))
        flash(f"Groep {group['name']} staat nu als uitgevallen.", "ok")
    db.commit()
    return redirect(safe_back(url_for("index")))


@app.route("/uitslag")
@login_required("admin", "viewer")
def results():
    db = get_db()
    rows = db.execute(
        "SELECT g.id, g.name, g.category, g.retired_at, "
        "(SELECT GROUP_CONCAT(h.name, ', ') FROM hikers h WHERE h.group_id = g.id) AS members, "
        "COALESCE(SUM(s.quiz), 0) AS quiz, COALESCE(SUM(s.practical), 0) AS practical, "
        "COUNT(s.id) AS visits FROM groups g LEFT JOIN scores s ON s.group_id = g.id "
        "GROUP BY g.id ORDER BY (COALESCE(SUM(s.quiz), 0) + COALESCE(SUM(s.practical), 0)) DESC, CAST(g.name AS INTEGER)"
    ).fetchall()
    by_cat = {cat: [r for r in rows if r["category"] == cat] for cat in CATEGORIES}
    return render_template("results.html", by_cat=by_cat)


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
        else:
            number = next_group_number(db, cat)
            if number is None:
                lo, hi = CATEGORIES[cat]
                flash(f"Alle nummers van {lo} tot {hi} zijn al in gebruik.", "error")
            else:
                cur = db.execute("INSERT INTO groups (name, category) VALUES (?, ?)", (str(number), cat))
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
        if op == "scores":
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
        elif op == "group_delete":
            db.execute("DELETE FROM hikers WHERE group_id = ?", (gid,))
            db.execute("DELETE FROM groups WHERE id = ?", (gid,))
            db.commit()
            audit(f"groep {group['name']} verwijderd")
            flash("Groep verwijderd.", "ok")
            return redirect(url_for("groups"))
        return redirect(url_for("group_edit", gid=gid))
    hikers = db.execute("SELECT * FROM hikers WHERE group_id = ? ORDER BY id", (gid,)).fetchall()
    return render_template("group_edit.html", group=group, hikers=hikers,
                           progress=group_progress(db, group))


# ------------------------------------------------------------------- users

@app.route("/gebruikers", methods=["GET", "POST"])
@login_required("admin")
def users():
    db = get_db()
    if request.method == "POST":
        form = request.form
        op = form.get("op")
        role = form.get("role")
        cp = form.get("checkpoint", "")
        cp = int(cp) if cp.isdigit() and int(cp) in CHECKPOINTS else None
        if role == "staff" and cp is None:
            flash("Een checkpointpost moet een checkpoint hebben.", "error")
        elif op == "create":
            username = form.get("username", "").strip()
            password = form.get("password", "")
            if not username or len(password) < 8 or role not in ROLES:
                flash("Vul gebruikersnaam, rol en een wachtwoord van minstens 8 tekens in.", "error")
            else:
                try:
                    db.execute("INSERT INTO users (username, password_hash, role, checkpoint) "
                               "VALUES (?, ?, ?, ?)", (username, generate_password_hash(password),
                                                       role, cp if role == "staff" else None))
                    db.commit()
                    audit(f"gebruiker {username} aangemaakt als {role}")
                    flash("Gebruiker aangemaakt.", "ok")
                except sqlite3.IntegrityError:
                    flash("Die gebruikersnaam bestaat al.", "error")
        elif op == "update":
            uid = int(form.get("user_id", 0))
            target = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if target and role in ROLES:
                active = 1 if form.get("active") else 0
                if uid == current_user()["id"] and (role != "admin" or not active):
                    flash("Je kunt je eigen beheerdersrechten niet afnemen.", "error")
                else:
                    db.execute("UPDATE users SET role = ?, checkpoint = ?, active = ? WHERE id = ?",
                               (role, cp if role == "staff" else None, active, uid))
                    if form.get("password"):
                        if len(form["password"]) < 8:
                            flash("Wachtwoord moet minstens 8 tekens hebben.", "error")
                        else:
                            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                                       (generate_password_hash(form["password"]), uid))
                    db.commit()
                    audit(f"gebruiker {target['username']} gewijzigd: rol={role} cp={cp} actief={active}"
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
    page = max(int(request.args.get("p", "1") or 1), 1)
    per = 100
    where, params = "", []
    if who:
        where, params = "WHERE username = ?", [who]
    rows = db.execute(f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                      params + [per + 1, (page - 1) * per]).fetchall()
    names = [r[0] for r in db.execute(
        "SELECT DISTINCT username FROM audit_log WHERE username IS NOT NULL ORDER BY username")]
    return render_template("audit.html", rows=rows[:per], more=len(rows) > per,
                           page=page, who=who, names=names)


@app.errorhandler(403)
def forbidden(e):
    audit("toegang geweigerd")
    return render_template("message.html", text="Je hebt geen toegang tot deze pagina."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("message.html", text="Pagina niet gevonden."), 404


if __name__ == "__main__":
    app.run(debug=True)
