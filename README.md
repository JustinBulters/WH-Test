# WH Hike

Website for tracking hikers at checkpoints during a two-day hike. Lightweight on purpose: server-rendered pages, no JavaScript, one small stylesheet (about 2 KB), so it stays usable on phones with poor signal. The interface is in Dutch.

## What it does

- **Checkpoints 1-12.** Checkpoints 1-7 are day 1 and 8-12 are day 2.
- **Groups and hikers.** Hikers walk in groups of 2-3. Each group gets the next free number in its category (junioren 101-199, senioren 201-299, veteranen 301-399). Each hiker has a name and a comment field, for example for allergies. A group can have an emergency phone number that only admins see.
- **Where is each group now.** The overview page lists every group with its last checkpoint: at a checkpoint, on the way after one, or not started yet.
- **Quick lookup.** Type a group number at the top of a checkpoint page, the overview or the groups page to jump straight to that group.
- **Group cards.** Admins can print a card per group with a QR code. Scanning it opens the group: staff land on their own checkpoint's page for that group, others on the group page.
- **Scoreboard and export.** `/scorebord` is a big ranking per category for a screen, refreshed every 30 seconds. Admins can download everything (ranking, groups, times and scores) as an Excel file from the results page.
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

- `WH_ADMIN_PASSWORD`: password for the first admin. If unset, `init-db` generates one and prints it.
- `WH_BASE_URL`: the public web address used in the QR codes, for example `https://hike.example.nl`. If unset, the address you opened the cards page with is used.

## Hosting on your own server or NAS (Docker)

Every merge to `main` runs the tests on GitHub and then publishes a Docker image to `ghcr.io/justinbulters/wh-test:latest` (for Intel/AMD and ARM).

`compose.yaml` runs two containers:

- `web`: the site on port 9999, with the database in the `data` volume, which is kept across updates. `pull_policy: always` makes a rebuild fetch the newest image.
- `updater`: checks right away and then every 2 minutes for a newer image, and restarts `web` only when there is one, so merges go live by themselves. Its log shows each check.

The first admin password is not in the repository. Put it in a `.env` file next to `compose.yaml`:

```
WH_ADMIN_PASSWORD=your-password
```

It is only used when the database is created for the first time. Change passwords later on the Profiel page.

On a Synology NAS: in **Container Manager > Project > Create**, name the project `wh-hike`, pick a folder, and paste `compose.yaml`. Put the Synology reverse proxy with a Let's Encrypt certificate in front for HTTPS, then set `WH_HTTPS: "1"`.

## Hosting on Render

`render.yaml` describes the live site for [Render](https://render.com): a web service in Frankfurt with a 1 GB disk for the database. Every push to `main` redeploys it automatically; the data on the disk is kept.

1. In Render, choose **New > Blueprint** and pick this repository.
2. Fill in `WH_ADMIN_PASSWORD` when asked. Log in as `admin` with that password.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```
