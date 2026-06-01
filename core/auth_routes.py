# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "").strip()
EDIT_PASSWORD = os.environ.get("EDIT_PASSWORD", "").strip()
VIEW_PASSWORD = os.environ.get("VIEW_PASSWORD", "").strip()
AUTH_ROLES = {"view", "edit"}

_AUTH_EXEMPT = {"/login", "/logout", "/setup-password", "/favicon.ico"}

configure_auth_helpers(
    get_db=get_db,
    site_password=SITE_PASSWORD,
    edit_password=EDIT_PASSWORD,
    view_password=VIEW_PASSWORD,
    auth_roles=AUTH_ROLES,
)


@app.before_request
def check_auth() -> None:
    """Require password setup/login before allowing access."""
    if request.path.startswith("/static") or request.path.startswith("/hero-image") or request.path.startswith("/map-image"):
        return
    if request.path in _AUTH_EXEMPT:
        return
    if not _is_password_configured():
        return redirect(url_for("setup_password", next=request.path))
    if not _is_session_authenticated():
        _clear_auth_session()
        return redirect(url_for("login", next=request.path))
    if request.path in {"/api/draft-agent", "/api/machine-chat", "/api/machine-chat-stream", "/api/jarvis-command"}:
        return
    if _is_write_request() and not _is_edit_session():
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Edit access required"}), 403
        flash("Edit access required for changes. Sign in with edit access.", "error")
        return redirect(url_for("login", next=request.path, role="edit"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _is_password_configured():
        return redirect(url_for("setup_password", next=_normalize_next_path()))

    requested_role = (request.values.get("role") or "view").strip().lower()
    if requested_role not in AUTH_ROLES:
        requested_role = "view"

    if request.method == "POST":
        pw = request.form.get("password", "")
        requested_role = (request.form.get("role") or "view").strip().lower()
        if requested_role not in AUTH_ROLES:
            requested_role = "view"

        edit_secret = _resolve_edit_password_secret()
        view_secret = _resolve_view_password_secret()
        password_is_valid = False

        if requested_role == "edit":
            if EDIT_PASSWORD or SITE_PASSWORD:
                password_is_valid = bool(edit_secret) and (pw == edit_secret)
            else:
                password_is_valid = bool(edit_secret) and check_password_hash(edit_secret, pw)
        else:
            if VIEW_PASSWORD:
                password_is_valid = pw == VIEW_PASSWORD
            elif _get_stored_view_password_hash():
                password_is_valid = check_password_hash(view_secret, pw)
            elif EDIT_PASSWORD or SITE_PASSWORD:
                password_is_valid = bool(edit_secret) and (pw == edit_secret)
            else:
                password_is_valid = bool(edit_secret) and check_password_hash(edit_secret, pw)

        if password_is_valid:
            _mark_session_authenticated(requested_role)
            session["play_boot_sequence"] = True
            next_url = _normalize_next_path()
            return redirect(next_url)
        flash("Incorrect password.", "error")
    return render_template(
        "login.html",
        next=_normalize_next_path(),
        selected_role=requested_role,
        setup_mode=False,
        form_action=url_for("login"),
    )


@app.route("/setup-password", methods=["GET", "POST"])
def setup_password():
    if SITE_PASSWORD or EDIT_PASSWORD or VIEW_PASSWORD:
        return redirect(url_for("login", next=_normalize_next_path()))

    if _get_stored_password_hash():
        return redirect(url_for("login", next=_normalize_next_path()))

    if request.method == "POST":
        edit_pw = request.form.get("edit_password", "")
        confirm_edit = request.form.get("confirm_edit_password", "")
        view_pw = request.form.get("view_password", "")
        confirm_view = request.form.get("confirm_view_password", "")
        if not edit_pw.strip() or not view_pw.strip():
            flash("Both edit and view passwords are required.", "error")
        elif len(edit_pw) < 8 or len(view_pw) < 8:
            flash("Passwords must be at least 8 characters.", "error")
        elif edit_pw != confirm_edit or view_pw != confirm_view:
            flash("Passwords do not match their confirmation.", "error")
        else:
            edit_result = get_db().execute(
                "UPDATE app_state SET state_value = ? WHERE state_key = ? AND state_value = ''",
                (generate_password_hash(edit_pw), "site_password_hash"),
            )
            view_result = get_db().execute(
                "UPDATE app_state SET state_value = ? WHERE state_key = ? AND state_value = ''",
                (generate_password_hash(view_pw), "view_password_hash"),
            )
            get_db().commit()
            if edit_result.rowcount == 1 and view_result.rowcount == 1:
                _mark_session_authenticated("edit")
                session["play_boot_sequence"] = True
                return redirect(_normalize_next_path())
            flash("Password has already been set. Please sign in.", "error")
            return redirect(url_for("login", next=_normalize_next_path()))

    return render_template(
        "login.html",
        next=_normalize_next_path(),
        setup_mode=True,
        form_action=url_for("setup_password"),
    )


@app.route("/logout")
def logout():
    _clear_auth_session()
    return redirect(url_for("login"))


@app.before_request
def refresh_app_state_from_db() -> None:
    global LAST_STATE_REFRESH_AT
    # Keep in-memory state in sync across hosted worker processes without reloading every request.
    if STATE_REFRESH_INTERVAL_SECONDS <= 0:
        load_app_state()
        return
    elapsed = time.monotonic() - LAST_STATE_REFRESH_AT
    if elapsed >= STATE_REFRESH_INTERVAL_SECONDS:
        if current_persisted_state_rev() != LAST_SCRIMS_REV:
            load_app_state()
        else:
            LAST_STATE_REFRESH_AT = time.monotonic()


register_team_routes(
    app,
    is_edit_session=_is_edit_session,
    get_db=get_db,
)


