# Security model

Threat model for a single-operator homelab that hosts internet-facing applications. Not an enterprise
posture — an honest account of the boundaries that exist, and which ones are thin.

---

## Exposure surface

| Reachable from | What is reachable |
| --- | --- |
| Public internet | Exactly one forwarded port (`:443`) on the app node. Nothing else. |
| Home LAN | The control panel (session auth required), Redis, the observability stack, SSH on both nodes. |
| Neither | The hypervisor management interface is LAN-only. Certificate private keys are root-only on the app node. |

The control panel binds to a LAN address explicitly rather than `0.0.0.0`, so a misconfigured port
forward cannot expose it by accident.

**Weakest link, stated plainly:** the LAN is a flat trust zone. Anything with LAN access reaches the
panel's login page, Redis and the observability stack. Redis has no password — it is bound to a LAN
interface and relies on network position for access control. On a network with untrusted devices this
would be inadequate; segmenting it is the most valuable unimplemented hardening step.

---

## The privilege boundary

The orchestrator authenticates as an unprivileged user and escalates only through an explicit `sudoers`
allowlist. Paraphrased:

```sudoers
# service lifecycle, per app family
NOPASSWD: /usr/bin/systemctl {start,stop,restart,enable,disable,status} gunicorn@*
NOPASSWD: /usr/bin/systemctl {start,stop,restart,enable,disable,status} celery@*
NOPASSWD: /usr/bin/systemctl {start,stop,restart,enable,disable,status} celerybeat@*
NOPASSWD: /usr/bin/systemctl daemon-reload

# install generated files, ONLY from a staging directory the deploy user owns
NOPASSWD: /usr/bin/cp /home/deploy/.cp-staging/*.service /etc/systemd/system/*.service
NOPASSWD: /usr/bin/cp /home/deploy/.cp-staging/*.conf    /etc/nginx/sites-available/*
NOPASSWD: /usr/bin/ln -sf /etc/nginx/sites-available/* /etc/nginx/sites-enabled/*
NOPASSWD: /usr/bin/rm -f /etc/nginx/sites-enabled/*

# proxy validation and reload
NOPASSWD: /usr/sbin/nginx -t
NOPASSWD: /usr/bin/systemctl reload nginx

# certificates
NOPASSWD: /usr/bin/certbot certonly *
NOPASSWD: /usr/bin/certbot delete *

# cleanup of generated unit files
NOPASSWD: /usr/bin/rm -f /etc/systemd/system/{gunicorn,celery,celerybeat}@*.service
```

What this deliberately does **not** grant: a root shell, arbitrary `cp`, package installation, editing
`sudoers`, reading `/etc/letsencrypt`, or writing anywhere outside the two target directories.

### Write-then-privileged-copy

The deploy user cannot write into system directories. Instead it writes to a staging directory it owns,
and `sudoers` permits `cp` **only** from that exact path. Two properties follow: generated content is
reviewable on disk before it is installed, and the privileged operation is a fixed-shape copy rather than
an arbitrary write.

### Why a second authorisation layer exists

`sudoers` can only pattern-match a command string. `systemctl stop gunicorn@*` matches *every*
application — the wildcard cannot express "all except the production one." So protection lives in the
application, checked before a command is constructed:

- **Protected app names** — refuses stop, delete-files, delete-certificate, pause for critical apps.
- **Protected filenames** — refuses any app whose name would collide with a shared unit template or the
  catch-all proxy config. This was a real gap: an app could be created with a name that would overwrite
  the catch-all.
- **Subdomain uniqueness** — refuses configuring one app with a domain already claimed by another, checked
  both at record time and again immediately before every action that touches DNS, certificates or the
  proxy. Name protection alone doesn't stop a harmlessly-named test app from requesting a real
  certificate for a production domain.

---

## Secret handling

### At rest

Application configuration values are stored Fernet-encrypted (authenticated symmetric encryption) in
SQLite, keyed from the panel's own environment. The stored value is always ciphertext. Plaintext exists
only in process memory during a write.

### In transit

The `.env` write is the one action that is **not** a shell command. Content is assembled in memory,
decrypted there, and delivered over SFTP to a `0600` file.

**A decrypted secret is never interpolated into a command string.** Doing so would put it in
`pending_actions.command` and the audit log — the same flat-readable-secret problem, relocated into the
database. The preview an operator reviews shows *variable names only*.

### Key separation

Three distinct SSH keys, never conflated:

| Key | Purpose | Where the private half lives |
| --- | --- | --- |
| Orchestrator key | Panel → app node | Control plane only |
| Per-app deploy key | App host → GitHub (read-only clone) | App node |
| CI key | GitHub Actions → app node | Displayed once, never stored by the panel |

### Tokens with a bounded lifetime on disk

The DNS provider's API token is needed on the app node only during certificate issuance, inside a hook
script. That script is written over SFTP, used, and **deleted immediately after the run regardless of
outcome** — the token doesn't sit on disk longer than one issuance. It never appears in a previewed
command or the audit log.

---

## Injection and parsing hazards

| Hazard | Mitigation |
| --- | --- |
| Shell metacharacters in config values | `shlex.quote()` on every generated value. A real connection string containing `&` was silently truncated before this. |
| Untrusted input in commands | Commands are built server-side from validated enum and database values. Request text never reaches a command string. |
| Service name from a URL path | Validated against the app's own enabled-service list before use. |
| Duplicate keys in generated files | A governed-names set is filtered at three layers, because `EnvironmentFile` honours the last occurrence. |
| Provider APIs returning 200 on failure | The response body is parsed, not the status code. |

---

## Audit trail

Every mutating action writes to an append-only log with actor, action, target, result and timestamp.
Automated actions use distinct actor names (for example a renewal actor), so unattended activity is
separable from operator activity.

`pending_actions` retains the exact command, exit code, stdout and stderr for every confirmed action —
in practice an execution history alongside the audit log.

Neither ever contains a decrypted secret.

---

## Authentication

Session cookies signed with a secret from the environment; passwords hashed with bcrypt. Password change
requires the current password. Failed and successful attempts are both audited.

The initial account is seeded from environment variables on first run only, when the user table is empty.

**Gap:** no multi-factor authentication and no rate limiting on the login endpoint. Acceptable only
because the panel is not internet-reachable; it would not be if that changed.

---

## Known weaknesses

Ranked by how much they'd matter if the threat model shifted.

1. **Flat LAN.** No segmentation between the trusted control plane and general household devices. Redis
   has no password and relies entirely on network position.
2. **`DEBUG` is not turned off automatically.** The `.env` builder hard-codes debug mode on, with a
   comment saying the flip belongs at the end of go-live once HTTPS is verified — a flip that was never
   implemented. Live apps therefore serve with debug enabled, which exposes tracebacks and settings on
   error pages. This is the highest-priority open item.
3. **No MFA, no login rate limiting.** Mitigated only by the panel not being internet-reachable.
4. **Single operator, single account.** No separation of duties; the audit log is the only control.
5. **Third-party trust.** The DNS provider can direct traffic; the managed database holds application
   data; the error tracker receives stack traces that may contain request context.
6. **Orphaned artefacts accumulate.** Deleted apps can leave behind deploy keys, unit files and proxy
   configs. Not exploitable on its own, but stale credentials with no owner are exactly what you don't
   want lying around.
7. **`known_hosts` verification is disabled** on the orchestrator's SSH connections. On a flat LAN this
   trades host-key verification for convenience — a poor trade if the LAN is ever hostile.

---

## What went right

Two safety gaps in this project were caught by tooling *before* they touched anything real: an app that
could be named to collide with the catch-all proxy config, and a test app configured with a production
domain that would have requested a real certificate for it. Both were found because destructive actions
must be previewed and because a classifier flagged the operation as consequential — not because anyone
reviewed the design carefully.

The lesson generalises: mechanisms that make dangerous actions *visible before execution* catch things
review does not.
