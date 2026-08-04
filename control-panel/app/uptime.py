"""External uptime + cert-expiry prober — the telemetry that would have
caught the 2026-07-24 outage the moment it happened.

Standalone oneshot entrypoint for the control-panel-uptime systemd timer
(deploy/control-panel-uptime.{service,timer}) — NOT part of the running web
app. Same shape as app/ddns.py / app/renewal.py: runs briefly on VM 103,
exits. Invoked as:

    /home/deploy/control-panel/venv/bin/python3 -m app.uptime

For every non-decommissioned project with a DuckDNS subdomain it does a real
HTTPS GET of the public URL (the outside-world view the systemd service
columns can't give — services can be perfectly healthy while DNS/nginx/the
WAN make the site unreachable, exactly what happened on 2026-07-24) and reads
cert_expiry_date. Results are upserted into project_health; ntfy alerts fire
only on a state TRANSITION (up<->down, after a small consecutive-failure
threshold) and when a cert is within UPTIME_CERT_ALERT_DAYS of expiry.

Unlike app/ddns.py this does NOT exclude primary-store: probing is read-only
(a GET), so the protected-project rule (which guards mutation) doesn't apply,
and primary-store is genuinely live and worth watching.

Never raises for a normal failure — every probe/publish returns or swallows
into a result the same never-crash way ssh_client/ddns do. The ntfy topic is
built from settings and messages never contain secrets.
"""

import datetime
import ssl
import sys
import urllib.error
import urllib.request

from app.db import get_conn, init_db
from app.config import settings


def collect_probe_targets() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, duckdns_subdomain, cert_expiry_date FROM projects "
            "WHERE duckdns_subdomain IS NOT NULL AND TRIM(duckdns_subdomain) != '' "
            "AND status != 'decommissioned' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def probe_one(url: str) -> dict:
    """Real HTTPS GET. Any HTTP response below 500 (including a 404 at /,
    which is a healthy no-root-route response for these apps) counts as UP;
    a 5xx, a TLS error, a timeout, or a connection failure counts as DOWN.
    Never raises."""
    start = datetime.datetime.now()
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        return {"ok": False, "http_status": None, "latency_ms": None, "error": str(reason)[:200]}
    latency_ms = int((datetime.datetime.now() - start).total_seconds() * 1000)
    ok = code < 500
    return {"ok": ok, "http_status": code, "latency_ms": latency_ms,
            "error": "" if ok else f"HTTP {code}"}


def cert_days_left(cert_expiry_date) -> int:
    """cert_expiry_date is 'YYYY-MM-DD' (captured from certbot at issuance —
    the only reliable source; /etc/letsencrypt is unreadable). None -> None."""
    if not cert_expiry_date:
        return None
    try:
        expiry = datetime.datetime.strptime(cert_expiry_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (expiry - datetime.date.today()).days


def _ntfy_publish(title: str, message: str, priority: str = "default", tags: str = "") -> None:
    """POST to the configured ntfy topic. Best-effort, never raises — an
    alerting failure must not crash the prober."""
    url = f"{settings.NTFY_BASE_URL.rstrip('/')}/{settings.UPTIME_NTFY_TOPIC}"
    req = urllib.request.Request(url, data=message.encode(), method="POST")
    req.add_header("Title", title)
    req.add_header("Priority", priority)
    if tags:
        req.add_header("Tags", tags)
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # noqa: BLE001 - alerting is best-effort
        print(f"  (ntfy publish failed: {e})")


def _get_health(conn, project_id: int) -> dict:
    row = conn.execute("SELECT * FROM project_health WHERE project_id = ?", (project_id,)).fetchone()
    return dict(row) if row else {}


def record_and_alert(project: dict, probe: dict, days_left) -> str:
    """Upsert project_health and decide alerts. Returns a short status word
    for the run log."""
    now = datetime.datetime.utcnow().isoformat(timespec="seconds")
    new_status = "up" if probe["ok"] else "down"
    with get_conn() as conn:
        prev = _get_health(conn, project["id"])
        prev_fail = prev.get("consecutive_failures", 0) or 0
        consecutive = 0 if probe["ok"] else prev_fail + 1
        last_alerted = prev.get("last_alerted_status")
        last_cert_alert_at = prev.get("last_cert_alert_at")

        # Reachability alert only on a confirmed state transition.
        alert_status = last_alerted
        if not probe["ok"] and consecutive >= settings.UPTIME_FAIL_THRESHOLD and last_alerted != "down":
            _ntfy_publish(
                f"🔴 {project['name']} is DOWN",
                f"{project['duckdns_subdomain']}.duckdns.org unreachable: {probe['error']} "
                f"(after {consecutive} checks)",
                priority="high", tags="rotating_light",
            )
            alert_status = "down"
        elif probe["ok"] and last_alerted == "down":
            _ntfy_publish(
                f"✅ {project['name']} recovered",
                f"{project['duckdns_subdomain']}.duckdns.org is reachable again (HTTP {probe['http_status']}).",
                priority="default", tags="white_check_mark",
            )
            alert_status = "up"

        # Cert-expiry alert, at most once a day.
        if days_left is not None and days_left <= settings.UPTIME_CERT_ALERT_DAYS:
            today = datetime.date.today().isoformat()
            already_today = (last_cert_alert_at or "").startswith(today)
            if not already_today:
                _ntfy_publish(
                    f"⚠️ {project['name']} cert expires in {days_left}d",
                    f"{project['duckdns_subdomain']}.duckdns.org certificate expires in {days_left} day(s). "
                    f"Auto-renewal may have failed — check control-panel-renew.",
                    priority="high", tags="warning",
                )
                last_cert_alert_at = now

        conn.execute(
            """INSERT INTO project_health
                 (project_id, last_status, last_http_status, last_latency_ms, last_checked_at,
                  last_error, consecutive_failures, last_alerted_status, cert_days_left, last_cert_alert_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET
                 last_status=excluded.last_status, last_http_status=excluded.last_http_status,
                 last_latency_ms=excluded.last_latency_ms, last_checked_at=excluded.last_checked_at,
                 last_error=excluded.last_error, consecutive_failures=excluded.consecutive_failures,
                 last_alerted_status=excluded.last_alerted_status, cert_days_left=excluded.cert_days_left,
                 last_cert_alert_at=excluded.last_cert_alert_at""",
            (project["id"], new_status, probe["http_status"], probe["latency_ms"], now,
             probe["error"], consecutive, alert_status, days_left, last_cert_alert_at),
        )
    return new_status


def main() -> int:
    init_db()
    targets = collect_probe_targets()
    if not targets:
        print("uptime: no projects with a subdomain to probe.")
        return 0
    for project in targets:
        url = f"https://{project['duckdns_subdomain']}.duckdns.org"
        probe = probe_one(url)
        days = cert_days_left(project["cert_expiry_date"])
        status = record_and_alert(project, probe, days)
        extra = f"HTTP {probe['http_status']} {probe['latency_ms']}ms" if probe["ok"] else probe["error"]
        cert_note = f", cert {days}d" if days is not None else ""
        print(f"uptime: {project['name']:20} {status.upper():4} ({extra}){cert_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
