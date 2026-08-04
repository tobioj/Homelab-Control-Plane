"""Standalone entrypoint for the certbot auto-renewal systemd timer
(deploy/control-panel-renew.service, oneshot, triggered daily by
deploy/control-panel-renew.timer) — NOT part of the running web app, runs
as its own short-lived process on VM 103, exits when done. Invoked as:

    /home/deploy/control-panel/venv/bin/python3 -m app.renewal

Iterates every project with a real cert on record and lets
dns_cert.renew_cert_if_due() decide, per project, whether it's actually
within the renewal window — see that function's docstring for the full
design reasoning (why this is a deliberate, narrow exception to the
preview/confirm rule, and why the DuckDNS token still never touches VM 100
disk longer than one run)."""

import asyncio

from app.db import get_conn, init_db
from app import dns_cert


async def main():
    init_db()
    with get_conn() as conn:
        projects = [
            dict(r) for r in conn.execute(
                "SELECT * FROM projects WHERE cert_expiry_date IS NOT NULL AND status != 'decommissioned'"
            ).fetchall()
        ]

    if not projects:
        print("No projects with a recorded certificate — nothing to check.")
        return

    for project in projects:
        result = await dns_cert.renew_cert_if_due(project)
        if result is None:
            print(f"{project['name']}: not due yet (or skipped) — cert_expiry_date={project['cert_expiry_date']}")
        elif result["exit_status"] == 0:
            print(f"{project['name']}: renewed successfully")
        else:
            print(f"{project['name']}: renewal FAILED — {result['stderr'][:300]}")


if __name__ == "__main__":
    asyncio.run(main())
