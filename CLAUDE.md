# WH hike site

Website for a two-day hike in the Netherlands. It tracks where each group of hikers is (check-in and check-out per checkpoint), holds their scores, and keeps an audit log of what staff do.

## Who you're working with
- Justin owns this repo. He is a beginner: explain things simply, in short steps, and skip jargon.
- Keep pages light and mobile-friendly. Staff use phones outdoors on a weak signal.
- The repo is **public**. Never commit passwords, `.env` files or databases.

## Stack and layout
- Python 3.13, Flask 3, SQLite, Jinja templates. Plain CSS, and only small vanilla JS (no build step, no frameworks).
- `app.py` holds everything: schema and migrations (`SCHEMA` plus `ALTER TABLE` in `init_db`), auth, routes and the audit log.
- `templates/`: one file per page, plus partials (`_*.html`). `base.html` holds the nav, notification bell and toasts.
- `static/`:
  - `style.css`, with a dark mode
  - `notify.js`: polls `/meldingen/nieuw` and shows pop-ups
  - `offline.js` and `sw.js`: queue check-ins taken without signal
  - `leaflet/`: vendored map library
- `tests/test_app.py`: pytest with Flask's test client.
- Dependencies: `openpyxl` (Excel export) and `segno` (QR codes on group cards).

## Run and test
```
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/flask --app app init-db      # first run: prints the admin password
.venv/bin/flask --app app run
.venv/bin/python -m pytest -q          # must pass before merging
```

## Domain rules
- **Day 1** has checkpoints 1-7 and **day 2** has 8-12 by default. Admins can change the number per day and the CP names, theme and crew. The two days themselves are fixed.
- **Groups** have 2-3 hikers and are numbered automatically by category: Junioren 101-199, Senioren 201-299, Veteranen 301-399. Hikers have no numbers of their own, only a name and a comment (for example allergies).
- Groups can be merged. The source group is retired, and each moved hiker's comment notes their original group.
- **Per group per checkpoint**: one check-in and check-out time, plus scores for quiz and practical (1-10 each).
  - Staff enter a score once. Changing it later needs a change request with a reason, which an admin approves or declines. Admin and staff get in-site notifications about it.
- **Roles**:
  - superadmin (`role='admin'` plus `is_super=1`): sets the start date and can wipe all group data
  - admin
  - checkpoint staff: their own CP only
  - viewer: read-only
  - Per-user "Kaart zien" flag (`can_map`), which only the superadmin can set.
  - Only admins see the red "Uitgevallen" button and the emergency phone numbers.
- A group is "late" when it exceeds the expected leg time (minutes, set per leg) plus 30 minutes.
- Usernames are not case-sensitive.
- **UI is Dutch.** Store times in UTC and show them in Europe/Amsterdam (CET/CEST).

## Deploy
- Every merge to `main` makes GitHub Actions (`.github/workflows/docker.yml`) run the tests and push `ghcr.io/justinbulters/wh-test:latest`.
- Justin's Synology NAS runs `compose.yaml` in Container Manager, with the site on port **9999**. An `updater` container pulls the new image every 2 minutes, so a merge goes live by itself.
- Data (database plus session key) lives in `/volume1/docker/wh-hike/data`, bind-mounted to `/data`.
- Secrets go in `.env` on the NAS only: `WH_ADMIN_PASSWORD` (used on first start only) and optionally `WH_BASE_URL` for the QR codes.
- The site sits behind HAProxy with a FortiGate in front:
  - `WH_HTTPS=1` makes the session cookie HTTPS-only.
  - `WH_PROXIES=1` together with HAProxy's `option forwardfor` makes the audit log show each visitor's real IP address.
