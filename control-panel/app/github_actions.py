"""GitHub Actions deploy-workflow generation (brief §3.1.16) — genuinely
NOT automated end to end: this only renders the workflow YAML and manages
a dedicated CI SSH keypair for the operator to wire up themselves (add the
YAML to the repo, add the private key as a GitHub Actions secret). Actually
pushing a file to the operator's GitHub repo, or calling GitHub's API at
all, is out of scope — this tool has never made an outbound GitHub API
call anywhere, and doing so on the operator's behalf would be a much
bigger trust boundary than anything else it does.

Template is based on primary-store's own real, working
.github/workflows/deploy.yml (read live off VM 100 to build this) —
appleboy/ssh-action, a dedicated CI key (NEVER the deploy key or the
orchestrator key — brief §2.6, three distinct keys, three distinct
purposes), git pull + install + (Django: migrate + collectstatic) +
restart each of this project's own enabled units."""

import shlex

from app.db import get_conn
from app.audit import log_action
from app import ssh_client, actions, safety


def render_deploy_workflow(project: dict, external_host: str, external_port: int) -> str:
    """external_host/external_port are NOT auto-detected — whatever router
    port-forward maps to VM 100's SSH port is entirely the operator's own
    network setup, invisible from inside the LAN this app runs on
    (primary-store's own real workflow uses a non-standard port, 2223,
    confirmed live — this can't be assumed to be 22)."""
    name = project["name"]
    branch = project["branch"]
    dest = f"/srv/apps/{name}"
    app_dir = f"{dest}/{project['app_subdir']}" if project["app_subdir"] else dest

    script_lines = [f"cd {app_dir}", f"git pull origin {branch}", "source venv/bin/activate"]
    dep_file = project["dependency_file"] or "requirements.txt"
    if dep_file.endswith("pyproject.toml"):
        script_lines.append("pip install .")
    else:
        script_lines.append(f"pip install -r {dep_file.rsplit('/', 1)[-1]}")

    if project["framework"] == "django":
        script_lines += ["python manage.py migrate --noinput", "python manage.py collectstatic --noinput"]

    for svc in _enabled_services(project):
        script_lines.append(f"sudo systemctl restart {svc}@{name}")

    script_block = "\n            ".join(script_lines)

    return f"""name: Deploy to VM100 ({name})
on:
  push:
    branches: [{branch}]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy via SSH
        uses: appleboy/ssh-action@v1.0.0
        with:
          host: {external_host}
          username: deploy
          key: ${{{{ secrets.SSH_PRIVATE_KEY }}}}
          port: {external_port}
          script: |
            {script_block}
"""


def _enabled_services(project: dict):
    services = ["gunicorn"]
    if project["uses_celery"]:
        services.append("celery")
    if project["uses_celerybeat"]:
        services.append("celerybeat")
    return services


def sudoers_snippet(project: dict) -> str:
    """The CI key needs its OWN narrow sudoers rule, scoped to exactly this
    project's own units — mirroring github-actions-primary-store's existing
    scoping (CLAUDE.md §5). Never widen an existing project's rule to cover
    a new one; each project gets its own line. This is display-only text
    for the operator to add by hand — this app never edits /etc/sudoers.d/
    itself, on either VM, for anything."""
    name = project["name"]
    restarts = ", ".join(f"/usr/bin/systemctl restart {svc}@{name}" for svc in _enabled_services(project))
    return f"github-actions-{name} ALL=(ALL) NOPASSWD: {restarts}"


async def generate_ci_key(project: dict, actor: str) -> dict:
    """Generates a dedicated keypair for GitHub Actions to use, SEPARATE
    from both the deploy key (generate_deploy_key, repo-read-only, lives on
    VM 100 forever) and the orchestrator key (this app's own SSH identity,
    never leaves this app). Unlike the deploy key, the CI key's PRIVATE
    half needs to leave VM 100 — it becomes a GitHub Actions secret, this
    app never stores it. Public half is appended to VM 100's
    authorized_keys (so the CI key can actually log in); private half is
    read back once, returned to the caller for one-time display, then
    deleted from VM 100 disk immediately — same 'never leave a secret
    sitting around longer than strictly necessary' discipline as the
    DuckDNS token in dns_cert.py's hook scripts. This app's own DB never
    stores the private key at all, encrypted or otherwise — there's
    nothing for a caller to persist even if it wanted to."""
    safety.assert_project_not_protected(project["name"])
    key_path = f"/home/deploy/.ssh/ci_{project['name']}"

    gen = await ssh_client.run(
        f"ssh-keygen -t ed25519 -f {shlex.quote(key_path)} -N '' -C {shlex.quote('github-actions-ci-' + project['name'])}",
        host=project["vm_ip"],
    )
    if gen["exit_status"] != 0:
        raise actions.ActionError(f"CI key generation failed: {gen['stderr']}")

    append = await ssh_client.run(
        f"cat {shlex.quote(key_path + '.pub')} >> ~/.ssh/authorized_keys", host=project["vm_ip"]
    )
    if append["exit_status"] != 0:
        raise actions.ActionError(f"Failed to trust the new CI key: {append['stderr']}")

    private_key = await ssh_client.run(f"cat {shlex.quote(key_path)}", host=project["vm_ip"])
    public_key = await ssh_client.run(f"cat {shlex.quote(key_path + '.pub')}", host=project["vm_ip"])

    # Delete BOTH halves from VM 100 disk — the public half only ever
    # needed to exist there long enough to append to authorized_keys
    # (the actual trust lives in that file now, not the .pub file itself),
    # and the private half must never persist anywhere once GitHub has it.
    await ssh_client.run(f"rm -f {shlex.quote(key_path)} {shlex.quote(key_path + '.pub')}", host=project["vm_ip"])

    log_action(actor=actor, action="ci_key_generated", target=project["name"], details="(key material not logged)")
    return {"private_key": private_key["stdout"], "public_key": public_key["stdout"]}
