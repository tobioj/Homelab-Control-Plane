"""Onboarding wizard Phase 3 (brief §3.1 steps 11-13): verify the DuckDNS
subdomain actually exists, get a Let's Encrypt cert via certbot's DNS-01
challenge, install the nginx site, and prove HTTPS actually works.

DuckDNS subdomain *creation* has no API (confirmed — DuckDNS only exposes
the update endpoint, never subdomain creation) and stays a manual operator
step on DuckDNS's own site, checked independently via check_dns_resolves()
rather than trusted (brief §4 bug #5: duck.sh returns 'OK' even for a
subdomain that was never actually created). The ACME DNS-01 TXT record,
however, IS automatable through the same update endpoint duck.sh already
uses (`https://www.duckdns.org/update?domains=...&token=...&txt=...`),
confirmed live against VM 100 (which already has outbound connectivity to
duckdns.org) before writing this. certbot's --manual-auth-hook calls a
script that sets the TXT value itself and waits out DNS propagation, so the
whole certbot run is one synchronous, non-interactive call — no held-open
session, no in-memory state, no background/detached process.

DUCKDNS_TOKEN (app/config.py) is only ever embedded in a hook script staged
via SFTP on VM 100 for the duration of one certbot run, then deleted
immediately after — never in a pending_actions.command string or audit_log
entry, same discipline as onboarding.confirm_write_env's handling of
per-project secrets.
"""

import datetime
import re
import shlex
import socket
from typing import Optional

from app.db import get_conn
from app.audit import log_action
from app.config import settings
from app import actions, safety, ssh_client, deploy_templates

# How close to expiry before autonomous renewal (app/renewal.py) will
# re-issue — Let's Encrypt certs are ~90 days, this leaves comfortable
# margin for the daily systemd timer to catch it and retry on failure.
RENEWAL_WINDOW_DAYS = 14

STAGING_DIR = "/home/deploy/.cp-staging"

# Ceiling on how long the auth hook will actively poll for the TXT record
# to actually be visible via public resolvers before giving up and letting
# certbot try validation anyway. Real failures hit live, twice, on the
# same project (`app-beta`):
#   1. A flat 30s sleep wasn't always enough — certbot's own validation
#      saw an empty "" TXT record even though the value was genuinely set
#      moments later (confirmed by an independent `dig` right after).
#   2. After switching to a single-resolver poll (query 1.1.1.1 until it
#      matches), certbot STILL failed — but this time with a much more
#      specific error: "During secondary validation: Incorrect TXT record
#      <OLD VALUE>". Let's Encrypt validates from multiple network vantage
#      points specifically to defend against DNS hijacking, and the
#      secondary check hit a DIFFERENT DuckDNS serving node than the one
#      1.1.1.1 happened to be forwarding to — one that still had the
#      STALE value from a previous attempt. This is DuckDNS's own
#      infrastructure replication lag across its serving nodes, which a
#      single resolver's view can't represent. Querying two independent
#      public resolvers and requiring BOTH to agree, plus a longer grace
#      period, is a meaningfully stronger (though still not airtight)
#      signal of wide-enough propagation than any single query.
DNS_PROPAGATION_POLL_TIMEOUT_SECONDS = 120
DNS_PROPAGATION_POLL_INTERVAL_SECONDS = 3
DNS_PROPAGATION_RESOLVERS = ["1.1.1.1", "8.8.8.8"]
# Extra buffer after BOTH resolvers confirm the record, before letting
# certbot's validation query run — Let's Encrypt's own validation servers
# still may not hit the exact same DuckDNS serving nodes we just checked,
# so a longer grace period after first-seen is cheap insurance against the
# secondary-validation failure mode above.
DNS_PROPAGATION_GRACE_SECONDS = 30

CERTBOT_RUN_TIMEOUT_SECONDS = 180

# Matches certbot's own "Your cert will expire on 2026-10-07." stdout line —
# confirmed live this session against a real successful issuance.
CERT_EXPIRY_RE = re.compile(r"expire on (\d{4}-\d{2}-\d{2})")


def check_dns_resolves(subdomain: str):
    """Real DNS lookup, not a trust in duck.sh's response (brief §4 bug #5).
    Returns the resolved IP, or None if the name doesn't resolve at all."""
    fqdn = f"{subdomain}.duckdns.org"
    try:
        return socket.gethostbyname(fqdn)
    except socket.gaierror:
        return None


def create_dns_check_and_mark(project_id: int, actor: str) -> str:
    """Read-only — no SSH, no VM 100 involvement at all, just a public DNS
    lookup from wherever this app runs. Raises if the subdomain doesn't
    resolve yet, telling the operator to create it on DuckDNS's own site
    first (never implying it already exists)."""
    with get_conn() as conn:
        project = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
    subdomain = project["duckdns_subdomain"]
    if not subdomain:
        raise actions.ActionError("No DuckDNS subdomain recorded for this project yet — set it in the subdomain step first.")
    # Defense-in-depth re-check, independent of onboarding.set_subdomain_intent
    # already validating this when the subdomain was first recorded — this is
    # the first Phase 3 action that does anything with the subdomain for
    # real, so it re-verifies rather than trusting the DB row wasn't edited
    # or set some other way since then.
    safety.assert_subdomain_available(subdomain, project_id)

    resolved_ip = check_dns_resolves(subdomain)
    if resolved_ip is None:
        raise actions.ActionError(
            f"'{subdomain}.duckdns.org' does not resolve yet. Create it on DuckDNS's own site "
            f"(duckdns.org) first, then come back and check again — a duck.sh update alone will "
            f"NOT create a new subdomain, and this tool refuses to move on until it's proven to "
            f"actually resolve, not just assumed."
        )

    # Only ADVANCES onboarding_step, never regresses it — see
    # onboarding.advance_onboarding_step()'s docstring for the full
    # incident writeup (bug #23): revisiting this step after a LATER step
    # already succeeded used to unconditionally stomp onboarding_step back
    # to 'dns_checked', wasting a redundant certbot issuance. Local import:
    # onboarding.py already does a local import of THIS module (dns_cert)
    # inside build_deployment_summary specifically to avoid a circular
    # import — importing onboarding at this file's top level would create
    # the same cycle from the other direction.
    from app import onboarding
    with get_conn() as conn:
        onboarding.advance_onboarding_step(conn, project_id, "dns_checked")
    log_action(actor=actor, action="dns_check_passed", target=subdomain, details=f"resolved to {resolved_ip}")
    return resolved_ip


# ---------------------------------------------------------------------------
# certbot DNS-01 challenge — automated via DuckDNS's update API
# ---------------------------------------------------------------------------

def _paths(project_name: str) -> dict:
    return {
        "auth_hook": f"{STAGING_DIR}/{project_name}-auth-hook.sh",
        "cleanup_hook": f"{STAGING_DIR}/{project_name}-cleanup-hook.sh",
        "log_file": f"{STAGING_DIR}/{project_name}-certbot.log",
    }


def _auth_hook_script(subdomain: str, token: str) -> str:
    """Invoked by certbot with $CERTBOT_VALIDATION set. Sets the TXT record
    via DuckDNS's own update endpoint, confirms the literal 'OK' response
    body (DuckDNS always returns HTTP 200 even on failure — the body is the
    only real signal, same gotcha as duck.sh's false-positive per brief §4
    bug #5, just now checked instead of ignored), then ACTIVELY POLLS a
    public resolver until the TXT record is really visible (confirmed live
    reachable from VM 100: `dig ... @1.1.1.1` works cleanly) instead of a
    fixed sleep — real failure this replaced: a flat 30s wait let certbot's
    own validation run before the record had actually propagated, even
    though an independent check moments later showed the correct value was
    there. Falls through to the grace-period sleep even on a poll timeout
    (rather than failing outright) — the record might still show up in
    the last few seconds and it's cheap to just let certbot's own
    validation be the final word rather than giving up early ourselves."""
    fqdn = f"_acme-challenge.{subdomain}.duckdns.org"
    resolvers_bash = " ".join(shlex.quote(r) for r in DNS_PROPAGATION_RESOLVERS)
    return f"""#!/bin/bash
# Auto-generated by control-panel — DO NOT edit by hand, regenerated per run.
set -e
RESPONSE=$(curl -fsS "https://www.duckdns.org/update?domains={shlex.quote(subdomain)}&token={shlex.quote(token)}&txt=$CERTBOT_VALIDATION")
if [ "$RESPONSE" != "OK" ]; then
    echo "DuckDNS TXT update failed, response was: $RESPONSE" >&2
    exit 1
fi

RESOLVERS=({resolvers_bash})
DEADLINE=$(( $(date +%s) + {DNS_PROPAGATION_POLL_TIMEOUT_SECONDS} ))
FOUND=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    ALL_MATCH=1
    for R in "${{RESOLVERS[@]}}"; do
        SEEN=$(dig +short TXT {shlex.quote(fqdn)} "@$R" | tr -d '"')
        if [ "$SEEN" != "$CERTBOT_VALIDATION" ]; then
            ALL_MATCH=0
            break
        fi
    done
    if [ "$ALL_MATCH" = "1" ]; then
        FOUND=1
        break
    fi
    sleep {DNS_PROPAGATION_POLL_INTERVAL_SECONDS}
done
if [ "$FOUND" = "1" ]; then
    echo "TXT record confirmed visible via all of: {', '.join(DNS_PROPAGATION_RESOLVERS)}"
else
    echo "TXT record not confirmed on all resolvers within {DNS_PROPAGATION_POLL_TIMEOUT_SECONDS}s — proceeding anyway, certbot's own validation is the real check" >&2
fi
sleep {DNS_PROPAGATION_GRACE_SECONDS}
"""


def _cleanup_hook_script(project_name: str) -> str:
    p = _paths(project_name)
    return f"""#!/bin/bash
# Auto-generated by control-panel — DO NOT edit by hand, regenerated per run.
rm -f {shlex.quote(p['auth_hook'])} {shlex.quote(p['cleanup_hook'])}
"""


async def create_certbot_start_preview(actor: str, project: dict) -> str:
    """The preview command intentionally never contains DUCKDNS_TOKEN —
    only the hook script *paths* certbot will call. The token only ever
    reaches VM 100 inside the SFTP-written hook script content, in
    confirm_certbot_start(), never through a logged shell command string."""
    safety.assert_project_not_protected(project["name"])
    if not project["duckdns_subdomain"]:
        raise actions.ActionError("No DuckDNS subdomain recorded for this project.")
    if project["onboarding_step"] != "dns_checked":
        raise actions.ActionError("DNS must be checked and confirmed to resolve before starting certbot.")
    if not settings.DUCKDNS_TOKEN:
        raise actions.ActionError("DUCKDNS_TOKEN is not set in .env — required before certbot can automate the DNS-01 challenge.")
    # This is the action that actually calls DuckDNS's real update API and
    # requests a real cert — re-verify no other project shares this
    # subdomain immediately before doing anything live, not just at intake.
    safety.assert_subdomain_available(project["duckdns_subdomain"], project["id"])

    fqdn = f"{project['duckdns_subdomain']}.duckdns.org"
    p = _paths(project["name"])
    command = (
        f"sudo -n /usr/bin/certbot certonly --manual "
        f"--preferred-challenges dns "
        f"--manual-auth-hook {shlex.quote(p['auth_hook'])} "
        f"--manual-cleanup-hook {shlex.quote(p['cleanup_hook'])} "
        f"--manual-public-ip-logging-ok "
        f"-d {shlex.quote(fqdn)} "
        f"-m {shlex.quote(settings.CERTBOT_CONTACT_EMAIL)} --agree-tos --no-eff-email -n"
    )
    return actions.create_preview_row(actor, project, command, fqdn, "onboarding_certbot_start")


async def confirm_certbot_start(token: str, actor: str) -> dict:
    """Bespoke confirm, mirroring onboarding.confirm_write_env's shape:
    stages the hook scripts (the auth hook embeds the real DuckDNS token,
    never logged), runs the previewed certbot command synchronously — total
    time is bounded (one DuckDNS update call + a fixed propagation sleep +
    certbot's own validation), so no detached/background process is needed
    — then deletes both hook scripts from VM 100 regardless of outcome,
    since the auth hook is the only place the token ever touches disk."""
    row = actions.load_pending_for_confirm(token)
    with get_conn() as conn:
        project = dict(conn.execute("SELECT * FROM projects WHERE name = ?", (row["project_name"],)).fetchone())

    p = _paths(project["name"])
    log_action(actor=actor, action="confirm_execute", target=row["target_unit"], details="(staging certbot hook scripts, token redacted)")

    for path, content in (
        (p["auth_hook"], _auth_hook_script(project["duckdns_subdomain"], settings.DUCKDNS_TOKEN)),
        (p["cleanup_hook"], _cleanup_hook_script(project["name"])),
    ):
        stage_result = await ssh_client.write_file(path, content, host=project["vm_ip"], mode=0o700)
        if stage_result["exit_status"] != 0:
            row = actions.record_result(token, row, stage_result)
            return row

    result = await ssh_client.run(row["command"], host=project["vm_ip"], timeout=CERTBOT_RUN_TIMEOUT_SECONDS)

    # Best-effort cleanup regardless of certbot's own exit status — the
    # auth hook has the token in plaintext, don't leave it on disk longer
    # than necessary even if certbot itself failed.
    await ssh_client.run(f"rm -f {shlex.quote(p['auth_hook'])} {shlex.quote(p['cleanup_hook'])}", host=project["vm_ip"], timeout=15)

    row = actions.record_result(token, row, result)
    if row["status"] == "executed":
        # certbot's own stdout is the only reliable source of the expiry
        # date this app has — /etc/letsencrypt/ is root-only (0700),
        # unreadable by the orchestrator SSH user even for a plain
        # existence check (see lifecycle.py's cert_ok bug this session).
        # Captured once, here, rather than attempting a live read later.
        #
        # Only overwrite cert_expiry_date when the regex actually matched —
        # real bug hit live: re-running this step against a domain that
        # already has a still-valid cert, certbot exits 0 but prints
        # "Certificate not yet due for renewal" / "Keeping the existing
        # certificate" instead of the usual "...expire on YYYY-MM-DD" line,
        # so the regex doesn't match. The old code wrote NULL in that case
        # (expiry_match.group(1) if expiry_match else None), clobbering a
        # perfectly good previously-recorded expiry date with nothing —
        # which then broke lifecycle.py's cert_ok / decommission's
        # delete_cert gating for a project that actually still had a real
        # cert the whole time. A failed regex match means "no NEW date to
        # record," never "the old one is gone."
        expiry_match = CERT_EXPIRY_RE.search(result["stdout"])
        from app import onboarding
        with get_conn() as conn:
            if expiry_match:
                conn.execute("UPDATE projects SET cert_expiry_date = ? WHERE id = ?", (expiry_match.group(1), project["id"]))
            onboarding.advance_onboarding_step(conn, project["id"], "cert_issued")
    return row


# ---------------------------------------------------------------------------
# autonomous cert renewal — no human confirm gate, see app/renewal.py
# ---------------------------------------------------------------------------

async def renew_cert_if_due(project: dict) -> Optional[dict]:
    """Deliberate, narrow exception to this app's 'every mutating action
    goes through preview/confirm' rule (CLAUDE.md §3) — a human clicking
    'confirm' on a schedule defeats the purpose of unattended renewal, and
    a genuinely unattended timer is the whole point of this feature (see
    the design discussion in CLAUDE.md — an earlier design considered
    persisting the DuckDNS token on VM 100 permanently instead, rejected in
    favor of this: the token stays in exactly one place, this app's own
    encrypted config, and VM 100 never initiates anything back to this
    app). Scoped narrowly enough that skipping human review is safe:
    - Only ever touches a project that ALREADY has a real, previously
      human-approved Phase-3 cert (cert_expiry_date already set) — never a
      new/unfamiliar mutation against a project that hasn't been through
      Phase 3 the normal way.
    - Builds the EXACT SAME certbot command shape a human already reviewed
      and approved once during onboarding (create_certbot_start_preview),
      just with --force-renewal added (certbot certonly refuses to re-issue
      a still-valid cert without it) and no pending_actions preview row.
    - Every run is fully logged to audit_log with a distinct actor
      ('system-renewal') and a distinct outcome action name, so it's
      clearly attributable and reviewable after the fact — the review
      happens AFTER the action for this one case instead of before,
      matching how `certbot renew` itself is designed to run unattended
      everywhere it's normally used.
    - The DuckDNS token is staged into a hook script and deleted again
      immediately after, exactly like every interactive certbot run —
      autonomy doesn't relax that discipline.

    Returns None if this project isn't due for renewal yet (or has no
    cert/subdomain to renew), else the ssh_client-shaped result dict."""
    if not project["cert_expiry_date"] or not project["duckdns_subdomain"]:
        return None
    try:
        expiry = datetime.date.fromisoformat(project["cert_expiry_date"])
    except ValueError:
        return None
    days_left = (expiry - datetime.date.today()).days
    if days_left > RENEWAL_WINDOW_DAYS:
        return None

    fqdn = f"{project['duckdns_subdomain']}.duckdns.org"
    try:
        safety.assert_project_not_protected(project["name"])
    except actions.ActionError as e:
        log_action(actor="system-renewal", action="cert_renewal_skipped", target=fqdn, details=str(e), result="failed")
        return None
    if not settings.DUCKDNS_TOKEN:
        log_action(actor="system-renewal", action="cert_renewal_skipped", target=fqdn, details="DUCKDNS_TOKEN not set", result="failed")
        return None

    p = _paths(project["name"])
    command = (
        f"sudo -n /usr/bin/certbot certonly --manual "
        f"--preferred-challenges dns "
        f"--manual-auth-hook {shlex.quote(p['auth_hook'])} "
        f"--manual-cleanup-hook {shlex.quote(p['cleanup_hook'])} "
        f"--manual-public-ip-logging-ok "
        f"-d {shlex.quote(fqdn)} "
        f"-m {shlex.quote(settings.CERTBOT_CONTACT_EMAIL)} --agree-tos --no-eff-email -n --force-renewal"
    )
    log_action(actor="system-renewal", action="cert_renewal_started", target=fqdn, details=f"{days_left} days left before expiry")

    for path, content in (
        (p["auth_hook"], _auth_hook_script(project["duckdns_subdomain"], settings.DUCKDNS_TOKEN)),
        (p["cleanup_hook"], _cleanup_hook_script(project["name"])),
    ):
        stage_result = await ssh_client.write_file(path, content, host=project["vm_ip"], mode=0o700)
        if stage_result["exit_status"] != 0:
            log_action(actor="system-renewal", action="cert_renewal_failed", target=fqdn, details=f"staging failed: {stage_result['stderr']}", result="failed")
            return stage_result

    result = await ssh_client.run(command, host=project["vm_ip"], timeout=CERTBOT_RUN_TIMEOUT_SECONDS)
    await ssh_client.run(f"rm -f {shlex.quote(p['auth_hook'])} {shlex.quote(p['cleanup_hook'])}", host=project["vm_ip"], timeout=15)

    if result["exit_status"] == 0:
        # Same "never clobber a known-good expiry with NULL on a regex
        # miss" fix as confirm_certbot_start above — --force-renewal makes
        # this unlikely to hit here in practice, but the guard costs
        # nothing and keeps both call sites consistent.
        expiry_match = CERT_EXPIRY_RE.search(result["stdout"])
        if expiry_match:
            with get_conn() as conn:
                conn.execute("UPDATE projects SET cert_expiry_date = ? WHERE id = ?", (expiry_match.group(1), project["id"]))
            log_action(actor="system-renewal", action="cert_renewal_succeeded", target=fqdn, details=f"new expiry {expiry_match.group(1)}", result="ok")
        else:
            log_action(actor="system-renewal", action="cert_renewal_succeeded", target=fqdn, details="succeeded but couldn't parse new expiry from stdout — previous cert_expiry_date left unchanged", result="ok")
    else:
        log_action(actor="system-renewal", action="cert_renewal_failed", target=fqdn, details=(result["stderr"] or "")[:500], result="failed")
    return result


# ---------------------------------------------------------------------------
# nginx site install
# ---------------------------------------------------------------------------

async def create_nginx_install_preview(actor: str, project: dict) -> str:
    """Same shape as onboarding.create_units_install_preview: stage the
    rendered file via SFTP first (no secrets in an nginx site file), then
    preview the sudo cp/ln/test/reload chain. The `&&` chaining is load-
    bearing — `nginx -t` must pass before reload ever runs (brief §3.1.12),
    never a blind reload.

    The `ln -sf` step self-cleans on a failed `nginx -t` — same fix
    `lifecycle._recommission_start_command()` got after a real incident
    (§3/§4 bug #15): `ln -sf` always succeeds regardless of whether the
    site it just enabled is actually valid, so a bare `&&` chain would
    leave a broken symlink sitting in sites-enabled/ on failure, which
    blocks `nginx -t` (and therefore any reload) for every OTHER project
    too until manually cleaned up. This was flagged as an unfixed gap
    (CLAUDE.md §5) mirroring the same risk in the recommission path —
    fixed here the same way: `(nginx -t || (rm -f <link> && exit 1))`."""
    safety.assert_project_not_protected(project["name"])
    if project["onboarding_step"] != "cert_issued":
        raise actions.ActionError("Certificate must be issued before installing the nginx site.")

    filename = project["name"]
    safety.assert_filename_not_protected(filename)
    safety.assert_subdomain_available(project["duckdns_subdomain"], project["id"])
    content = deploy_templates.render_nginx_site(
        project["name"], project["duckdns_subdomain"], project["app_subdir"] or ""
    )
    staging_path = f"{STAGING_DIR}/{filename}.conf"
    result = await ssh_client.write_file(staging_path, content, host=project["vm_ip"], mode=0o644)
    if result["exit_status"] != 0:
        raise actions.ActionError(f"Failed to stage nginx site: {result['stderr']}")

    available = shlex.quote(f"/etc/nginx/sites-available/{filename}")
    enabled = shlex.quote(f"/etc/nginx/sites-enabled/{filename}")
    command = " && ".join([
        f"sudo -n /usr/bin/cp {shlex.quote(staging_path)} {available}",
        f"sudo -n /usr/bin/ln -sf {available} {enabled} && "
        f"(sudo -n /usr/sbin/nginx -t || (sudo -n /usr/bin/rm -f {enabled} && exit 1))",
        "sudo -n /usr/bin/systemctl reload nginx",
    ])
    return actions.create_preview_row(actor, project, command, filename, "onboarding_nginx_install")


def finalize_nginx_install(project_id: int, actor: str):
    from app import onboarding
    with get_conn() as conn:
        onboarding.advance_onboarding_step(conn, project_id, "nginx_installed")
    log_action(actor=actor, action="onboarding_nginx_installed", target=str(project_id))


# ---------------------------------------------------------------------------
# final HTTPS health check
# ---------------------------------------------------------------------------

async def run_https_health_check(project: dict) -> dict:
    """Same philosophy as onboarding.run_health_check: systemctl/nginx -t
    passing isn't proof anything actually works end to end. Curls through
    nginx on VM 100 itself with an explicit Host header, rather than
    relying on the public hostname resolving back through the router's NAT
    (hairpin isn't guaranteed on this network)."""
    fqdn = f"{project['duckdns_subdomain']}.duckdns.org"
    result = await ssh_client.run(
        f"curl -sk -o /dev/null -w '%{{http_code}}' https://localhost/ -H {shlex.quote('Host: ' + fqdn)} 2>&1",
        host=project["vm_ip"], timeout=15,
    )
    return {
        "https_response": result["stdout"].strip() or f"(no response: {result['stderr']})",
        "fqdn": fqdn,
    }


def finalize_https_live(project_id: int, actor: str, checks: dict):
    """Deliberately not gated on the check looking perfect — same as
    finalize_onboarding, a human reviews the actual numbers rather than the
    tool silently deciding pass/fail on its own."""
    from app import onboarding
    with get_conn() as conn:
        conn.execute("UPDATE projects SET status = 'running' WHERE id = ?", (project_id,))
        onboarding.advance_onboarding_step(conn, project_id, "https_live")
    log_action(actor=actor, action="onboarding_https_live", target=str(project_id), details=str(checks))
