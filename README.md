# WH Hike

Website for tracking hikers at checkpoints during a two-day hike. Lightweight on purpose: server-rendered pages, no JavaScript, one small stylesheet (about 2 KB), so it stays usable on phones with poor signal. The interface is in Dutch.

## What it does

- **Checkpoints 1-12.** Checkpoints 1-7 are day 1 and 8-12 are day 2.
- **Groups and hikers.** Hikers walk in groups of 2-3. Each hiker has a number (junioren 101-199, senioren 201-299, veteranen 301-399), a name and a comment field, for example for allergies. The number must fit the group's category.
- **Check-in and check-out** per group per checkpoint, stamped with the server time when the button is pressed. Times are stored in UTC and shown in Dutch time (Europe/Amsterdam).
- **Scores.** Each group gets a quiz score and a practical score from 1 to 10 at each checkpoint. The results page ranks groups per category.
- **Roles.**
  - *Beheerder* (admin): everything, including users, groups and the log.
  - *Checkpointpost* (staff): only their own checkpoint. They see hiker names and comments, check groups in and out, and enter group scores.
  - *Meekijker* (viewer): can see all checkpoints, groups and results, but cannot change anything or see the log.
- **Logboek** (audit log): every page view and every change, with user, time and what happened. Failed logins and refused access are logged too.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/flask --app app init-db          # prints the first admin password
.venv/bin/flask --app app run --host 0.0.0.0
```

Settings via environment variables:

- `WH_DATABASE`: path to the SQLite file (default `wh.sqlite3` next to `app.py`).
- `WH_SECRET_KEY`: session secret. If unset, one is generated in `.secret_key`.

For real use, run it behind HTTPS, for example with `gunicorn app:app` behind a reverse proxy.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```
