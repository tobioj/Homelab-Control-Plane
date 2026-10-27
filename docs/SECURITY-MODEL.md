# Security model

The threat model for a homelab that serves public apps **and** runs a CI service that executes other people's
code. Not an enterprise posture: an honest account of the boundaries that exist, and of the ones that are thin.

---

## Contents

- [Exposure surface](#exposure-surface)
- [The edge](#the-edge)
- [CI: running other people's code](#ci-running-other-peoples-code)
- [Accounts and access](#accounts-and-access)
- [The panel's privilege boundary](#the-panels-privilege-boundary)
- [Secret handling](#secret-handling)
- [Injection and parsing hazards](#injection-and-parsing-hazards)
- [Audit trail](#audit-trail)
- [Known weaknesses](#known-weaknesses)
- [What went right](#what-went-right)

---

## Exposure surface

| Reachable from | What is reachable |
| --- | --- |
| **The internet** | One forwarded port, `443`, on `edge-node`: the deployed apps, the CI site, its API and the GitHub webhook. Nothing else. |
| **The home LAN** | The control panel (sign-in required), Redis, the observability stack, SSH, Forgejo's internal port. |
| **Neither** | The hypervisor's management UI (LAN only), certificate private keys (root-only), the Actions cache server (localhost on the runner), every secret store. |

Services bind to explicit addresses rather than `0.0.0.0`, so a misconfigured port forward can't expose them by
accident. Internal APIs check the caller's address: the CI API and the GitHub service answer only the edge
proxy and localhost, and their admin endpoints only localhost.

---

## The edge

`edge-node` is the only guest the internet can reach, and it is built to take that load:

| Control | Detail (thresholds illustrative) |
| --- | --- |
| **TLS everywhere** | Let's Encrypt certificates over DNS-01, renewed automatically |
| **Catch-all vhost** | Unknown `Host` names get `444`, the connection simply dropped, so scanners learn nothing |
| **Rate limits** | Per client: sign-in, 2FA and token creation 12/min; the API 6/s; everything else 40/s. The CI runner is exempt, so job checkouts are never throttled ([config](../examples/edge/nginx-ci-site.conf)) |
| **fail2ban: auth** | Repeated `401`s on sign-in or the API: banned after 6, longer each time ([jails](../examples/edge/fail2ban/jail.local)) |
| **fail2ban: rate limit** | Clients that keep hitting the rate limit |
| **fail2ban: scanners** | Probes for `wp-login.php`, `.env`, `.git/config`, `phpmyadmin` and the like |
| **fail2ban: repeat offenders** | Anyone banned repeatedly is blocked on every port for 7 days |
| **fail2ban: SSH** | The usual SSH jail |
| **Alerts** | Bans and anomalies are pushed to my phone; a daily digest summarises the last 24 hours and is sent at high priority when the day looks unusual |
| **Manual control** | `ci-admin blocked`, `block IP` and `unblock IP` list and change every blocklist at once |

The LAN range is never banned, so a mistake at home can't lock me out.

One deliberate detail: the GitHub webhook answers a bad signature with `403`, not `401`. The auth jail counts
401s, and a mis-set secret must never get GitHub's delivery servers banned.

---

## CI: running other people's code

A CI service exists to run code its users wrote, so the runner is treated as hostile territory:

| Boundary | How |
| --- | --- |
| **Its own VM** | Jobs run on `ci-runner` only, never next to the panel, Forgejo or the apps. It can be rebuilt from scratch at any time. |
| **Containers, unprivileged** | Docker with user-namespace remapping; never `--privileged`; no Docker socket; no host folders mounted. |
| **Resource limits** | Per job: CPU, memory (plus a little zram swap), a pid limit and a time limit. The VM's memory is fixed, so jobs can't take RAM from other guests. |
| **Network** | Hypervisor firewall: only SSH from the host comes in. Out, jobs reach the internet, Forgejo's runner port and the edge proxy, and every other private range is dropped. IP and MAC filters stop spoofing. |
| **Secrets in jobs** | Repository secrets (such as the GitHub deploy key) are injected only into the managed jobs that need them and masked in logs. |
| **Per-repository credentials** | Each repository has its own GitHub deploy key; a leak affects one repository. GitHub App tokens are minted per call and limited to one repository. |
| **Cache** | The Actions cache server listens on localhost, signs requests with a shared secret, and partitions entries by repository. |

Who can trigger a run matters too: only accounts with write access on Pushlane, plus, when GitHub sync is on,
whoever can push to the connected GitHub repository. Pull requests from forks are never synced, so outsiders
can't run code here.

---

## Accounts and access

- **Two-factor sign-in for every account**, including service accounts, which are enrolled by script.
- **Private by default.** Repositories are private; job logs are readable only by people who can read the
  repository, checked by Forgejo itself on every request.
- **Plans and quotas.** Storage and repository counts are enforced per plan by Forgejo's quota groups.
- **Tokens, not passwords, for automation.** The Pushlane API and agents use access tokens with explicit scopes;
  a re-run needs a write-scoped token and write access, a read-only token can only read.
- **Service accounts are single-purpose.** The admin automation account, the smoke-test account and the GitHub
  sync account each do one job; the sync account is only a collaborator on repositories connected to GitHub.

---

## The panel's privilege boundary

The panel connects as an unprivileged user and escalates only through an explicit `sudoers` allowlist.
Paraphrased:

```sudoers
# service lifecycle, per app family
NOPASSWD: /usr/bin/systemctl {start,stop,restart,enable,disable,status} gunicorn@*
NOPASSWD: /usr/bin/systemctl {start,stop,restart,enable,disable,status} celery@*
NOPASSWD: /usr/bin/systemctl {start,stop,restart,enable,disable,status} celerybeat@*
NOPASSWD: /usr/bin/systemctl daemon-reload

# install generated files, ONLY from a staging folder the deploy user owns
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

Deliberately **not** granted: a root shell, arbitrary `cp`, package installs, editing `sudoers`, reading
`/etc/letsencrypt`, or writing anywhere outside the two target folders.

**Write, then a fixed privileged copy.** The deploy user can't write into system folders. It writes to a
staging folder it owns, and `sudoers` allows `cp` only from that exact path. Generated files are reviewable on
disk before they're installed, and the privileged step is a fixed-shape copy, not an arbitrary write.

**Why a second layer exists.** `sudoers` matches command strings, and `systemctl stop gunicorn@*` matches every
app; it can't express "all except production". So the panel itself refuses, before building a command:

- **Protected apps**: no stop, file deletion, certificate deletion or pause for critical apps.
- **Protected file names**: no app may be named so that its files would overwrite a shared unit template or the
  catch-all proxy config. That was a real gap once.
- **Subdomain ownership**: no app may be given a domain another app owns, checked when the subdomain is recorded
  and again before every action that touches DNS, certificates or the proxy.

---

## Secret handling

**At rest.** App configuration values are stored Fernet-encrypted in SQLite, keyed from the panel's environment;
plaintext exists only in memory during a write. Pushlane's secrets (admin credentials, runner tokens, the GitHub
App's key) live in root-only files on the host or in owner-only files inside the service that uses them.

**In transit.** The `.env` write is not a shell command: it is assembled and decrypted in memory and delivered
over SFTP to a `0600` file. **A decrypted secret is never placed in a command string**, where it would land in
`pending_actions` and the audit log; the preview shows variable names only. Git credentials for the GitHub sync
are passed through the environment, never on a command line where `ps` could show them.

**Never on screen.** The GitHub App is created through GitHub's manifest flow, so its private key and webhook
secret go from GitHub to the server directly.

**Key separation:**

| Key | Purpose | Private half lives |
| --- | --- | --- |
| Orchestrator key | Panel to app host | Control plane only |
| Per-app deploy key | App host to GitHub (read-only clone) | App host |
| Panel CI key | GitHub Actions to app host | Shown once, never stored |
| Per-repository forward key | Pushlane to GitHub (push) | That repository's CI secret |
| GitHub App key | Sync and statuses | The sync service, owner-only |

**Short-lived on disk.** The DNS provider's token is needed on the app host only during issuance, inside a hook
script written over SFTP and deleted right after the run, whatever the outcome.

---

## Injection and parsing hazards

| Hazard | Mitigation |
| --- | --- |
| Shell metacharacters in config values | `shlex.quote()` on every generated value; a connection string with `&` was once cut off |
| Untrusted input in commands | Commands are built from validated enums and database values; request text never reaches them |
| Service names from a URL | Checked against the app's own enabled services |
| Duplicate keys in generated files | One governed-names set, filtered at three layers, because `EnvironmentFile` keeps the last value |
| Branch names from webhooks | Validated with `git check-ref-format`; names starting with `-` are refused |
| Non-ASCII text in HTTP headers | Normalised to ASCII before sending |
| Provider APIs that return 200 on failure | The response body is parsed, not the status code |
| Path tricks in API URLs | Owner and repository names come from Forgejo's answer, never from the URL |

---

## Audit trail

Every mutating panel action is written to an append-only log with actor, action, target, result and time.
Automated actions use their own actor names (the renewal timer, for example), so unattended activity is separate
from mine. `pending_actions` keeps the exact command, exit code, stdout and stderr of every confirmed action.
Neither ever contains a decrypted secret.

On the CI side, every re-run and cancel through the API is logged with who asked, and every sync and status the
GitHub service performs is logged with the repository and commit.

---

## Known weaknesses

Ranked by how much they would matter if the threat model changed.

1. **A flat home LAN.** The CI runner is firewalled off, but the panel's login page, Redis (no password, bound to
   the LAN) and the observability stack rely on network position. Segmenting the LAN is the most valuable
   hardening step not yet taken.
2. **`DEBUG` isn't switched off automatically** for deployed apps. The flip was planned for the end of go-live
   and never implemented. Top of the list.
3. **The panel has no MFA and no login rate limit.** Acceptable only because it isn't reachable from the
   internet. Pushlane, which is, has both.
4. **One operator.** No separation of duties; the audit log is the only control.
5. **Trusted users run code.** CI users are people I know, and jobs are contained, not sandboxed like a public
   CI provider's. GitHub sync widens "who can trigger a run" to everyone with push access to a connected repo.
6. **Third parties.** The DNS provider can redirect traffic, a managed database holds app data, the error
   tracker receives stack traces, and GitHub holds the published code.
7. **Leftovers.** Deleted apps can leave deploy keys, unit files and proxy configs behind: not exploitable alone,
   but ownerless credentials are exactly what you don't want lying around.
8. **`known_hosts` checking is off** for the panel's SSH connections, a convenience that would be a poor trade on
   a hostile LAN.

---

## What went right

Two safety gaps in the panel were caught by tooling *before* they touched anything real: an app name that would
have collided with the catch-all proxy config, and a test app configured with a production domain that would
have requested a real certificate for it. Both surfaced because destructive actions must be previewed and a
classifier flagged them as consequential, not because anyone reviewed the design carefully.

The same idea runs through Pushlane: nothing reaches GitHub without passing, nothing is restarted by killing it,
and the agent docs are run before they are trusted. **Mechanisms that make risky actions visible before they
happen catch what review doesn't.**
