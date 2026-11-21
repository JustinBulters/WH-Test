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

# Hiker categories and their number ranges.
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
    path = os.path.join(BASE_DIR, ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'staff', 'viewer')),
    checkpoint INTEGER,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL CHECK (category IN ('junioren', 'senioren', 'veteranen'))
);
CREATE TABLE IF NOT EXISTS hikers (
    id INTEGER PRIMARY KEY,
    number INTEGER NOT NULL UNIQUE,
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
    hiker_id INTEGER NOT NULL REFERENCES hikers(id) ON DELETE CASCADE,
    checkpoint INTEGER NOT NULL,
    quiz INTEGER CHECK (quiz BETWEEN 1 AND 10),
    practical INTEGER CHECK (practical BETWEEN 1 AND 10),
    updated_at TEXT NOT NULL,
    updated_by INTEGER REFERENCES users(id),
    UNIQUE (hiker_id, checkpoint)
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
    db.commit()


@app.cli.command("init-db")
@click.option("--admin", default="admin", help="Gebruikersnaam van de eerste beheerder.")
def init_db_command(admin):
    """Maak de database aan en een eerste beheerder."""
    init_db()
    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
        click.echo("Database bijgewerkt. Er bestaat al een beheerder.")
        return
    password = secrets.token_urlsafe(9)
    db.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
               (admin, generate_password_hash(password)))
    db.commit()
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


def category_for_number(number):
    for cat, (lo, hi) in CATEGORIES.items():
        if lo <= number <= hi:
            return cat
    return None


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


@app.route("/wachtwoord", methods=["GET", "POST"])
@login_required()
def change_password():
    if request.method == "POST":
        user = current_user()
        new = request.form.get("new", "")
        if not check_password_hash(user["password_hash"], request.form.get("old", "")):
            flash("Huidig wachtwoord klopt niet.", "error")
        elif len(new) < 8:
            flash("Nieuw wachtwoord moet minstens 8 tekens hebben.", "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (generate_password_hash(new), user["id"]))
            db.commit()
            audit("eigen wachtwoord gewijzigd")
            flash("Wachtwoord gewijzigd.", "ok")
            return redirect(url_for("index"))
    return render_template("password.html")


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
    return render_template("index.html", stats=stats, total=total)


@app.route("/checkpoint/<int:cp>")
@login_required()
def checkpoint(cp):
    check_cp(cp)
    db = get_db()
    groups = db.execute(
        "SELECT g.*, c.checkin_at, c.checkout_at, "
        "(SELECT COUNT(*) FROM hikers h WHERE h.group_id = g.id) AS size, "
        "(SELECT GROUP_CONCAT(h.number, ', ') FROM hikers h WHERE h.group_id = g.id) AS numbers "
        "FROM groups g LEFT JOIN checkins c ON c.group_id = g.id AND c.checkpoint = ? "
        "ORDER BY g.category, g.name", (cp,)).fetchall()
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
    hikers = db.execute(
        "SELECT h.*, s.quiz, s.practical FROM hikers h "
        "LEFT JOIN scores s ON s.hiker_id = h.id AND s.checkpoint = ? "
        "WHERE h.group_id = ? ORDER BY h.number", (cp, gid)).fetchall()

    if request.method == "POST":
        check_cp(cp, edit=True)
        changed = []
        for h in hikers:
            vals = []
            for field in ("quiz", "practical"):
                raw = request.form.get(f"{field}_{h['id']}", "")
                vals.append(int(raw) if raw.isdigit() and 1 <= int(raw) <= 10 else None)
            if vals != [h["quiz"], h["practical"]]:
                db.execute(
                    "INSERT INTO scores (hiker_id, checkpoint, quiz, practical, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (hiker_id, checkpoint) DO UPDATE SET "
                    "quiz = excluded.quiz, practical = excluded.practical, "
                    "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                    (h["id"], cp, vals[0], vals[1], now_utc(), current_user()["id"]))
                changed.append(f"#{h['number']} quiz={vals[0]} praktijk={vals[1]}")
        db.commit()
        audit(f"scores CP{cp} groep {group['name']}: " + ("; ".join(changed) or "geen wijziging"))
        flash("Scores opgeslagen.", "ok")
        return redirect(url_for("checkpoint_group", cp=cp, gid=gid))

    visit = db.execute("SELECT * FROM checkins WHERE group_id = ? AND checkpoint = ?",
                       (gid, cp)).fetchone()
    return render_template("group_scores.html", cp=cp, group=group, hikers=hikers,
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
    if action == "in":
        if visit and visit["checkin_at"]:
            flash("Deze groep is hier al ingecheckt.", "error")
        else:
            db.execute("INSERT INTO checkins (group_id, checkpoint, checkin_at, checkin_by) "
                       "VALUES (?, ?, ?, ?)", (gid, cp, ts, uid))
            audit(f"groep {group['name']} ingecheckt op CP{cp}")
            flash(f"{group['name']} ingecheckt om {localtime(ts, '%H:%M')}.", "ok")
    elif action == "uit":
        if not visit or not visit["checkin_at"]:
            flash("Groep moet eerst inchecken.", "error")
        elif visit["checkout_at"]:
            flash("Deze groep is hier al uitgecheckt.", "error")
        else:
            db.execute("UPDATE checkins SET checkout_at = ?, checkout_by = ? WHERE id = ?",
                       (ts, uid, visit["id"]))
            audit(f"groep {group['name']} uitgecheckt op CP{cp}")
            flash(f"{group['name']} uitgecheckt om {localtime(ts, '%H:%M')}.", "ok")
    elif action == "herstel" and current_user()["role"] == "admin":
        db.execute("DELETE FROM checkins WHERE group_id = ? AND checkpoint = ?", (gid, cp))
        audit(f"in/uitcheck van groep {group['name']} op CP{cp} gewist")
        flash("In- en uitcheck gewist.", "ok")
    else:
        abort(400)
    db.commit()
    return redirect(request.form.get("back") or url_for("checkpoint", cp=cp))


@app.route("/uitslag")
@login_required("admin", "viewer")
def results():
    db = get_db()
    rows = db.execute(
        "SELECT h.number, h.name, g.name AS group_name, "
        "COALESCE(SUM(s.quiz), 0) AS quiz, COALESCE(SUM(s.practical), 0) AS practical, "
        "COUNT(s.id) AS visits FROM hikers h "
        "LEFT JOIN groups g ON g.id = h.group_id LEFT JOIN scores s ON s.hiker_id = h.id "
        "GROUP BY h.id ORDER BY (COALESCE(SUM(s.quiz), 0) + COALESCE(SUM(s.practical), 0)) DESC, h.number"
    ).fetchall()
    by_cat = {cat: [] for cat in CATEGORIES}
    for r in rows:
        cat = category_for_number(r["number"])
        if cat:
            by_cat[cat].append(r)
    return render_template("results.html", by_cat=by_cat)


# --------------------------------------------------------- groups & hikers

@app.route("/groepen", methods=["GET", "POST"])
@login_required("admin", "viewer")
def groups():
    db = get_db()
    if request.method == "POST":
        if current_user()["role"] != "admin":
            abort(403)
        name = request.form.get("name", "").strip()
        cat = request.form.get("category")
        if not name or cat not in CATEGORIES:
            flash("Vul een naam en categorie in.", "error")
        else:
            try:
                db.execute("INSERT INTO groups (name, category) VALUES (?, ?)", (name, cat))
                db.commit()
                audit(f"groep {name} ({cat}) aangemaakt")
                flash("Groep aangemaakt.", "ok")
            except sqlite3.IntegrityError:
                flash("Er bestaat al een groep met die naam.", "error")
        return redirect(url_for("groups"))
    groups = db.execute("SELECT * FROM groups ORDER BY category, name").fetchall()
    hikers = {}
    for h in db.execute("SELECT * FROM hikers ORDER BY number"):
        hikers.setdefault(h["group_id"], []).append(h)
    return render_template("groups.html", groups=groups, hikers=hikers)


@app.route("/groepen/<int:gid>", methods=["GET", "POST"])
@login_required("admin")
def group_edit(gid):
    db = get_db()
    group = db.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
    if group is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        op = form.get("op")
        if op == "hiker_save":
            hid = form.get("hiker_id")
            name = form.get("name", "").strip()
            comment = form.get("comment", "").strip()
            raw = form.get("number", "")
            number = int(raw) if raw.isdigit() else None
            lo, hi = CATEGORIES[group["category"]]
            if not name or number is None:
                flash("Vul nummer en naam in.", "error")
            elif not lo <= number <= hi:
                flash(f"Nummer moet tussen {lo} en {hi} liggen voor {group['category']}.", "error")
            else:
                try:
                    if hid:
                        db.execute("UPDATE hikers SET number = ?, name = ?, comment = ? "
                                   "WHERE id = ? AND group_id = ?", (number, name, comment, hid, gid))
                        audit(f"wandelaar #{number} gewijzigd")
                    else:
                        db.execute("INSERT INTO hikers (number, name, comment, group_id) "
                                   "VALUES (?, ?, ?, ?)", (number, name, comment, gid))
                        audit(f"wandelaar #{number} toegevoegd aan groep {group['name']}")
                    db.commit()
                    flash("Wandelaar opgeslagen.", "ok")
                except sqlite3.IntegrityError:
                    flash(f"Nummer {number} is al in gebruik.", "error")
        elif op == "hiker_delete":
            h = db.execute("SELECT number FROM hikers WHERE id = ? AND group_id = ?",
                           (form.get("hiker_id"), gid)).fetchone()
            if h:
                db.execute("DELETE FROM hikers WHERE id = ?", (form.get("hiker_id"),))
                db.commit()
                audit(f"wandelaar #{h['number']} verwijderd")
                flash("Wandelaar verwijderd.", "ok")
        elif op == "group_rename":
            name = form.get("name", "").strip()
            if name:
                try:
                    db.execute("UPDATE groups SET name = ? WHERE id = ?", (name, gid))
                    db.commit()
                    audit(f"groep {group['name']} hernoemd naar {name}")
                except sqlite3.IntegrityError:
                    flash("Er bestaat al een groep met die naam.", "error")
        elif op == "group_delete":
            db.execute("DELETE FROM hikers WHERE group_id = ?", (gid,))
            db.execute("DELETE FROM groups WHERE id = ?", (gid,))
            db.commit()
            audit(f"groep {group['name']} verwijderd")
            flash("Groep verwijderd.", "ok")
            return redirect(url_for("groups"))
        return redirect(url_for("group_edit", gid=gid))
    hikers = db.execute("SELECT * FROM hikers WHERE group_id = ? ORDER BY number", (gid,)).fetchall()
    return render_template("group_edit.html", group=group, hikers=hikers)


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
