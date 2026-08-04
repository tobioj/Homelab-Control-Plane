# Architecture

A component-by-component walkthrough. For the visual overview see
[the topology diagram](diagrams/01-topology.svg); for *why* things are shaped this way see
[DESIGN-DECISIONS.md](DESIGN-DECISIONS.md).

---

## Physical and virtual layout

| Layer | What runs there |
| --- | --- |
| Hypervisor | Type-1 bare metal. Snapshots taken before risky changes. |
| `app-node` (10.10.0.20) | Public data plane: nginx, gunicorn, Celery worker + beat per app |
| `infra-node` (10.10.0.21) | Control plane: the panel, shared Redis, observability stack, timers |
| Router | NAT, one dynamic WAN IP, a single forwarded `:443` |

Both VMs run the same LTS Linux release with 4 vCPU / ~6 GB RAM. Everything is addressed **by IP, never
by hostname** — the app node's own hostname collides with the hypervisor's, a cosmetic misconfiguration
that would silently make hostname-based addressing ambiguous.

---

## Control plane: the panel

A FastAPI application (~5.6k LOC of Python) served by uvicorn under systemd with `Restart=always`, bound
to a LAN address only.

### Module responsibilities

| Module | Responsibility |
| --- | --- |
| `main.py` | App construction, router mounting |
| `config.py` | Settings from the environment, with defaults |
| `db.py` | Schema, idempotent column migrations, seeding, Redis index allocation |
| `auth.py` | Session-cookie auth, bcrypt hashing, login/admin dependencies |
| `crypto.py` | Fernet encrypt/decrypt for secrets at rest |
| `audit.py` | Append-only action log |
| `safety.py` | Protected-app and protected-filename denylists, subdomain uniqueness |
| `ssh_client.py` | **The only** module that opens a connection to a managed host |
| `actions.py` | Generic preview → confirm → execute pipeline |
| `onboarding.py` | Wizard business logic — the largest module by far |
| `dns_cert.py` | DNS verification, DNS-01 issuance, proxy install, HTTPS verification |
| `decommission.py` | Ordered shutdown: stop → free resources → delete files → delete cert |
| `lifecycle.py` | Pause, resume, recommission, permanent delete |
| `deploy_plan.py` | Push-to-deploy with an incoming-commit preview |
| `observability.py` | Per-app error tracking, log shipping and push-alert wiring |
| `deploy_templates.py` | Renders systemd units and proxy configs from Jinja templates |
| `ddns.py` / `uptime.py` / `renewal.py` | Standalone `oneshot` entrypoints driven by timers |
| `metrics.py` | Per-unit memory, restart count and uptime in one call |
| `routers/` | One module per route group |
| `templates/` | Server-rendered HTML |

### The SSH chokepoint

Every remote operation funnels through one module. It exposes:

- `run()` — execute a command. **Never raises.** Timeouts return `124`, connection errors `255`.
- `write_file()` — SFTP. The only path by which secret content reaches a managed host.
- `check_reachable()` / `unit_status()` — read-only probes.
- `cached_unit_status()` — an 8-second TTL cache, used *only* by the dashboard grid. Detail pages and
  post-action verification always read live state, because that's precisely when a stale answer misleads.

Authentication is a single dedicated key. The target host may be passed per call, so additional app hosts
need no code change.

### State

One SQLite database:

| Table | Contents |
| --- | --- |
| `projects` | One row per managed app: repo, branch, framework, entrypoint, allocated Redis indices, subdomain, status, onboarding position, certificate expiry |
| `project_env_vars` | Per-app configuration. Values are **always** ciphertext. |
| `redis_ledger` | One row per Redis database index: owner, purpose, status |
| `pending_actions` | Previewed commands awaiting confirmation, plus their results |
| `project_health` | Latest probe result, consecutive failures, last-alerted state, cert days remaining |
| `audit_log` | Append-only record of every mutating action |
| `users` | Operator accounts (bcrypt) |

Schema changes are applied by an idempotent `_ensure_column()` pass at startup.

---

## Data plane: how an app is assembled

```
/srv/apps/<app>/
├── <repository contents>
├── venv/                 # per-app virtualenv
├── staticfiles/          # collected static assets, served directly by nginx
└── .env                  # 0600, delivered over SFTP, never in version control
```

Three systemd units per app, all reading the same `.env` through `EnvironmentFile=`:

| Unit | Command shape |
| --- | --- |
| `gunicorn@<app>` | 2 workers, `gthread`, bound to a unix socket, 120s timeout |
| `celery@<app>` | worker, concurrency 2 |
| `celerybeat@<app>` | beat; for Django, with the database-backed scheduler |

Notable unit settings:

- **`RuntimeDirectoryPreserve=yes`** — mandatory. Without it, a restart can delete the shared runtime
  directory and take every app's socket with it.
- **`Restart=on-failure`** for app units; `Restart=always` for the panel itself.
- **Unix sockets, not TCP ports** — an app is reachable only through the proxy, enforced by filesystem
  permissions.

The scheduler flag is framework-gated. The database-backed scheduler is Django-specific; rendering it
into a non-Django unit produces a service that cannot start.

---

## Request routing

nginx terminates TLS and proxies to the per-app socket:

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

A **catch-all vhost is mandatory** and sorts first by filename:

```nginx
server {
    listen 443 ssl default_server;
    server_name _;
    ssl_certificate     /etc/letsencrypt/live/<primary>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<primary>/privkey.pem;
    return 444;
}
```

Without it, a request with an unrecognised `Host` header falls through to whichever vhost nginx happens to
consider first — meaning one app answers for a domain that isn't its own.

The `/static/` alias is also framework-gated: it points at Django's collected-static output. For a
framework that doesn't produce that directory, the alias would shadow a route the app serves itself.

---

## Redis as a ledgered shared resource

One Redis instance on the control plane serves every app. It is configured with 16 databases, so valid
indices are 0–15. Each app receives distinct indices for its broker and its cache.

The `redis_ledger` table is the allocator's source of truth. Allocation:

1. Prefer the lowest genuinely-free index over incrementing a counter.
2. Refuse rather than allocate at or beyond the configured maximum.
3. Exclude index 0 — the protocol default a bare client connects to, not a safely reusable slot.
4. Accept an `exclude` set, so a caller needing two indices requests the second one properly instead of
   doing arithmetic on the first.
5. Every write is an upsert, because a "free" index may be an existing row.

Points 3–5 are all consequences of [incident #6](INCIDENTS.md#6-a-resource-allocator-with-no-knowledge-of-its-own-limit).

**Redis lives on the control plane, not with the apps.** Every app's broker URL points back across the
LAN regardless of which host the app runs on.

---

## Certificates over DNS-01

Inbound `:80` is blocked by the ISP, so HTTP-01 validation is impossible. The flow:

1. The panel stages an authentication hook script on the app host over SFTP.
2. `certbot certonly` runs with a manual DNS hook.
3. The hook publishes the challenge TXT record via the DNS provider's HTTP API.
4. It polls **two independent public resolvers** until both report the expected value, then waits out a
   grace period.
5. Validation proceeds; the certificate is issued.
6. Both hook scripts are deleted regardless of outcome, so the API token doesn't persist on disk.

The expiry date is parsed from the tool's own output at issuance and stored. There is no live read
available — the certificate directory is root-only and unreadable to the deploy user.

The whole run completes in well under a minute, synchronously, with no backgrounded remote process and no
state held in the panel's memory across requests.

> The provider always returns HTTP 200, even on failure — the response *body* is the only real signal.
> The same trap applies to its DNS update endpoint.

---

## Reconciliation timers

Three `oneshot` services, each deriving its work from the database:

| Timer | Interval | Behaviour |
| --- | --- | --- |
| DDNS | 5 min | Collects every non-decommissioned app's subdomain and points them at the current WAN IP in one call. Protected apps are excluded — their records are owned elsewhere. |
| Uptime | 3 min | Real HTTPS GET of each public URL. Anything under 500 counts as up (a 404 at `/` is a healthy app with no root route). Records latency and certificate days remaining. |
| Renewal | daily | Renews certificates approaching expiry, with a randomised delay to avoid a thundering herd. |

Alerting is **transition-based**: a push notification fires on up→down and down→up after a failure
threshold, deduplicated by last-alerted state, plus a certificate-expiry warning. A persistent outage
notifies once, not every three minutes.

Because the DDNS request originates from the control plane, the provider sees the household's real WAN IP
as the request source — no IP detection needed.

---

## Push-to-deploy

For an already-onboarded app, redeployment is a preview of real work:

1. Read-only `git fetch` plus a log of commits between local `HEAD` and the remote branch — so the
   preview shows *what will actually land*.
2. The generated command pulls with the app's deploy key, installs dependencies, runs migrations and
   collects static files, then restarts each unit through the scoped `sudoers` grant.
3. It flows through the same generic confirm route as any other action — no bespoke execution path.

The panel also generates a CI workflow and a dedicated CI key for GitHub-triggered deploys. That key is
separate from both the per-app deploy key and the orchestrator key; its private half is displayed once and
never stored.

---

## Observability wiring

Enabling observability for an app:

1. Creates its project via the error tracker's API and reads back the ingest key.
2. Grants a dedicated push-notification topic.
3. Injects the shared log-shipping credentials.

None of these variable names were invented here — the applications already expected them. This is
auto-filling values the tool already asks for.

Deployment state is reported as **three** states, not a boolean: `in_sync`, `saved_not_deployed`, and
`deployed_but_different`. The comparison normalises shell quoting first, because values written by the
panel are quoted on disk while values hand-edited on the host are not.

The log viewer is a polled `journalctl` snapshot over SSH, refreshed every 4 seconds. It is not a
streaming follow — there is no WebSocket infrastructure here. Service names are validated against the
app's own enabled-service list before reaching a command.

---

## Failure modes and recovery

| Failure | Behaviour |
| --- | --- |
| App host unreachable | Every remote call returns exit 255; the UI reports it instead of raising |
| Panel process dies | systemd restarts it (`Restart=always`) |
| Host reboots | Units are enabled; apps come back. The panel's own unit was the gap this exposed. |
| Managed database unavailable | Web requests 500 and DB-backed schedulers fail to start; workers survive on Redis alone |
| WAN IP change | DDNS timer corrects within 5 minutes |
| Certificate near expiry | Daily renewal timer, plus a push warning under a day threshold |
| Broken proxy config | Compound commands self-clean, so validation stays passable for other apps |
| Duplicate confirm submit | Atomic claim; the loser gets a clear error |
