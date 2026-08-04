from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin
from app.db import get_conn
from app.config import settings
from app import actions, onboarding, dns_cert, safety

router = APIRouter(prefix="/wizard")
templates = Jinja2Templates(directory="app/templates")
# Available to every template rendered through this router's `templates`
# object (the breadcrumb include reads it) without needing to pass it
# explicitly in each route's context dict.
templates.env.globals["WIZARD_STEPS"] = onboarding.WIZARD_STEPS
templates.env.globals["WIZARD_STEP_NAMES"] = onboarding.WIZARD_STEP_NAMES
templates.env.globals["WIZARD_PHASES"] = onboarding.WIZARD_PHASES


def _get_project(project_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise ValueError(f"No such project id: {project_id}")
        return dict(row)


def _get_project_by_name(name: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ValueError(f"No such project: {name}")
        return dict(row)


def _error(e) -> HTMLResponse:
    return HTMLResponse(f"<p class='error'>{e}</p><p><a href='/'>Back</a></p>", status_code=400)


@router.get("/new", response_class=HTMLResponse)
def new_form(request: Request, user=Depends(require_admin)):
    return templates.TemplateResponse("wizard_intake.html", {"request": request, "user": user, "error": None})


@router.post("/new", response_class=HTMLResponse)
def new_submit(request: Request, name: str = Form(...), repo_url: str = Form(...),
               branch: str = Form("main"), user=Depends(require_admin)):
    try:
        project_id = onboarding.create_draft(
            actor=user["username"], name=name.strip(), repo_url=repo_url, branch=branch, vm_ip=settings.VM100_IP,
        )
    except actions.ActionError as e:
        return templates.TemplateResponse("wizard_intake.html", {"request": request, "user": user, "error": str(e)}, status_code=400)
    return RedirectResponse(f"/wizard/{project_id}", status_code=303)


@router.get("/{project_id}", response_class=HTMLResponse)
async def status_page(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        pubkey = await onboarding.generate_deploy_key(proj) if proj["deploy_key_path"] else None
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return templates.TemplateResponse(
        "wizard_status.html", {"request": request, "user": user, "project": proj, "pubkey": pubkey}
    )


@router.get("/{project_id}/summary", response_class=HTMLResponse)
async def summary_page(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        if proj["onboarding_step"] not in ("onboarding_complete", "dns_checked", "cert_issued", "nginx_installed", "https_live"):
            raise actions.ActionError("Nothing to summarize yet — finish Phase 1/2 onboarding first.")
        summary = await onboarding.build_deployment_summary(proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return templates.TemplateResponse(
        "wizard_summary.html", {"request": request, "user": user, "project": proj, "summary": summary}
    )


@router.post("/{project_id}/deploy-key/generate", response_class=HTMLResponse)
async def deploy_key_generate(project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        await onboarding.generate_deploy_key(proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/wizard/{project_id}", status_code=303)


@router.post("/{project_id}/clone")
def clone_step(project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        token = onboarding.create_clone_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{project_id}/retry-clone")
def retry_clone_step(project_id: int, branch: str = Form(...), user=Depends(require_admin)):
    """Corrects the branch after a clone failure and re-previews — same
    preview/confirm pipeline as the original clone, the operator still
    reviews the corrected command before it runs, no bypass."""
    try:
        with get_conn() as conn:
            conn.execute("UPDATE projects SET branch = ? WHERE id = ?", (branch.strip(), project_id))
        proj = _get_project(project_id)
        token = onboarding.create_clone_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.get("/{project_id}/reclone", response_class=HTMLResponse)
async def reclone_page(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        safety.assert_project_not_protected(proj["name"])
        readiness = await onboarding.check_reclone_readiness(proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return templates.TemplateResponse(
        "wizard_reclone.html", {"request": request, "user": user, "project": proj, "readiness": readiness}
    )


@router.post("/{project_id}/reclone-confirm")
def reclone_confirm_step(project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        token = onboarding.create_reclone_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.get("/{project_id}/locate", response_class=HTMLResponse)
async def locate_form(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        directories = await onboarding.list_repo_directories(proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return templates.TemplateResponse(
        "wizard_locate.html", {"request": request, "user": user, "project": proj, "directories": directories}
    )


@router.post("/{project_id}/locate")
def locate_submit(
    project_id: int,
    app_subdir: str = Form(""),
    app_subdir_pick: str = Form(""),
    framework: str = Form(...),
    user=Depends(require_admin),
):
    # A typed path always wins over the radio pick — the picker is a
    # convenience for what's really there, never a replacement for typing
    # something the picker didn't list.
    chosen = app_subdir.strip() or app_subdir_pick
    try:
        onboarding.set_location_and_framework(project_id, actor=user["username"], app_subdir=chosen, framework=framework)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/wizard/{project_id}/entrypoint", status_code=303)


@router.get("/{project_id}/entrypoint", response_class=HTMLResponse)
def entrypoint_form(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
    except ValueError as e:
        return _error(e)
    return templates.TemplateResponse("wizard_entrypoint.html", {"request": request, "user": user, "project": proj})


@router.post("/{project_id}/entrypoint")
def entrypoint_submit(project_id: int, entrypoint: str = Form(...), user=Depends(require_admin)):
    try:
        onboarding.set_entrypoint(project_id, actor=user["username"], entrypoint=entrypoint)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/wizard/{project_id}/dependency-file", status_code=303)


@router.get("/{project_id}/dependency-file", response_class=HTMLResponse)
async def dependency_file_form(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        candidates = await onboarding.find_dependency_candidates(proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return templates.TemplateResponse(
        "wizard_dependency_file.html", {"request": request, "user": user, "project": proj, "candidates": candidates}
    )


@router.post("/{project_id}/dependency-file")
async def dependency_file_submit(project_id: int, dependency_file: str = Form(...), custom_path: str = Form(""),
                                  user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        chosen = custom_path.strip() if dependency_file == "__custom__" else dependency_file
        await onboarding.set_dependency_file(proj, actor=user["username"], dependency_file_rel=chosen)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/wizard/{project_id}", status_code=303)


@router.post("/{project_id}/setup")
def setup_step(project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        token = onboarding.create_setup_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.get("/{project_id}/services-redis", response_class=HTMLResponse)
def services_redis_form(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
    except ValueError as e:
        return _error(e)
    return templates.TemplateResponse("wizard_services_redis.html", {"request": request, "user": user, "project": proj})


@router.post("/{project_id}/services-redis")
def services_redis_submit(project_id: int, uses_celery: str = Form(""), uses_celerybeat: str = Form(""),
                           separate_cache_index: str = Form(""), celery_app: str = Form("config"),
                           user=Depends(require_admin)):
    try:
        onboarding.configure_services_and_redis(
            project_id, actor=user["username"],
            uses_celery=bool(uses_celery), uses_celerybeat=bool(uses_celerybeat),
            separate_cache_index=bool(separate_cache_index), celery_app=celery_app,
        )
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/wizard/{project_id}/env-vars", status_code=303)


@router.post("/{project_id}/services-redis/free")
def services_redis_free_step(project_id: int, user=Depends(require_admin)):
    """The undo path for configure_services_and_redis()'s explicit refusal
    to reallocate — frees the current indices and lets the operator submit
    the form again. Not preview/confirm-gated like remote actions (this is
    a pure local DB operation, same category as onboarding's other direct
    DB-field setters), but still a real state change so it's its own
    explicit POST, not folded silently into the GET page."""
    try:
        onboarding.free_services_and_redis(project_id, actor=user["username"])
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/wizard/{project_id}/services-redis", status_code=303)


@router.get("/{project_id}/env-vars", response_class=HTMLResponse)
async def env_vars_form(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        # parse_env_example still runs for its side effects (var-name
        # detection + SECRET_KEY auto-gen); its pairs are now demoted to
        # OPTIONAL suggestions, not the authoritative field set.
        suggestion_pairs, _auto = await onboarding.parse_env_example(proj)
        proj = _get_project(project_id)  # re-fetch: parse_env_example updated the var-name columns
        editable, generated = onboarding.load_editable_env_vars(proj)
        existing_keys = {k for k, _ in editable}
        hidden = onboarding.hidden_var_names(proj)
        suggestions = [(k, ex) for k, ex in suggestion_pairs if k not in existing_keys and k not in hidden]
        required_verdicts = await onboarding.detect_required_env_vars(
            proj, [k for k, _ in editable] + [k for k, _ in suggestions])
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return templates.TemplateResponse(
        "wizard_env_vars.html",
        {
            "request": request, "user": user, "project": proj,
            "editable": editable, "generated": generated, "suggestions": suggestions,
            "required_verdicts": required_verdicts,
        },
    )


@router.post("/{project_id}/env-vars/upload", response_class=HTMLResponse)
async def env_vars_upload(request: Request, project_id: int, envfile: UploadFile = File(...), user=Depends(require_admin)):
    """Pre-fills the SAME form from an uploaded .env file — nothing is
    saved to the database here, only re-rendered with `value=` set instead
    of `placeholder=`. The operator still reviews/edits before hitting the
    real 'Save & continue' button, exactly as asked: upload only changes
    what's pre-filled, not what's stored."""
    try:
        proj = _get_project(project_id)
        raw = await envfile.read()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _error(actions.ActionError("That doesn't look like a text .env file — couldn't decode it as UTF-8."))
        suggestion_pairs, _auto = await onboarding.parse_env_example(proj)
        # Re-fetch AFTER parse_env_example — it just wrote this repo's detected
        # var-name columns (e.g. DJANGO_ALLOWED_HOSTS). The governed-key filter
        # below reads them; without the re-fetch, a first-ever parse via upload
        # would still see the stale bare ALLOWED_HOSTS/DEBUG and fail to drop
        # the uploaded DJANGO_-prefixed values (re-opening bug #24).
        proj = _get_project(project_id)
        uploaded_values = onboarding.parse_uploaded_env(content)

        # Redis/Celery URLs the uploaded file brings with it are always
        # dropped, never pre-filled — these are governed entirely by this
        # project's own Redis ledger allocation (the Services & Redis step),
        # and an operator's own file almost always points at THEIR previous
        # host's Redis instance or an index this ledger has no record of.
        # Silently keeping it would let the wizard write a working-looking
        # but wrong broker URL that either can't be reached from VM 100 or
        # collides with a different project's real index (the exact
        # collision class the ledger exists to prevent).
        redis_celery_keys = onboarding.BROKER_VAR_NAMES | onboarding.CACHE_VAR_NAMES
        ignored_redis_keys = sorted(k for k in uploaded_values if k in redis_celery_keys)
        for k in ignored_redis_keys:
            del uploaded_values[k]

        # Same reasoning as the Redis/Celery exclusion above, for
        # ALLOWED_HOSTS/DEBUG — real bug hit live: an uploaded file's own
        # ALLOWED_HOSTS/DEBUG values got saved as ordinary "extra" vars,
        # duplicating the dedicated ALLOWED_HOSTS/DEBUG lines the wizard's
        # own subdomain/framework-default steps already write, and since
        # EnvironmentFile= honors the LAST duplicate key, the uploaded
        # (wrong) values silently won — DEBUG=False plus a stale
        # ALLOWED_HOSTS missing the real subdomain produced a live Django
        # DisallowedHost 400 after HTTPS went live. These two are governed
        # entirely by their own dedicated wizard steps, never hand-typed or
        # uploaded, same as the Redis/Celery vars above.
        governed_keys = {proj["allowed_hosts_var_name"] or "ALLOWED_HOSTS", proj["debug_var_name"] or "DEBUG"}
        ignored_governed_keys = sorted(k for k in uploaded_values if k in governed_keys)
        for k in ignored_governed_keys:
            del uploaded_values[k]

        # If the upload explicitly provides a value for a signing key that
        # would otherwise be auto-generated (e.g. SECRET_KEY), respect it
        # instead — auto-generation is a fallback for when nothing was given,
        # never an override of something the operator explicitly provided.
        # Saved immediately (additive) and then dropped from the editable set
        # (signing keys are never shown as editable rows).
        respected_secret_keys = [k for k in uploaded_values if k.upper() in onboarding.SECRET_KEY_NAMES]
        if respected_secret_keys:
            onboarding.save_env_vars(
                project_id, actor=user["username"],
                values={k: uploaded_values[k] for k in respected_secret_keys},
            )
            for k in respected_secret_keys:
                del uploaded_values[k]

        # Editable rows = what's already saved, MERGED with the upload
        # (uploaded value wins on a key collision). Upload now augments rather
        # than replaces, and never silently discards a value the operator had.
        editable_saved, generated = onboarding.load_editable_env_vars(proj)
        merged = dict(editable_saved)
        merged.update(uploaded_values)
        editable = sorted(merged.items())
        existing_keys = set(merged)
        hidden = onboarding.hidden_var_names(proj)
        suggestions = [(k, ex) for k, ex in suggestion_pairs if k not in existing_keys and k not in hidden]
        required_verdicts = await onboarding.detect_required_env_vars(
            proj, list(existing_keys) + [k for k, _ in suggestions])
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return templates.TemplateResponse(
        "wizard_env_vars.html",
        {
            "request": request, "user": user, "project": proj,
            "editable": editable, "generated": generated, "suggestions": suggestions,
            "required_verdicts": required_verdicts, "upload_applied": True,
            "ignored_redis_keys": ignored_redis_keys, "ignored_governed_keys": ignored_governed_keys,
        },
    )


@router.post("/{project_id}/env-vars")
async def env_vars_submit(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        form = await request.form()
        # Rows are paired inputs (env_key[i], env_value[i]) — parse with
        # getlist()+zip, NOT form.items() (which collapses repeated names to a
        # single value, silently dropping every row but the last).
        keys = form.getlist("env_key")
        vals = form.getlist("env_value")
        # Defense-in-depth backstop: ALLOWED_HOSTS/DEBUG/Redis-Celery names are
        # never saved as ordinary custom vars regardless of what the operator
        # typed, since they're governed by their own steps and
        # build_env_file_content() writes dedicated lines for them.
        governed_keys = onboarding.governed_var_names(proj)
        values = {}
        for k, v in zip(keys, vals):
            k = k.strip()
            if not k or k in governed_keys:
                continue
            values[k] = v   # last-wins on a duplicate key the operator typed twice
        onboarding.reconcile_env_vars(project_id, actor=user["username"], values=values)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/wizard/{project_id}/subdomain", status_code=303)


@router.get("/{project_id}/subdomain", response_class=HTMLResponse)
def subdomain_form(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
    except ValueError as e:
        return _error(e)
    return templates.TemplateResponse("wizard_subdomain.html", {"request": request, "user": user, "project": proj})


@router.post("/{project_id}/subdomain")
def subdomain_submit(project_id: int, subdomain: str = Form(...), user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        onboarding.set_subdomain_intent(project_id, actor=user["username"], subdomain=subdomain, vm_ip=proj["vm_ip"])
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/wizard/{project_id}", status_code=303)


@router.post("/{project_id}/write-env")
def write_env_step(project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        token = onboarding.create_write_env_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{project_id}/migrate")
async def migrate_step(project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        token = await onboarding.create_migrate_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{project_id}/install-units")
async def install_units_step(project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        token = await onboarding.create_units_install_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{project_id}/start")
def start_step(project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        token = onboarding.create_start_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{project_id}/health-check", response_class=HTMLResponse)
async def health_check_step(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        checks = await onboarding.run_health_check(proj)
        onboarding.finalize_onboarding(project_id, actor=user["username"], checks=checks)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return templates.TemplateResponse(
        "wizard_health_check.html", {"request": request, "user": user, "project": _get_project(project_id), "checks": checks}
    )


@router.get("/{project_id}/dns-check", response_class=HTMLResponse)
def dns_check_form(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
    except ValueError as e:
        return _error(e)
    return templates.TemplateResponse("wizard_dns_check.html", {"request": request, "user": user, "project": proj, "error": None})


@router.post("/{project_id}/dns-check", response_class=HTMLResponse)
def dns_check_submit(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        dns_cert.create_dns_check_and_mark(project_id, actor=user["username"])
    except (ValueError, actions.ActionError) as e:
        return templates.TemplateResponse(
            "wizard_dns_check.html", {"request": request, "user": user, "project": _get_project(project_id), "error": str(e)},
            status_code=400,
        )
    return RedirectResponse(f"/wizard/{project_id}", status_code=303)


@router.post("/{project_id}/certbot-start")
async def certbot_start_step(project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        token = await dns_cert.create_certbot_start_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{project_id}/nginx-install")
async def nginx_install_step(project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        token = await dns_cert.create_nginx_install_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{project_id}/https-health-check", response_class=HTMLResponse)
async def https_health_check_step(request: Request, project_id: int, user=Depends(require_admin)):
    try:
        proj = _get_project(project_id)
        checks = await dns_cert.run_https_health_check(proj)
        dns_cert.finalize_https_live(project_id, actor=user["username"], checks=checks)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return templates.TemplateResponse(
        "wizard_health_check.html", {"request": request, "user": user, "project": _get_project(project_id), "checks": checks}
    )


@router.post("/{token}/confirm", response_class=HTMLResponse)
async def confirm(request: Request, token: str, user=Depends(require_admin)):
    try:
        pending = actions.get_pending(token)
        if pending is None:
            raise actions.ActionError("No such pending action")
        if pending["action_type"] == "onboarding_write_env":
            row = await onboarding.confirm_write_env(token, actor=user["username"])
        elif pending["action_type"] == "onboarding_certbot_start":
            row = await dns_cert.confirm_certbot_start(token, actor=user["username"])
        else:
            row = await actions.confirm_and_run(token, actor=user["username"])
    except actions.ActionError as e:
        return _error(e)

    # A project_name is always present on a pending_actions row regardless
    # of outcome — resolve it on failure too (not just success) so the
    # result page can link back to the right project and, for a clone
    # failure, offer an interpreted message + a retry-with-corrected-branch
    # form instead of dead-ending at "Back to dashboard" with raw stderr.
    try:
        proj = _get_project_by_name(row["project_name"])
    except ValueError:
        proj = None

    if row["status"] == "executed" and proj:
        finalize_map = {
            "onboarding_clone": onboarding.finalize_clone,
            "onboarding_setup": onboarding.finalize_setup,
            "onboarding_migrate": onboarding.finalize_migrate,
            "onboarding_install_units": onboarding.finalize_units_install,
            "onboarding_start": onboarding.finalize_start,
            "onboarding_nginx_install": dns_cert.finalize_nginx_install,
            "onboarding_reclone": onboarding.finalize_reclone,
        }
        finalize_fn = finalize_map.get(row["action_type"])
        if finalize_fn:
            finalize_fn(proj["id"], actor=user["username"])

    interpreted_error = None
    retry_branches = None
    if row["status"] == "failed":
        interpreted_error = onboarding.interpret_action_failure(row["action_type"], row["stderr"])
        if interpreted_error and interpreted_error["needs_branch_fix"] and proj:
            retry_branches = await onboarding.list_remote_branches(proj)

    return templates.TemplateResponse(
        "action_result.html",
        {
            "request": request, "user": user, "action": row, "project": proj,
            "interpreted_error": interpreted_error, "retry_branches": retry_branches,
        },
    )
