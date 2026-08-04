"""Panel-owned dynamic-DNS updater — keeps every panel-managed project's
DuckDNS A-record pointed at the home network's current public IP.

Standalone entrypoint for the DDNS systemd timer
(deploy/control-panel-ddns.service, oneshot, triggered every few minutes by
deploy/control-panel-ddns.timer) — NOT part of the running web app, runs as
its own short-lived process on VM 103 and exits when done. Invoked as:

    /home/deploy/control-panel/venv/bin/python3 -m app.ddns

WHY THIS EXISTS (real incident, 2026-07-24): the ISP handed the home network
a new public IP. VM 100's `~/duckdns/duck.sh` cron only ever updated a
hand-maintained domain list (`legacy-host,primary-store`), so `primary-store`
followed the new IP but `app-alpha` (app-alpha) and `app-beta` (app-beta) stayed
frozen at the dead old IP and went unreachable — even though every service on
VM 100 was perfectly healthy. The root flaw: that manual list has no
connection to the set of projects the panel actually manages, so every newly
onboarded project is born stale-DNS-prone. This job removes the drift by
deriving the domain set from the panel's own database every run.

SCOPE — deliberately only projects the panel onboarded:
  * A project is included iff it has a non-empty `duckdns_subdomain` recorded
    AND is not decommissioned. Paused projects ARE included (their DNS must
    stay current so a resume actually comes back reachable).
  * `primary-store` has NO `duckdns_subdomain` in the panel DB (it predates
    the wizard and is a hard-protected project — see safety.py), so it is
    NEVER in this set. Its A-record stays owned by VM 100's duck.sh exactly
    as before. This is intentional: the panel does not manage primary-store,
    for DNS or anything else.
  * DDNS_EXTRA_SUBDOMAINS (config, default empty) lets the operator OPT any
    non-DB names into panel management later if they ever want to retire
    duck.sh entirely — it is empty by default, so out of the box this touches
    nothing primary-store depends on.

DuckDNS mechanics: one authenticated call updates every listed domain to the
caller's detected public IP (`ip=` left blank → DuckDNS uses the source IP of
this request, which from VM 103 is the home network's real WAN IP — exactly
what we want). The body is the only real success signal: `OK` on the first
line, `KO` on failure (DuckDNS returns HTTP 200 either way — same gotcha the
certbot auth hook in dns_cert.py already guards against). The token is only
ever placed in the request URL, never logged: every log line here redacts it,
same discipline as the .env write and the certbot hook.
"""

import sys
import urllib.error
import urllib.parse
import urllib.request

from app.db import get_conn, init_db
from app.config import settings
from app import safety

DUCKDNS_UPDATE_URL = "https://www.duckdns.org/update"


def collect_managed_subdomains() -> list:
    """Every DuckDNS subdomain the panel is responsible for, from the DB —
    onboarded projects with a recorded subdomain that aren't decommissioned,
    plus any operator-configured extras. De-duplicated, order-stable."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, duckdns_subdomain FROM projects "
            "WHERE duckdns_subdomain IS NOT NULL AND TRIM(duckdns_subdomain) != '' "
            "AND status != 'decommissioned' "
            "ORDER BY id"
        ).fetchall()

    subs = []
    for r in rows:
        # Hard-protected projects (primary-store) are never managed by the
        # panel — for DNS exactly as for every mutating action in safety.py.
        # primary-store DOES have a duckdns_subdomain recorded, and its
        # A-record stays owned by VM 100's duck.sh; the panel must not also
        # drive it unless the operator explicitly opts in via
        # DDNS_EXTRA_SUBDOMAINS below (see config.py).
        if r["name"] in safety.PROTECTED_PROJECT_NAMES:
            continue
        sub = (r["duckdns_subdomain"] or "").strip()
        if sub and sub not in subs:
            subs.append(sub)

    extras = getattr(settings, "DDNS_EXTRA_SUBDOMAINS", "") or ""
    for extra in extras.split(","):
        extra = extra.strip()
        if extra and extra not in subs:
            subs.append(extra)

    return subs


def update_duckdns(subdomains: list) -> dict:
    """Point every given subdomain's A-record at this caller's current public
    IP via one DuckDNS update call. Never raises for a normal failure — returns
    a result dict {ok, ip, body, error} so the entrypoint can log per-run
    outcomes the same never-crash way renewal.py / ssh_client.py do."""
    if not settings.DUCKDNS_TOKEN:
        return {"ok": False, "ip": "", "body": "", "error": "DUCKDNS_TOKEN is not set in .env"}
    if not subdomains:
        return {"ok": True, "ip": "", "body": "", "error": "", "noop": True}

    # `ip=` blank → DuckDNS records the request's source IP (the home WAN IP).
    # verbose=true makes the body report the IP it actually stored, useful in logs.
    domains = ",".join(subdomains)
    url = (
        f"{DUCKDNS_UPDATE_URL}?domains={urllib.parse.quote(domains)}"
        f"&token={urllib.parse.quote(settings.DUCKDNS_TOKEN)}&ip=&verbose=true"
    )
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = resp.read().decode(errors="replace").strip()
    except urllib.error.URLError as e:
        return {"ok": False, "ip": "", "body": "", "error": f"could not reach DuckDNS: {e.reason}"}

    lines = body.splitlines()
    ok = bool(lines) and lines[0].strip() == "OK"
    reported_ip = lines[1].strip() if len(lines) > 1 else ""
    return {"ok": ok, "ip": reported_ip, "body": body, "error": "" if ok else "DuckDNS returned KO"}


def main() -> int:
    init_db()
    subs = collect_managed_subdomains()
    if not subs:
        print("DDNS: no panel-managed subdomains to update — nothing to do.")
        return 0

    result = update_duckdns(subs)
    domains = ",".join(subs)
    if result["ok"]:
        ip_note = f" -> {result['ip']}" if result["ip"] else ""
        print(f"DDNS: updated {len(subs)} subdomain(s) [{domains}]{ip_note}: OK")
        return 0
    else:
        # Body is safe to print — it never contains the token (token is only in
        # the request URL, which is never logged).
        print(
            f"DDNS: FAILED for [{domains}]: {result['error']}"
            + (f" | body={result['body']!r}" if result.get("body") else "")
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
