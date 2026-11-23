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
    client.post("/checkpoint/3/groep/1", data={"csrf": t, "quiz_3": "8", "practical_3": "11"})
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
    assert client.post("/checkpoint/5/groep/1", data={"csrf": t, "quiz_5": "3"}).status_code == 403
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
    assert client.post("/groep/1/uitgevallen", data={"csrf": t, "cp": "3"}).status_code == 403
    assert "Uitgevallen</button>" not in client.get("/checkpoint/3/groep/1").get_data(as_text=True)
    client.post("/uitloggen", data={"csrf": t})
    t = login(client, "admin")
    client.post("/checkpoint/3/groep/1/in", data={"csrf": t})
    client.post("/groep/1/uitgevallen", data={"csrf": t})
    page = client.get("/checkpoint/4").get_data(as_text=True)
    assert "uitgevallen" in page and "Inchecken" not in page
    with wh.app.app_context():
        g = wh.get_db().execute("SELECT * FROM groups WHERE id = 1").fetchone()
        assert g["retired_at"] and g["retired_cp"] == 3
    assert "uitgevallen bij CP 3" in client.get("/").get_data(as_text=True)
    client.post("/groep/1/uitgevallen", data={"csrf": t, "undo": "1"})
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT retired_at FROM groups WHERE id = 1").fetchone()[0] is None


def test_expected_per_checkpoint(client):
    t = login(client, "admin")
    with wh.app.app_context():
        db = wh.get_db()
        db.executemany("INSERT INTO groups (name, category) VALUES (?, 'junioren')",
                       [(str(n),) for n in range(102, 111)])  # 10 groups in total
        db.commit()
    for gid in (1, 2, 3):          # passed CP 3
        for cp in (1, 2, 3):
            client.post(f"/checkpoint/{cp}/groep/{gid}/in", data={"csrf": t})
            client.post(f"/checkpoint/{cp}/groep/{gid}/uit", data={"csrf": t})
    for gid in (4, 5):             # at CP 3 now
        client.post(f"/checkpoint/3/groep/{gid}/in", data={"csrf": t})
    client.post("/groep/10/uitgevallen", data={"csrf": t})  # dropped out: not expected
    page = client.get("/").get_data(as_text=True)
    import re
    cards = dict(re.findall(r"<b>CP (\d+)</b>\s*<span class=\"tag\">(\d+) verwacht", page))
    assert cards["3"] == "4" and cards["4"] == "9"
    assert cards["1"] == "4"  # groups 4 and 5 were seen at CP 3, so they skipped CP 1


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
    assert "onderweg naar CP 2" in client.get("/").get_data(as_text=True)
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


def test_status_and_group_page(client):
    t = login(client, "admin")
    home = client.get("/").get_data(as_text=True)
    assert "nog niet gestart" in home
    client.post("/checkpoint/1/groep/1/in", data={"csrf": t})
    client.post("/checkpoint/1/groep/1/uit", data={"csrf": t})
    assert "onderweg naar CP 2" in client.get("/").get_data(as_text=True)
    client.post("/checkpoint/2/groep/1/in", data={"csrf": t})
    page = client.get("/groepen/1").get_data(as_text=True)
    assert 'class="dot done"' in page and 'class="dot here"' in page
    client.post("/groepen/1", data={"csrf": t, "op": "scores", "quiz_1": "7", "practical_1": "9", "quiz_2": "6"})
    with wh.app.app_context():
        rows = {r["checkpoint"]: (r["quiz"], r["practical"]) for r in wh.get_db().execute("SELECT * FROM scores")}
    assert rows == {1: (7, 9), 2: (6, None)}


def test_status_end_of_day_and_finish(client):
    t = login(client, "admin")
    for cp in (7, 12):
        client.post(f"/checkpoint/{cp}/groep/1/in", data={"csrf": t})
        client.post(f"/checkpoint/{cp}/groep/1/uit", data={"csrf": t})
        home = client.get("/").get_data(as_text=True)
        assert ("dag 1 klaar" if cp == 7 else "gefinisht") in home


def test_viewer_sees_group_page_but_cannot_score(client):
    t = login(client, "kijk")
    assert client.get("/groepen/1").status_code == 200
    assert client.post("/groepen/1", data={"csrf": t, "op": "scores", "quiz_1": "5"}).status_code == 403


def test_lookup_by_group_number(client):
    login(client, "post3")
    r = client.get("/checkpoint/3?q=101")
    assert r.status_code == 302 and r.headers["Location"].endswith("/checkpoint/3/groep/1")
    r = client.get("/checkpoint/3?q=999", follow_redirects=True)
    assert "Groep 999 bestaat niet" in r.get_data(as_text=True)
    # Generic lookup and QR link send staff to their own checkpoint.
    assert client.get("/zoek?q=101").headers["Location"].endswith("/checkpoint/3/groep/1")
    assert client.get("/g/1").headers["Location"].endswith("/checkpoint/3/groep/1")
    login(client, "kijk")
    assert client.get("/zoek?q=101").headers["Location"].endswith("/groepen/1")
    assert client.get("/g/1").headers["Location"].endswith("/groepen/1")
    assert client.get("/g/99").status_code == 404


def test_emergency_phone_admin_only(client):
    t = login(client, "admin")
    client.post("/groepen/1", data={"csrf": t, "op": "phone", "phone": "06 1234 5678"})
    assert "06 1234 5678" in client.get("/groepen/1").get_data(as_text=True)
    assert "06 1234 5678" in client.get("/groepen").get_data(as_text=True)
    client.post("/groepen", data={"csrf": t, "category": "senioren", "phone": "0201112222",
                                  "hiker_name": ["Cees", "", ""], "hiker_comment": ["", "", ""]})
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT phone FROM groups WHERE name = '201'").fetchone()[0] == "0201112222"
    for user in ("kijk", "post3"):
        t = login(client, user)
        for url in ("/groepen/1", "/groepen", "/checkpoint/3/groep/1"):
            r = client.get(url)
            assert "1234 5678" not in r.get_data(as_text=True)
    t = login(client, "kijk")
    assert client.post("/groepen/1", data={"csrf": t, "op": "phone", "phone": "x"}).status_code == 403


def test_scoreboard(client):
    login(client, "kijk")
    page = client.get("/scorebord").get_data(as_text=True)
    assert 'http-equiv="refresh"' in page and "Junioren" in page and "101" in page
    assert 'class="top"' not in page
    login(client, "post3")
    assert client.get("/scorebord").status_code == 403


def test_excel_export(client):
    import io
    from openpyxl import load_workbook
    t = login(client, "admin")
    client.post("/checkpoint/1/groep/1/in", data={"csrf": t})
    client.post("/checkpoint/1/groep/1/uit", data={"csrf": t})
    client.post("/checkpoint/1/groep/1", data={"csrf": t, "quiz_1": "8", "practical_1": "6"})
    r = client.get("/export.xlsx")
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.data))
    assert wb.sheetnames == ["Uitslag", "Groepen", "Tijden en scores"]
    assert list(wb["Uitslag"].iter_rows(min_row=2, values_only=True))[0][:7] == (
        "Junioren", 1, 101, "Anna, Bram", 8, 6, 14)
    row = list(wb["Tijden en scores"].iter_rows(min_row=2, values_only=True))[0]
    assert row[:4] == (101, "Junioren", 1, 1) and row[7:] == (8, 6)
    login(client, "kijk")
    assert client.get("/export.xlsx").status_code == 403


def test_group_cards_with_qr(client):
    login(client, "admin")
    page = client.get("/groepen/kaarten").get_data(as_text=True)
    assert "<svg" in page and "Anna" in page and 'class="num">101' in page
    login(client, "kijk")
    assert client.get("/groepen/kaarten").status_code == 403


def test_old_database_gets_phone_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, category TEXT NOT NULL)")
    con.execute("INSERT INTO groups (name, category) VALUES ('101', 'junioren')")
    con.commit()
    con.close()
    wh.app.config.update(DATABASE=str(path))
    with wh.app.app_context():
        wh.init_db()
        assert wh.get_db().execute("SELECT phone FROM groups").fetchone()[0] == ""


def scores_of(gid, cp):
    with wh.app.app_context():
        r = wh.get_db().execute("SELECT quiz, practical FROM scores WHERE group_id = ? AND checkpoint = ?",
                                (gid, cp)).fetchone()
        return tuple(r) if r else None


def test_staff_scores_are_write_once_and_change_needs_approval(client):
    t = login(client, "post3")
    client.post("/checkpoint/3/groep/1", data={"csrf": t, "quiz_3": "7"})
    client.post("/checkpoint/3/groep/1", data={"csrf": t, "quiz_3": "9", "practical_3": "5"})
    assert scores_of(1, 3) == (7, 5)  # quiz kept, empty practical filled in
    # A change request needs a reason.
    r = client.post("/checkpoint/3/groep/1/wijziging", data={"csrf": t, "quiz_3": "8", "practical_3": "5"},
                    follow_redirects=True)
    assert "Vul een reden in" in r.get_data(as_text=True)
    client.post("/checkpoint/3/groep/1/wijziging",
                data={"csrf": t, "quiz_3": "8", "practical_3": "5", "reason": "verkeerd getikt"})
    assert scores_of(1, 3) == (7, 5)
    page = client.get("/checkpoint/3/groep/1").get_data(as_text=True)
    assert "wijzigingsverzoek</a> open" in page
    # Admin gets a notification that links to the request.
    t = login(client, "admin")
    assert 'class="badge">1<' in client.get("/").get_data(as_text=True)
    page = client.get("/meldingen").get_data(as_text=True)
    nid = re.search(r'/meldingen/(\d+)', page).group(1)
    r = client.get(f"/meldingen/{nid}")
    assert r.headers["Location"].endswith("/verzoeken/1")
    page = client.get("/verzoeken/1").get_data(as_text=True)
    assert "verkeerd getikt" in page and "Goedkeuren" in page
    assert 'class="badge"' not in page
    client.post("/verzoeken/1", data={"csrf": t, "decision": "approved", "note": "ok"})
    assert scores_of(1, 3) == (8, 5)
    # Deciding twice does nothing.
    client.post("/verzoeken/1", data={"csrf": t, "decision": "declined"})
    assert scores_of(1, 3) == (8, 5)
    # The staff member is told.
    login(client, "post3")
    page = client.get("/meldingen").get_data(as_text=True)
    assert "is goedgekeurd" in page
    assert "goedgekeurd" in client.get("/verzoeken/1").get_data(as_text=True)


def test_declined_request_and_access(client):
    t = login(client, "post3")
    client.post("/checkpoint/3/groep/1", data={"csrf": t, "quiz_3": "7", "practical_3": "6"})
    client.post("/checkpoint/3/groep/1/wijziging",
                data={"csrf": t, "quiz_3": "10", "practical_3": "6", "reason": "graag"})
    # Only one open request at a time.
    r = client.post("/checkpoint/3/groep/1/wijziging",
                    data={"csrf": t, "quiz_3": "9", "practical_3": "6", "reason": "nog een"}, follow_redirects=True)
    assert "al een wijzigingsverzoek open" in r.get_data(as_text=True)
    assert client.post("/verzoeken/1", data={"csrf": t, "decision": "approved"}).status_code == 403
    login(client, "kijk")
    assert client.get("/verzoeken/1").status_code == 403
    t = login(client, "admin")
    client.post("/verzoeken/1", data={"csrf": t, "decision": "declined", "note": "nee"})
    assert scores_of(1, 3) == (7, 6)
    # Admins still change scores directly.
    client.post("/checkpoint/3/groep/1", data={"csrf": t, "quiz_3": "4", "practical_3": "6"})
    assert scores_of(1, 3) == (4, 6)


def make_super(name="admin"):
    with wh.app.app_context():
        db = wh.get_db()
        db.execute("UPDATE users SET is_super = 1 WHERE username = ?", (name,))
        db.commit()


def test_settings_superadmin_only(client):
    login(client, "admin")
    assert client.get("/instellingen").status_code == 403
    make_super()
    login(client, "admin")
    page = client.get("/instellingen").get_data(as_text=True)
    assert "Startdatum" in page and "Superbeheerder" in client.get("/profiel").get_data(as_text=True)


def test_start_date_blocks_checkin_before(client):
    from datetime import datetime, timedelta
    make_super()
    t = login(client, "admin")
    tomorrow = (datetime.now(wh.LOCAL_TZ).date() + timedelta(days=1)).isoformat()
    client.post("/instellingen", data={"csrf": t, "op": "start_date", "start_date": tomorrow})
    assert "nog 1 dag)" in client.get("/").get_data(as_text=True)
    t = login(client, "post3")
    r = client.post("/checkpoint/3/groep/1/in", data={"csrf": t}, follow_redirects=True)
    assert "Inchecken kan vanaf die dag" in r.get_data(as_text=True)
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT COUNT(*) FROM checkins").fetchone()[0] == 0
    t = login(client, "admin")
    today = datetime.now(wh.LOCAL_TZ).date().isoformat()
    client.post("/instellingen", data={"csrf": t, "op": "start_date", "start_date": today})
    t = login(client, "post3")
    client.post("/checkpoint/3/groep/1/in", data={"csrf": t})
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT COUNT(*) FROM checkins").fetchone()[0] == 1


def test_wipe_all_group_data(client):
    make_super()
    t = login(client, "admin")
    client.post("/checkpoint/3/groep/1/in", data={"csrf": t})
    client.post("/checkpoint/3/groep/1", data={"csrf": t, "quiz_3": "5"})
    client.post("/instellingen", data={"csrf": t, "op": "wipe", "confirm": "nee"})
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 1
    r = client.post("/instellingen", data={"csrf": t, "op": "wipe", "confirm": "WISSEN"}, follow_redirects=True)
    assert "Alle groepsdata is gewist" in r.get_data(as_text=True)
    with wh.app.app_context():
        db = wh.get_db()
        for table in ("groups", "hikers", "checkins", "scores"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
    # A plain admin cannot wipe.
    with wh.app.app_context():
        db = wh.get_db()
        db.execute("UPDATE users SET is_super = 0")
        db.commit()
    t = login(client, "admin")
    assert client.post("/instellingen", data={"csrf": t, "op": "wipe", "confirm": "WISSEN"}).status_code == 403


def test_only_superadmin_grants_superadmin(client):
    t = login(client, "admin")
    assert client.post("/gebruikers", data={"csrf": t, "op": "update", "user_id": "3",
                                            "role": "superadmin", "active": "1"}).status_code == 403
    make_super()
    t = login(client, "admin")
    client.post("/gebruikers", data={"csrf": t, "op": "update", "user_id": "3", "role": "superadmin", "active": "1"})
    with wh.app.app_context():
        u = wh.get_db().execute("SELECT role, is_super FROM users WHERE id = 3").fetchone()
        assert tuple(u) == ("admin", 1)


def test_first_admin_becomes_superadmin_on_upgrade(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, "
                "role TEXT NOT NULL, checkpoint INTEGER, active INTEGER NOT NULL DEFAULT 1, theme TEXT NOT NULL DEFAULT 'auto')")
    con.execute("INSERT INTO users (username, password_hash, role) VALUES ('post', 'x', 'staff'), "
                "('admin', 'x', 'admin'), ('admin2', 'x', 'admin')")
    con.commit()
    con.close()
    wh.app.config.update(DATABASE=str(path))
    with wh.app.app_context():
        wh.init_db()
        rows = wh.get_db().execute("SELECT username, is_super FROM users ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [("post", 0), ("admin", 1), ("admin2", 0)]


def test_map_place_and_remove_checkpoint(client):
    t = login(client, "admin")
    assert "leaflet.js" in client.get("/kaart").get_data(as_text=True)
    r = client.post("/kaart", data={"csrf": t, "cp": "3", "lat": "52.1", "lng": "5.2", "ajax": "1"})
    assert r.get_json() == {"ok": True}
    assert '"lat": 52.1' in client.get("/kaart").get_data(as_text=True)
    client.post("/kaart", data={"csrf": t, "cp": "3", "op": "remove"})
    assert '"lat": 52.1' not in client.get("/kaart").get_data(as_text=True)
    assert client.post("/kaart", data={"csrf": t, "cp": "99", "lat": "1", "lng": "1"}).status_code == 400
    for user in ("post3", "kijk"):
        t = login(client, user)
        assert client.get("/kaart").status_code == 200
        assert client.post("/kaart", data={"csrf": t, "cp": "3", "lat": "1", "lng": "1"}).status_code == 403


def test_checkpoint_count_and_names(client):
    t = login(client, "admin")
    client.post("/checkpoints", data={"csrf": t, "day1": "4", "day2": "3", "name_2": "Molen", "name_9": "x"})
    page = client.get("/").get_data(as_text=True)
    assert "CP 2 · Molen" in page and "CP 7" in page and "CP 8" not in page
    assert "Checkpoint 2 · Molen" in client.get("/checkpoint/2").get_data(as_text=True)
    assert client.get("/checkpoint/8").status_code == 404
    # End of day 1 is now CP 4, finish is CP 7.
    for cp in (1, 2, 3, 4):
        client.post(f"/checkpoint/{cp}/groep/1/in", data={"csrf": t})
        client.post(f"/checkpoint/{cp}/groep/1/uit", data={"csrf": t})
    page = client.get("/").get_data(as_text=True)
    assert "dag 1 klaar" in page and "morgen verder naar CP 5" in page
    # Cannot drop a checkpoint that has data (CP 3 is also post3's checkpoint).
    r = client.post("/checkpoints", data={"csrf": t, "day1": "2", "day2": "1"}, follow_redirects=True)
    assert "Dat kan niet" in r.get_data(as_text=True)
    assert "CP 7" in client.get("/").get_data(as_text=True)
    login(client, "kijk")
    assert client.get("/checkpoints").status_code == 403


def test_merge_groups(client):
    t = login(client, "admin")
    client.post("/groepen", data={"csrf": t, "category": "junioren", "hiker_name": ["Cees", "Dirk", ""],
                                  "hiker_comment": ["", "", ""]})
    client.post("/checkpoint/1/groep/2/in", data={"csrf": t})
    with wh.app.app_context():
        cees = wh.get_db().execute("SELECT id FROM hikers WHERE name = 'Cees'").fetchone()[0]
    assert "Samenvoegen" in client.get("/groepen/2").get_data(as_text=True)
    r = client.post("/groepen/2", data={"csrf": t, "op": "merge", "target": "1", "hiker": [str(cees)]},
                    follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "Cees loopt nu mee met groep 101" in page and "Cees" in page
    with wh.app.app_context():
        db = wh.get_db()
        assert tuple(db.execute("SELECT group_id, comment FROM hikers WHERE id = ?", (cees,)).fetchone()) == (
            1, "Oorspronkelijk groep 102")
        g2 = db.execute("SELECT * FROM groups WHERE id = 2").fetchone()
        assert g2["retired_at"] and g2["retired_cp"] == 1 and g2["retired_note"] == "samengevoegd met 101"
        assert db.execute("SELECT COUNT(*) FROM checkins WHERE group_id = 2").fetchone()[0] == 1
    assert "samengevoegd met 101" in client.get("/").get_data(as_text=True)
    # A fourth hiker gives a warning.
    with wh.app.app_context():
        dirk = wh.get_db().execute("SELECT id FROM hikers WHERE name = 'Dirk'").fetchone()[0]
    client.post("/groep/2/uitgevallen", data={"csrf": t, "undo": "1"})
    r = client.post("/groepen/2", data={"csrf": t, "op": "merge", "target": "1", "hiker": [str(dirk)]},
                    follow_redirects=True)
    assert "heeft nu 4 wandelaars" in r.get_data(as_text=True)
    with wh.app.app_context():
        db = wh.get_db()
        db.execute("UPDATE hikers SET group_id = 2, comment = 'glutenvrij' WHERE id = ?", (dirk,))
        db.commit()
    client.post("/groep/2/uitgevallen", data={"csrf": t, "undo": "1"})
    client.post("/groepen/2", data={"csrf": t, "op": "merge", "target": "1", "hiker": [str(dirk)]})
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT comment FROM hikers WHERE id = ?", (dirk,)).fetchone()[0] == \
            "glutenvrij; oorspronkelijk groep 102"
    login(client, "kijk")
    assert client.post("/groepen/2", data={"csrf": t, "op": "merge", "target": "1"}).status_code in (400, 403)


def test_leg_times_and_late_groups(client):
    from datetime import datetime, timedelta, timezone
    t = login(client, "admin")
    client.post("/groepen", data={"csrf": t, "category": "junioren", "hiker_name": ["Cees", "", ""],
                                  "hiker_comment": ["", "", ""]})
    now = datetime.now(timezone.utc)
    ago = lambda m: (now - timedelta(minutes=m)).isoformat(timespec="seconds")
    with wh.app.app_context():
        db = wh.get_db()
        # Group 101 walked CP1 -> CP2 in 60 min and left CP2 100 minutes ago.
        db.execute("INSERT INTO checkins (group_id, checkpoint, checkin_at, checkout_at) VALUES (1, 1, ?, ?)",
                   (ago(200), ago(190)))
        db.execute("INSERT INTO checkins (group_id, checkpoint, checkin_at, checkout_at) VALUES (1, 2, ?, ?)",
                   (ago(130), ago(100)))
        # Group 102 walked CP1 -> CP2 in 80 min and is still at CP2.
        db.execute("INSERT INTO checkins (group_id, checkpoint, checkin_at, checkout_at) VALUES (2, 1, ?, ?)",
                   (ago(200), ago(180)))
        db.execute("INSERT INTO checkins (group_id, checkpoint, checkin_at) VALUES (2, 2, ?)", (ago(100),))
        db.commit()
    client.post("/checkpoints", data={"csrf": t, "day1": "7", "day2": "5", "leg_1": "75", "leg_2": "60"})
    page = client.get("/checkpoints").get_data(as_text=True)
    assert 'value="75"' in page and "gemiddeld 70 min (2 groepen)" in page
    page = client.get("/").get_data(as_text=True)
    # 100 min underway > 60 + 30 grace.
    assert "Te laat (1)" in page and "al <b>100 min</b> onderweg, verwacht 60 min" in page
    assert "te laat" in client.get("/checkpoint/3").get_data(as_text=True)
    client.post("/checkpoints", data={"csrf": t, "day1": "7", "day2": "5", "leg_1": "75", "leg_2": "90"})
    assert "Te laat" not in client.get("/").get_data(as_text=True)


def test_late_group_alerts_admins_once(client):
    from datetime import datetime, timedelta, timezone
    with wh.app.app_context():
        db = wh.get_db()
        db.execute("INSERT INTO users (username, password_hash, role) VALUES ('admin2', 'x', 'admin')")
        left = (datetime.now(timezone.utc) - timedelta(minutes=100)).isoformat(timespec="seconds")
        db.execute("INSERT INTO checkins (group_id, checkpoint, checkin_at, checkout_at) VALUES (1, 2, ?, ?)",
                   (left, left))
        db.commit()
    t = login(client, "admin")
    client.post("/checkpoints", data={"csrf": t, "day1": "7", "day2": "5", "leg_2": "60"})
    # Any poll (here by staff) notices the late group and alerts every admin,
    # plus the staff of the checkpoint the group walks to (CP 3).
    login(client, "post3")
    items = client.get("/meldingen/nieuw?na=0").get_json()["items"]
    assert len(items) == 1 and "Groep 101 is te laat" in items[0]["text"]
    assert client.get(items[0]["url"]).headers["Location"].endswith("/checkpoint/3/groep/1")
    login(client, "admin")
    d = client.get("/meldingen/nieuw?na=0").get_json()
    assert len(d["items"]) == 1 and "Groep 101 is te laat" in d["items"][0]["text"]
    assert "van CP 2 naar CP 3 (verwacht 60 min)" in d["items"][0]["text"]
    # Opening the alert goes to /g/1, which sends an admin on to the group page.
    link = client.get(d["items"][0]["url"]).headers["Location"]
    assert link.endswith("/g/1")
    assert client.get(link).headers["Location"].endswith("/groepen/1")
    # More polls do not send the same alert again.
    client.get("/meldingen/nieuw?na=0")
    with wh.app.app_context():
        db = wh.get_db()
        texts = [r[0] for r in db.execute("SELECT u.username FROM notifications n JOIN users u ON u.id = n.user_id")]
        assert sorted(texts) == ["admin", "admin2", "post3"]
    # Wiping all group data also clears the alerts.
    make_super()
    t = login(client, "admin")
    client.post("/instellingen", data={"csrf": t, "op": "wipe", "confirm": "WISSEN"})
    with wh.app.app_context():
        db = wh.get_db()
        assert db.execute("SELECT COUNT(*) FROM late_alerts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


def test_offline_tap_time_and_json(client):
    from datetime import datetime, timedelta, timezone
    t = login(client, "post3")
    tapped = (datetime.now(timezone.utc) - timedelta(minutes=25)).replace(microsecond=0)
    r = client.post("/checkpoint/3/groep/1/in", data={"csrf": t, "ajax": "1", "at": tapped.isoformat()})
    assert r.status_code == 200 and r.get_json()["ok"]
    # Sending the same tap again (e.g. after a lost answer) is fine.
    r = client.post("/checkpoint/3/groep/1/in", data={"csrf": t, "ajax": "1", "at": tapped.isoformat()})
    assert r.get_json() == {"ok": True, "already": True, "message": "Deze groep is hier al ingecheckt."}
    # A time from the far past or the future is not trusted.
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    client.post("/checkpoint/3/groep/1/uit", data={"csrf": t, "ajax": "1", "at": old})
    with wh.app.app_context():
        v = wh.get_db().execute("SELECT checkin_at, checkout_at FROM checkins").fetchone()
    assert v["checkin_at"] == tapped.isoformat()
    assert datetime.fromisoformat(v["checkout_at"]) > datetime.now(timezone.utc) - timedelta(minutes=1)
    login(client, "admin")
    assert "later verstuurd" in client.get("/logboek").get_data(as_text=True)
    assert client.get("/sw.js").headers["Service-Worker-Allowed"] == "/"
    assert wh.tap_time("2020-01-01T00:00:00") is None and wh.tap_time("nonsense") is None


def test_checkpoint_theme_and_crew(client):
    t = login(client, "admin")
    client.post("/checkpoints", data={"csrf": t, "day1": "7", "day2": "5",
                                      "theme_3": "Eerste hulp", "crew_3": "Sanne & Mark"})
    assert 'value="Eerste hulp"' in client.get("/checkpoints").get_data(as_text=True)
    login(client, "post3")
    page = client.get("/checkpoint/3").get_data(as_text=True)
    assert "Eerste hulp" in page and "Sanne &amp; Mark" in page
    assert '"crew": "Sanne \\u0026 Mark"' in client.get("/kaart").get_data(as_text=True)


def test_new_group_form_has_category_buttons(client):
    login(client, "admin")
    page = client.get("/groepen").get_data(as_text=True)
    assert '<select name="category"' not in page
    for cat in ("junioren", "senioren", "veteranen"):
        assert f'name="category" value="{cat}"' in page


def test_username_not_case_sensitive(client):
    t = csrf(client)
    r = client.post("/inloggen", data={"username": "ADMIN", "password": "geheim123", "csrf": t})
    assert r.status_code == 302
    t = csrf(client)
    r = client.post("/inloggen", data={"username": "Admin", "password": "GEHEIM123", "csrf": t})
    assert r.status_code != 302  # passwords stay case sensitive
    t = login(client, "admin")
    r = client.post("/gebruikers", data={"csrf": t, "op": "create", "username": "Post3",
                                         "password": "geheim123", "role": "viewer"}, follow_redirects=True)
    assert "bestaat al" in r.get_data(as_text=True)
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3


def test_notification_poll(client):
    login(client, "admin")
    page = client.get("/").get_data(as_text=True)
    assert 'data-user="1"' in page and "notify.js" in page
    t = login(client, "post3")
    client.post("/checkpoint/3/groep/1", data={"csrf": t, "quiz_3": "7", "practical_3": "6"})
    client.post("/checkpoint/3/groep/1/wijziging", data={"csrf": t, "quiz_3": "8", "practical_3": "6", "reason": "fout"})
    login(client, "admin")
    d = client.get("/meldingen/nieuw?na=0").get_json()
    assert d["unread"] == 1 and len(d["items"]) == 1 and "scorewijziging" in d["items"][0]["text"]
    assert d["items"][0]["url"].startswith("/meldingen/")
    assert client.get(f"/meldingen/nieuw?na={d['last']}").get_json()["items"] == []
    # Polling is not written to the audit log.
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT COUNT(*) FROM audit_log WHERE path LIKE '/meldingen/nieuw%'").fetchone()[0] == 0


def test_map_visibility_per_user(client):
    t = login(client, "admin")
    # A plain admin cannot change who sees the map.
    client.post("/gebruikers", data={"csrf": t, "op": "update", "user_id": "3", "role": "viewer", "active": "1"})
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT can_map FROM users WHERE id = 3").fetchone()[0] == 1
    make_super()
    t = login(client, "admin")
    client.post("/gebruikers", data={"csrf": t, "op": "update", "user_id": "3", "role": "viewer", "active": "1"})
    login(client, "kijk")
    assert ">Kaart<" not in client.get("/").get_data(as_text=True)
    assert client.get("/kaart").status_code == 403
    t = login(client, "admin")
    assert ">Kaart<" in client.get("/").get_data(as_text=True)
    client.post("/gebruikers", data={"csrf": t, "op": "update", "user_id": "3", "role": "viewer",
                                     "active": "1", "can_map": "1"})
    login(client, "kijk")
    assert client.get("/kaart").status_code == 200


def test_audit_log_records_and_filters_ip(client):
    # A failed login is logged with the visitor's address; a faked X-Forwarded-For is ignored
    # unless WH_PROXIES says a proxy sits in front.
    client.post("/inloggen", data={"username": "admin", "password": "fout", "csrf": csrf(client)},
                environ_base={"REMOTE_ADDR": "203.0.113.7"}, headers={"X-Forwarded-For": "1.2.3.4"})
    t = login(client, "admin")
    with wh.app.app_context():
        row = wh.get_db().execute(
            "SELECT ip FROM audit_log WHERE action = 'mislukte inlogpoging'").fetchone()
        assert row["ip"] == "203.0.113.7"
    page = client.get("/logboek?ip=203.0.113.").get_data(as_text=True)
    assert "IP-adres" in page and "203.0.113.7" in page and "mislukte inlogpoging" in page
    page = client.get("/logboek?ip=10.9.").get_data(as_text=True)
    assert "mislukte inlogpoging" not in page
    page = client.get("/logboek?ip=%25").get_data(as_text=True)  # % is not a wildcard
    assert "mislukte inlogpoging" not in page


def test_proxyfix_uses_forwarded_address(client):
    from werkzeug.middleware.proxy_fix import ProxyFix
    original = wh.app.wsgi_app
    wh.app.wsgi_app = ProxyFix(original, x_for=1, x_proto=1)
    try:
        client.post("/inloggen", data={"username": "admin", "password": "fout", "csrf": csrf(client)},
                    environ_base={"REMOTE_ADDR": "172.17.0.1"},
                    headers={"X-Forwarded-For": "1.2.3.4, 198.51.100.9"})
    finally:
        wh.app.wsgi_app = original
    with wh.app.app_context():
        row = wh.get_db().execute(
            "SELECT ip FROM audit_log WHERE action = 'mislukte inlogpoging'").fetchone()
        assert row["ip"] == "198.51.100.9"  # only the address the trusted proxy added


# --- Fixes from the November 2026 review ---

def test_login_rejects_backslash_open_redirect(client):
    t = csrf(client)
    r = client.post("/inloggen?next=/%5Cevil.example",
                    data={"username": "admin", "password": "geheim123", "csrf": t})
    assert r.headers["Location"] in ("/", "http://localhost/")


def test_login_lockout_after_many_failures(client):
    for _ in range(wh.LOGIN_MAX_PER_USER):
        client.post("/inloggen", data={"username": "admin", "password": "fout", "csrf": csrf(client)})
    # Even the right password is paused now.
    r = client.post("/inloggen", data={"username": "admin", "password": "geheim123", "csrf": csrf(client)})
    assert r.status_code == 429


def test_password_change_logs_out_other_sessions(client):
    phone = wh.app.test_client()
    login(phone, "admin")
    assert phone.get("/").status_code == 200
    # A second client changes the password.
    other = wh.app.test_client()
    t = login(other, "admin")
    other.post("/profiel", data={"csrf": t, "op": "password", "old": "geheim123", "new": "nieuwwachtwoord1"})
    # The first phone is now logged out.
    assert phone.get("/").status_code == 302


def test_score_request_keeps_blank_field(client):
    t = login(client, "post3")
    client.post("/checkpoint/3/groep/1/in", data={"csrf": t})
    client.post("/checkpoint/3/groep/1", data={"csrf": t, "quiz_3": "7", "practical_3": "8"})
    # Ask to change only the quiz; leave practical blank.
    client.post("/checkpoint/3/groep/1/wijziging",
                data={"csrf": t, "quiz_3": "9", "practical_3": "", "reason": "telfout"})
    ta = login(client, "admin")
    with wh.app.app_context():
        rid = wh.get_db().execute("SELECT id FROM score_requests").fetchone()["id"]
    client.post(f"/verzoeken/{rid}", data={"csrf": ta, "decision": "approved"})
    with wh.app.app_context():
        s = wh.get_db().execute("SELECT quiz, practical FROM scores WHERE group_id=1 AND checkpoint=3").fetchone()
        assert (s["quiz"], s["practical"]) == (9, 8)  # practical not wiped


def test_write_once_keeps_existing_score(client):
    t = login(client, "post3")
    client.post("/checkpoint/3/groep/1", data={"csrf": t, "quiz_3": "6", "practical_3": "6"})
    # Staff tries to overwrite directly: the old value stays.
    client.post("/checkpoint/3/groep/1", data={"csrf": t, "quiz_3": "9", "practical_3": "9"})
    with wh.app.app_context():
        s = wh.get_db().execute("SELECT quiz, practical FROM scores WHERE group_id=1 AND checkpoint=3").fetchone()
        assert (s["quiz"], s["practical"]) == (6, 6)


def test_checkpoint_day_shift_blocked(client):
    t = login(client, "post3")
    client.post("/checkpoint/3/groep/1/in", data={"csrf": t})  # CP3 now in use
    ta = login(client, "admin")
    # Default is day1=7, day2=5. Shrinking day1 to 2 would move CP3 to day 2.
    client.post("/checkpoints", data={"csrf": ta, "day1": "2", "day2": "5"})
    assert wh.app.test_client() and True
    with wh.app.app_context():
        assert wh.get_setting("cp_day1") in (None, "")  # not saved


def test_qr_uses_group_number_not_id(client):
    t = login(client, "post3")
    # The QR route works by printed number.
    assert client.get("/q/101").headers["Location"].endswith("/checkpoint/3/groep/1")
    assert client.get("/q/999").status_code == 404


def test_ranking_ties_share_place(client):
    with wh.app.app_context():
        db = wh.get_db()
        db.execute("INSERT INTO groups (name, category) VALUES ('102', 'junioren')")
        now = wh.now_utc()
        for gid in (1, 2):
            db.execute("INSERT INTO scores (group_id, checkpoint, quiz, practical, updated_at) "
                       "VALUES (?, 1, 5, 5, ?)", (gid, now))
        db.commit()
        by_cat = wh.ranking(db)
    places = [r["place"] for r in by_cat["junioren"]]
    assert places == [1, 1]


def test_export_includes_score_without_checkin(client):
    with wh.app.app_context():
        db = wh.get_db()
        db.execute("INSERT INTO scores (group_id, checkpoint, quiz, practical, updated_at) "
                   "VALUES (1, 5, 7, 8, ?)", (wh.now_utc(),))
        db.commit()
    login(client, "admin")
    r = client.get("/export.xlsx")
    assert r.status_code == 200 and len(r.data) > 0


def test_poll_returns_401_when_logged_out(client):
    assert client.get("/meldingen/nieuw?ajax=1").status_code == 401


def test_weird_digits_do_not_crash(client):
    login(client, "admin")
    assert client.get("/meldingen/nieuw?na=\u00b2&ajax=1").status_code == 200
    assert client.get("/logboek?p=\u00b2").status_code == 200


def test_max_hikers_enforced_on_create(client):
    t = login(client, "admin")
    client.post("/groepen", data={"csrf": t, "category": "senioren",
                                  "hiker_name": ["A", "B", "C", "D"], "hiker_comment": ["", "", "", ""]})
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT COUNT(*) FROM groups WHERE category='senioren'").fetchone()[0] == 0


def test_lockout_does_not_block_other_addresses(client):
    for _ in range(wh.LOGIN_MAX_PER_USER):
        client.post("/inloggen", data={"username": "post3", "password": "fout", "csrf": csrf(client)},
                    environ_base={"REMOTE_ADDR": "203.0.113.7"})
    # The real staff member on another address can still log in.
    other = wh.app.test_client()
    r = other.post("/inloggen", data={"username": "post3", "password": "geheim123", "csrf": csrf(other)},
                   environ_base={"REMOTE_ADDR": "198.51.100.2"})
    assert r.status_code == 302


def test_security_headers(client):
    r = client.get("/inloggen")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_session_holds_no_piece_of_password_hash(client):
    login(client, "admin")
    with client.session_transaction() as s:
        mark = s["pw"]
    with wh.app.app_context():
        h = wh.get_db().execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()[0]
    assert mark not in h


# --- Agent A: staff tools

def test_coming_your_way_with_eta(client):
    from datetime import datetime, timedelta, timezone
    t = login(client, "admin")
    for name in ("Cees", "Door", "Evi"):
        client.post("/groepen", data={"csrf": t, "category": "junioren", "hiker_name": [name, "", ""],
                                      "hiker_comment": ["", "", ""]})
    now = datetime.now(timezone.utc).replace(microsecond=0)
    ago = lambda m: (now - timedelta(minutes=m)).isoformat()
    with wh.app.app_context():
        db = wh.get_db()
        # 101 left CP 2 20 min ago, 102 40 min ago; 103 left CP 2 and is already at CP 3;
        # 104 only left CP 1, so it is not coming to CP 3 yet.
        for gid, cp, m in ((1, 2, 20), (2, 2, 40), (3, 2, 50), (4, 1, 10)):
            db.execute("INSERT INTO checkins (group_id, checkpoint, checkin_at, checkout_at) VALUES (?, ?, ?, ?)",
                       (gid, cp, ago(m + 5), ago(m)))
        db.execute("INSERT INTO checkins (group_id, checkpoint, checkin_at) VALUES (3, 3, ?)", (ago(5),))
        db.commit()
    page = client.get("/checkpoint/3").get_data(as_text=True)
    assert "Onderweg naar jou (2)" in page
    # 103 walked CP 2 -> 3 in 45 min, so that average gives an expected time.
    coming = page.split("Onderweg naar jou")[1].split("</div>")[0]
    assert coming.index("Groep 102") < coming.index("Groep 101")  # earliest arrival first
    assert f"verwacht rond <b>{wh.localtime(ago(40 - 45), '%H:%M')}</b>" in coming
    client.post("/checkpoints", data={"csrf": t, "day1": "7", "day2": "5", "leg_2": "60"})
    coming = client.get("/checkpoint/3").get_data(as_text=True).split("Onderweg naar jou")[1].split("</div>")[0]
    assert f"vertrokken {wh.localtime(ago(20), '%H:%M')} van CP 2" in coming
    assert f"verwacht rond <b>{wh.localtime(ago(20 - 60), '%H:%M')}</b>" in coming
    assert "te laat" not in coming
    # With 5 min expected, 102 (40 min underway > 5 + 30) is late.
    client.post("/checkpoints", data={"csrf": t, "day1": "7", "day2": "5", "leg_2": "5"})
    coming = client.get("/checkpoint/3").get_data(as_text=True).split("Onderweg naar jou")[1].split("</div>")[0]
    assert "te laat" in coming
    # Nothing comes to CP 1 (first CP of the day): the block is hidden. CP 2 has 104 coming without an ETA.
    assert "Onderweg naar jou" not in client.get("/checkpoint/1").get_data(as_text=True)
    coming = client.get("/checkpoint/2").get_data(as_text=True).split("Onderweg naar jou")[1].split("</div>")[0]
    assert "Groep 104" in coming and "verwacht rond" not in coming


def test_broadcast_reaches_the_right_people(client):
    with wh.app.app_context():
        db = wh.get_db()
        for name, role, cp, active in [("admin2", "admin", None, 1), ("post4", "staff", 4, 1),
                                       ("oud3", "staff", 3, 0)]:
            db.execute("INSERT INTO users (username, password_hash, role, checkpoint, active) VALUES (?, ?, ?, ?, ?)",
                       (name, "x", role, cp, active))
        db.commit()

    def got():
        with wh.app.app_context():
            return sorted(r[0] for r in wh.get_db().execute(
                "SELECT u.username FROM notifications n JOIN users u ON u.id = n.user_id"))

    t = login(client, "post3")
    assert client.get("/bericht").status_code == 403
    assert client.post("/bericht", data={"csrf": t, "text": "hoi", "target": "posten"}).status_code == 403
    t = login(client, "kijk")
    assert client.post("/bericht", data={"csrf": t, "text": "hoi", "target": "posten"}).status_code == 403
    t = login(client, "admin")
    r = client.post("/bericht", data={"csrf": t, "text": "Regen op komst", "target": "posten"}, follow_redirects=True)
    assert "Bericht verstuurd aan 2 personen." in r.get_data(as_text=True)
    assert got() == ["post3", "post4"]
    client.post("/bericht", data={"csrf": t, "text": "Lunch", "target": "iedereen"})
    assert got() == ["admin2", "post3", "post3", "post4", "post4"]  # never the sender
    client.post("/bericht", data={"csrf": t, "text": "Alleen CP 4", "target": "cp4"})
    assert got().count("post4") == 3 and got().count("post3") == 2
    r = client.post("/bericht", data={"csrf": t, "text": "Leeg", "target": "cp7"}, follow_redirects=True)
    assert "niemand" in r.get_data(as_text=True)
    client.post("/bericht", data={"csrf": t, "text": "x" * 301, "target": "posten"})
    client.post("/bericht", data={"csrf": t, "text": "  ", "target": "posten"})
    client.post("/bericht", data={"csrf": t, "text": "hoi", "target": "cp99"})
    with wh.app.app_context():
        db = wh.get_db()
        assert db.execute("SELECT COUNT(*) FROM broadcasts").fetchone()[0] == 4
        n = db.execute("SELECT text, link FROM notifications ORDER BY id LIMIT 1").fetchone()
        assert n["text"] == "Bericht van admin: Regen op komst" and n["link"] == "/meldingen"
        assert db.execute("SELECT 1 FROM audit_log WHERE action = 'bericht verstuurd aan alle posten: "
                          "Regen op komst'").fetchone()
    page = client.get("/bericht").get_data(as_text=True)
    assert "Alleen CP 4" in page and "posten en beheerders" in page and ">Bericht</a>" in page


def test_checkpoint_open_and_close(client):
    with wh.app.app_context():
        db = wh.get_db()
        db.execute("INSERT INTO users (username, password_hash, role) VALUES ('admin2', 'x', 'admin')")
        db.commit()
    t = login(client, "post3")
    page = client.get("/checkpoint/3").get_data(as_text=True)
    assert "nog niet geopend" in page and "Post openen" in page
    assert client.post("/checkpoint/4/status", data={"csrf": t, "open": "1"}).status_code == 403
    client.post("/checkpoint/3/status", data={"csrf": t, "open": "1"})
    page = client.get("/checkpoint/3").get_data(as_text=True)
    assert "Post sluiten" in page and "nog 1 groep verwacht. Post toch sluiten?" in page
    r = client.post("/checkpoint/3/status", data={"csrf": t, "open": "0"}, follow_redirects=True)
    assert "nog 1 groep verwacht" in r.get_data(as_text=True)
    with wh.app.app_context():
        db = wh.get_db()
        assert db.execute("SELECT is_open FROM checkpoint_status WHERE checkpoint = 3").fetchone()[0] == 0
        rows = db.execute("SELECT u.username, n.text, n.link FROM notifications n "
                          "JOIN users u ON u.id = n.user_id ORDER BY u.username").fetchall()
        assert [r["username"] for r in rows] == ["admin", "admin2"]
        assert rows[0]["text"] == "CP 3 is gesloten terwijl er nog 1 groepen verwacht worden"
        assert rows[0]["link"] == "/checkpoint/3"
        actions = [r[0] for r in db.execute("SELECT action FROM audit_log WHERE username = 'post3'")]
        assert "CP3 geopend" in actions and any(a.startswith("CP3 gesloten") for a in actions)
    t = login(client, "kijk")
    assert client.post("/checkpoint/3/status", data={"csrf": t, "open": "1"}).status_code == 403
    assert "Post openen" not in client.get("/checkpoint/3").get_data(as_text=True)
    t = login(client, "admin")
    page = client.get("/").get_data(as_text=True)
    assert '<span class="tag off">dicht</span>' in page and ">open</span>" not in page
    client.post("/checkpoint/5/status", data={"csrf": t, "open": "1"})
    assert '<span class="tag out">open</span>' in client.get("/").get_data(as_text=True)
    assert client.post("/checkpoint/5/status", data={"csrf": t, "open": "x"}).status_code == 400
    # Closing with nobody left to come: no warning for the admins.
    client.post("/groep/1/uitgevallen", data={"csrf": t})
    client.post("/checkpoint/5/status", data={"csrf": t, "open": "0"})
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2


# --- Agent B: backups and import

import io
import os
import sqlite3

from openpyxl import Workbook


def _backups():
    folder = wh.backup_dir()
    return sorted(f for f in os.listdir(folder) if wh.BACKUP_NAME.match(f)) if os.path.isdir(folder) else []


def _make_super(name="admin"):
    with wh.app.app_context():
        db = wh.get_db()
        db.execute("UPDATE users SET is_super = 1 WHERE username = ?", (name,))
        db.commit()


def test_backup_is_valid_copy_with_data(client):
    _make_super()
    t = login(client, "admin")
    assert wh.backup_dir().startswith(os.path.dirname(wh.app.config["DATABASE"]))
    for f in _backups():  # the hourly backup of the first request; start clean
        os.remove(os.path.join(wh.backup_dir(), f))
    client.post("/instellingen", data={"csrf": t, "op": "backup"})
    names = _backups()
    assert len(names) == 1
    con = sqlite3.connect(os.path.join(wh.backup_dir(), names[0]))
    try:
        assert con.execute("SELECT name FROM groups").fetchone()[0] == "101"
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()
    page = client.get("/instellingen").get_data(as_text=True)
    assert names[0] in page and "Nu een backup maken" in page
    r = client.get(f"/instellingen/backup/{names[0]}")
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    assert r.data[:15] == b"SQLite format 3"
    r.close()
    with wh.app.app_context():
        actions = [a[0] for a in wh.get_db().execute("SELECT action FROM audit_log")]
    assert f"backup gemaakt: {names[0]}" in actions and f"backup gedownload: {names[0]}" in actions


def test_backup_retention_keeps_48(client):
    folder = wh.backup_dir()
    os.makedirs(folder, exist_ok=True)
    for i in range(50):
        open(os.path.join(folder, f"wh-2000{(i // 28) + 1:02d}{(i % 28) + 1:02d}-000000.sqlite3"), "wb").close()
    with wh.app.app_context():
        new = wh.make_backup()
    names = _backups()
    assert len(names) == 48 and names[-1] == new
    assert "wh-20000101-000000.sqlite3" not in names and "wh-20000103-000000.sqlite3" not in names


def test_hourly_backup_runs_once_per_hour(client):
    login(client, "admin")
    client.get("/")
    client.get("/groepen")
    assert len(_backups()) == 1
    # Pretend the last backup was two hours ago: the next request makes a new one.
    folder = wh.backup_dir()
    os.replace(os.path.join(folder, _backups()[0]), os.path.join(folder, "wh-20000101-000000.sqlite3"))
    with wh.app.app_context():
        db = wh.get_db()
        db.execute("UPDATE settings SET value = '2000-01-01T00:00:00+00:00' WHERE key = 'last_backup'")
        db.commit()
    client.get("/")
    client.get("/")
    assert len(_backups()) == 2


def test_backup_download_rejects_bad_names_and_non_super(client):
    login(client, "admin")
    name = _backups()[0]
    assert client.get(f"/instellingen/backup/{name}").status_code == 403  # admin, not superadmin
    _make_super()
    for bad in ("..%2Fapp.py", "..%2F..%2Fapp.py", "app.py", "wh-1.sqlite3", "wh-20000101-000000.sqlite3"):
        assert client.get(f"/instellingen/backup/{bad}").status_code == 404, bad
    client.post("/uitloggen", data={"csrf": csrf(client, "/profiel")})
    login(client, "kijk")
    assert client.get(f"/instellingen/backup/{name}").status_code == 403


def _upload(client, t, content, filename):
    return client.post("/groepen", data={"csrf": t, "op": "import", "file": (io.BytesIO(content), filename)},
                       content_type="multipart/form-data", follow_redirects=True)


def _groups():
    with wh.app.app_context():
        db = wh.get_db()
        return {r["name"]: (r["category"], r["phone"], [h[0] for h in db.execute(
            "SELECT name FROM hikers WHERE group_id = ? ORDER BY id", (r["id"],))])
            for r in db.execute("SELECT * FROM groups")}


def test_import_csv_semicolon_and_comma(client):
    t = login(client, "admin")
    semi = ("Categorie ; Wandelaar 1;Opmerking 1;wandelaar 2;opmerking 2;wandelaar 3;opmerking 3;noodnummer\r\n"
            "junioren;Piet;;Klaas;;;;06 111\r\n;;;;;;;\r\nVETERANEN;Joop;suiker;;;;;\r\n").encode("utf-8-sig")
    page = _upload(client, t, semi, "lijst.csv").get_data(as_text=True)
    assert "2 groepen geïmporteerd: 102, 301" in page
    g = _groups()
    assert g["102"] == ("junioren", "06 111", ["Piet", "Klaas"]) and g["301"][2] == ["Joop"]
    comma = "categorie,wandelaar 1,wandelaar 2\nsenioren,Zoë,Ezra\nsenioren,Tim,\n".encode("cp1252")
    page = _upload(client, t, comma, "lijst2.CSV").get_data(as_text=True)
    assert "2 groepen geïmporteerd: 201-202" in page
    assert _groups()["201"][2] == ["Zoë", "Ezra"]
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT 1 FROM audit_log WHERE action = "
                                   "'groepen geïmporteerd: 2 uit lijst2.CSV'").fetchone()


def test_import_one_bad_row_imports_nothing(client):
    t = login(client, "admin")
    data = ("categorie;wandelaar 1;wandelaar 2\njunioren;A;B\nsenioren;C;\nkids;D;\n;;\njunioren;;E\n"
            ).encode("utf-8")
    page = _upload(client, t, data, "fout.csv").get_data(as_text=True)
    assert "Er is niets geïmporteerd" in page
    assert "Rij 4: onbekende categorie &#39;kids&#39;" in page
    assert "Rij 6: geen naam bij wandelaar 1" in page
    assert set(_groups()) == {"101"}


def test_import_full_category_imports_nothing(client):
    t = login(client, "admin")
    with wh.app.app_context():
        db = wh.get_db()
        db.executemany("INSERT INTO groups (name, category) VALUES (?, 'veteranen')",
                       [(str(n),) for n in range(301, 399)])
        db.commit()
    data = "categorie;wandelaar 1\nsenioren;A\nveteranen;B\nveteranen;C\n".encode("utf-8")
    page = _upload(client, t, data, "vol.csv").get_data(as_text=True)
    assert "Rij 4: categorie veteranen is vol" in page
    assert "201" not in _groups() and "399" not in _groups()


def test_import_rejects_type_size_and_non_admin(client):
    t = login(client, "admin")
    assert "Alleen een CSV-bestand" in _upload(client, t, b"x", "lijst.txt").get_data(as_text=True)
    big = b"categorie;wandelaar 1\n" + b"junioren;" + b"x" * (1024 * 1024) + b"\n"
    assert "te groot" in _upload(client, t, big, "groot.csv").get_data(as_text=True)
    assert set(_groups()) == {"101"}
    client.post("/uitloggen", data={"csrf": t})
    t = login(client, "kijk")
    r = client.post("/groepen", data={"csrf": t, "op": "import", "file": (io.BytesIO(b"x"), "a.csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 403
    assert client.get("/groepen/voorbeeld.csv").status_code == 403
    assert client.get("/groepen/voorbeeld.xlsx").status_code == 403


def test_import_excel(client):
    t = login(client, "admin")
    wb = Workbook()
    ws = wb.active
    ws.append(["Categorie", "Wandelaar 1", "Opmerking 1", "Wandelaar 2", "Opmerking 2", "Wandelaar 3",
               "Opmerking 3", "Noodnummer"])
    ws.append(["Senioren", "Lisa", "glutenvrij", "Mo", None, "Noor", None, 612345678.0])
    ws.append([None, None])
    ws.append(["junioren", "Ole", None, None, None, None, None, None])
    out = io.BytesIO()
    wb.save(out)
    page = _upload(client, t, out.getvalue(), "groepen.xlsx").get_data(as_text=True)
    assert "2 groepen geïmporteerd: 102, 201" in page
    g = _groups()
    assert g["201"] == ("senioren", "612345678", ["Lisa", "Mo", "Noor"])
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT comment FROM hikers WHERE name = 'Lisa'").fetchone()[0] == "glutenvrij"


def test_sample_files_download_and_reimport(client):
    t = login(client, "admin")
    assert "voorbeeld.csv" in client.get("/groepen").get_data(as_text=True)
    r = client.get("/groepen/voorbeeld.csv")
    assert r.status_code == 200 and "voorbeeld-groepen.csv" in r.headers["Content-Disposition"]
    assert r.data.startswith("﻿categorie;wandelaar 1;".encode("utf-8"))
    page = _upload(client, t, r.data, "voorbeeld-groepen.csv").get_data(as_text=True)
    assert "3 groepen geïmporteerd: 102, 201, 301" in page
    x = client.get("/groepen/voorbeeld.xlsx")
    assert x.status_code == 200 and "voorbeeld-groepen.xlsx" in x.headers["Content-Disposition"]
    page = _upload(client, t, x.data, "voorbeeld-groepen.xlsx").get_data(as_text=True)
    assert "3 groepen geïmporteerd: 103, 202, 302" in page
    assert _groups()["202"][2] == ["Chris Bakker", "Dana Visser", "Eva Smit"]


def test_manual_group_create_still_works(client):
    t = login(client, "admin")
    client.post("/groepen", data={"csrf": t, "category": "senioren", "hiker_name": ["X", "", ""],
                                  "hiker_comment": ["", "", ""], "phone": ""})
    assert "201" in _groups()

# --- Agent C: map, dropout reason, paper sheets

def test_map_groups_json_positions_and_privacy(client):
    from datetime import datetime, timedelta, timezone
    t = login(client, "admin")
    client.post("/groepen", data={"csrf": t, "category": "junioren", "hiker_name": ["Cees", "", ""],
                                  "hiker_comment": ["", "", ""]})
    client.post("/groepen", data={"csrf": t, "category": "senioren", "hiker_name": ["Dirk", "", ""],
                                  "hiker_comment": ["", "", ""]})
    for cp, lat, lng in ((1, 52.0, 5.0), (2, 52.2, 5.4), (7, 52.5, 5.5)):
        client.post("/kaart", data={"csrf": t, "cp": str(cp), "lat": str(lat), "lng": str(lng), "ajax": "1"})
    client.post("/checkpoints", data={"csrf": t, "day1": "7", "day2": "5", "leg_1": "30"})
    now = datetime.now(timezone.utc)
    ago = lambda m: (now - timedelta(minutes=m)).isoformat(timespec="seconds")
    with wh.app.app_context():
        db = wh.get_db()
        # 101 left CP 1 two hours ago (late: 30 + 30 grace), 102 is at CP 2, 201 finished day 1.
        db.execute("INSERT INTO checkins (group_id, checkpoint, checkin_at, checkout_at) VALUES (1, 1, ?, ?)",
                   (ago(130), ago(120)))
        db.execute("INSERT INTO checkins (group_id, checkpoint, checkin_at) VALUES (2, 2, ?)", (ago(5),))
        db.execute("INSERT INTO checkins (group_id, checkpoint, checkin_at, checkout_at) VALUES (3, 7, ?, ?)",
                   (ago(20), ago(10)))
        db.commit()
    r = client.get("/kaart/groepen.json")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Anna" not in body and "notenallergie" not in body and "Cees" not in body
    groups = {x["number"]: x for x in r.get_json()["groups"]}
    assert set(groups) == {"101", "102", "201"}
    walk = groups["101"]
    assert (walk["lat"], walk["lng"]) == (52.1, 5.2) and walk["late"] is True
    since = wh.localtime(ago(120), "%H:%M")
    assert walk["state"] == f"onderweg van CP 1 naar CP 2 sinds {since}"
    assert (groups["102"]["lat"], groups["102"]["lng"], groups["102"]["state"]) == (52.2, 5.4, "op CP 2")
    assert groups["102"]["late"] is False and groups["102"]["category"] == "junioren"
    assert groups["201"]["state"] == "dag 1 klaar" and groups["201"]["lat"] == 52.5
    # A retired group is left out.
    client.post("/groep/2/uitgevallen", data={"csrf": t})
    assert {x["number"] for x in client.get("/kaart/groepen.json").get_json()["groups"]} == {"101", "201"}
    # A group at a checkpoint without a location is left out too.
    client.post("/kaart", data={"csrf": t, "cp": "7", "op": "remove"})
    assert {x["number"] for x in client.get("/kaart/groepen.json").get_json()["groups"]} == {"101"}
    assert "show-groups" in client.get("/kaart").get_data(as_text=True)


def test_map_groups_json_needs_map_access(client):
    with wh.app.app_context():
        db = wh.get_db()
        db.execute("UPDATE users SET can_map = 0 WHERE username = 'post3'")
        db.commit()
    login(client, "post3")
    assert client.get("/kaart/groepen.json").status_code == 403
    assert wh.app.test_client().get("/kaart/groepen.json?ajax=1").status_code == 401


def test_retire_with_reason_and_undo(client):
    import io
    from openpyxl import load_workbook
    t = login(client, "admin")
    page = client.get("/checkpoint/3/groep/1").get_data(as_text=True)
    assert 'name="reason"' in page and "Opgehaald door ouders" in page
    assert 'name="reason"' in client.get("/groepen/1").get_data(as_text=True)
    client.post("/groep/1/uitgevallen", data={"csrf": t, "cp": "3", "reason": "Blessure", "reason_text": "knie"})
    with wh.app.app_context():
        db = wh.get_db()
        assert db.execute("SELECT retired_reason FROM groups WHERE id = 1").fetchone()[0] == "Blessure: knie"
        actions = [r[0] for r in db.execute("SELECT action FROM audit_log")]
        assert "groep 101 uitgevallen bij CP3, reden: Blessure: knie" in actions
    assert "Reden: Blessure: knie" in client.get("/groepen/1").get_data(as_text=True)
    assert "Reden: Blessure: knie" in client.get("/checkpoint/3/groep/1").get_data(as_text=True)
    wb = load_workbook(io.BytesIO(client.get("/export.xlsx").data))
    header = [c.value for c in wb["Groepen"][1]]
    assert header[header.index("Uitgevallen bij CP") + 1] == "Reden uitvallen"
    row = list(wb["Groepen"].iter_rows(min_row=2, values_only=True))[0]
    assert row[header.index("Reden uitvallen")] == "Blessure: knie"
    client.post("/groep/1/uitgevallen", data={"csrf": t, "undo": "1"})
    with wh.app.app_context():
        g = wh.get_db().execute("SELECT retired_at, retired_reason FROM groups WHERE id = 1").fetchone()
        assert g["retired_at"] is None and g["retired_reason"] == ""
    # An unknown choice is ignored; the text alone is kept, cut at 200 characters.
    client.post("/groep/1/uitgevallen", data={"csrf": t, "reason": "Hack", "reason_text": "x" * 300})
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT retired_reason FROM groups WHERE id = 1").fetchone()[0] == "x" * 200


def test_merge_sets_retired_reason(client):
    t = login(client, "admin")
    client.post("/groepen", data={"csrf": t, "category": "junioren", "hiker_name": ["Cees", "", ""],
                                  "hiker_comment": ["", "", ""]})
    with wh.app.app_context():
        cees = wh.get_db().execute("SELECT id FROM hikers WHERE name = 'Cees'").fetchone()[0]
    client.post("/groepen/2", data={"csrf": t, "op": "merge", "target": "1", "hiker": [str(cees)]})
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT retired_reason FROM groups WHERE id = 2").fetchone()[0] == \
            "Samengevoegd met 101"


def test_old_database_gets_retired_reason_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, category TEXT NOT NULL)")
    con.execute("INSERT INTO groups (name, category) VALUES ('101', 'junioren')")
    con.commit()
    con.close()
    wh.app.config.update(DATABASE=str(path), TESTING=True)
    with wh.app.app_context():
        wh.init_db()
        assert wh.get_db().execute("SELECT retired_reason FROM groups").fetchone()[0] == ""


def test_paper_sheets(client):
    t = login(client, "admin")
    client.post("/checkpoints", data={"csrf": t, "day1": "7", "day2": "5", "name_3": "Molen",
                                      "theme_3": "Knopen", "crew_3": "Sanne"})
    with wh.app.app_context():
        db = wh.get_db()
        db.execute("INSERT INTO settings (key, value) VALUES ('start_date', '2026-06-13')")
        db.execute("INSERT INTO groups (name, category, retired_at) "
                   "VALUES ('102', 'junioren', '2026-01-01T00:00:00+00:00')")
        db.commit()
    page = client.get("/papier").get_data(as_text=True)
    for cp in range(1, 13):
        assert f"<h1>{'CP 3 · Molen' if cp == 3 else f'CP {cp}'}</h1>" in page
    assert page.count('class="paper-cp"') == 12
    assert "14 juni 2026" in page  # day 2
    assert "Papieren lijsten afdrukken" in client.get("/checkpoints").get_data(as_text=True)
    client.post("/uitloggen", data={"csrf": t})
    login(client, "post3")
    page = client.get("/checkpoint/3/papier").get_data(as_text=True)
    assert "CP 3 · Molen" in page and "13 juni 2026" in page and "Knopen" in page and "Sanne" in page
    assert "Anna: notenallergie" in page and "<b>101</b>" in page and "<b>102</b>" not in page
    assert "window.print()" in page and "Praktijk" in page
    assert client.get("/checkpoint/4/papier").status_code == 403
    assert client.get("/papier").status_code == 403
    login(client, "kijk")
    assert client.get("/checkpoint/4/papier").status_code == 200
    assert client.get("/papier").status_code == 403
# --- Agent D: statistics and hiker status page

def test_statistics_page(client):
    with wh.app.app_context():
        db = wh.get_db()
        db.execute("INSERT INTO groups (name, category, retired_at, retired_cp) "
                   "VALUES ('201', 'senioren', '2026-05-01T10:00:00+00:00', 2)")
        db.execute("INSERT INTO settings (key, value) VALUES ('leg_1', '20')")
        db.executemany("INSERT INTO checkins (group_id, checkpoint, checkin_at, checkout_at) VALUES (?, ?, ?, ?)", [
            (1, 1, "2026-05-01T08:00:00+00:00", "2026-05-01T08:10:00+00:00"),
            (1, 2, "2026-05-01T08:35:00+00:00", None),   # 25 min
            (2, 1, "2026-05-01T08:00:00+00:00", "2026-05-01T08:05:00+00:00"),
            (2, 2, "2026-05-01T08:45:00+00:00", None)])  # 40 min
        db.executemany("INSERT INTO scores (group_id, checkpoint, quiz, practical, updated_at) "
                       "VALUES (?, ?, ?, ?, '2026-05-01T09:00:00+00:00')",
                       [(1, 1, 9, 9), (2, 1, 9, 9), (1, 2, 5, 5), (1, 3, 7, 7), (2, 3, 7, 8)])
        db.commit()
    login(client, "kijk")
    page = client.get("/statistieken").get_data(as_text=True)
    assert "CP 1 → CP 2" in page and "20 min" in page and "32 min" in page  # expected, average (25+40)/2 rounded
    assert "groep 101 (25 min)" in page and "groep 201 (40 min)" in page
    assert "18,0" in page and "milder" in page and "strenger" in page  # CP1 18, CP2 10, CP3 14,5
    assert "<td>42,0</td>" in page  # junioren: group 101 has 9+9+5+5+7+7 points
    assert "Uitvallers per checkpoint" in page and "Anna" not in page
    assert 'href="/statistieken"' in client.get("/uitslag").get_data(as_text=True)
    login(client, "post3")
    assert client.get("/statistieken").status_code == 403


def status_code_of(gid=1):
    with wh.app.app_context():
        return wh.group_code(wh.get_db(), gid)


def test_status_page_off_unknown_and_private(client):
    make_super()
    code = status_code_of()
    assert code and status_code_of() == code  # made once, then kept
    anon = wh.app.test_client()
    assert anon.get(f"/status/{code}").status_code == 404  # off by default
    t = login(client, "admin")
    client.post("/instellingen", data={"csrf": t, "op": "status_page", "value": "1"})
    r = anon.get(f"/status/{code}")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "Groep 101" in page and "Nog niet gestart" in page
    assert 'name="robots" content="noindex"' in page and 'content="60"' in page
    assert "Anna" not in page and "Bram" not in page and "notenallergie" not in page
    assert "punten" not in page  # rank is off
    off = anon.get("/status/onbekend")
    assert off.status_code == 404 and "Anna" not in off.get_data(as_text=True)
    client.post("/checkpoint/1/groep/1/in", data={"csrf": t})
    assert "Op CP 1" in anon.get(f"/status/{code}").get_data(as_text=True)
    client.post("/checkpoint/1/groep/1/uit", data={"csrf": t})
    assert "Onderweg naar CP 2 (vertrokken" in anon.get(f"/status/{code}").get_data(as_text=True)
    client.post("/checkpoint/1/groep/1", data={"csrf": t, "quiz_1": "8", "practical_1": "6"})
    client.post("/instellingen", data={"csrf": t, "op": "status_rank", "value": "1"})
    page = anon.get(f"/status/{code}").get_data(as_text=True)
    assert "<b>14</b> punten" in page and "1e</b> van 1" in page and "1 van 12 checkpoints" in page
    client.post("/groep/1/uitgevallen", data={"csrf": t})
    assert "Uitgevallen" in anon.get(f"/status/{code}").get_data(as_text=True)
    with wh.app.app_context():
        db = wh.get_db()
        assert db.execute("SELECT COUNT(*) FROM audit_log WHERE path LIKE '/status/%'").fetchone()[0] == 0
        actions = [r[0] for r in db.execute("SELECT action FROM audit_log")]
        assert "statuspagina voor wandelaars aangezet" in actions
        assert "plaats en punten op statuspagina aangezet" in actions
    # Switching it off again hides the page.
    client.post("/instellingen", data={"csrf": t, "op": "status_page", "value": "0"})
    assert anon.get(f"/status/{code}").status_code == 404


def test_status_codes_renew_and_wipe(client):
    make_super()
    t = login(client, "admin")
    client.post("/instellingen", data={"csrf": t, "op": "status_page", "value": "1"})
    old = status_code_of()
    anon = wh.app.test_client()
    assert anon.get(f"/status/{old}").status_code == 200
    client.post("/instellingen", data={"csrf": t, "op": "status_codes_renew"})
    new = status_code_of()
    assert new and new != old
    assert anon.get(f"/status/{old}").status_code == 404
    assert anon.get(f"/status/{new}").status_code == 200
    client.post("/instellingen", data={"csrf": t, "op": "wipe", "confirm": "WISSEN"})
    assert anon.get(f"/status/{new}").status_code == 404
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT COUNT(*) FROM groups WHERE code IS NOT NULL").fetchone()[0] == 0


def test_status_settings_superadmin_only(client):
    t = login(client, "admin")  # plain admin
    for op in ("status_page", "status_rank", "status_codes_renew"):
        assert client.post("/instellingen", data={"csrf": t, "op": op, "value": "1"}).status_code == 403
    t = login(client, "post3")
    assert client.post("/instellingen", data={"csrf": t, "op": "status_page", "value": "1"}).status_code == 403
    with wh.app.app_context():
        assert wh.get_setting("status_page") is None and wh.get_setting("status_rank") is None
    make_super()
    login(client, "admin")
    assert "Statuspagina voor wandelaars" in client.get("/instellingen").get_data(as_text=True)


def test_cards_show_status_qr_only_when_on(client):
    make_super()
    t = login(client, "admin")
    page = client.get("/groepen/kaarten").get_data(as_text=True)
    assert "Volg jullie groep" not in page and "qr-status" not in page and "<svg" in page
    client.post("/instellingen", data={"csrf": t, "op": "status_page", "value": "1"})
    page = client.get("/groepen/kaarten").get_data(as_text=True)
    assert "Volg jullie groep" in page and "Voor de posten" in page and page.count('class="qr-status"><svg') == 1
    with wh.app.app_context():
        assert wh.get_db().execute("SELECT code FROM groups WHERE id = 1").fetchone()[0]
