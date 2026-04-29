import hashlib
from flask import request, session


_GET_DB = None
_SITE_PASSWORD = ""
_EDIT_PASSWORD = ""
_VIEW_PASSWORD = ""
_AUTH_ROLES = {"view", "edit"}


def configure_auth_helpers(*, get_db, site_password: str, edit_password: str, view_password: str, auth_roles: set[str]) -> None:
    global _GET_DB, _SITE_PASSWORD, _EDIT_PASSWORD, _VIEW_PASSWORD, _AUTH_ROLES
    _GET_DB = get_db
    _SITE_PASSWORD = (site_password or "").strip()
    _EDIT_PASSWORD = (edit_password or "").strip()
    _VIEW_PASSWORD = (view_password or "").strip()
    _AUTH_ROLES = set(auth_roles or {"view", "edit"})


def is_write_request() -> bool:
    return request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}


def is_edit_session() -> bool:
    return session.get("access_level") == "edit"


def get_stored_password_hash() -> str:
    row = _GET_DB().execute(
        "SELECT state_value FROM app_state WHERE state_key = ?",
        ("site_password_hash",),
    ).fetchone()
    if not row:
        return ""
    return (row["state_value"] or "").strip()


def get_stored_view_password_hash() -> str:
    row = _GET_DB().execute(
        "SELECT state_value FROM app_state WHERE state_key = ?",
        ("view_password_hash",),
    ).fetchone()
    if not row:
        return ""
    return (row["state_value"] or "").strip()


def resolve_edit_password_secret() -> str:
    if _EDIT_PASSWORD:
        return _EDIT_PASSWORD
    if _SITE_PASSWORD:
        return _SITE_PASSWORD
    return get_stored_password_hash()


def resolve_view_password_secret() -> str:
    if _VIEW_PASSWORD:
        return _VIEW_PASSWORD
    stored_view_hash = get_stored_view_password_hash()
    if stored_view_hash:
        return stored_view_hash
    return resolve_edit_password_secret()


def is_password_configured() -> bool:
    return bool(resolve_edit_password_secret())


def current_auth_revision() -> str:
    edit_secret = resolve_edit_password_secret()
    view_secret = resolve_view_password_secret()
    if not edit_secret:
        return ""
    raw_value = f"edit:{edit_secret}|view:{view_secret}"
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


def is_session_authenticated() -> bool:
    if not session.get("logged_in"):
        return False
    if session.get("access_level") not in _AUTH_ROLES:
        return False
    return session.get("auth_revision") == current_auth_revision()


def mark_session_authenticated(access_level: str) -> None:
    session["logged_in"] = True
    session["access_level"] = access_level
    session["auth_revision"] = current_auth_revision()


def clear_auth_session() -> None:
    session.pop("logged_in", None)
    session.pop("access_level", None)
    session.pop("auth_revision", None)


def normalize_next_path(default: str = "/") -> str:
    next_path = (request.values.get("next") or default).strip()
    if not next_path.startswith("/"):
        return default
    return next_path
