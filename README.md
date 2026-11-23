# WH Hike

Website for tracking hikers at checkpoints during a two-day hike. Lightweight on purpose: server-rendered pages, one small stylesheet and only a little plain JavaScript (offline check-ins and notification pop-ups), so it stays usable on phones with poor signal. The interface is in Dutch.

## What it does

- **Checkpoints.** Day 1 has checkpoints 1-7 and day 2 has 8-12 by default. Admins can change the number per day and give each checkpoint a name, theme and crew on the Checkpoints page, plus the expected walking time per leg.
- **Groups and hikers.** Hikers walk in groups of 2-3. Each group gets the next free number in its category (junioren 101-199, senioren 201-299, veteranen 301-399). Each hiker has a name and a comment field, for example for allergies. A group can have an emergency phone number that only admins see.
- **Merging groups.** Hikers can move to another group. The old group is marked as dropped out, and each moved hiker's comment notes their original group.
- **Where is each group now.** The overview page lists every group with its last checkpoint, and shows groups that are late: longer on the way than the expected leg time plus 30 minutes. Admins get a notification once per late group.
- **Quick lookup.** Type a group number at the top of a checkpoint page, the overview or the groups page to jump straight to that group.
- **Group cards.** Admins can print a card per group with a QR code holding the group number. Scanning it opens the group: staff land on their own checkpoint's page for that group, others on the group page.
- **Check-in and check-out** per group per checkpoint. Times are stored in UTC and shown in Dutch time (Europe/Amsterdam).
- **Offline check-in.** Without signal, Inchecken/Uitchecken taps are kept on the phone and sent automatically when there is a connection again, with the time of the tap (at most 12 hours old). A tap only leaves the phone once the server has stored it; taps the server refuses are shown on screen. The checkpoint list page stays available offline (needs HTTPS).
- **Scores.** Each group gets a quiz score and a practical score from 1 to 10 at each checkpoint. Staff enter a score once; changing it later needs a change request with a reason, which an admin approves or declines.
- **Notifications.** A bell in the header, with pop-ups, for change requests, their outcome and late groups.
- **Map.** OpenStreetMap with the checkpoints; admins place them by clicking. Who may see the map is set per user by the superadmin.
- **Scoreboard and export.** `/scorebord` is a big ranking per category for a screen, refreshed every 30 seconds. Equal totals share a place. Admins can download everything (ranking, groups, times and scores) as an Excel file from the results page.
- **Roles.**
  - *Superbeheerder* (superadmin): everything an admin can, plus the start date of the hike and wiping all group data.
  - *Beheerder* (admin): users, groups, checkpoints, scores and the log.
  - *Checkpointpost* (staff): only their own checkpoint. They see hiker names and comments, check groups in and out, and enter group scores.
  - *Meekijker* (viewer): can see all checkpoints, groups and results, but cannot change anything or see the log.
- **Logboek** (audit log): every page view and every change, with user, time, IP address and what happened. Failed logins and refused access are logged too.
- **Login safety.** After 10 wrong passwords for one username from one address, logging in from there pauses for 15 minutes. Changing a password logs that account out on all other phones.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/flask --app app init-db          # prints the first admin password
.venv/bin/flask --app app run --host 0.0.0.0
```

Settings via environment variables:

- `WH_DATABASE`: path to the SQLite file (default `wh.sqlite3` next to `app.py`).
- `WH_SECRET_KEY`: session secret. If unset, one is generated in `.secret_key` next to the database.
- `WH_ADMIN_PASSWORD`: password for the first admin. If unset, `init-db` generates one and prints it.
- `WH_BASE_URL`: the public web address used in the QR codes, for example `https://hike.example.nl`. If unset, the address you opened the cards page with is used.
- `WH_HTTPS=1`: the site is only opened via `https://`. The login cookie is then HTTPS-only and browsers are told to keep using HTTPS.
- `WH_PROXIES=1`: the site sits behind one reverse proxy. See "Real IP addresses" below.

## Hosting on your own server or NAS (Docker)

Every merge to `main` runs the tests on GitHub and then publishes a Docker image to `ghcr.io/justinbulters/wh-test:latest` (for Intel/AMD and ARM).

`compose.yaml` runs three containers:

- `web`: the site on port 9999. The database and session key are in the `data` folder next to `compose.yaml` (`WH_DIR/data`, default `/volume1/docker/wh-hike/data`), kept across updates and visible in File Station. Set `WH_DIR` in `.env` if the project folder is somewhere else. `pull_policy: always` makes a rebuild fetch the newest image.
- `move-data`: runs once before `web`. If the `data` folder is empty, it copies the database from the old `wh-hike_data` Docker volume (used by earlier versions) and makes the folder writable for the site. It never overwrites existing files.
- `updater`: checks right away and then every 2 minutes for a newer image, and restarts `web` only when there is one, so merges go live by themselves. Its log shows each check.

Secrets and settings go in a `.env` file next to `compose.yaml` on the NAS (never in the repository):

```
WH_ADMIN_PASSWORD=your-password
WH_HTTPS=1
WH_PROXIES=1
```

`WH_ADMIN_PASSWORD` is only used when the database is created for the first time. Change passwords later on the Profiel page. After changing `.env` or `compose.yaml`, stop and build the project again in Container Manager; the updater only fetches new images.

On a Synology NAS: in **Container Manager > Project > Create**, name the project `wh-hike`, pick a folder, and paste `compose.yaml`. Put a reverse proxy with HTTPS in front (HAProxy, or the Synology one), then set `WH_HTTPS=1` in `.env`. After that, log in only via the `https://` address.

**Real IP addresses.** Behind a reverse proxy every visitor seems to come from the proxy. Add `option forwardfor` to the HAProxy backend (or defaults) and set `WH_PROXIES=1`: the site then uses the address HAProxy adds to `X-Forwarded-For`, for the logbook and the login limit, and ignores anything a visitor puts in that header themselves. Only set it when the site is reached through the proxy alone; if port 9999 is also open to others, they could fake their address.

## Tests

```bash
.venv/bin/python -m pytest -q
```
