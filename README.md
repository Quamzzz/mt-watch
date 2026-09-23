# mt-watch

External heartbeat for the [MT](https://github.com/Quamzzz/Mt) engine, running on Railway.

MT's own health check runs on the MT box. That catches a wedged process, but it cannot catch the
machine being gone: if the box dies, its watcher dies with it, and silence looks exactly like
health. This is the part that has to live somewhere else.

MT posts a short report every 15 minutes. If nothing arrives for 30, this tells you.

## Why Railway, when MT itself is on a VPS

Deliberately. MT keeps all state in one SQLite file, which forces every process into one
container, blocks zero-downtime deploys, and puts a database on a network volume. Railway is a
poor fit for that and a good fit for this — so the two together are the honest comparison:

| | MT | mt-watch |
|---|---|---|
| services | one container, four processes behind a supervisor | web + Postgres + cron |
| state | one SQLite file on a volume | managed Postgres, wired with `${{Postgres.DATABASE_URL}}` |
| deploys | volume blocks rolling deploys; every push is an outage | stateless web service, zero downtime |
| public URL | must not have one — Streamlit has no auth | a status page is meant to be public |
| build | pinned Dockerfile | Railway's own builder, no Dockerfile at all |

## Deploying it

1. **New Project → GitHub Repository →** this repo. Railway reads `railway.json` and builds with
   Railpack; there is no Dockerfile on purpose.
2. **Add a Postgres service** to the same project: **New → Database → PostgreSQL**. One click.
3. On the **web service**, set Variables:

   | variable | value |
   |---|---|
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` — a reference, not a pasted string |
   | `BEAT_TOKEN` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
   | `SILENT_AFTER_S` | `1800` |

   The reference variable is the bit worth noticing: Railway resolves it at deploy time and
   routes over the project's private network, so the database is never exposed publicly and the
   traffic does not count as egress.

4. **Settings → Networking → Generate Domain.** The healthcheck at `/healthz` is already wired in
   `railway.json`.
5. **Add a second service** from the same repo for the alerting cron. The start command and
   schedule come from `railway.cron.json`, not from the dashboard:
   - **Settings → Config-as-code:** set the path to `railway.cron.json`
   - Variables: `DATABASE_URL` (same reference), `SILENT_AFTER_S`, plus `TELEGRAM_BOT_TOKEN` and
     `TELEGRAM_CHAT_ID` — the same bot MT already uses.

   Two config files rather than dashboard settings, because **config defined in code always
   overrides the dashboard**. Both services build from one repo, so without a second file the
   cron service would inherit `railway.json` — it would run `uvicorn` instead of the checker,
   and then fail a healthcheck written for a process that is supposed to exit.

   `restartPolicyType: NEVER` matters for the same reason: a cron job that finishes its work and
   exits 0 has succeeded, and restarting it would turn a ten-minute schedule into a hot loop.

   **Set the cron schedule in the dashboard as well.** `cronSchedule` is a documented `deploy`
   field — it is in Railway's own `railway.schema.json` — but declaring it here did not take
   effect on first deploy (observed 2026-09-23); the schedule had to be entered by hand. It is
   left in the file because it is valid, and because if it starts working the two agree.

   Without the Telegram variables it still runs and logs what it *would* have said, which is a
   reasonable way to watch it work before wiring the alerts up.

## Wiring MT to it

On the MT box, in `deploy/health-check.sh`, uncomment the reporting block and set `WATCH_URL` and
`WATCH_TOKEN` in `/opt/mt/.env`. It already runs every 15 minutes, so there is nothing new to
schedule.

## Endpoints

| route | what |
|---|---|
| `GET /` | status page — one card per box, per-service last cycle, disk |
| `POST /beat` | receives a report. `Authorization: Bearer $BEAT_TOKEN` |
| `GET /healthz` | Railway's healthcheck. Says this service is up, nothing about MT |

```
POST /beat
{"host": "mt-prod", "healthy": true, "disk_pct": 37,
 "services": {"scanner": "2026-09-23T10:22:01+00:00", "monitor": null},
 "report": "optional text from `mt health`"}
```

## What it deliberately does not do

It alerts on **silence and recovery only**. A wedged scanner, a stale price, a filling disk — MT
notices those itself and messages you directly. Two systems shouting about the same thing is how
people learn to ignore alerts. The one thing MT cannot tell you is that MT is gone.

## Running it locally

```bash
docker run -d --name pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:16-alpine
uv venv && uv pip install -r requirements.txt
export DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres BEAT_TOKEN=dev
.venv/bin/uvicorn watch.app:app --port 8777
.venv/bin/python -m watch.cron          # the checker, one pass
```
