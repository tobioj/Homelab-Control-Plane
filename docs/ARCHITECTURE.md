# Control Panel architecture

How the deploy tool is put together, one component at a time. For the picture, see the
[topology diagram](diagrams/01-topology.svg); for the reasoning behind each shape, see
[DESIGN-DECISIONS.md](DESIGN-DECISIONS.md). Pushlane CI has its own document:
[PUSHLANE-CI.md](PUSHLANE-CI.md).

---

## Where it sits

| Layer | Role in the deploy story |
| --- | --- |
| `hv-01` | Proxmox VE host. Snapshots before risky changes; nightly backups of every stateful guest. |
| `edge-node` (`10.10.0.10`) | The **data plane**: nginx, and one gunicorn + Celery worker + beat set per deployed app. |
| `infra-node` (`10.10.0.11`) | The **control plane**: the panel, the shared Redis, the observability stack and the timers. |
| Router | NAT with one dynamic WAN IP and a single forwarded port, `443`. |

Both nodes run the same LTS Linux release. The panel addresses them **by IP, never by hostname**: one guest's
hostname once matched the hypervisor's, a harmless typo that would have made name-based addressing ambiguous.

---

## The panel

A FastAPI application served by uvicorn under systemd (`Restart=always`), bound to a LAN address only.

### Modules

| Module | Responsibility |
| --- | --- |
| `main.py` | Builds the app and mounts the routers |
| `config.py` | Settings from the environment, with defaults |
| `db.py` | Schema, idempotent column migrations, seeding, the Redis index allocator |
| `auth.py` | Session cookies, bcrypt, login and admin dependencies |
| `crypto.py` | Fernet encryption and decryption for secrets at rest |
| `audit.py` | The append-only action log |
| `safety.py` | Protected apps, protected file names, subdomain ownership |
| `ssh_client.py` | **The only** module that opens a connection to a managed host |
| `actions.py` | The generic preview, confirm, execute pipeline |
| `onboarding.py` | The wizard's business logic, by far the largest module |
| `dns_cert.py` | DNS checks, DNS-01 issuance, nginx install, the public HTTPS check |
| `lifecycle.py` | Pause, resume, recommission, permanent delete |
| `decommission.py` | Ordered shutdown: stop, free resources, delete files, delete the certificate |
| `github_actions.py` | The CI deploy workflow and its dedicated key |
| `observability.py` | Per-app error tracking, log shipping and push alerts |
| `metrics.py` | Memory, restart count and uptime per unit, in one call |
| `ddns.py`, `uptime.py`, `renewal.py` | Standalone `oneshot` entry points run by timers |
| `routers/`, `templates/` | One router per route group; server-rendered HTML |

Two modules the code imports are not part of this public extract: `deploy_plan.py` (push-to-deploy, described
[below](#push-to-deploy)) and `deploy_templates.py` (renders the Jinja files in `unit_templates/` into concrete
systemd units and nginx vhosts).

### The SSH chokepoint

Every remote operation goes through one module, which offers:

- `run()`: execute a command. **It never raises.** A timeout returns `124`, a connection failure `255`.
- `write_file()`: SFTP, the only route by which secret content reaches a managed host.
- `check_reachable()` and `unit_status()`: read-only probes.
- `cached_unit_status()`: an 8-second cache used **only** by the dashboard grid. Detail pages and post-action
  checks always read live state, because that is exactly when a stale answer misleads.

It authenticates with one dedicated key. The target host is a parameter, so adding another app host needs no
code change.

### State

One SQLite database:

| Table | Holds |
| --- | --- |
| `projects` | One row per managed app: repo, branch, framework, entrypoint, Redis indices, subdomain, status, wizard position, certificate expiry |
| `project_env_vars` | Per-app configuration; values are **always** ciphertext |
| `redis_ledger` | One row per Redis database index: owner, purpose, status |
| `pending_actions` | Previewed commands waiting for confirmation, and their results |
| `project_health` | The latest probe, consecutive failures, last alerted state, certificate days left |
| `audit_log` | Every mutating action, append-only |
| `users` | Operator accounts (bcrypt) |

Schema changes are applied at startup by an idempotent `_ensure_column()` pass.

---

## The data plane: how an app is assembled

```
/srv/apps/<app>/
├── <repository contents>
├── venv/            # the app's own virtualenv
├── staticfiles/     # collected static assets, served by nginx directly
└── .env             # 0600, delivered over SFTP, never in version control
```

Three systemd units per app, all reading the same `.env` through `EnvironmentFile=`:

| Unit | Shape |
| --- | --- |
| `gunicorn@<app>` | 2 workers, `gthread`, a unix socket, 120 s timeout |
| `celery@<app>` | Worker, concurrency 2 |
| `celerybeat@<app>` | Beat; for Django, the database-backed scheduler |

Settings that matter:

- **`RuntimeDirectoryPreserve=yes`** is mandatory. Without it a restart can delete the shared runtime folder and
  every app's socket with it.
- **`Restart=on-failure`** for app units, **`Restart=always`** for the panel itself.
- **Unix sockets, not TCP ports**: an app is reachable only through the proxy, enforced by file permissions.

The scheduler flag depends on the framework. The database-backed scheduler is Django-only; rendered into
another framework's unit it produces a service that can't start.

---

## Request routing

nginx terminates TLS and proxies to the app's socket:

```nginx
server {
    listen 443 ssl;
    server_name <app>.duckdns.org;

    ssl_certificate     /etc/letsencrypt/live/<app>.duckdns.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<app>.duckdns.org/privkey.pem;

    location /static/ { alias /srv/apps/<app>/staticfiles/; }

    location / {
        include proxy_params;
        proxy_pass http://unix:/run/gunicorn/<app>.sock;
    }
}
```

A **catch-all vhost is mandatory** and sorts first by file name:

```nginx
server {
    listen 443 ssl default_server;
    server_name _;
    ssl_certificate     /etc/letsencrypt/live/<primary>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<primary>/privkey.pem;
    return 444;
}
```

Without it, a request for an unknown `Host` lands on whichever vhost nginx considers first, and one app ends up
answering for a domain that isn't its own.

The `/static/` alias is framework-dependent too: it points at Django's collected output. For a framework that
doesn't produce that folder it would shadow a route the app serves itself.

---

## Redis as a ledgered shared resource

One Redis instance on the control plane serves every app. It has 16 databases (indices 0 to 15), and each app
gets distinct indices for its broker and its cache.

The `redis_ledger` table is the allocator's source of truth. The allocator:

1. Prefers the lowest index that is genuinely free over incrementing a counter.
2. Refuses, rather than allocating at or beyond the configured maximum.
3. Never hands out index 0, the default a bare client connects to.
4. Accepts an `exclude` set, so a caller that needs two indices asks for the second properly instead of adding
   one to the first.
5. Writes every row as an upsert, because a "free" index may already have a row.

Points 3 to 5 all came out of [incident #6](INCIDENTS.md#6-an-allocator-that-didnt-know-its-limit).

**Redis lives on the control plane, not next to the apps.** Every broker URL points back across the LAN,
whichever host the app runs on.

---

## Certificates over DNS-01

The ISP blocks inbound port 80, so HTTP-01 validation is impossible. The flow:

1. The panel stages an auth hook script on the app host over SFTP.
2. `certbot certonly` runs with that manual DNS hook.
3. The hook publishes the challenge TXT record through the DNS provider's HTTP API.
4. It polls **two independent public resolvers** until both return the expected value, then waits a grace
   period.
5. Validation runs and the certificate is issued.
6. Both hook scripts are deleted whatever the outcome, so the API token doesn't stay on disk.

The expiry date is parsed from certbot's own output and stored; there is nothing to read live, because the
certificate folder is root-only and invisible to the deploy user.

The whole run takes under a minute, synchronously: no backgrounded remote process, no state held in the
panel's memory between requests.

> The provider answers HTTP 200 even when it fails; only the response *body* tells you what happened. Its DNS
> update endpoint behaves the same way.

---

## Reconciliation timers

Three `oneshot` services, each working out its targets from the database:

| Timer | Every | Behaviour |
| --- | --- | --- |
| DDNS | 5 min | Collects the subdomain of every app that isn't decommissioned and points them all at the current WAN IP in one call. Protected apps are skipped; their records are managed elsewhere. |
| Uptime | 3 min | A real HTTPS GET of each public URL. Anything below 500 counts as up (a 404 at `/` is a healthy app without a root route). Records latency and certificate days left. |
| Renewal | daily | Renews certificates that are close to expiry, after a random delay so they don't all fire at once. |

Alerts fire on **transitions**: up to down and down to up after a failure threshold, deduplicated by the last
alerted state, plus a warning before a certificate expires. A long outage notifies once, not every three
minutes.

Because the DDNS call leaves from the control plane, the provider sees the household's real WAN IP as the
source address; no IP detection is needed.

---

## Push-to-deploy

For an app that is already live, a redeploy is a preview of the real change:

1. A read-only `git fetch` and a log of the commits between the local `HEAD` and the remote branch, so the
   preview shows *what will actually land*.
2. The generated command pulls with the app's deploy key, installs dependencies, runs migrations, collects
   static files and restarts each unit through the scoped `sudoers` grant.
3. It goes through the same confirm route as every other action; there is no special execution path.

The panel can also generate a CI workflow with its own CI key for deploys triggered from GitHub. That key is
separate from both the deploy key and the orchestrator key; its private half is shown once and never stored.

---

## Observability wiring

Enabling observability for an app:

1. Creates the app's project through the error tracker's API and reads back its ingest key.
2. Grants it a dedicated push-notification topic.
3. Injects the shared log-shipping credentials.

None of these variable names were invented here; the apps already expected them. The panel fills in values it
would otherwise ask for.

Deployment state has **three** values, not two: `in_sync`, `saved_not_deployed` and `deployed_but_different`.
The comparison normalises shell quoting first, because values the panel writes are quoted on disk and values
edited by hand on the host are not.

The log viewer is a `journalctl` snapshot over SSH, refreshed every 4 seconds; it is not a streaming follow.
Service names are checked against the app's own enabled services before they reach a command.

---

## Failure modes and recovery

| Failure | What happens |
| --- | --- |
| App host unreachable | Every remote call returns exit 255; the UI reports it instead of crashing |
| The panel process dies | systemd restarts it |
| A host reboots | Units are enabled, so apps come back. The panel's own unit was the gap a reboot once exposed. |
| The managed database is down | Web requests fail and database-backed schedulers can't start; workers carry on with Redis alone |
| The WAN IP changes | The DDNS timer corrects it within 5 minutes |
| A certificate nears expiry | The daily renewal timer, plus a push warning below a threshold |
| A broken proxy config | Compound commands clean up after themselves, so validation keeps passing for every other app |
| A double-submitted confirm | The atomic claim lets one through; the other gets a clear error |
