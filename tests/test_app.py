import re

import pytest
from werkzeug.security import generate_password_hash

import app as wh


@pytest.fixture
def client(tmp_path):
    wh.app.config.update(DATABASE=str(tmp_path / "t.sqlite3"), TESTING=True)
    with wh.app.app_context():
        wh.init_db()
        db = wh.get_db()
        for name, role, cp in [("admin", "admin", None), ("post3", "staff", 3), ("kijk", "viewer", None)]:
            db.execute("INSERT INTO users (username, password_hash, role, checkpoint) VALUES (?, ?, ?, ?)",
                       (name, generate_password_hash("geheim123"), role, cp))
        db.execute("INSERT INTO groups (name, category) VALUES ('101', 'junioren')")
        db.execute("INSERT INTO hikers (name, comment, group_id) VALUES ('Anna', 'notenallergie', 1)")
        db.execute("INSERT INTO hikers (name, group_id) VALUES ('Bram', 1)")
        db.commit()
    return wh.app.test_client()


def csrf(c, url="/inloggen"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)


def login(c, user):
    t = csrf(c)
    r = c.post("/inloggen", data={"username": user, "password": "geheim123", "csrf": t})
    assert r.status_code == 302
    return csrf(c, "/profiel")


def test_requires_login(client):
    assert client.get("/").status_code == 302


def test_post_without_csrf_rejected(client):
    login(client, "admin")
    assert client.post("/groepen", data={"name": "X", "category": "junioren"}).status_code == 400


def test_staff_checkin_scores_and_limits(client):
    t = login(client, "post3")
    assert client.get("/").headers["Location"].endswith("/checkpoint/3")
    page = client.get("/checkpoint/3/groep/1").get_data(as_text=True)
    assert "Anna" in page and "notenallergie" in page
    assert client.get("/checkpoint/4").status_code == 403
    assert client.get("/logboek").status_code == 403
    assert client.post("/checkpoint/4/groep/1/in", data={"csrf": t}).status_code == 403
    # checkout before checkin is refused
    client.post("/checkpoint/3/groep/1/uit", data={"csrf": t})
    client.post("/checkpoint/3/groep/1/in", data={"csrf": t})
    client.post("/checkpoint/3/groep/1/uit", data={"csrf": t})
    client.post("/checkpoint/3/groep/1", data={"csrf": t, "quiz": "8", "practical": "11"})
    with wh.app.app_context():
        db = wh.get_db()
        v = db.execute("SELECT * FROM checkins").fetchone()
        assert v["checkin_at"] and v["checkout_at"] and v["checkpoint"] == 3
        s = [(r["group_id"], r["checkpoint"], r["quiz"], r["practical"]) for r in db.execute("SELECT * FROM scores")]
        assert s == [(1, 3, 8, None)]
        actions = [r["action"] for r in db.execute("SELECT action FROM audit_log WHERE username='post3'")]
        assert "groep 101 ingecheckt op CP3" in actions
        assert "toegang geweigerd" in actions


def test_viewer_read_only(client):
    t = login(client, "kijk")
    page = client.get("/uitslag").get_data(as_text=True)
    assert "101" in page and "Anna, Bram" in page
    assert client.get("/checkpoint/5").status_code == 200
    assert client.post("/checkpoint/5/groep/1/in", data={"csrf": t}).status_code == 403
    assert client.post("/checkpoint/5/groep/1", data={"csrf": t, "quiz": "3"}).status_code == 403
    assert client.get("/gebruikers").status_code == 403


def test_new_groups_get_next_number(client):
    t = login(client, "admin")
    form = {"csrf": t, "category": "junioren", "hiker_name": ["Cas", "Dirk", ""],
            "hiker_comment": ["", "glutenvrij", ""]}
    client.post("/groepen", data=form)
    client.post("/groepen", data={**form, "hiker_name": ["Eva", "", ""]})
    client.post("/groepen", data={**form, "category": "veteranen", "hiker_name": ["Fien", "", ""]})
    client.post("/groepen", data={**form, "hiker_name": ["", "", ""]})  # no names: refused
    with wh.app.app_context():
        db = wh.get_db()
        groups = {r["name"]: r["category"] for r in db.execute("SELECT * FROM groups")}
        assert groups == {"101": "junioren", "102": "junioren", "103": "junioren", "301": "veteranen"}
        names = [r[0] for r in db.execute(
            "SELECT h.name FROM hikers h JOIN groups g ON g.id = h.group_id WHERE g.name = '102' ORDER BY h.id")]
        assert names == ["Cas", "Dirk"]
        assert db.execute("SELECT comment FROM hikers WHERE name = 'Dirk'").fetchone()[0] == "glutenvrij"
    assert client.get("/logboek").status_code == 200


def test_next_number_fills_gap_when_range_is_full(client):
    with wh.app.app_context():
        db = wh.get_db()
        db.execute("DELETE FROM groups")
        db.executemany("INSERT INTO groups (name, category) VALUES (?, 'senioren')",
                       [(str(n),) for n in range(201, 300) if n != 250])
        assert wh.next_group_number(db, "senioren") == 250
        db.execute("INSERT INTO groups (name, category) VALUES ('250', 'senioren')")
        assert wh.next_group_number(db, "senioren") is None


def test_retire_group(client):
    t = login(client, "post3")
    assert client.post("/groep/1/uitgevallen", data={"csrf": t, "cp": "4"}).status_code == 403
    client.post("/groep/1/uitgevallen", data={"csrf": t, "cp": "3"})
    page = client.get("/checkpoint/3").get_data(as_text=True)
    assert "uitgevallen" in page and "Inchecken" not in page
    client.post("/checkpoint/3/groep/1/in", data={"csrf": t})
    assert client.post("/groep/1/uitgevallen", data={"csrf": t, "cp": "3", "undo": "1"}).status_code == 403
    with wh.app.app_context():
        g = wh.get_db().execute("SELECT * FROM groups WHERE id = 1").fetchone()
        assert g["retired_at"] and g["retired_cp"] == 3
        assert wh.get_db().execute("SELECT COUNT(*) FROM checkins").fetchone()[0] == 0
    client.post("/uitloggen", data={"csrf": t})
    t = login(client, "admin")
    assert "uitgevallen bij CP 3" in client.get("/").get_data(as_text=True)
    client.post("/groep/1/uitgevallen", data={"csrf": t, "undo": "1"})
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT retired_at FROM groups WHERE id = 1").fetchone()[0] is None


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, category TEXT NOT NULL);
        CREATE TABLE hikers (id INTEGER PRIMARY KEY, number INTEGER NOT NULL UNIQUE, name TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '', group_id INTEGER REFERENCES groups(id));
        INSERT INTO groups VALUES (1, 'De Bevers', 'junioren'), (2, 'Uilen', 'veteranen');
        INSERT INTO hikers VALUES (1, 101, 'Anna', 'noten', 1), (2, 301, 'Bob', '', 2);
    """)
    con.commit(); con.close()
    wh.app.config.update(DATABASE=str(path))
    with wh.app.app_context():
        wh.init_db()
        db = wh.get_db()
        assert [r[0] for r in db.execute("SELECT name FROM groups ORDER BY id")] == ["101", "301"]
        assert "number" not in [r[1] for r in db.execute("PRAGMA table_info(hikers)")]
        assert db.execute("SELECT name, comment, group_id FROM hikers WHERE id = 1").fetchone()[:] == ("Anna", "noten", 1)


def test_localtime_filter():
    assert wh.localtime("2026-06-13T08:00:00+00:00", "%H:%M") == "10:00"  # summer, UTC+2
    assert wh.localtime("2026-11-21T08:00:00+00:00", "%H:%M") == "09:00"  # winter, UTC+1


def test_where_is_each_group(client):
    t = login(client, "admin")
    assert "nog niet gestart" in client.get("/").get_data(as_text=True)
    client.post("/checkpoint/1/groep/1/in", data={"csrf": t})
    client.post("/checkpoint/1/groep/1/uit", data={"csrf": t})
    assert "onderweg na CP 1" in client.get("/").get_data(as_text=True)
    client.post("/checkpoint/2/groep/1/in", data={"csrf": t})
    assert "op CP 2" in client.get("/").get_data(as_text=True)


def test_theme_setting(client):
    t = login(client, "kijk")
    assert 'data-theme="auto"' in client.get("/profiel").get_data(as_text=True)
    client.post("/profiel", data={"csrf": t, "op": "theme", "theme": "dark"})
    assert 'data-theme="dark"' in client.get("/").get_data(as_text=True)
    client.post("/profiel", data={"csrf": t, "op": "theme", "theme": "paars"})
    assert 'data-theme="dark"' in client.get("/").get_data(as_text=True)


def test_old_database_gets_theme_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, "
                "password_hash TEXT NOT NULL, role TEXT NOT NULL, checkpoint INTEGER, "
                "active INTEGER NOT NULL DEFAULT 1)")
    con.commit(); con.close()
    wh.app.config.update(DATABASE=str(path))
    with wh.app.app_context():
        wh.init_db()
        cols = [r[1] for r in wh.get_db().execute("PRAGMA table_info(users)")]
    assert "theme" in cols
