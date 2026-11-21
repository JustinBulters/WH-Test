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
