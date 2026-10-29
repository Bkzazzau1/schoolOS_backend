# Production deployment: skulgen.com on the shared VPS

This backend is deployed at `/opt/skulgen/backend` on `159.198.75.103`
(`server1.tgcg.online`), a VPS shared with an unrelated, already-live
production app (TGCG). Everything below is scoped to stay fully isolated from
it — a separate Linux user, a separate Postgres database, a separate
systemd service on a different port, and a separate nginx server block.
Nothing here was done to, or needs to touch, anything under `/opt/tgcg`,
TGCG's `tgcg-api.service`, or the `tgcg` Postgres role/database.

## Layout

| What | Where |
| --- | --- |
| Code | `/opt/skulgen/backend` (git clone of this repo, `origin/main`) |
| Linux user | `skulgen` (system user, `/sbin/nologin`, owns `/opt/skulgen`) |
| Python | `/opt/skulgen/backend/.venv` (python3.12) |
| Env file | `/opt/skulgen/backend/.env` (mode 600, owned by `skulgen`) |
| Postgres | database `skulgen`, role `skulgen`, same Postgres 16 server as `tgcg` |
| App process | `skulgen-api.service` (systemd), gunicorn bound to `127.0.0.1:8001` |
| Static files | `/opt/skulgen/backend/staticfiles` (nginx serves `/static/` directly) |
| nginx | `/etc/nginx/conf.d/skulgen.conf` |
| TLS | `/etc/letsencrypt/live/skulgen.com/` (one cert covering skulgen.com, www, api, admin, app); certbot's own systemd timer renews it |
| Cache | Valkey (Redis-compatible), `valkey.service`, `127.0.0.1:6379`, password in `/opt/skulgen/backend/.env`'s `REDIS_URL` — shared with nothing else on this box |

## Postgres authentication

The server's default `pg_hba.conf` uses `ident` for TCP connections on
127.0.0.1, which Django/psycopg cannot satisfy (there is no identd running).
Rather than change that default (which TGCG's own connection method may rely
on), two narrow rules were inserted **before** the generic `ident` rules,
scoped only to `skulgen`/`skulgen`:

```
host    skulgen         skulgen         127.0.0.1/32            scram-sha-256
host    skulgen         skulgen         ::1/128                 scram-sha-256
```

Applied with `pg_ctl reload` (a config reload, not a server restart — TGCG's
connections were never interrupted). A backup of the original file is at
`/var/lib/pgsql/data/pg_hba.conf.bak-skulgen-<timestamp>`.

## systemd unit

`/etc/systemd/system/skulgen-api.service` mirrors TGCG's own unit's hardening
(`ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, `UMask=0077`,
`ReadWritePaths` limited to the `media` directory), with its own
`RuntimeDirectory=skulgen`, its own `User=skulgen`, and `--bind 127.0.0.1:8001`
(TGCG owns 8000). `systemctl status skulgen-api.service` / `journalctl -u
skulgen-api.service` for logs.

## nginx

`/etc/nginx/conf.d/skulgen.conf` has the HTTP (port 80) block serving the
ACME challenge and redirecting everything else to HTTPS, followed by the
HTTPS server blocks for `api.skulgen.com` and `admin.skulgen.com` (proxying
to `127.0.0.1:8001`) — also kept here as `deploy/nginx-skulgen-https.conf`,
the exact text appended to the live file.

`skulgen.com`, `www.skulgen.com` and `app.skulgen.com` have DNS and will be
covered by the TLS cert, but have no nginx server block yet: there is no
content to serve there (the marketing site and a future web build don't
exist yet). Unmatched HTTPS traffic falls through to the server's existing
default `444` block, the same as any other unmatched host on this box.

## What a redeploy looks like

```
cd /opt/skulgen/backend
git pull
sudo -u skulgen .venv/bin/pip install -r requirements.txt
sudo -u skulgen .venv/bin/python manage.py migrate --noinput
sudo -u skulgen .venv/bin/python manage.py collectstatic --noinput
systemctl restart skulgen-api.service
```

## Prepared but not installed: billing automation timers

`skulgen-roster-meters.{service,timer}` and `skulgen-billing-cycles.{service,timer}`
(kept here as the exact unit files to install) run `publish_roster_meters` daily at
01:45 and `run_billing_cycles` daily at 02:15 - roster meters must be fresh before a
billing cycle can issue an invoice, hence the ordering. Neither is installed or enabled
yet; commercial automatic invoicing stays off regardless (each plan's own
`BillingCyclePolicy.automatic_invoicing_enabled` is a separate, per-plan gate, off by
default, so installing these timers alone does not start charging anyone - but do not
install them without explicit authorization to activate billing automation). To install:

```
sudo cp deploy/skulgen-roster-meters.service deploy/skulgen-roster-meters.timer \
        deploy/skulgen-billing-cycles.service deploy/skulgen-billing-cycles.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now skulgen-roster-meters.timer skulgen-billing-cycles.timer
```

## Not yet wired up here

- **Background workers** (`run_media_jobs --loop`, `run_mandate_jobs --loop`,
  `run_collection_jobs --loop`) and `expire_transferverify_alerts` — see the
  "Process types" section of the main `README.md`. None of these have a
  systemd unit or cron entry on this server yet.
- **Backups** for the `skulgen` Postgres database — nothing is scheduled yet.
- **EMAIL_URL** — unset, so invitation emails and the password-reset email
  (`apps/credentials/password_reset.py`) will fail until a real SMTP
  provider is chosen and configured in `/opt/skulgen/backend/.env`.
- A firewall layer on this VPS (`firewalld`/`ufw`) is inactive; whatever
  network-level filtering exists is at the cloud provider, outside this
  repo's scope to change.
