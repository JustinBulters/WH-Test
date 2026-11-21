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
        db.execute("INSERT INTO groups (name, category) VALUES ('De Bevers', 'junioren')")
        db.execute("INSERT INTO hikers (number, name, comment, group_id) VALUES (101, 'Anna', 'notenallergie', 1)")
        db.execute("INSERT INTO hikers (number, name, group_id) VALUES (102, 'Bram', 1)")
        db.commit()
    return wh.app.test_client()


def csrf(c, url="/inloggen"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)


def login(c, user):
    t = csrf(c)
    r = c.post("/inloggen", data={"username": user, "password": "geheim123", "csrf": t})
    assert r.status_code == 302
    return csrf(c, "/wachtwoord")


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
        assert "groep De Bevers ingecheckt op CP3" in actions
        assert "toegang geweigerd" in actions


def test_viewer_read_only(client):
    t = login(client, "kijk")
    page = client.get("/uitslag").get_data(as_text=True)
    assert "De Bevers" in page and "101 Anna, 102 Bram" in page
    assert client.get("/checkpoint/5").status_code == 200
    assert client.post("/checkpoint/5/groep/1/in", data={"csrf": t}).status_code == 403
    assert client.post("/checkpoint/5/groep/1", data={"csrf": t, "quiz": "3"}).status_code == 403
    assert client.get("/gebruikers").status_code == 403


def test_admin_hiker_number_range(client):
    t = login(client, "admin")
    client.post("/groepen/1", data={"csrf": t, "op": "hiker_save", "number": "205", "name": "Fout"})
    client.post("/groepen/1", data={"csrf": t, "op": "hiker_save", "number": "103", "name": "Cas"})
    with wh.app.app_context():
        nums = [r[0] for r in wh.get_db().execute("SELECT number FROM hikers ORDER BY number")]
    assert nums == [101, 102, 103]
    assert client.get("/logboek").status_code == 200


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
